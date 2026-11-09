"""Cartera simulada en EUR sobre activos en USD, con comisiones, deslizamiento y cambio de divisa."""
from __future__ import annotations

from datetime import datetime, timezone

from . import store

TRADE_FIELDS = ["ts", "date", "phase", "ticker", "side", "shares", "price_usd", "fx", "value_eur",
                "fees_eur", "reason", "prob", "pnl_eur", "pnl_pct", "hold_days", "entry_date"]
EQUITY_FIELDS = ["ts", "date", "phase", "equity_eur", "cash_eur", "invested_eur", "bench_eur", "n_positions"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Portfolio:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.c = cfg["costs"]
        self.state = store.read_json("portfolio.json")
        self.new_trades: list[dict] = []

    # ── ciclo de vida ──
    @property
    def initialized(self) -> bool:
        return self.state is not None

    def init(self, prices: dict[str, float], fx: float, date: str) -> None:
        cap = float(self.cfg["capital_eur"])
        bench = self.cfg["market"]["benchmark"]
        net_eur = cap * (1 - self.c["fx_fee_pct"] - self.c["commission_pct"]) - self.c.get("commission_fixed_eur", 0)
        usd = net_eur * fx
        bench_shares = usd / (prices[bench] * (1 + self.c["slippage_pct"]))
        self.state = {"created": now_iso(), "start_date": date, "capital_eur": cap, "cash_eur": cap,
                      "positions": {}, "bench_shares": bench_shares, "realized_pnl_eur": 0.0}
        self.save()

    def save(self) -> None:
        store.write_json("portfolio.json", self.state)
        store.append_csv("trades.csv", self.new_trades, TRADE_FIELDS)
        self.new_trades = []

    # ── valoración ──
    def position_value_eur(self, t: str, prices: dict, fx: float) -> float:
        p = self.state["positions"][t]
        return p["shares"] * prices.get(t, p["last_price_usd"]) / fx

    def equity(self, prices: dict, fx: float) -> float:
        return self.state["cash_eur"] + sum(self.position_value_eur(t, prices, fx) for t in self.state["positions"])

    def snapshot(self, prices: dict, fx: float, date: str, phase: str) -> dict:
        for t, p in self.state["positions"].items():
            if t in prices:
                p["last_price_usd"] = prices[t]
        eq = self.equity(prices, fx)
        bench = self.cfg["market"]["benchmark"]
        row = {"ts": now_iso(), "date": date, "phase": phase, "equity_eur": round(eq, 4),
               "cash_eur": round(self.state["cash_eur"], 4), "invested_eur": round(eq - self.state["cash_eur"], 4),
               "bench_eur": round(self.state["bench_shares"] * prices[bench] / fx, 4),
               "n_positions": len(self.state["positions"])}
        store.append_csv("equity.csv", [row], EQUITY_FIELDS)
        return row

    # ── operaciones ──
    def buy(self, t: str, eur: float, price: float, fx: float, date: str, phase: str, reason: str, prob=None) -> None:
        eur = min(eur, self.state["cash_eur"])
        if eur < self.cfg["strategy"]["min_trade_eur"]:
            return
        fees = eur * (self.c["commission_pct"] + self.c["fx_fee_pct"]) + self.c.get("commission_fixed_eur", 0)
        if eur - fees <= 0:
            return
        exec_px = price * (1 + self.c["slippage_pct"])
        shares = (eur - fees) * fx / exec_px
        pos = self.state["positions"].get(t)
        if pos:
            tot = pos["shares"] + shares
            pos["entry_price_usd"] = (pos["entry_price_usd"] * pos["shares"] + exec_px * shares) / tot
            pos["cost_eur"] += eur
            pos["shares"] = tot
        else:
            self.state["positions"][t] = {"shares": shares, "entry_price_usd": exec_px, "entry_date": date,
                                          "entry_fx": fx, "cost_eur": eur, "entry_prob": prob,
                                          "last_price_usd": price}
        self.state["cash_eur"] -= eur
        self.new_trades.append({"ts": now_iso(), "date": date, "phase": phase, "ticker": t, "side": "BUY",
                                "shares": round(shares, 6), "price_usd": round(exec_px, 4), "fx": round(fx, 5),
                                "value_eur": round(eur, 4), "fees_eur": round(fees, 4), "reason": reason,
                                "prob": None if prob is None else round(prob, 4)})

    def sell(self, t: str, price: float, fx: float, date: str, phase: str, reason: str, fraction: float = 1.0) -> None:
        pos = self.state["positions"].get(t)
        if not pos:
            return
        shares = pos["shares"] * fraction
        exec_px = price * (1 - self.c["slippage_pct"])
        gross = shares * exec_px / fx
        fees = gross * (self.c["commission_pct"] + self.c["fx_fee_pct"]) + self.c.get("commission_fixed_eur", 0)
        net = gross - fees
        cost = pos["cost_eur"] * fraction
        pnl = net - cost
        self.state["cash_eur"] += net
        self.state["realized_pnl_eur"] += pnl
        hold = _days_between(pos["entry_date"], date)
        self.new_trades.append({"ts": now_iso(), "date": date, "phase": phase, "ticker": t, "side": "SELL",
                                "shares": round(shares, 6), "price_usd": round(exec_px, 4), "fx": round(fx, 5),
                                "value_eur": round(net, 4), "fees_eur": round(fees, 4), "reason": reason,
                                "prob": pos.get("entry_prob"), "pnl_eur": round(pnl, 4),
                                "pnl_pct": round(pnl / cost, 5) if cost else None, "hold_days": hold,
                                "entry_date": pos["entry_date"]})
        if fraction >= 0.999:
            del self.state["positions"][t]
        else:
            pos["shares"] -= shares
            pos["cost_eur"] -= cost

    def risk_checks(self, prices: dict, fx: float, date: str, phase: str) -> list[str]:
        """Stop-loss y take-profit sobre el precio en USD respecto al de entrada."""
        s = self.cfg["strategy"]
        events = []
        for t in list(self.state["positions"]):
            if t not in prices:
                continue
            p = self.state["positions"][t]
            ch = prices[t] / p["entry_price_usd"] - 1
            if ch <= -s["stop_loss_pct"]:
                self.sell(t, prices[t], fx, date, phase, f"stop-loss ({ch:+.1%})")
                events.append(f"{t}: stop-loss {ch:+.1%}")
            elif ch >= s["take_profit_pct"]:
                self.sell(t, prices[t], fx, date, phase, f"take-profit ({ch:+.1%})")
                events.append(f"{t}: take-profit {ch:+.1%}")
        return events

    def rebalance(self, targets: dict[str, float], probs: dict[str, float], prices: dict, fx: float,
                  date: str, phase: str) -> list[str]:
        """Lleva la cartera a los pesos objetivo. Primero ventas, luego compras.

        No recompra el mismo día un activo que acaba de salir por stop-loss o take-profit."""
        s = self.cfg["strategy"]
        cooled = {t["ticker"] for t in self.new_trades if t["side"] == "SELL" and t["date"] == date
                  and ("stop" in t["reason"] or "take" in t["reason"])}
        targets = {t: w for t, w in targets.items() if t not in cooled}
        eq = self.equity(prices, fx)
        actions = []
        for t in list(self.state["positions"]):
            if t not in targets and t in prices:
                self.sell(t, prices[t], fx, date, phase, "sale del objetivo del modelo")
                actions.append(f"vende {t}")
        for t, w in targets.items():
            if t not in prices or t not in self.state["positions"]:
                continue
            cur = self.position_value_eur(t, prices, fx)
            if cur / eq - w > s["rebalance_tolerance"]:
                frac = (cur - w * eq) / cur
                if cur - w * eq >= s["min_trade_eur"]:
                    self.sell(t, prices[t], fx, date, phase, "reequilibrio (sobreponderada)", frac)
                    actions.append(f"reduce {t}")
        eq = self.equity(prices, fx)
        for t, w in sorted(targets.items(), key=lambda x: -probs.get(x[0], 0)):
            if t not in prices:
                continue
            cur = self.position_value_eur(t, prices, fx) if t in self.state["positions"] else 0.0
            gap = w * eq - cur
            if gap / eq > s["rebalance_tolerance"] or (cur == 0 and gap >= s["min_trade_eur"]):
                before = len(self.new_trades)
                reason = "entrada por señal" if cur == 0 else "reequilibrio (infraponderada)"
                self.buy(t, gap, prices[t], fx, date, phase, reason, probs.get(t))
                if len(self.new_trades) > before:
                    actions.append(f"compra {t}")
        return actions


def target_weights(preds, held: list[str], cfg: dict, base_ticker: str | None = None) -> dict[str, float]:
    """Top-k con histéresis: lo que ya tenemos se mantiene si sigue razonablemente arriba.

    Si hay preferencia del usuario (`strategy.preferred`), se reservan `min_positions` plazas
    para la mejor valorada de esa lista. `base_ticker` (p. ej. SPY) rellena el resto de plazas
    cuando el modelo no tiene ventaja demostrada.
    """
    s = cfg["strategy"]
    k = s["top_k"]
    pref = s.get("preferred") or {}
    plist = [t for t in pref.get("tickers", []) if t in preds.index]
    n_pref = min(int(pref.get("min_positions", 0)), k) if plist else 0

    # 1) plazas reservadas: se mantiene la que ya tenemos si no se ha hundido en el ranking
    pref_chosen = [t for t in held if t in plist and preds.loc[t, "prob"] >= s["keep_prob"] - 0.05][:n_pref]
    for t in preds.index:  # ordenado por probabilidad
        if len(pref_chosen) >= n_pref:
            break
        if t in plist and t not in pref_chosen:
            pref_chosen.append(t)

    # 2) resto de plazas: modelo (o benchmark si no hay ventaja)
    free = k - len(pref_chosen)
    if base_ticker:
        chosen = [base_ticker] if free > 0 else []
    else:
        chosen = [t for t in held if t not in pref_chosen and t in preds.index
                  and preds.loc[t, "rank"] <= s["keep_rank"] and preds.loc[t, "prob"] >= s["keep_prob"]][:free]
        for t, row in preds.iterrows():
            if len(chosen) >= free:
                break
            if t not in chosen and t not in pref_chosen and row["prob"] >= s["min_prob"]:
                chosen.append(t)
    picks = pref_chosen + chosen
    if not picks:
        return {}
    if base_ticker and chosen:  # el benchmark ocupa todas las plazas libres
        w = {t: 1.0 / k for t in pref_chosen}
        w[base_ticker] = 1.0 - sum(w.values())
        return w
    return {t: 1.0 / k for t in picks}  # plazas sin candidato se quedan en liquidez


def _days_between(a: str, b: str) -> int:
    from datetime import date

    try:
        return (date.fromisoformat(b) - date.fromisoformat(a)).days
    except Exception:  # noqa: BLE001
        return 0

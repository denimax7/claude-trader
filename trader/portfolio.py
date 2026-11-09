"""Cartera simulada en EUR sobre activos en USD, con comisiones, deslizamiento y cambio de divisa."""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

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
    """Elige las posiciones objetivo maximizando lo que espera el modelo.

    - Ranking por probabilidad del campeón, con histéresis (lo que ya tenemos se mantiene
      mientras siga razonablemente arriba: cambiar cuesta comisiones).
    - Preferencia del usuario (`strategy.preferred`): NO reserva plazas. Solo desempata:
      los activos de la lista reciben `tie_margin` puntos de probabilidad, de modo que se
      eligen frente a otro solo si su previsión es parecida. Si otro sector tiene clara
      ventaja, gana el otro sector.
    - Sin ventaja demostrada (`base_ticker`, p. ej. SPY): las plazas van al benchmark; una
      puede ir al ETF del sector preferido (`no_edge_ticker`) si su previsión es parecida
      o mejor que la del benchmark.
    """
    s = cfg["strategy"]
    k = s["top_k"]
    pref = s.get("preferred") or {}
    pset = {t for t in pref.get("tickers", []) if t in preds.index}
    margin = float(pref.get("tie_margin", 0.0))
    prob = preds["prob"].astype(float)
    adj = prob + pd.Series({t: margin for t in pset}, dtype=float).reindex(prob.index).fillna(0.0)
    order = list(adj.sort_values(ascending=False).index)
    rank = {t: i + 1 for i, t in enumerate(order)}

    if base_ticker:
        w: dict[str, float] = {}
        safe = pref.get("no_edge_ticker")
        if safe in prob.index and base_ticker in prob.index and k >= 1:
            gap = prob[base_ticker] - prob[safe]
            # si ya lo tenemos, exigimos más para cambiarlo (evitar 2 € de comisiones por ruido)
            if gap <= margin * (2 if safe in held else 1):
                w[safe] = 1.0 / k
        rest = 1.0 - sum(w.values())
        if rest > 1e-9:
            w[base_ticker] = rest
        return w

    chosen = [t for t in held if t in rank and rank[t] <= s["keep_rank"] and prob[t] >= s["keep_prob"]]
    chosen = sorted(chosen, key=lambda t: rank[t])[:k]
    for t in order:
        if len(chosen) >= k:
            break
        if t not in chosen and prob[t] >= s["min_prob"]:
            chosen.append(t)
    return {t: 1.0 / k for t in chosen}  # plazas sin candidato se quedan en liquidez


def _days_between(a: str, b: str) -> int:
    from datetime import date

    try:
        return (date.fromisoformat(b) - date.fromisoformat(a)).days
    except Exception:  # noqa: BLE001
        return 0

"""Punto de entrada: python -m trader --phase auto|open|mid|close|review [--force]

  open   (≈9:35 NY)  ejecuta las órdenes que el modelo dejó preparadas al cierre anterior.
  mid    (≈12:45 NY) vigila riesgos: stop-loss / take-profit.
  close  (≈15:45 NY) valora la cartera, evalúa predicciones vencidas, reentrena y
                     compara modelos (walk-forward), genera la señal de mañana y
                     escribe la nota diaria con Claude.
  review (sábado)    revisión semanal con Claude: lecciones + un experimento nuevo,
                     que se valida antes de poder entrar en producción.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from . import claude_agent, clock, dashboard, store
from .data import download_daily, latest_prices, news_headlines
from .features import build_panel
from .model import (DEFAULT_EXPERIMENTS, choose_champion, feature_importance, fit_full,
                    predict_latest, validate_experiment, walk_forward)
from .portfolio import Portfolio, now_iso, target_weights

PRED_FIELDS = ["date", "ticker", "prob", "rank", "selected", "model", "eval_date",
               "fwd_ret", "excess", "correct"]


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


# ─────────────────────────── utilidades ───────────────────────────
def get_prices(cfg: dict) -> tuple[dict, float]:
    tick = cfg["universe"] + [cfg["market"]["fx_ticker"]]
    px = latest_prices(tick)
    fx = px.pop(cfg["market"]["fx_ticker"], None)
    if not fx:
        raise RuntimeError("sin tipo de cambio EURUSD")
    if cfg["market"]["benchmark"] not in px:
        raise RuntimeError("sin precio del benchmark; se reintentará en la próxima ejecución")
    return px, fx


def experiments() -> list[dict]:
    exps = store.read_json("experiments.json")
    if not exps:
        exps = [dict(e, status="activo", created=now_iso()) for e in DEFAULT_EXPERIMENTS]
        store.write_json("experiments.json", exps)
    return exps


def load_panel(cfg: dict) -> tuple[pd.DataFrame, dict]:
    d = download_daily(cfg["universe"], cfg["model"]["history_years"])
    panel = build_panel(d["close"], d["volume"], cfg["market"]["benchmark"], cfg["model"]["horizon_days"])
    return panel, d


def train_and_signal(cfg: dict, panel: pd.DataFrame, date: str, held: list[str]) -> dict:
    """Evalúa todos los experimentos activos, elige campeón y genera los pesos objetivo."""
    exps = [e for e in experiments() if e.get("status") == "activo"]
    reg = store.read_json("models.json", {}) or {}
    evals = {}
    for e in exps:
        log(f"walk-forward {e['name']} …")
        evals[e["name"]] = walk_forward(panel, e, cfg)
        log(f"  score={evals[e['name']]['score']:+.4f} auc={evals[e['name']]['auc']:.3f}")
    champ, reason = choose_champion(reg, evals, cfg["model"]["promote_margin"])
    if champ != reg.get("champion"):
        store.append_journal({"ts": now_iso(), "kind": "modelo", "author": "sistema",
                              "text": f"**Nuevo campeón: `{champ}`** — {reason}."})
    exp = next(e for e in exps if e["name"] == champ)
    model = fit_full(panel, exp, cfg)
    preds = predict_latest(model, panel, exp)
    hist = (reg.get("history") or [])[-400:]
    hist.append({"date": date, "champion": champ, **{n: round(v["score"], 5) for n, v in evals.items()}})
    store.write_json("models.json", {"champion": champ, "reason": reason if champ != reg.get("champion") else reg.get("reason", reason),
                                     "last_eval": evals, "importance": feature_importance(model, exp),
                                     "history": hist, "updated": now_iso()})
    _prune_experiments(cfg, evals, champ)

    weights = target_weights(preds, held, cfg)
    probs = {t: float(preds.loc[t, "prob"]) for t in preds.index}
    # Si el campeón no demuestra ventaja fuera de muestra, no apostamos por sus señales.
    edge = evals[champ].get("score", float("nan"))
    fb = cfg["strategy"].get("no_edge_fallback", "benchmark")
    mode = "modelo"
    if not (edge > 0) and fb != "trade":
        weights = {cfg["market"]["benchmark"]: 1.0} if fb == "benchmark" else {}
        mode = f"sin ventaja demostrada (score {edge:+.4f}) → {fb}"
    store.write_json("targets.json", {"date": preds.attrs["date"], "created": now_iso(), "model": champ, "mode": mode,
                                      "weights": weights, "probs": {t: round(probs[t], 4) for t in list(preds.index)[:12]},
                                      "executed": False})
    eval_date = clock.add_trading_days(preds.attrs["date"], cfg["model"]["horizon_days"])
    rows = [{"date": preds.attrs["date"], "ticker": t, "prob": round(r["prob"], 4), "rank": int(r["rank"]),
             "selected": int(t in weights), "model": champ, "eval_date": eval_date} for t, r in preds.iterrows()]
    old = [r for r in store.read_csv("predictions.csv") if r["date"] != preds.attrs["date"]]
    store.write_csv("predictions.csv", old + rows, PRED_FIELDS)
    return {"champion": champ, "weights": weights, "probs": probs, "mode": mode,
            "importance": feature_importance(model, exp)}


def _prune_experiments(cfg: dict, evals: dict, champ: str) -> None:
    exps = experiments()
    active = [e for e in exps if e.get("status") == "activo"]
    for e in exps:
        if e["name"] in evals:
            e["last_score"] = round(evals[e["name"]]["score"], 5)
    while len(active) > cfg["model"]["max_experiments"]:
        worst = min((e for e in active if e["name"] != champ), key=lambda e: e.get("last_score", -1))
        worst["status"] = "retirado"
        active.remove(worst)
    store.write_json("experiments.json", exps)


def score_predictions(panel: pd.DataFrame) -> dict:
    """Rellena el resultado real de las predicciones cuyo horizonte ya venció."""
    rows = store.read_csv("predictions.csv")
    if not rows:
        return {}
    lab = panel[panel["excess"].notna()][["fwd_ret", "excess"]]
    for r in rows:
        if r.get("correct") not in (None, ""):
            continue
        key = (pd.Timestamp(r["date"]), r["ticker"])
        if key in lab.index:
            fr, ex = lab.loc[key]
            r["fwd_ret"], r["excess"] = round(float(fr), 5), round(float(ex), 5)
            r["correct"] = int((float(r["prob"]) >= 0.5) == (ex > 0))
    store.write_csv("predictions.csv", rows, PRED_FIELDS)
    done = [r for r in rows if r.get("correct") not in (None, "")]
    sel = [r for r in done if r["selected"] == "1" or r["selected"] == 1]
    m = {"n": len(done)}
    if done:
        m["hit_rate"] = float(np.mean([int(r["correct"]) for r in done]))
        # calibración por tramos de probabilidad
        df = pd.DataFrame(done).astype({"prob": float, "excess": float})
        df["bucket"] = pd.cut(df["prob"], [0, .45, .5, .55, .6, 1])
        m["calibration"] = {str(b): {"n": int(len(g)), "beat_rate": round(float((g["excess"] > 0).mean()), 3)}
                            for b, g in df.groupby("bucket", observed=True)}
    if sel:
        m["sel_n"] = len(sel)
        m["sel_hit_rate"] = float(np.mean([float(r["excess"]) > 0 for r in sel]))
        m["sel_excess"] = float(np.mean([float(r["excess"]) for r in sel]))
    store.write_json("live_metrics.json", m)
    return m


def performance(cfg: dict, snap: dict) -> dict:
    cap = cfg["capital_eur"]
    return {"equity_eur": snap["equity_eur"], "return_pct": snap["equity_eur"] / cap - 1,
            "bench_eur": snap["bench_eur"], "bench_return_pct": snap["bench_eur"] / cap - 1,
            "cash_eur": snap["cash_eur"], "n_positions": snap["n_positions"]}


def recent_closed(n: int = 8) -> list[dict]:
    return [t for t in store.read_csv("trades.csv") if t["side"] == "SELL"][-n:]


# ─────────────────────────── fases ───────────────────────────
def phase_open(cfg: dict, date: str) -> None:
    px, fx = get_prices(cfg)
    pf = Portfolio(cfg)
    if not pf.initialized:
        pf.init(px, fx, date)
        log(f"cartera creada con {cfg['capital_eur']} €")
    tg = store.read_json("targets.json")
    if not tg:  # primer arranque: entrenar ya para poder operar hoy
        log("sin señal previa: entrenamiento inicial")
        panel, _ = load_panel(cfg)
        train_and_signal(cfg, panel, date, list(pf.state["positions"]))
        tg = store.read_json("targets.json")
    events = pf.risk_checks(px, fx, date, "open")
    actions = []
    if not tg.get("executed"):
        actions = pf.rebalance(tg["weights"], tg.get("probs", {}), px, fx, date, "open")
        tg["executed"], tg["executed_at"] = True, now_iso()
        store.write_json("targets.json", tg)
    snap = pf.snapshot(px, fx, date, "open")
    pf.save()
    text = (f"Apertura: {', '.join(actions) if actions else 'sin cambios'}"
            + (f". Riesgo: {'; '.join(events)}" if events else "") + f". Valor {snap['equity_eur']:.2f} €.")
    store.append_journal({"ts": now_iso(), "kind": "apertura", "author": "sistema", "text": text})
    log(text)


def phase_mid(cfg: dict, date: str) -> None:
    pf = Portfolio(cfg)
    if not pf.initialized:
        log("cartera aún no creada; nada que vigilar")
        return
    px, fx = get_prices(cfg)
    events = pf.risk_checks(px, fx, date, "mid")
    snap = pf.snapshot(px, fx, date, "mid")
    pf.save()
    if events:
        store.append_journal({"ts": now_iso(), "kind": "media sesión", "author": "sistema",
                              "text": "Control de riesgo: " + "; ".join(events)})
    log(f"media sesión: {snap['equity_eur']:.2f} € {events or ''}")


def phase_close(cfg: dict, date: str) -> None:
    pf = Portfolio(cfg)
    px, fx = get_prices(cfg)
    if not pf.initialized:
        pf.init(px, fx, date)
    events = pf.risk_checks(px, fx, date, "close")
    panel, _ = load_panel(cfg)
    live = score_predictions(panel)
    ingest_proposals(cfg, panel, date)
    sig = train_and_signal(cfg, panel, date, list(pf.state["positions"]))
    snap = pf.snapshot(px, fx, date, "close")
    pf.save()

    held = list(pf.state["positions"])
    new_targets = {t: {"prob": round(sig["probs"][t], 3), "weight": w} for t, w in sig["weights"].items()}
    ctx = {
        "date": date, "performance": performance(cfg, snap), "risk_events": events,
        "positions": pf.state["positions"], "targets": new_targets,
        "exits_planned": [t for t in held if t not in sig["weights"]],
        "champion_model": sig["champion"], "signal_mode": sig["mode"], "top_features": sig["importance"],
        "model_scores": (store.read_json("models.json") or {}).get("last_eval"),
        "live_metrics": live, "recent_closed": recent_closed(),
        "headlines": news_headlines(sorted(set(held) | set(sig["weights"]))),
        "lessons": _lessons_text(),
    }
    store.write_json("last_close_context.json", ctx)  # la sesión diaria de Claude parte de aquí
    author, text = claude_agent.daily_note(cfg, ctx)
    store.append_journal({"ts": now_iso(), "kind": "cierre", "author": author, "text": text})
    log(f"cierre: {snap['equity_eur']:.2f} € · objetivo {list(sig['weights'])} · nota por {author}")


def phase_review(cfg: dict, date: str) -> None:
    trades = store.read_csv("trades.csv")
    closed = [t for t in trades if t["side"] == "SELL"]
    recent = [t for t in closed if t["date"] >= (pd.Timestamp(date) - pd.Timedelta(days=7)).date().isoformat()]
    pnl = [float(t["pnl_eur"] or 0) for t in recent]
    eq = store.read_csv("equity.csv")
    ctx = {
        "date": date,
        "week_stats": {"n_closed": len(recent), "win_rate": float(np.mean([p > 0 for p in pnl])) if pnl else 0,
                       "pnl_eur": float(sum(pnl))},
        "equity_curve_close": [(r["date"], r["equity_eur"], r["bench_eur"]) for r in eq if r["phase"] == "close"][-60:],
        "all_closed_trades": closed[-60:], "live_metrics": store.read_json("live_metrics.json", {}),
        "models": store.read_json("models.json", {}), "experiments": experiments(),
        "lessons_so_far": _lessons_text(), "config_strategy": cfg["strategy"],
    }
    md, out = claude_agent.weekly_review(cfg, ctx)
    if out and out.get("lessons"):
        _add_lessons(date, out["lessons"])
    if out and out.get("proposal"):
        try:
            prop = validate_experiment({**out["proposal"], "created_by": "claude"})
            exps = experiments()
            if any(e["name"] == prop["name"] for e in exps):
                prop["name"] = f"{prop['name']}_{date.replace('-', '')}"
            panel, _ = load_panel(cfg)
            res = walk_forward(panel, prop, cfg)
            reg = store.read_json("models.json", {}) or {}
            champ_score = (reg.get("last_eval", {}).get(reg.get("champion"), {}) or {}).get("score", float("nan"))
            exps.append({**prop, "status": "activo", "created": now_iso(), "first_eval": res})
            store.write_json("experiments.json", exps)
            md += (f"\n\n**Experimento propuesto: `{prop['name']}`** ({prop['algo']}, {len(prop['features'])} variables). "
                   f"Primera validación: score {res['score']:+.4f} vs campeón {champ_score:+.4f}. "
                   "Competirá cada día al cierre; solo será campeón si gana con margen.")
        except Exception as e:  # noqa: BLE001
            md += f"\n\n_Propuesta descartada: {e}_"
    store.append_journal({"ts": now_iso(), "kind": "revisión semanal", "author": "claude" if out else "sistema", "text": md})
    (store.ROOT / "reviews").mkdir(exist_ok=True)
    (store.ROOT / "reviews" / f"{date}.md").write_text(f"# Revisión semanal {date}\n\n{md}\n", encoding="utf-8")
    log("revisión semanal escrita")


def ingest_proposals(cfg: dict, panel: pd.DataFrame, date: str) -> None:
    """Procesa las propuestas que deja Claude en state/proposals.json (ver trader/tools.py).

    - model:    experimento nuevo → se valida en walk-forward y entra a competir.
    - retire:   retira un experimento (nunca el campeón).
    - strategy: ajusta un parámetro de estrategia dentro de límites seguros.
    """
    from .tools import STRATEGY_BOUNDS

    props = store.read_json("proposals.json", []) or []
    pending = [p for p in props if p.get("status") == "pendiente"]
    if not pending:
        return
    exps = experiments()
    reg = store.read_json("models.json", {}) or {}
    champ = reg.get("champion")
    champ_score = (reg.get("last_eval", {}).get(champ, {}) or {}).get("score", float("nan"))
    notes = []
    for p in pending:
        try:
            kind = p.get("type")
            if kind == "model":
                exp = validate_experiment({**p["experiment"], "created_by": p.get("author", "claude")})
                if any(e["name"] == exp["name"] for e in exps):
                    exp["name"] = f"{exp['name']}_{date.replace('-', '')}"[:40]
                res = walk_forward(panel, exp, cfg)
                exps.append({**exp, "status": "activo", "created": now_iso(), "first_eval": res})
                p["result"] = f"en competición · primer score {res['score']:+.4f} (campeón {champ_score:+.4f})"
            elif kind == "retire":
                name = p["name"]
                if name == champ:
                    raise ValueError("no se puede retirar al campeón")
                hit = [e for e in exps if e["name"] == name and e.get("status") == "activo"]
                if not hit:
                    raise ValueError(f"experimento activo '{name}' no encontrado")
                hit[0]["status"] = "retirado"
                p["result"] = "retirado"
            elif kind == "strategy":
                key, val = p["param"], p["value"]
                if key not in STRATEGY_BOUNDS:
                    raise ValueError(f"parámetro no ajustable: {key}")
                lo, hi, typ = STRATEGY_BOUNDS[key]
                if typ is str:
                    if val not in lo:
                        raise ValueError(f"valor no permitido: {val}")
                else:
                    val = typ(min(max(float(val), lo), hi))
                ov = store.read_json("strategy_overrides.json", {}) or {}
                old = ov.get(key, cfg["strategy"].get(key))
                ov[key] = val
                store.write_json("strategy_overrides.json", ov)
                cfg["strategy"][key] = val
                p["result"] = f"aplicado: {key} {old} → {val}"
            else:
                raise ValueError(f"tipo desconocido: {kind}")
            p["status"] = "aplicada"
        except Exception as e:  # noqa: BLE001
            p["status"], p["result"] = "rechazada", str(e)[:200]
        p["processed"] = now_iso()
        notes.append(f"- `{p.get('type')}` {p.get('title', '')}: **{p['status']}** — {p['result']}")
    store.write_json("experiments.json", exps)
    store.write_json("proposals.json", props[-200:])
    store.append_journal({"ts": now_iso(), "kind": "propuestas", "author": "sistema",
                          "text": "Propuestas procesadas al cierre:\n" + "\n".join(notes)})
    log(f"{len(pending)} propuestas procesadas")


def phase_explore(cfg: dict, date: str) -> None:
    """Búsqueda automática semanal: variantes aleatorias del campeón compiten en walk-forward.

    Solo entran a competir las que superan al campeón en la primera validación; aun así,
    para ser campeonas tienen que seguir ganando con margen en los cierres siguientes.
    """
    from .features import ALL_FEATURES
    from .model import PARAM_BOUNDS

    rng = np.random.default_rng(int(pd.Timestamp(date).strftime("%Y%m%d")))
    exps = experiments()
    reg = store.read_json("models.json", {}) or {}
    champ = next((e for e in exps if e["name"] == reg.get("champion")), exps[0])
    champ_score = (reg.get("last_eval", {}).get(champ["name"], {}) or {}).get("score", float("nan"))
    panel, _ = load_panel(cfg)
    n = int(cfg["model"].get("explore_trials", 4))
    tried, kept = [], []
    for i in range(n):
        algo = champ["algo"] if rng.random() < 0.75 else ("logit" if champ["algo"] == "lgbm" else "lgbm")
        feats = list(champ["features"]) if algo == champ["algo"] else list(ALL_FEATURES)
        # mutar variables: quitar alguna y añadir otra
        if len(feats) > 5 and rng.random() < 0.7:
            feats.remove(rng.choice(feats))
        extra = [f for f in ALL_FEATURES if f not in feats]
        if extra and rng.random() < 0.7:
            feats.append(rng.choice(extra))
        params = dict(champ["params"]) if algo == champ["algo"] else {}
        for k, (lo, hi, typ) in PARAM_BOUNDS[algo].items():
            if rng.random() < 0.5:
                base = float(params.get(k, (lo + hi) / 2))
                if typ is int:
                    v = round(base * float(np.exp(rng.normal(0, 0.35))))
                else:
                    v = base * float(np.exp(rng.normal(0, 0.35)))
                params[k] = typ(min(max(v, lo), hi))
        exp = validate_experiment({"name": f"auto_{date.replace('-', '')}_{i + 1}", "algo": algo,
                                   "features": feats, "params": params, "created_by": "exploración",
                                   "rationale": f"Variante aleatoria de {champ['name']}."})
        res = walk_forward(panel, exp, cfg)
        tried.append(f"`{exp['name']}` ({algo}, {len(feats)} var.) score {res['score']:+.4f}")
        log(tried[-1])
        if res["n_days"] and res["score"] > (champ_score if champ_score == champ_score else -1):
            exps.append({**exp, "status": "activo", "created": now_iso(), "first_eval": res})
            kept.append(exp["name"])
    store.write_json("experiments.json", exps)
    store.append_journal({"ts": now_iso(), "kind": "exploración", "author": "sistema",
                          "text": f"Exploración semanal frente a `{champ['name']}` (score {champ_score:+.4f}):\n"
                          + "\n".join(f"- {t}" for t in tried)
                          + (f"\n\nEntran a competir: {', '.join(kept)}." if kept else "\n\nNinguna mejora al campeón.")})


def phase_research(cfg: dict, date: str) -> None:
    """Replay histórico nocturno: busca variantes en el pasado y examina las mejores."""
    from . import research

    panel, _ = load_panel(cfg)
    s = research.run(cfg, panel, date, log=log)
    if not s:
        log("investigación sin campeón de referencia")
        return
    ch = s["champion"]
    best = s["top"][0] if s["top"] else None
    g = s["strategy_grid"][0] if s["strategy_grid"] else None
    lines = [f"Replay histórico: {s['trials_this_run']} variantes probadas en {s['minutes']} min "
             f"(total acumulado {s['totals']['trials']}).",
             f"- Campeón `{ch['name']}`: {ch['research']['score']:+.3%}/semana en investigación, "
             f"{(ch.get('holdout') or {}).get('score', float('nan')):+.3%} en examen final."]
    if best:
        lines.append(f"- Mejor del ranking: `{best['name']}` ({best['origin']}) {best['research']['score']:+.3%}/semana, "
                     f"t={best['research']['t_stat']:+.2f}.")
    if s["promoted"]:
        lines.append(f"- **Aprueban el examen final y entran a competir en vivo:** {', '.join(s['promoted'])}.")
    if g:
        lines.append(f"- Mejor estrategia en el pasado con el campeón: top_k={g['top_k']}, min_prob={g['min_prob']} "
                     f"({g['score']:+.3%}/semana, t={g['t_stat']:+.2f}); actual: top_k={s['current_strategy']['top_k']}, "
                     f"min_prob={s['current_strategy']['min_prob']}.")
    store.append_journal({"ts": now_iso(), "kind": "investigación", "author": "sistema", "text": "\n".join(lines)})
    log("investigación terminada")


def _lessons_text() -> str:
    p = store.STATE / "lessons.md"
    return p.read_text(encoding="utf-8")[-4000:] if p.exists() else ""


def _add_lessons(date: str, lessons: list[str]) -> None:
    p = store.STATE / "lessons.md"
    old = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    new = [f"- **{date}** · {l}" for l in lessons]
    p.write_text("\n".join((new + old)[:30]) + "\n", encoding="utf-8")


# ─────────────────────────── main ───────────────────────────
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="trader")
    ap.add_argument("--phase", default="auto", choices=["auto", "open", "mid", "close", "review", "explore", "research"])
    ap.add_argument("--force", action="store_true", help="ejecuta aunque ya se haya hecho hoy o el mercado esté cerrado")
    ap.add_argument("--now", help="hora simulada ISO (pruebas)")
    a = ap.parse_args(argv)
    cfg = store.load_config()
    now = datetime.fromisoformat(a.now) if a.now else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    date = clock.trading_date(now)
    phase = a.phase
    if phase == "auto":
        phase = clock.detect_phase(now, cfg["market"]["calendar"])
        if phase is None:
            log(f"fuera de ventana de mercado ({now.astimezone(clock.NY):%a %H:%M} NY); nada que hacer")
            return 0
    elif phase not in ("review", "explore", "research") and not a.force and clock.session(now, cfg["market"]["calendar"]) is None:
        log("hoy no hay mercado (usa --force para forzar)")
        return 0
    runs = store.read_json("last_runs.json", {}) or {}
    if not a.force and runs.get(phase) == date:
        log(f"fase {phase} ya ejecutada hoy ({date}); se omite")
        return 0
    log(f"fase {phase} · {date}")
    {"open": phase_open, "mid": phase_mid, "close": phase_close, "review": phase_review,
     "explore": phase_explore, "research": phase_research}[phase](cfg, date)
    runs[phase] = date
    runs[f"{phase}_ts"] = now_iso()
    store.write_json("last_runs.json", runs)
    dashboard.build(cfg)
    log("panel actualizado")
    return 0


if __name__ == "__main__":
    sys.exit(main())

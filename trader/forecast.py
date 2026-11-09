"""Previsiones con incertidumbre: rango probable, probabilidad de pérdida y riesgo.

Todo se calibra con las predicciones FUERA DE MUESTRA del walk-forward del campeón:
para cada tramo de probabilidad se mira qué pasó de verdad (cuántas veces perdió dinero,
cuánto se movió en relación a su volatilidad). Así una "probabilidad 0,60" no es una
opinión del modelo sino lo que históricamente ocurrió cuando dijo 0,60.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .model import fit_full, predict_latest


def _phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def calibrate(oos: pd.DataFrame, h: int, n_bins: int = 5) -> list[dict]:
    """Tramos de probabilidad con su resultado real (fuera de muestra)."""
    df = oos.dropna(subset=["prob", "fwd_ret", "vol_21"]).copy()
    df = df[df["vol_21"] > 0]
    if len(df) < 200:
        return []
    df["z"] = df["fwd_ret"] / (df["vol_21"] * math.sqrt(h / 252))
    df["bin"] = pd.qcut(df["prob"], n_bins, duplicates="drop")
    out = []
    for b, g in df.groupby("bin", observed=True):
        out.append({
            "lo": float(b.left), "hi": float(b.right), "n": int(len(g)),
            "prob_mean": float(g["prob"].mean()),
            "beat_rate": float((g["excess"] > 0).mean()),       # ¿batió a la media del universo?
            "p_loss": float((g["fwd_ret"] < 0).mean()),         # ¿perdió dinero en 5 sesiones?
            "ret_mean": float(g["fwd_ret"].mean()),
            "z05": float(g["z"].quantile(0.05)), "z50": float(g["z"].quantile(0.50)),
            "z95": float(g["z"].quantile(0.95)),
        })
    return out


def _bin_for(p: float, bins: list[dict]) -> dict:
    for b in bins:
        if p <= b["hi"]:
            return b
    return bins[-1]


def build(cfg: dict, panel: pd.DataFrame, exps: list[dict], champ: str, champ_preds: pd.DataFrame,
          oos: pd.DataFrame | None, weights: dict, mode: str, equity_eur: float | None) -> dict:
    h = cfg["model"]["horizon_days"]
    bins = calibrate(oos, h) if oos is not None else []
    last = panel.index.get_level_values("date").max()
    cur = panel.xs(last, level="date")

    # predicciones de todos los modelos en competición (consenso)
    by_model = {champ: champ_preds["prob"]}
    for e in exps:
        if e["name"] == champ:
            continue
        try:
            by_model[e["name"]] = predict_latest(fit_full(panel, e, cfg), panel, e)["prob"]
        except Exception:  # noqa: BLE001
            continue
    probs = pd.DataFrame(by_model)

    pref = set((cfg["strategy"].get("preferred") or {}).get("tickers", []))
    show = list(champ_preds.index[:12])
    for t in list(weights) + sorted(pref):
        if t in champ_preds.index and t not in show:
            show.append(t)
    rows = []
    for t in show:
        p = float(champ_preds.loc[t, "prob"])
        sig = float(cur.loc[t, "vol_21"]) * math.sqrt(h / 252) if t in cur.index else float("nan")
        r = {"ticker": t, "prob": round(p, 4), "rank": int(champ_preds.loc[t, "rank"]),
             "selected": t in weights, "defense": t in pref, "sigma": round(sig, 5),
             "models": {m: round(float(probs.loc[t, m]), 4) for m in probs.columns if t in probs.index},
             "consensus": round(float(probs.loc[t].mean()), 4) if t in probs.index else None,
             "agree": round(float((probs.loc[t] > 0.5).mean()), 3) if t in probs.index else None}
        if bins and sig == sig:
            b = _bin_for(p, bins)
            r.update({"p_loss": round(b["p_loss"], 3), "beat_rate": round(b["beat_rate"], 3),
                      "q05": round(b["z05"] * sig, 5), "q50": round(b["z50"] * sig, 5), "q95": round(b["z95"] * sig, 5),
                      "var95_100eur": round(max(0.0, -b["z05"] * sig) * 100, 2)})
        rows.append(r)

    # riesgo de la cartera objetivo (covarianza de 63 sesiones escalada a 5)
    port = {}
    tick = [t for t in weights if t in cur.index]
    if tick:
        rets = panel["ret_1"].unstack()[tick].iloc[-63:].dropna(how="all").fillna(0)
        w = np.array([weights[t] for t in tick])
        cov = rets.cov().values * h
        sig_p = float(math.sqrt(max(w @ cov @ w, 0)))
        mu_rows = {r["ticker"]: r for r in rows}
        mu = float(sum(weights[t] * (mu_rows.get(t, {}).get("q50") or 0.0) for t in tick))
        invested = float(w.sum())
        port = {"exp_ret": round(mu, 5), "sigma": round(sig_p, 5),
                "p_loss": round(_phi(-mu / sig_p), 3) if sig_p > 0 else None,
                "q05": round(mu - 1.645 * sig_p, 5), "q95": round(mu + 1.645 * sig_p, 5),
                "var95_eur": round(max(0.0, 1.645 * sig_p - mu) * (equity_eur or cfg["capital_eur"]), 2),
                "invested": round(invested, 3)}
    return {"date": str(last.date()), "horizon": h, "champion": champ, "mode": mode,
            "models": list(probs.columns), "rows": rows, "calibration": bins, "portfolio": port}

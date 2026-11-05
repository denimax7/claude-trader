"""Modelos, validación walk-forward y sistema campeón/aspirante.

Idea clave: ningún cambio (ni los que propone Claude) entra en producción si
no mejora al campeón actual en datos que el modelo no vio al entrenar.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from .features import ALL_FEATURES

PARAM_BOUNDS = {
    "lgbm": {
        "n_estimators": (50, 800, int),
        "learning_rate": (0.005, 0.2, float),
        "num_leaves": (4, 63, int),
        "max_depth": (-1, 12, int),
        "min_child_samples": (10, 400, int),
        "subsample": (0.4, 1.0, float),
        "colsample_bytree": (0.3, 1.0, float),
        "reg_lambda": (0.0, 50.0, float),
    },
    "logit": {"C": (0.0005, 100.0, float)},
}

DEFAULT_EXPERIMENTS = [
    {
        "name": "base_lgbm", "algo": "lgbm", "features": ALL_FEATURES,
        "params": {"n_estimators": 250, "learning_rate": 0.03, "num_leaves": 15,
                   "min_child_samples": 80, "subsample": 0.8, "colsample_bytree": 0.8,
                   "reg_lambda": 2.0},
        "created_by": "sistema", "rationale": "Modelo base con todas las variables.",
    },
    {
        "name": "momentum_logit", "algo": "logit",
        "features": ["ret_21", "ret_63", "ret_126", "ret_252", "xs_rank_63", "xs_rank_126",
                     "rel_bench_63", "vol_63", "sma200_gap", "mkt_sma200_gap"],
        "params": {"C": 0.1},
        "created_by": "sistema", "rationale": "Regresión logística simple de momentum: referencia robusta.",
    },
]


def validate_experiment(exp: dict) -> dict:
    """Limpia una propuesta (p. ej. de Claude): features y parámetros dentro de rangos."""
    algo = exp.get("algo", "lgbm")
    if algo not in PARAM_BOUNDS:
        raise ValueError(f"algoritmo no permitido: {algo}")
    feats = [f for f in exp.get("features", []) if f in ALL_FEATURES]
    if len(feats) < 3:
        raise ValueError("se necesitan al menos 3 variables válidas")
    params = {}
    for k, v in (exp.get("params") or {}).items():
        if k in PARAM_BOUNDS[algo]:
            lo, hi, typ = PARAM_BOUNDS[algo][k]
            params[k] = typ(min(max(float(v), lo), hi))
    name = "".join(ch for ch in str(exp.get("name", "exp")) if ch.isalnum() or ch in "_-")[:40] or "exp"
    return {"name": name, "algo": algo, "features": feats, "params": params,
            "created_by": exp.get("created_by", "claude"), "rationale": str(exp.get("rationale", ""))[:600]}


def make_model(algo: str, params: dict):
    if algo == "lgbm":
        from lightgbm import LGBMClassifier

        p = {"subsample_freq": 1, "verbose": -1, "n_jobs": 2, "random_state": 7, **params}
        return LGBMClassifier(**p)
    return make_pipeline(StandardScaler(), LogisticRegression(max_iter=2000, **params))


def _xy(df: pd.DataFrame, feats: list[str]):
    X = df[feats].astype(float)
    if X.isna().any().any():
        X = X.fillna(X.median()).fillna(0)
    return X, df["label"].astype(int)


def walk_forward(panel: pd.DataFrame, exp: dict, cfg: dict) -> dict:
    """Valida el experimento en los últimos `wf_folds` meses, uno a uno, sin mirar al futuro."""
    m = cfg["model"]
    k = cfg["strategy"]["top_k"]
    lab = panel[panel["label"].notna()]
    dates = lab.index.get_level_values("date").unique().sort_values()
    h, fd, nf, win = m["horizon_days"], m["fold_days"], m["wf_folds"], m["train_window_days"]
    daily, probs, ys = [], [], []
    for i in range(nf, 0, -1):
        t0 = len(dates) - i * fd
        if t0 - h - 60 < 0:
            continue
        test_d = dates[t0: t0 + fd]
        train_end = dates[t0 - h]  # purga: el label de entrenamiento no puede solapar el test
        train_start = dates[max(0, t0 - h - win)]
        dts = lab.index.get_level_values("date")
        tr = lab[(dts >= train_start) & (dts < train_end)]
        te = lab[dts.isin(test_d)]
        if len(tr) < 500 or te.empty:
            continue
        Xtr, ytr = _xy(tr, exp["features"])
        model = make_model(exp["algo"], exp["params"]).fit(Xtr, ytr)
        Xte, yte = _xy(te, exp["features"])
        p = model.predict_proba(Xte)[:, 1]
        te = te.assign(prob=p)
        probs.extend(p)
        ys.extend(yte)
        for _, g in te.groupby(level="date"):
            top = g.nlargest(k, "prob")
            daily.append({"topk_excess": top["excess"].mean(), "topk_hit": (top["excess"] > 0).mean()})
    if not daily:
        return {"score": float("nan"), "auc": float("nan"), "hit": float("nan"), "n_days": 0}
    d = pd.DataFrame(daily)
    auc = roc_auc_score(ys, probs) if len(set(ys)) > 1 else float("nan")
    return {
        "score": float(d["topk_excess"].mean()),  # exceso medio a 5 días de las top-k elegidas
        "hit": float(d["topk_hit"].mean()),
        "auc": float(auc),
        "n_days": int(len(d)),
        "evaluated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def fit_full(panel: pd.DataFrame, exp: dict, cfg: dict):
    lab = panel[panel["label"].notna()]
    dates = lab.index.get_level_values("date").unique().sort_values()
    start = dates[max(0, len(dates) - cfg["model"]["train_window_days"])]
    tr = lab[lab.index.get_level_values("date") >= start]
    X, y = _xy(tr, exp["features"])
    return make_model(exp["algo"], exp["params"]).fit(X, y)


def predict_latest(model, panel: pd.DataFrame, exp: dict) -> pd.DataFrame:
    last = panel.index.get_level_values("date").max()
    cur = panel.xs(last, level="date")
    X = cur[exp["features"]].astype(float)
    X = X.fillna(X.median()).fillna(0)
    out = pd.DataFrame({"prob": model.predict_proba(X)[:, 1]}, index=cur.index)
    out["rank"] = out["prob"].rank(ascending=False, method="first").astype(int)
    out.attrs["date"] = last.date().isoformat()
    return out.sort_values("prob", ascending=False)


def feature_importance(model, exp: dict) -> dict[str, float]:
    try:
        if exp["algo"] == "lgbm":
            imp = model.feature_importances_.astype(float)
        else:
            imp = np.abs(model[-1].coef_[0])
        imp = imp / (imp.sum() or 1)
        return dict(sorted(zip(exp["features"], map(float, imp)), key=lambda x: -x[1])[:10])
    except Exception:  # noqa: BLE001
        return {}


def choose_champion(registry: dict, evals: dict[str, dict], margin: float) -> tuple[str, str]:
    """Devuelve (campeón, motivo). Solo cambia si el aspirante supera al campeón + margen."""
    valid = {n: e for n, e in evals.items() if e.get("n_days", 0) > 0 and not np.isnan(e["score"])}
    if not valid:
        return registry.get("champion") or next(iter(evals)), "sin evaluaciones válidas"
    best = max(valid, key=lambda n: valid[n]["score"])
    cur = registry.get("champion")
    if cur not in valid:
        return best, "primer campeón"
    if best != cur and valid[best]["score"] > valid[cur]["score"] + margin:
        return best, f"{best} supera a {cur} ({valid[best]['score']:+.4f} vs {valid[cur]['score']:+.4f})"
    return cur, "el campeón se mantiene"

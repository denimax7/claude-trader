"""Investigación con replay histórico: acelera el aprendizaje usando el pasado.

Cómo funciona el replay de un experimento:
  - Se coloca en una fecha del pasado (p. ej. un lunes) con SOLO los datos hasta ese día.
  - Entrena (con purga: ninguna etiqueta de entrenamiento mira más allá de esa fecha).
  - Elige las top-k, las "mantiene" 5 sesiones, cobra comisiones por rotación y mide
    contra el SPY. Avanza 5 sesiones y repite. Reentrena cada `refit_every` periodos.

Para no engañarse (sobreajuste por probar muchas variantes sobre el mismo pasado):
  - Ventana de INVESTIGACIÓN (≈2 años): aquí se buscan y ordenan las variantes.
  - EXAMEN FINAL (últimos ≈6 meses): la búsqueda nunca lo ve. Una variante solo se
    evalúa en él si ya gana en investigación, y solo entra a competir en vivo si
    también lo aprueba. Luego aún tiene que ganar al campeón en el día a día real.
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from . import store
from .features import ALL_FEATURES
from .model import PARAM_BOUNDS, _xy, make_model, validate_experiment

RDIR = "research"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def exp_key(exp: dict) -> str:
    spec = {"algo": exp["algo"], "features": sorted(exp["features"]), "params": exp["params"]}
    return hashlib.sha1(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:10]


def _sig(exp: dict) -> str:
    """Firma "de fondo" de un modelo: mismo algoritmo y mismas variables = misma idea."""
    return exp["algo"] + ":" + ",".join(sorted(exp["features"]))


def windows(panel: pd.DataFrame, cfg: dict) -> dict:
    """Fechas de rebalanceo de la ventana de investigación y del examen final."""
    r = cfg["research"]
    h = cfg["model"]["horizon_days"]
    lab = panel[panel["fwd_ret"].notna()]
    dates = lab.index.get_level_values("date").unique().sort_values()
    n = len(dates)
    hold_start = n - r["holdout_days"]
    res_start = max(hold_start - r["research_days"], cfg["model"]["train_window_days"] // 2 + h)
    reb = lambda a, b: list(dates[a:b:h])  # noqa: E731  (una decisión cada h sesiones)
    return {"research": reb(res_start, hold_start - h), "holdout": reb(hold_start, n), "all_dates": dates}


def replay(panel: pd.DataFrame, exp: dict, cfg: dict, rebal_dates: list) -> pd.DataFrame:
    """Simula decisiones en `rebal_dates` sin mirar al futuro. Devuelve una fila por periodo,
    con las probabilidades de todo el universo para poder probar estrategias después."""
    m, r, c = cfg["model"], cfg["research"], cfg["costs"]
    h, win = m["horizon_days"], m["train_window_days"]
    bench = cfg["market"]["benchmark"]
    lab = panel[panel["label"].notna()]
    all_dates = panel.index.get_level_values("date").unique().sort_values()
    pos = {d: i for i, d in enumerate(all_dates)}
    ldates = lab.index.get_level_values("date")
    model, rows = None, []
    for i, d in enumerate(rebal_dates):
        if model is None or i % r["refit_every"] == 0:
            j = pos[d]
            train_end = all_dates[max(0, j - h)]          # purga del horizonte
            train_start = all_dates[max(0, j - h - win)]
            tr = lab[(ldates >= train_start) & (ldates < train_end)]
            if len(tr) < 500:
                continue
            X, y = _xy(tr, exp["features"])
            model = make_model(exp["algo"], exp["params"]).fit(X, y)
        cur = panel.xs(d, level="date")
        cur = cur[cur["fwd_ret"].notna()]
        if cur.empty or bench not in cur.index:
            continue
        X = cur[exp["features"]].astype(float)
        X = X.fillna(X.median()).fillna(0)
        p = pd.Series(model.predict_proba(X)[:, 1], index=cur.index)
        rows.append({"date": d, "probs": p.to_dict(), "fwd": cur["fwd_ret"].to_dict(),
                     "bench": float(cur.loc[bench, "fwd_ret"])})
    cost_side = c["commission_pct"] + c["slippage_pct"] + c["fx_fee_pct"]
    return pd.DataFrame(rows).assign(cost_side=cost_side) if rows else pd.DataFrame()


def strategy_returns(rp: pd.DataFrame, top_k: int, min_prob: float, fallback: str = "benchmark") -> pd.DataFrame:
    """Aplica una regla top-k sobre un replay ya calculado (barato: no reentrena)."""
    out, prev = [], set()
    for _, row in rp.iterrows():
        p = pd.Series(row["probs"]).sort_values(ascending=False)
        picks = [t for t in p.index[:top_k] if p[t] >= min_prob]
        if picks:
            gross = float(np.mean([row["fwd"][t] for t in picks]))
            hold = set(picks)
        elif fallback == "benchmark":
            gross, hold = row["bench"], {"__bench__"}
        else:
            gross, hold = 0.0, set()
        changed = len(hold.symmetric_difference(prev)) / max(1, 2 * max(len(hold), len(prev), 1))
        net = gross - 2 * changed * row["cost_side"]
        out.append({"date": row["date"], "picks": picks, "ret": net, "bench": row["bench"], "excess": net - row["bench"]})
        prev = hold
    return pd.DataFrame(out)


def metrics(sr: pd.DataFrame, h: int) -> dict:
    if sr.empty:
        return {"n": 0, "score": float("nan")}
    ex = sr["excess"].astype(float)
    cum = (1 + sr["ret"]).cumprod() / (1 + sr["bench"]).cumprod()
    sd = ex.std(ddof=1) if len(ex) > 1 else float("nan")
    return {
        "n": int(len(ex)),
        "score": float(ex.mean()),                                    # exceso medio por periodo (neto)
        "excess_annual": float(ex.mean() * 252 / h),
        "t_stat": float(ex.mean() / sd * np.sqrt(len(ex))) if sd and sd == sd and sd > 0 else 0.0,
        "hit": float((ex > 0).mean()),
        "total_ret": float((1 + sr["ret"]).prod() - 1),
        "bench_ret": float((1 + sr["bench"]).prod() - 1),
        "max_dd_vs_bench": float((cum / cum.cummax() - 1).min()),
    }


def mutate(base: dict, rng: np.random.Generator, tag: str) -> dict:
    algo = base["algo"] if rng.random() < 0.8 else ("logit" if base["algo"] == "lgbm" else "lgbm")
    feats = list(base["features"]) if algo == base["algo"] else list(rng.choice(ALL_FEATURES, size=12, replace=False))
    for _ in range(int(rng.integers(1, 4))):
        op = rng.random()
        extra = [f for f in ALL_FEATURES if f not in feats]
        if op < 0.4 and len(feats) > 4:
            feats.remove(rng.choice(feats))
        elif op < 0.8 and extra:
            feats.append(rng.choice(extra))
        elif extra and len(feats) > 4:
            feats[int(rng.integers(len(feats)))] = rng.choice(extra)
    params = dict(base["params"]) if algo == base["algo"] else {}
    for k, (lo, hi, typ) in PARAM_BOUNDS[algo].items():
        if k == "max_depth":
            continue
        if k not in params or rng.random() < 0.4:
            b = float(params.get(k, np.sqrt(max(lo, 1e-4) * hi) if typ is float else (lo + hi) / 2))
            v = b * float(np.exp(rng.normal(0, 0.4)))
            params[k] = typ(round(v) if typ is int else v)
    return validate_experiment({"name": f"r{tag}", "algo": algo, "features": sorted(set(feats)),
                                "params": params, "created_by": "investigación",
                                "rationale": f"Mutación de {base['name']} encontrada por replay histórico."})


def run(cfg: dict, panel: pd.DataFrame, date: str, log=print) -> dict:
    """Una sesión de investigación con presupuesto de tiempo."""
    r = cfg["research"]
    h = cfg["model"]["horizon_days"]
    t0 = time.time()
    budget = r["time_budget_min"] * 60
    rng = np.random.default_rng(int(time.time()))
    w = windows(panel, cfg)
    data_asof = str(w["all_dates"][-1].date())
    k, mp = cfg["strategy"]["top_k"], cfg["strategy"]["min_prob"]
    lb = store.read_json(f"{RDIR}/leaderboard.json", []) or []
    by_key = {e["key"]: e for e in lb}
    stats = store.read_json(f"{RDIR}/stats.json", {}) or {"trials": 0, "holdout_tests": 0, "promoted": 0}
    log(f"investigación: {len(w['research'])} periodos de investigación "
        f"({w['research'][0].date()}→{w['research'][-1].date()}), {len(w['holdout'])} de examen final")

    def evaluate(exp: dict, origin: str) -> dict | None:
        key = exp_key(exp)
        e = by_key.get(key)
        if e and e.get("data_asof") == data_asof:
            return e
        rp = replay(panel, exp, cfg, w["research"])
        if rp.empty:
            return None
        res = metrics(strategy_returns(rp, k, mp, cfg["strategy"].get("no_edge_fallback", "benchmark")), h)
        e = {**(e or {}), "key": key, "name": exp["name"], "origin": origin, "exp": exp,
             "research": res, "data_asof": data_asof, "evaluated": _now()}
        e.setdefault("created", _now())
        by_key[key] = e
        stats["trials"] += 1
        log(f"  {exp['name']:<26} invest. {res['score']:+.4%}/periodo  t={res['t_stat']:+.2f}  acierto {res['hit']:.0%}")
        return e

    # 1) referencias: experimentos activos en vivo (incluido el campeón)
    exps = [e for e in (store.read_json("experiments.json", []) or []) if e.get("status") == "activo"]
    champ_name = (store.read_json("models.json", {}) or {}).get("champion")
    live = {}
    for ex in exps:
        spec = {k2: ex[k2] for k2 in ("name", "algo", "features", "params")}
        e = evaluate(spec, "en vivo")
        if e:
            live[ex["name"]] = e
    champ = live.get(champ_name) or (max(live.values(), key=lambda e: e["research"]["score"]) if live else None)
    if champ is None:
        return {}
    champ_rp = replay(panel, champ["exp"], cfg, w["research"])  # para la rejilla de estrategia

    # 2) búsqueda evolutiva hasta agotar el tiempo (dejando margen para el examen final)
    n_new = 0
    while time.time() - t0 < budget * 0.75:
        pool = sorted([e for e in by_key.values() if e.get("research", {}).get("n")],
                      key=lambda e: e["research"]["score"], reverse=True)[:5]
        parent = pool[int(rng.integers(len(pool)))] if pool and rng.random() < 0.85 else champ
        n_new += 1
        cand = mutate(parent["exp"] | {"name": parent["name"]}, rng, f"{date.replace('-', '')[2:]}_{n_new}")
        evaluate(cand, "búsqueda")

    # 3) examen final: solo para las mejores que superan al campeón en investigación
    margin = r["promote_margin"]
    if "holdout" not in champ or champ.get("holdout_asof") != data_asof:
        rp = replay(panel, champ["exp"], cfg, w["holdout"])
        champ["holdout"] = metrics(strategy_returns(rp, k, mp), h)
        champ["holdout_asof"] = data_asof
    cands = sorted([e for e in by_key.values() if e["origin"] != "en vivo" and not e.get("holdout")
                    and e.get("data_asof") == data_asof
                    and e["research"].get("score", -1) > champ["research"]["score"] + margin
                    and e["research"].get("t_stat", 0) > r["min_t_stat"]],
                   key=lambda e: e["research"]["score"], reverse=True)[: r["max_holdout_tests"]]
    promoted = []
    # Una sola tanda de exámenes por cada día de datos nuevos: repetir búsquedas sobre los
    # mismos datos (p. ej. el fin de semana) solo aumenta la probabilidad de "ganar por suerte".
    if stats.get("exam_asof") == data_asof:
        log("  examen final ya hecho con estos datos; se omite")
        cands = []
    else:
        stats["exam_asof"] = data_asof
    live_sigs = {_sig(x) for x in (store.read_json("experiments.json", []) or []) if x.get("status") == "activo"}
    for e in cands:
        if _sig(e["exp"]) in live_sigs:
            e["status"] = "duplicada"
            log(f"  {e['name']}: mismas variables que un modelo en competición; no se examina")
            continue
        rp = replay(panel, e["exp"], cfg, w["holdout"])
        e["holdout"] = metrics(strategy_returns(rp, k, mp), h)
        e["holdout_asof"] = data_asof
        stats["holdout_tests"] += 1
        ok = e["holdout"]["score"] > max(0.0, champ["holdout"]["score"])
        e["status"] = "aprobada" if ok else "suspende examen"
        log(f"  examen final {e['name']}: {e['holdout']['score']:+.4%} vs campeón {champ['holdout']['score']:+.4%} → {e['status']}")
        if ok:
            promoted.append(e)
            live_sigs.add(_sig(e["exp"]))
        if len(promoted) >= r.get("max_promotions", 1):
            break

    # 4) las aprobadas entran a competir en vivo
    if promoted:
        all_exps = store.read_json("experiments.json", []) or []
        names = {x["name"] for x in all_exps}
        for e in promoted:
            spec = dict(e["exp"])
            if spec["name"] in names:
                spec["name"] = f"{spec['name']}_{e['key'][:4]}"
            all_exps.append({**spec, "status": "activo", "created": _now(), "created_by": "investigación",
                             "research": e["research"], "holdout": e["holdout"]})
            e["status"] = "en competición"
            stats["promoted"] += 1
        store.write_json("experiments.json", all_exps)

    # 5) rejilla de estrategia sobre el replay del campeón (sin reentrenar)
    grid = []
    for tk in r["grid_top_k"]:
        for mpp in r["grid_min_prob"]:
            res = metrics(strategy_returns(champ_rp, tk, mpp), h)
            grid.append({"top_k": tk, "min_prob": mpp, **{x: res[x] for x in ("score", "t_stat", "hit", "n")}})
    grid.sort(key=lambda g: g["score"], reverse=True)

    # 6) guardar
    lb = sorted(by_key.values(), key=lambda e: e["research"].get("score", -1), reverse=True)
    keep = [e for e in lb if e["origin"] == "en vivo" or e.get("status") in ("en competición", "aprobada")]
    lb = (keep + [e for e in lb if e not in keep])[: r["leaderboard_size"]]
    store.STATE.joinpath(RDIR).mkdir(parents=True, exist_ok=True)
    store.write_json(f"{RDIR}/leaderboard.json", lb)
    stats.update({"last_run": _now(), "data_asof": data_asof,
                  "research_window": [str(w["research"][0].date()), str(w["research"][-1].date())],
                  "holdout_window": [str(w["holdout"][0].date()), str(w["holdout"][-1].date())]})
    store.write_json(f"{RDIR}/stats.json", stats)
    summary = {
        "date": date, "data_asof": data_asof, "trials_this_run": n_new, "minutes": round((time.time() - t0) / 60, 1),
        "champion": {"name": champ["name"], "research": champ["research"], "holdout": champ.get("holdout")},
        "top": [{"name": e["name"], "origin": e["origin"], "algo": e["exp"]["algo"], "n_features": len(e["exp"]["features"]),
                 "research": e["research"], "holdout": e.get("holdout"), "status": e.get("status", "")} for e in lb[:10]],
        "promoted": [e["name"] for e in promoted], "strategy_grid": grid[:12],
        "current_strategy": {"top_k": k, "min_prob": mp}, "totals": stats,
    }
    store.write_json(f"{RDIR}/summary.json", summary)
    return summary

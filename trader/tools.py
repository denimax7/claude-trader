"""Herramientas para las sesiones programadas de Claude (sin API ni internet).

Claude lee el estado, escribe en el diario y deja propuestas. Las propuestas NO se
aplican aquí: el siguiente cierre en GitHub Actions las valida (walk-forward con
datos reales) y las aplica o las rechaza.

  python -m trader.tools context                 # resumen del estado para analizar
  python -m trader.tools info                    # variables y rangos permitidos
  python -m trader.tools journal --kind "nota diaria" --file nota.md
  python -m trader.tools lesson "texto de la lección"
  python -m trader.tools review --file revision.md
  python -m trader.tools propose-model --file exp.json --title "..."
  python -m trader.tools propose-strategy --param stop_loss_pct --value 0.08 --title "..." --reason "..."
  python -m trader.tools retire --name exp --reason "..."
  python -m trader.tools dashboard
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from . import store

STRATEGY_BOUNDS = {
    "top_k": (2, 6, int),
    "min_prob": (0.50, 0.60, float),
    "keep_rank": (2, 10, int),
    "keep_prob": (0.45, 0.55, float),
    "stop_loss_pct": (0.03, 0.15, float),
    "take_profit_pct": (0.05, 0.30, float),
    "rebalance_tolerance": (0.02, 0.15, float),
    "no_edge_fallback": (("benchmark", "cash", "trade"), None, str),
}
MAX_PENDING_MODELS = 2


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("Europe/Madrid")).date().isoformat()


def _text(a) -> str:
    if getattr(a, "file", None):
        return open(a.file, encoding="utf-8").read().strip()
    if getattr(a, "text", None):
        return a.text.strip()
    return sys.stdin.read().strip()


def _add_proposal(p: dict) -> None:
    props = store.read_json("proposals.json", []) or []
    props.append({"created": _now(), "status": "pendiente", "author": "claude", **p})
    store.write_json("proposals.json", props)
    print(f"Propuesta registrada ({p['type']}): se validará en el próximo cierre.")


def cmd_context(a) -> None:
    cfg = store.load_config()
    port = store.read_json("portfolio.json", {}) or {}
    eq = [r for r in store.read_csv("equity.csv") if r["phase"] == "close"]
    trades = store.read_csv("trades.csv")
    closed = [t for t in trades if t["side"] == "SELL"]
    out = {
        "hoy": _today(),
        "capital_inicial_eur": cfg["capital_eur"],
        "estrategia_actual": cfg["strategy"],
        "ajustes_aprobados": store.read_json("strategy_overrides.json", {}),
        "ultimo_cierre": store.read_json("last_close_context.json", {}),
        "curva_cierres": [(r["date"], r["equity_eur"], r["bench_eur"]) for r in eq][-a.days:],
        "cartera": port,
        "operaciones_cerradas": closed[-40:],
        "operaciones_recientes": trades[-20:],
        "predicciones_en_vivo": store.read_json("live_metrics.json", {}),
        "modelos": {k: v for k, v in (store.read_json("models.json", {}) or {}).items() if k != "history"},
        "historial_modelos": ((store.read_json("models.json", {}) or {}).get("history") or [])[-15:],
        "experimentos": store.read_json("experiments.json", []),
        "propuestas": (store.read_json("proposals.json", []) or [])[-15:],
        "lecciones": (store.STATE / "lessons.md").read_text(encoding="utf-8") if (store.STATE / "lessons.md").exists() else "",
        "diario_reciente": store.read_journal(12),
        "ultima_ejecucion": store.read_json("last_runs.json", {}),
    }
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str))


def cmd_info(a) -> None:
    from .features import ALL_FEATURES, FEATURE_HELP
    from .model import PARAM_BOUNDS

    print(json.dumps({"variables": ALL_FEATURES, "significado": FEATURE_HELP,
                      "algoritmos_y_rangos": {k: {p: [v[0], v[1]] for p, v in d.items()} for k, d in PARAM_BOUNDS.items()},
                      "estrategia_ajustable": {k: (list(v[0]) if v[2] is str else [v[0], v[1]]) for k, v in STRATEGY_BOUNDS.items()},
                      "max_propuestas_modelo_pendientes": MAX_PENDING_MODELS}, ensure_ascii=False, indent=1))


def cmd_journal(a) -> None:
    store.append_journal({"ts": _now(), "kind": a.kind, "author": "claude", "text": _text(a)})
    print("Entrada añadida al diario.")


def cmd_lesson(a) -> None:
    p = store.STATE / "lessons.md"
    old = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    p.write_text("\n".join(([f"- **{_today()}** · {a.text.strip()}"] + old)[:30]) + "\n", encoding="utf-8")
    print("Lección guardada.")


def cmd_review(a) -> None:
    md = _text(a)
    (store.ROOT / "reviews").mkdir(exist_ok=True)
    (store.ROOT / "reviews" / f"{_today()}.md").write_text(f"# Revisión semanal {_today()}\n\n{md}\n", encoding="utf-8")
    store.append_journal({"ts": _now(), "kind": "revisión semanal", "author": "claude", "text": md})
    print("Revisión guardada.")


def cmd_propose_model(a) -> None:
    from .model import validate_experiment

    pending = [p for p in (store.read_json("proposals.json", []) or [])
               if p.get("type") == "model" and p.get("status") == "pendiente"]
    if len(pending) >= MAX_PENDING_MODELS:
        sys.exit(f"Ya hay {len(pending)} propuestas de modelo pendientes; espera al próximo cierre.")
    raw = json.loads(_text(a))
    exp = validate_experiment({**raw, "created_by": "claude"})  # falla si es inválido
    dropped = sorted(set(raw.get("features", [])) - set(exp["features"]))
    if dropped:
        print(f"Aviso: variables ignoradas por no existir: {dropped}")
    _add_proposal({"type": "model", "title": a.title, "experiment": exp})


def cmd_propose_strategy(a) -> None:
    if a.param not in STRATEGY_BOUNDS:
        sys.exit(f"Parámetro no ajustable. Opciones: {list(STRATEGY_BOUNDS)}")
    _add_proposal({"type": "strategy", "title": a.title, "param": a.param, "value": a.value, "reason": a.reason})


def cmd_retire(a) -> None:
    _add_proposal({"type": "retire", "title": f"retirar {a.name}", "name": a.name, "reason": a.reason})


def cmd_dashboard(a) -> None:
    from . import dashboard

    dashboard.build(store.load_config())
    print("Panel regenerado (docs/index.html).")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="trader.tools")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("context"); c.add_argument("--days", type=int, default=60); c.set_defaults(f=cmd_context)
    sub.add_parser("info").set_defaults(f=cmd_info)
    c = sub.add_parser("journal"); c.add_argument("--kind", default="nota diaria"); c.add_argument("--file"); c.add_argument("--text"); c.set_defaults(f=cmd_journal)
    c = sub.add_parser("lesson"); c.add_argument("text"); c.set_defaults(f=cmd_lesson)
    c = sub.add_parser("review"); c.add_argument("--file"); c.add_argument("--text"); c.set_defaults(f=cmd_review)
    c = sub.add_parser("propose-model"); c.add_argument("--file"); c.add_argument("--text"); c.add_argument("--title", required=True); c.set_defaults(f=cmd_propose_model)
    c = sub.add_parser("propose-strategy"); c.add_argument("--param", required=True); c.add_argument("--value", required=True)
    c.add_argument("--title", required=True); c.add_argument("--reason", required=True); c.set_defaults(f=cmd_propose_strategy)
    c = sub.add_parser("retire"); c.add_argument("--name", required=True); c.add_argument("--reason", required=True); c.set_defaults(f=cmd_retire)
    sub.add_parser("dashboard").set_defaults(f=cmd_dashboard)
    a = ap.parse_args(argv)
    a.f(a)


if __name__ == "__main__":
    main()

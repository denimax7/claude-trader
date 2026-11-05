"""La parte "Claude" del agente: tesis, post-mortems, diario y revisión semanal.

Claude NO decide las operaciones: las decide el modelo estadístico, que es medible.
Claude explica, detecta patrones en los errores y propone experimentos, que solo
se adoptan si ganan en validación walk-forward.
"""
from __future__ import annotations

import json
import os
import re

from .features import ALL_FEATURES, FEATURE_HELP
from .model import PARAM_BOUNDS

SYSTEM = (
    "Eres el analista de un laboratorio de inversión SIMULADA (paper trading, 200 € ficticios) "
    "cuyo objetivo es aprender. Un modelo estadístico decide las operaciones; tú las explicas, "
    "haces post-mortems honestos y propones mejoras verificables. Escribe en español, claro y "
    "breve. Distingue siempre mala suerte (ruido) de mala decisión (proceso). No inventes datos: "
    "usa solo lo que se te da. Con pocas operaciones, no saques conclusiones fuertes."
)


def available(cfg: dict) -> bool:
    return bool(cfg.get("claude", {}).get("enabled")) and bool(os.environ.get("ANTHROPIC_API_KEY"))


def _ask(cfg: dict, model_key: str, prompt: str) -> str:
    import anthropic

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=cfg["claude"][model_key], max_tokens=cfg["claude"]["max_tokens"],
        system=SYSTEM, messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()


def daily_note(cfg: dict, ctx: dict) -> tuple[str, str]:
    """Devuelve (autor, texto markdown) con el análisis del día."""
    if not available(cfg):
        return "sistema", _fallback_daily(ctx)
    prompt = (
        "Datos del cierre de hoy (JSON):\n```json\n" + json.dumps(ctx, ensure_ascii=False, default=str)[:24000]
        + "\n```\n\nEscribe la nota diaria del diario de trading con estas secciones (markdown, máx. 350 palabras):\n"
        "1. **Resumen**: cartera vs benchmark hoy y desde el inicio.\n"
        "2. **Tesis de las nuevas posiciones objetivo**: por qué el modelo las elige (usa las variables más "
        "importantes y los titulares si los hay) y qué invalidaría la tesis.\n"
        "3. **Post-mortem** de las operaciones cerradas recientemente: acierto/error, ¿ruido o proceso?\n"
        "4. **Lección** (una frase) si hay alguna nueva; si no, escribe 'Sin lección nueva'."
    )
    try:
        return "claude", _ask(cfg, "daily_model", prompt)
    except Exception as e:  # noqa: BLE001
        return "sistema", _fallback_daily(ctx) + f"\n\n_(Claude no disponible: {type(e).__name__})_"


def weekly_review(cfg: dict, ctx: dict) -> tuple[str, dict | None]:
    """Revisión semanal: análisis + lecciones + (opcional) un experimento nuevo."""
    if not available(cfg):
        return "_Revisión automática sin Claude (falta ANTHROPIC_API_KEY)._\n\n" + _fallback_review(ctx), None
    schema = {
        "analysis_md": "markdown, máx. 450 palabras",
        "lessons": ["frases cortas y accionables, máx. 3"],
        "proposal": {
            "name": "nombre_corto_snake_case", "algo": "lgbm | logit",
            "features": f"subconjunto de {ALL_FEATURES}",
            "params": {k: f"{v[0]}..{v[1]}" for algo in PARAM_BOUNDS for k, v in PARAM_BOUNDS[algo].items()},
            "rationale": "por qué debería mejorar, en relación con los errores observados",
        },
    }
    prompt = (
        "Revisión semanal del laboratorio. Datos (JSON):\n```json\n"
        + json.dumps(ctx, ensure_ascii=False, default=str)[:40000]
        + "\n```\nSignificado de variables: " + json.dumps(FEATURE_HELP, ensure_ascii=False)
        + "\n\nTareas: (1) analiza rendimiento, calibración y patrones de error; (2) extrae lecciones; "
        "(3) propone COMO MUCHO un experimento nuevo del modelo (o `null` si no hay motivo). "
        "Se validará en walk-forward y solo se adoptará si supera al campeón, así que propone algo "
        "concreto y distinto de los experimentos existentes. Responde SOLO con un bloque ```json con "
        "este esquema:\n" + json.dumps(schema, ensure_ascii=False, indent=1)
    )
    try:
        raw = _ask(cfg, "review_model", prompt)
        m = re.search(r"```json\s*(\{.*\})\s*```", raw, re.S) or re.search(r"(\{.*\})", raw, re.S)
        data = json.loads(m.group(1))
        md = data.get("analysis_md", "")
        lessons = data.get("lessons") or []
        if lessons:
            md += "\n\n**Lecciones:**\n" + "\n".join(f"- {l}" for l in lessons[:3])
        return md, {"lessons": lessons[:3], "proposal": data.get("proposal")}
    except Exception as e:  # noqa: BLE001
        return f"_Revisión de Claude fallida ({type(e).__name__}); se usa la automática._\n\n" + _fallback_review(ctx), None


# ───────────── textos de respaldo sin API ─────────────
def _fallback_daily(ctx: dict) -> str:
    p = ctx.get("performance", {})
    lines = [f"**Resumen**: cartera {p.get('equity_eur', 0):.2f} € ({p.get('return_pct', 0):+.2%}) vs "
             f"benchmark {p.get('bench_return_pct', 0):+.2%}."]
    tgt = ctx.get("targets", {})
    if tgt:
        lines.append("**Objetivo para mañana**: " + ", ".join(
            f"{t} (p={v['prob']:.2f})" for t, v in tgt.items()))
    else:
        lines.append("**Objetivo para mañana**: liquidez; ninguna señal supera el umbral.")
    for tr in ctx.get("recent_closed", [])[:5]:
        lines.append(f"- Cierre {tr['ticker']}: {float(tr.get('pnl_eur') or 0):+.2f} € ({tr.get('reason')})")
    return "\n\n".join(lines)


def _fallback_review(ctx: dict) -> str:
    s = ctx.get("week_stats", {})
    return (f"Operaciones cerradas: {s.get('n_closed', 0)}, acierto {s.get('win_rate', 0):.0%}, "
            f"PnL {s.get('pnl_eur', 0):+.2f} €. Acierto de predicciones vivas: "
            f"{ctx.get('live_metrics', {}).get('hit_rate', float('nan')):.0%}.")

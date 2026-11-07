# Claude Trader 🧪📈

Laboratorio de **inversión simulada** que opera solo, analiza sus decisiones con Claude y **aprende de sus errores**. Usa datos reales de mercado (Yahoo Finance) pero **no mueve dinero real**: parte de **200 € ficticios**.

> Proyecto educativo. No es asesoramiento financiero.

## Cómo funciona

```
          ┌──────────── GitHub Actions (3 veces por sesión de NY) ────────────┐
          │                                                                    │
 APERTURA │  ejecuta las órdenes preparadas ayer  →  stop-loss / take-profit   │
 9:35 NY  │                                                                    │
          │                                                                    │
 MEDIA    │  vigila riesgos (stop-loss / take-profit)                          │
 12:45 NY │                                                                    │
          │                                                                    │
 CIERRE   │  1. valora la cartera y la compara con el SPY                      │
 15:45 NY │  2. puntúa las predicciones de hace 5 sesiones (¿acertó?)          │
          │  3. REENTRENA todos los modelos con los datos nuevos               │
          │  4. walk-forward: compara modelos en datos que no vieron           │
          │  5. el mejor (con margen) es el campeón → señal para mañana        │
          │  6. Claude escribe tesis, post-mortems y lecciones en el diario    │
          │                                                                    │
 SÁBADO   │  Exploración automática: 4 variantes aleatorias del campeón;       │
 8:17 ES  │  las que le ganan entran a competir                                │
          └────────────────────────────────────────────────────────────────────┘
          ┌──────── Tareas programadas de Claude (incluidas en el plan) ──────┐
 L-V      │  Nota diaria: tesis, post-mortems y lecciones                      │
 23:12 ES │                                                                    │
 SÁBADO   │  Revisión semanal: analiza errores y calibración, depura           │
 11:23 ES │  lecciones y PROPONE hasta 2 modelos, 1 ajuste de estrategia y     │
          │  retiradas → el siguiente cierre los valida y aplica o rechaza     │
          └────────────────────────────────────────────────────────────────────┘
                     │ commit de state/ y docs/
                     ▼
              Panel en GitHub Pages (docs/index.html)
```

En hora de España: **~15:35, ~18:45 y ~21:45** (de lunes a viernes, festivos de la NYSE excluidos).

### Quién decide qué

| Pieza | Papel |
|---|---|
| **Modelo estadístico** (LightGBM / regresión logística) | Decide las operaciones. Predice qué activos batirán a la media del universo en 5 sesiones. |
| **Walk-forward** | Juez imparcial: cada modelo se prueba en los últimos 6 meses, mes a mes, sin ver el futuro. |
| **Claude** | Analista: explica, hace post-mortems, guarda lecciones y propone experimentos. **No puede saltarse al juez.** |
| **Fallback** | Si el campeón no demuestra ventaja (score ≤ 0), la cartera se queda en el SPY en lugar de apostar a ciegas. |

### Cómo "aprende"

1. **Cada día**: los modelos se reentrenan con los datos nuevos (incluidos los resultados de sus predicciones).
2. **Cada día**: los experimentos compiten; si un aspirante supera al campeón con margen, lo sustituye.
3. **Cada sábado**: exploración automática de variantes del campeón (sin Claude).
4. **Cada sábado**: Claude revisa errores, calibración y el resultado de sus propuestas anteriores, depura las lecciones (`state/lessons.md`) y propone modelos nuevos, ajustes de estrategia (dentro de límites seguros) o retiradas. Todo pasa por `state/proposals.json` y lo valida el siguiente cierre.

Las instrucciones que siguen las sesiones de Claude están en [`CLAUDE.md`](CLAUDE.md). La clave `ANTHROPIC_API_KEY` es **opcional**: si existe, el cierre también escribe una nota vía API.

## Puesta en marcha (10 minutos)

1. **Crea un repositorio** en GitHub con este contenido (público si quieres el panel gratis en Pages).
2. **Secreto de Claude (opcional, de pago)**: *Settings → Secrets and variables → Actions → New repository secret*
   - Nombre: `ANTHROPIC_API_KEY`
   - Valor: tu clave de [console.anthropic.com](https://console.anthropic.com) (la API se factura aparte del plan de Claude; con Haiku a diario y Sonnet semanal el coste es de céntimos al día).
   - Sin clave también funciona: el diario se escribe con textos automáticos.
3. **Permisos**: *Settings → Actions → General → Workflow permissions → Read and write*.
4. **Panel**: *Settings → Pages → Deploy from a branch → `main` / `docs`*.
5. **Primer arranque**: *Actions → trader → Run workflow* con `phase = open` y `force = true`. Crea la cartera de 200 €, entrena y opera.

A partir de ahí funciona solo.

## Probar en local

```bash
pip install -r requirements.txt
python -m trader --phase close --force      # con datos reales
TRADER_OFFLINE=1 python -m trader --phase open --force   # con datos sintéticos
```

## Ajustes (`config.yaml`)

- `capital_eur`, `universe` (31 ETFs y acciones grandes), costes (comisión, deslizamiento, cambio EUR/USD).
- Estrategia: `top_k` posiciones, umbrales, stop-loss 6 %, take-profit 12 %, `no_edge_fallback`.
- Modelo: horizonte 5 días, ventana 3 años, 6 bloques de validación de 21 días.

## Ficheros de estado (`state/`)

| Fichero | Contenido |
|---|---|
| `portfolio.json` | cartera, liquidez y participaciones del benchmark |
| `trades.csv` | todas las operaciones con motivo, PnL y días en cartera |
| `equity.csv` | valor de la cartera y del SPY en cada ejecución |
| `predictions.csv` | cada predicción y su resultado real 5 sesiones después |
| `models.json` | campeón, puntuaciones walk-forward e importancia de variables |
| `experiments.json` | experimentos (del sistema y de Claude), activos y retirados |
| `journal.jsonl` · `lessons.md` | diario y lecciones aprendidas |

## Límites honestos

- Lo más probable es que **no bata al índice** de forma sostenida: eso también es aprendizaje.
- Con 200 € y pocas operaciones, unas semanas no permiten sacar conclusiones; mira meses.
- Los precios de Yahoo pueden llegar con unos minutos de retraso y fallar puntualmente.
- GitHub puede retrasar las ejecuciones programadas varios minutos.
- GitHub desactiva los workflows programados tras 60 días sin actividad; los commits del bot cuentan como actividad.

# Claude Trader — instrucciones para Claude

Laboratorio de inversión **simulada** (200 € ficticios, datos reales, sin dinero real).
Objetivo: **aprender**. Tu papel es el de analista y responsable de mejora continua.

## Reparto de trabajo

- **GitHub Actions** (automático, con datos reales): opera la cartera 3 veces por sesión,
  reentrena y compara modelos cada cierre (walk-forward) y, los sábados, explora
  variantes aleatorias del campeón. Es el **juez**: nada cambia sin pasar por él.
- **Investigación nocturna** (GitHub, automático): replay histórico. Recorre ~100 semanas
  del pasado decidiendo sin ver el futuro, prueba decenas de variantes y solo promueve a
  competir en vivo las que además aprueban un **examen final** (últimos ~6 meses) que la
  búsqueda nunca ve. Resultados en `state/research/summary.json` (`investigacion_replay`
  en el contexto), incluida una **rejilla de estrategia** (top_k × min_prob) sobre el pasado.
- **Tú (tareas programadas de Claude)**: escribes la nota diaria tras el cierre y la
  revisión semanal del sábado, extraes lecciones y dejas **propuestas**. El siguiente
  cierre de GitHub las valida y las aplica o rechaza (resultado en `state/proposals.json`).

Este entorno normalmente **no tiene acceso a datos de mercado** (Yahoo bloqueado): trabaja
con lo que hay en `state/`. `state/last_close_context.json` incluye titulares recientes.
Puedes usar búsqueda web para contexto de noticias o macro, citando la fuente.

## Preparación (siempre)

```bash
pip install -q --break-system-packages pandas numpy scikit-learn PyYAML
git pull --rebase origin main
python -m trader.tools context > /tmp/ctx.json   # estado completo
python -m trader.tools info                      # variables y rangos permitidos
```

## Rutina diaria (lunes a viernes, después del cierre)

1. Comprueba en `ultima_ejecucion` que el cierre de hoy (`close`) se ejecutó. Si no, escribe
   una nota breve indicándolo y analiza el último cierre disponible.
2. Escribe una nota (máx. ~350 palabras, markdown, español) y guárdala con
   `python -m trader.tools journal --kind "nota diaria" --file nota.md`:
   - **Resumen**: cartera vs SPY hoy y desde el inicio.
   - **Tesis** de cada posición objetivo para mañana: por qué la elige el modelo
     (variables importantes, titulares) y **qué la invalidaría**. Si el modo es
     "sin ventaja demostrada", explica por qué la cartera está en el benchmark.
   - **Post-mortem** de operaciones cerradas recientemente: ¿acierto o error?,
     ¿ruido (mala suerte) o proceso (mala decisión)?
   - **Propuestas procesadas** hoy (si las hay) y qué significan.
3. Si hay una lección nueva y bien fundada: `python -m trader.tools lesson "..."`.
   No repitas lecciones existentes. Una lección con <10 casos es una hipótesis: dilo.
4. Entre semana **no** propongas cambios salvo error evidente (p. ej. un experimento roto).
5. Publica (ver abajo).

## Revisión semanal (sábado)

1. Analiza la semana y el histórico: rentabilidad vs SPY, acierto de las elegidas,
   **calibración** (`predicciones_en_vivo.calibration`: ¿las probabilidades altas aciertan más?),
   evolución de puntuaciones (`historial_modelos`), resultado de la investigación con
   replay (ranking, exámenes aprobados/suspendidos, rejilla), de la exploración del sábado
   y de las propuestas anteriores (¿funcionaron?).
2. Revisa y depura las lecciones: si una lección quedó desmentida, añade otra que lo diga.
3. Propón mejoras **concretas y justificadas por lo observado**:
   - Hasta **2 modelos nuevos**: escribe un JSON
     `{"name","algo":"lgbm|logit","features":[...],"params":{...},"rationale"}` y
     `python -m trader.tools propose-model --file exp.json --title "..."`.
     Que sean distintos de los existentes y de los ya rechazados.
   - Usa la investigación como fuente principal de evidencia: ¿qué tipo de variantes
     ganan en el pasado y cuáles suspenden el examen? Propón modelos en las direcciones
     que funcionan (no copies variantes ya probadas en el ranking).
   - Como mucho **1 ajuste de estrategia**, solo si lo respaldan ≥20 operaciones cerradas
     **o** la rejilla de estrategia del replay (≥80 periodos, t ≥ 1 y mejor que la actual
     con claridad, no por décimas): `python -m trader.tools propose-strategy --param P --value V --title "..." --reason "..."`.
   - Retira experimentos que lleven ≥3 semanas claramente por debajo:
     `python -m trader.tools retire --name X --reason "..."`.
4. Escribe la revisión (máx. ~600 palabras) con `python -m trader.tools review --file revision.md`:
   qué funcionó, qué no, lecciones, propuestas y qué esperas que muestren.
5. Publica.

## Publicar

```bash
python -m trader.tools dashboard
git add -A state docs; [ -d reviews ] && git add -A reviews
git commit -m "claude: <nota diaria|revisión semanal> <fecha>"
git pull --rebase origin main && git push origin main
```
Si el push falla por conflicto, repite `git pull --rebase` y `git push` (máx. 3 veces).
Nunca hagas `push --force` ni edites a mano `trades.csv`, `equity.csv`, `portfolio.json`,
`predictions.csv`, `models.json` o `experiments.json`: solo los toca el motor.

## Principios

- El benchmark parte de ~−0,3 % porque su compra inicial simulada paga comisión,
  deslizamiento y cambio EUR→USD, igual que la cartera. Toda operación cuesta ~0,3 %
  ida y vuelta: tenlo en cuenta al juzgar la rotación.

- Distingue ruido de señal. Con pocas operaciones, conclusiones prudentes.
- Lo normal es no batir al índice: dilo con claridad si pasa. No maquilles resultados.
- No toques el código salvo para arreglar un fallo evidente que impida funcionar; si lo
  haces, explícalo en el diario.
- Escribe en español, claro y directo.

"""Genera docs/index.html (panel estático para GitHub Pages) con los datos embebidos."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from . import store


def build(cfg: dict) -> None:
    equity = store.read_csv("equity.csv")
    trades = store.read_csv("trades.csv")
    port = store.read_json("portfolio.json", {}) or {}
    data = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "capital": cfg["capital_eur"],
        "benchmark": cfg["market"]["benchmark"],
        "equity": [{"ts": r["ts"], "date": r["date"], "phase": r["phase"], "eq": float(r["equity_eur"]),
                    "bench": float(r["bench_eur"])} for r in equity[-2000:]],
        "portfolio": port,
        "trades": trades[-150:][::-1],
        "targets": store.read_json("targets.json", {}),
        "models": store.read_json("models.json", {}),
        "experiments": store.read_json("experiments.json", []),
        "live": store.read_json("live_metrics.json", {}),
        "journal": store.read_journal(40)[::-1],
        "lessons": (store.STATE / "lessons.md").read_text(encoding="utf-8") if (store.STATE / "lessons.md").exists() else "",
        "runs": store.read_json("last_runs.json", {}),
    }
    store.DOCS.mkdir(parents=True, exist_ok=True)
    (store.DOCS / "data.json").write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
    html = TEMPLATE.replace("__DATA__", json.dumps(data, ensure_ascii=False, default=str).replace("</", "<\\/"))
    (store.DOCS / "index.html").write_text(html, encoding="utf-8")


TEMPLATE = r"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Trader</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.2/marked.min.js"></script>
<style>
:root{--bg:#f6f5f1;--card:#fff;--ink:#1b1b18;--mute:#6b6a63;--line:#e4e2da;--pos:#1f7a4d;--neg:#b23a2e;--acc:#c2591d;--bench:#8a8a80}
@media (prefers-color-scheme:dark){:root{--bg:#141412;--card:#1d1d1a;--ink:#ecebe5;--mute:#9a998f;--line:#2e2e2a;--pos:#4fbf86;--neg:#ef7d6f;--acc:#e8844a;--bench:#77776e}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 "IBM Plex Sans",system-ui,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:24px 16px 60px}
header{display:flex;justify-content:space-between;align-items:flex-end;flex-wrap:wrap;gap:8px;border-bottom:1px solid var(--line);padding-bottom:14px;margin-bottom:20px}
h1{font-size:22px;margin:0;font-weight:600;letter-spacing:-.01em}h1 span{color:var(--acc)}
.sub{color:var(--mute);font-size:13px}.tag{font:500 11px "IBM Plex Mono",monospace;border:1px solid var(--line);padding:2px 8px;border-radius:99px;color:var(--mute)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:16px}
.k{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.k .l{font-size:12px;color:var(--mute)}.k .v{font:500 24px "IBM Plex Mono",monospace;margin-top:4px}
.grid{display:grid;grid-template-columns:2fr 1fr;gap:16px}@media(max-width:860px){.grid{grid-template-columns:1fr}}
.grid>div{min-width:0}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:16px;min-width:0}
.card h2{font-size:14px;margin:0 0 12px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--mute)}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:6px 6px;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--mute);font-weight:500}td.n{font-family:"IBM Plex Mono",monospace;text-align:right}th.n{text-align:right}
.tw{overflow-x:auto}.pos{color:var(--pos)}.neg{color:var(--neg)}
.j{border-left:3px solid var(--line);padding:2px 0 2px 12px;margin-bottom:16px}.j.claude{border-color:var(--acc)}
.j .m{font-size:12px;color:var(--mute)}.j .b{font-size:14px}.j .b p{margin:6px 0}
.chartbox{position:relative;height:300px}
.note{font-size:12px;color:var(--mute);margin-top:24px;text-align:center}
.champ{color:var(--acc);font-weight:600}
</style>
</head>
<body><div class="wrap">
<header><div><h1>Claude <span>Trader</span></h1><div class="sub">Laboratorio de inversión simulada · aprende de sus errores</div></div>
<div class="sub" id="upd"></div></header>
<div class="kpis" id="kpis"></div>
<div class="grid">
 <div>
  <div class="card"><h2>Cartera vs benchmark (EUR)</h2><div class="chartbox"><canvas id="ch"></canvas></div></div>
  <div class="card"><h2>Posiciones</h2><div class="tw"><table id="pos"></table></div></div>
  <div class="card"><h2>Operaciones</h2><div class="tw"><table id="trd"></table></div></div>
 </div>
 <div>
  <div class="card"><h2>Objetivo próxima apertura</h2><div class="tw"><table id="tgt"></table></div></div>
  <div class="card"><h2>Modelos (walk-forward)</h2><div class="tw"><table id="mdl"></table></div><div class="sub" id="mdlnote" style="margin-top:8px"></div></div>
  <div class="card"><h2>Predicciones en vivo</h2><div id="live" class="sub"></div></div>
  <div class="card"><h2>Lecciones</h2><div id="les" class="j b"></div></div>
 </div>
</div>
<div class="card"><h2>Diario</h2><div id="jr"></div></div>
<div class="note">Simulación educativa. No es asesoramiento financiero. No se invierte dinero real.</div>
</div>
<script>
const D=__DATA__;
const $=id=>document.getElementById(id);
const eur=v=>(v==null||isNaN(v))?"—":Number(v).toLocaleString("es-ES",{style:"currency",currency:"EUR"});
const pct=v=>(v==null||isNaN(v))?"—":(v>0?"+":"")+(v*100).toFixed(2)+" %";
const cls=v=>v>0?"pos":v<0?"neg":"";
const fmtTs=s=>new Date(s).toLocaleString("es-ES",{day:"2-digit",month:"short",hour:"2-digit",minute:"2-digit"});
$("upd").textContent="Actualizado "+fmtTs(D.updated);
const last=D.equity[D.equity.length-1]||{eq:D.capital,bench:D.capital};
const r=last.eq/D.capital-1, rb=last.bench/D.capital-1;
const closed=D.trades.filter(t=>t.side==="SELL"&&t.pnl_eur!=="");
const wins=closed.filter(t=>+t.pnl_eur>0).length;
const k=[["Valor cartera",eur(last.eq),""],["Rentabilidad",pct(r),cls(r)],["Benchmark "+D.benchmark,pct(rb),cls(rb)],
["Diferencia",pct(r-rb),cls(r-rb)],["Cerradas"+(closed.length?" · "+Math.round(wins/closed.length*100)+" % ganadoras":""),closed.length,""],
["Liquidez",eur(D.portfolio.cash_eur),""]];
$("kpis").innerHTML=k.map(([l,v,c])=>`<div class="k"><div class="l">${l}</div><div class="v ${c}">${v}</div></div>`).join("");
const css=getComputedStyle(document.documentElement);
if(window.Chart&&D.equity.length){new Chart($("ch"),{type:"line",data:{labels:D.equity.map(e=>e.date.slice(5)+" "+({open:"ap",mid:"med",close:"cie"}[e.phase]||"")),datasets:[
{label:"Cartera",data:D.equity.map(e=>e.eq),borderColor:css.getPropertyValue("--acc"),borderWidth:2,pointRadius:0,tension:.2},
{label:D.benchmark,data:D.equity.map(e=>e.bench),borderColor:css.getPropertyValue("--bench"),borderWidth:1.5,borderDash:[4,3],pointRadius:0,tension:.2}]},
options:{maintainAspectRatio:false,interaction:{mode:"index",intersect:false},plugins:{legend:{labels:{color:css.getPropertyValue("--mute")}}},
scales:{x:{ticks:{maxTicksLimit:6,color:css.getPropertyValue("--mute")},grid:{display:false}},y:{ticks:{color:css.getPropertyValue("--mute"),callback:v=>v+" €"},grid:{color:css.getPropertyValue("--line")}}}}});}
const P=D.portfolio.positions||{};
$("pos").innerHTML="<tr><th>Activo</th><th class=n>Coste</th><th class=n>Entrada $</th><th class=n>Último $</th><th class=n>Var.</th><th>Desde</th></tr>"+
(Object.keys(P).length?Object.entries(P).map(([t,p])=>{const ch=p.last_price_usd/p.entry_price_usd-1;return `<tr><td><b>${t}</b></td><td class=n>${eur(p.cost_eur)}</td><td class=n>${(+p.entry_price_usd).toFixed(2)}</td><td class=n>${(+p.last_price_usd).toFixed(2)}</td><td class="n ${cls(ch)}">${pct(ch)}</td><td>${p.entry_date}</td></tr>`}).join(""):"<tr><td colspan=6 class=sub>Sin posiciones (100 % liquidez)</td></tr>");
$("trd").innerHTML="<tr><th>Fecha</th><th>Fase</th><th>Op.</th><th>Activo</th><th class=n>Importe</th><th class=n>PnL</th><th>Motivo</th></tr>"+
(D.trades.length?D.trades.slice(0,40).map(t=>`<tr><td>${t.date}</td><td>${t.phase}</td><td class="${t.side==="BUY"?"pos":"neg"}">${t.side==="BUY"?"Compra":"Venta"}</td><td><b>${t.ticker}</b></td><td class=n>${eur(+t.value_eur)}</td><td class="n ${cls(+t.pnl_eur)}">${t.pnl_eur!==""&&t.pnl_eur!=null?eur(+t.pnl_eur):""}</td><td class=sub>${t.reason}</td></tr>`).join(""):"<tr><td colspan=7 class=sub>Aún no hay operaciones</td></tr>");
const T=D.targets.weights||{};
$("tgt").innerHTML="<tr><th>Activo</th><th class=n>Peso</th><th class=n>Prob.</th></tr>"+(Object.keys(T).length?Object.entries(T).map(([t,w])=>`<tr><td><b>${t}</b></td><td class=n>${(w*100).toFixed(0)} %</td><td class=n>${((D.targets.probs||{})[t]||0).toFixed(3)}</td></tr>`).join(""):"<tr><td colspan=3 class=sub>Liquidez</td></tr>")+
(D.targets.date?`<tr><td colspan=3 class=sub>Señal del ${D.targets.date} · modelo ${D.targets.model||""}${D.targets.mode&&D.targets.mode!=="modelo"?" · "+D.targets.mode:""}</td></tr>`:"");
const ev=(D.models.last_eval)||{};
$("mdl").innerHTML="<tr><th>Experimento</th><th class=n>Score</th><th class=n>Acierto</th><th class=n>AUC</th></tr>"+Object.entries(ev).sort((a,b)=>b[1].score-a[1].score).map(([n,e])=>`<tr><td class="${n===D.models.champion?"champ":""}">${n===D.models.champion?"★ ":""}${n}</td><td class="n ${cls(e.score)}">${pct(e.score)}</td><td class=n>${(e.hit*100).toFixed(0)} %</td><td class=n>${(+e.auc).toFixed(3)}</td></tr>`).join("");
$("mdlnote").textContent=(D.models.reason?"Último cambio: "+D.models.reason+". ":"")+"Score = exceso medio a 5 días de las elegidas, en datos no vistos.";
const L=D.live||{};$("live").innerHTML=L.n?`${L.n} predicciones evaluadas · acierto ${(L.hit_rate*100).toFixed(0)} % · elegidas: acierto ${(L.sel_hit_rate*100||0).toFixed(0)} %, exceso medio ${pct(L.sel_excess)}`:"Las predicciones se evalúan 5 sesiones después de hacerse.";
$("les").innerHTML=D.lessons?(window.marked?marked.parse(D.lessons):D.lessons):"<span class=sub>Las lecciones aparecen tras la primera revisión semanal.</span>";
$("jr").innerHTML=D.journal.length?D.journal.map(j=>`<div class="j ${j.author==="claude"?"claude":""}"><div class="m">${fmtTs(j.ts)} · ${j.kind} · ${j.author==="claude"?"Claude":"sistema"}</div><div class="b">${window.marked?marked.parse(j.text||""):j.text}</div></div>`).join(""):"<span class=sub>Sin entradas todavía.</span>";
</script>
</body></html>
"""

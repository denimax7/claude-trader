"""Vigilante continuo: sustituye a las programaciones de GitHub (que se retrasan horas).

Se ejecuta dentro de un job de GitHub Actions de ~5,5 h. Cada 5 minutos:
  1. trae el último estado del repositorio,
  2. ejecuta lo que toque (apertura / media sesión / cierre, investigación nocturna,
     exploración del sábado); el motor no repite nada ya hecho ese día,
  3. si algo cambió, guarda y sube los cambios.
Al terminar, el workflow se relanza a sí mismo; un vigilante horario lo rearranca si cae.

  python -m trader.loop --minutes 330
"""
from __future__ import annotations

import argparse
import subprocess
import time
import traceback
from datetime import datetime, timezone

from . import dashboard, store


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z] {msg}", flush=True)


def sh(cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, shell=True, text=True, capture_output=True, cwd=store.ROOT)


def sync() -> None:
    """Trae lo último de main. Si hay conflicto, el remoto manda (lo local ya se subió)."""
    r = sh("git pull -q --rebase origin main")
    if r.returncode != 0:
        log(f"pull con conflicto, se alinea con el remoto: {r.stderr.strip()[:200]}")
        sh("git rebase --abort")
        sh("git fetch -q origin main && git reset -q --hard origin/main")


def publish(msg: str) -> None:
    sh("git add -A state docs")
    if (store.ROOT / "reviews").exists():
        sh("git add -A reviews")
    if sh("git diff --cached --quiet").returncode == 0:
        return
    sh(f'git commit -q -m "{msg}"')
    for attempt in range(4):
        r = sh("git push -q origin main")
        if r.returncode == 0:
            log(f"subido: {msg}")
            return
        r = sh("git pull -q --rebase origin main")
        if r.returncode != 0:  # conflicto real: conservar lo remoto y rehacer el panel
            log(f"conflicto al subir ({r.stderr.strip()[:200]}); se rehace sobre el remoto")
            sh("git rebase --abort")
            sh("git fetch -q origin main && git reset -q --hard origin/main")
            return
        try:  # el panel se regenera con el estado combinado
            dashboard.build(store.load_config())
            sh("git add -A docs")
            if sh("git diff --cached --quiet").returncode != 0:
                sh('git commit -q -m "panel"')
        except Exception:  # noqa: BLE001
            pass
        time.sleep(3 + attempt * 5)
    log("no se pudo subir tras varios intentos")


def run_phase(args: list[str]) -> None:
    from .__main__ import main

    try:
        main(args)
    except SystemExit:
        pass
    except Exception:  # noqa: BLE001  (un fallo no debe parar al vigilante)
        log("error en la fase:\n" + traceback.format_exc()[-1500:])


def tick() -> None:
    now = datetime.now(timezone.utc)
    sync()
    # 1) trading: el motor decide si toca apertura, media sesión o cierre (o nada)
    run_phase(["--phase", "auto"])
    publish(f"trader: {now:%Y-%m-%dT%H:%MZ}")
    # 2) investigación nocturna: madrugada UTC de martes a sábado (tras días con mercado)
    if now.weekday() in (1, 2, 3, 4, 5) and 2 <= now.hour < 9:
        run_phase(["--phase", "research"])
        publish(f"research: {now:%Y-%m-%dT%H:%MZ}")
    # 3) exploración automática: sábado por la mañana
    if now.weekday() == 5 and 7 <= now.hour < 12:
        run_phase(["--phase", "explore"])
        publish(f"explore: {now:%Y-%m-%dT%H:%MZ}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=330)
    ap.add_argument("--every", type=float, default=5)
    a = ap.parse_args()
    end = time.time() + a.minutes * 60
    log(f"vigilante en marcha durante {a.minutes:.0f} min, cada {a.every:.0f} min")
    while time.time() < end:
        t0 = time.time()
        try:
            tick()
        except Exception:  # noqa: BLE001
            log("error en el ciclo:\n" + traceback.format_exc()[-1500:])
        time.sleep(max(10, a.every * 60 - (time.time() - t0)))
    log("fin del turno; el workflow se relanza")


if __name__ == "__main__":
    main()

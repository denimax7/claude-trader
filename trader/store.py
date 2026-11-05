"""Persistencia sencilla en ficheros JSON/CSV dentro de state/ (se versiona en git)."""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
DOCS = ROOT / "docs"


def load_config() -> dict:
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def read_json(name: str, default: Any = None) -> Any:
    p = STATE / name
    if not p.exists():
        return default
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def write_json(name: str, data: Any) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    p = STATE / name
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    tmp.replace(p)


def append_csv(name: str, rows: list[dict], fields: list[str]) -> None:
    if not rows:
        return
    STATE.mkdir(parents=True, exist_ok=True)
    p = STATE / name
    new = not p.exists()
    with open(p, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        for r in rows:
            w.writerow(r)


def read_csv(name: str) -> list[dict]:
    p = STATE / name
    if not p.exists():
        return []
    with open(p, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(name: str, rows: list[dict], fields: list[str]) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / name, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def append_journal(entry: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / "journal.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def read_journal(limit: int = 50) -> list[dict]:
    p = STATE / "journal.jsonl"
    if not p.exists():
        return []
    lines = p.read_text(encoding="utf-8").strip().splitlines()
    return [json.loads(l) for l in lines[-limit:] if l.strip()]

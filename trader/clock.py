"""Decide qué fase toca según la hora de Nueva York y el calendario de la NYSE.

GitHub Actions solo entiende UTC y no sabe de horario de verano, así que los
workflows se lanzan dos veces por fase (una por cada desfase posible) y aquí
se decide si toca o no. Si la fase ya se ejecutó hoy, se salta.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

NY = ZoneInfo("America/New_York")


def session(now_utc: datetime, calendar: str = "NYSE"):
    """Devuelve (apertura, cierre) en UTC de la sesión de hoy, o None si es festivo."""
    day = now_utc.astimezone(NY).date()
    try:
        import pandas_market_calendars as mcal

        sched = mcal.get_calendar(calendar).schedule(start_date=day, end_date=day)
        if sched.empty:
            return None
        o = sched.iloc[0]["market_open"].to_pydatetime()
        c = sched.iloc[0]["market_close"].to_pydatetime()
        return o, c
    except Exception:  # calendario no disponible: lunes-viernes 9:30-16:00
        if day.weekday() >= 5:
            return None
        o = datetime(day.year, day.month, day.day, 9, 30, tzinfo=NY).astimezone(timezone.utc)
        c = datetime(day.year, day.month, day.day, 16, 0, tzinfo=NY).astimezone(timezone.utc)
        return o, c


def detect_phase(now_utc: datetime, calendar: str = "NYSE") -> str | None:
    s = session(now_utc, calendar)
    if s is None:
        return None
    o, c = s
    if o - timedelta(minutes=5) <= now_utc < o + timedelta(minutes=100):
        return "open"
    if o + timedelta(minutes=120) <= now_utc < c - timedelta(minutes=45):
        return "mid"
    if c - timedelta(minutes=35) <= now_utc < c + timedelta(hours=4):
        return "close"
    return None


def trading_date(now_utc: datetime) -> str:
    return now_utc.astimezone(NY).date().isoformat()


def add_trading_days(date_str: str, n: int, calendar: str = "NYSE") -> str:
    start = pd.Timestamp(date_str)
    try:
        import pandas_market_calendars as mcal

        days = mcal.get_calendar(calendar).valid_days(start, start + pd.Timedelta(days=n * 2 + 10))
        days = [d.date().isoformat() for d in days if d.date() > start.date()]
        return days[n - 1]
    except Exception:
        return (start + pd.offsets.BDay(n)).date().isoformat()

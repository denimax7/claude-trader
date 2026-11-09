"""Descarga de datos de mercado (yfinance) con modo sintético para pruebas offline."""
from __future__ import annotations

import os
from datetime import datetime

import numpy as np
import pandas as pd

OFFLINE = os.environ.get("TRADER_OFFLINE") == "1"


def _field(df: pd.DataFrame, field: str, tickers: list[str]) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        out = df[field]
    else:  # un solo ticker
        out = df[[field]].rename(columns={field: tickers[0]})
    out = out.reindex(columns=tickers)
    out.index = pd.to_datetime(out.index).tz_localize(None).normalize()
    return out.sort_index()


def download_daily(tickers: list[str], years: int) -> dict[str, pd.DataFrame]:
    """Precios diarios ajustados: {'close','open','volume'} -> DataFrame fecha x ticker."""
    if OFFLINE:
        return _synthetic_daily(tickers, years)
    import yfinance as yf

    df = yf.download(
        tickers, period=f"{years}y", interval="1d", auto_adjust=True,
        progress=False, threads=False, group_by="column",
    )
    if df is None or df.empty:
        raise RuntimeError("yfinance no devolvió datos diarios")
    out = {k.lower(): _field(df, k, tickers) for k in ("Close", "Open", "Volume")}
    out["close"] = out["close"].ffill(limit=3)
    return out


def latest_prices(tickers: list[str]) -> dict[str, float]:
    """Último precio disponible (velas de 5 minutos; si fallan, cierre diario)."""
    if OFFLINE:
        d = _synthetic_daily(tickers, 1)["close"]
        rng = np.random.default_rng(int(datetime.utcnow().timestamp()) // 600)
        last = d.iloc[-1] * (1 + rng.normal(0, 0.004, len(tickers)))
        return {t: float(v) for t, v in last.items()}
    import yfinance as yf

    prices: dict[str, float] = {}
    try:
        df = yf.download(tickers, period="2d", interval="5m", auto_adjust=True,
                         progress=False, threads=False, group_by="column")
        if df is not None and not df.empty:
            close = df["Close"] if isinstance(df.columns, pd.MultiIndex) else df[["Close"]].set_axis(tickers[:1], axis=1)
            for t in tickers:
                if t in close.columns:
                    s = close[t].dropna()
                    if len(s) and s.iloc[-1] > 0:
                        prices[t] = float(s.iloc[-1])
    except Exception as e:  # noqa: BLE001
        print(f"[datos] intradía falló: {e}")
    missing = [t for t in tickers if t not in prices]
    if missing:
        print(f"[datos] sin intradía para {missing}; uso cierre diario")
        d = download_daily(missing, 1)["close"]
        for t in missing:
            s = d[t].dropna()
            if len(s):
                prices[t] = float(s.iloc[-1])
    return prices


def news_headlines(tickers: list[str], per_ticker: int = 4) -> dict[str, list[str]]:
    """Titulares recientes por ticker (contexto cualitativo para Claude)."""
    if OFFLINE:
        return {}
    import yfinance as yf

    out: dict[str, list[str]] = {}
    for t in tickers:
        try:
            items = yf.Ticker(t).news or []
            heads = []
            for it in items[:per_ticker]:
                c = it.get("content", it)
                title = c.get("title")
                date = c.get("pubDate") or c.get("displayTime") or ""
                if title:
                    heads.append(f"{str(date)[:10]} {title}".strip())
            if heads:
                out[t] = heads
        except Exception:  # noqa: BLE001
            continue
    return out


# ───────────────────────── modo sintético (pruebas) ─────────────────────────
def _synthetic_daily(tickers: list[str], years: int) -> dict[str, pd.DataFrame]:
    import zlib

    end = pd.Timestamp(os.environ.get("TRADER_SYNTH_END", pd.Timestamp.today().normalize()))
    full = pd.bdate_range(start="2019-01-01", periods=3000)  # camino fijo, se recorta hasta `end`
    n = len(full)
    mkt = np.random.default_rng(42).normal(0.0003, 0.01, n)
    closes, vols = {}, {}
    for i, t in enumerate(tickers):
        rng = np.random.default_rng(zlib.crc32(t.encode()))
        if t.endswith("=X"):
            closes[t] = 1.08 * np.exp(np.cumsum(rng.normal(0, 0.004, n)))
            vols[t] = np.zeros(n)
            continue
        beta = 0.5 + (i % 5) * 0.25
        # un poco de momentum persistente para que el modelo tenga algo que aprender
        drift = np.convolve(rng.normal(0, 0.0006, n), np.ones(40) / 40, mode="same")
        r = beta * mkt + drift + rng.normal(0, 0.012, n)
        closes[t] = 100 * np.exp(np.cumsum(r))
        vols[t] = rng.lognormal(15, 0.3, n)
    close = pd.DataFrame(closes, index=full).loc[:end]
    vol = pd.DataFrame(vols, index=full).loc[:end]
    return {"close": close, "open": close.shift(1).fillna(close), "volume": vol}

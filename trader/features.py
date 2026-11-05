"""Variables (features) y etiqueta. Todo se calcula solo con información pasada."""
from __future__ import annotations

import numpy as np
import pandas as pd

ALL_FEATURES = [
    "ret_1", "ret_5", "ret_10", "ret_21", "ret_63", "ret_126", "ret_252",
    "vol_21", "vol_63", "sma20_gap", "sma50_gap", "sma200_gap", "rsi_14",
    "dd_252", "volz_21", "xs_rank_21", "xs_rank_63", "xs_rank_126",
    "rel_bench_21", "rel_bench_63", "mkt_ret_21", "mkt_vol_21", "mkt_sma200_gap",
]

FEATURE_HELP = {
    "ret_N": "rentabilidad de los últimos N días",
    "vol_N": "volatilidad anualizada de N días",
    "smaN_gap": "distancia del precio a su media móvil de N días",
    "rsi_14": "RSI de 14 días (sobrecompra/sobreventa)",
    "dd_252": "caída desde el máximo de 1 año",
    "volz_21": "volumen anómalo (z-score 21 días)",
    "xs_rank_N": "ranking de momentum frente al resto del universo",
    "rel_bench_N": "rentabilidad relativa al benchmark",
    "mkt_*": "régimen de mercado (benchmark)",
}


def _rsi(close: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def build_panel(close: pd.DataFrame, volume: pd.DataFrame, bench: str, horizon: int) -> pd.DataFrame:
    """Devuelve un panel (date, ticker) con features, fwd_ret, excess y label."""
    close = close.dropna(how="all")
    r = close.pct_change(fill_method=None)
    f: dict[str, pd.DataFrame] = {}
    for n in (1, 5, 10, 21, 63, 126, 252):
        f[f"ret_{n}"] = close.pct_change(n, fill_method=None)
    f["vol_21"] = r.rolling(21).std() * np.sqrt(252)
    f["vol_63"] = r.rolling(63).std() * np.sqrt(252)
    for n in (20, 50, 200):
        f[f"sma{n}_gap"] = close / close.rolling(n).mean() - 1
    f["rsi_14"] = _rsi(close)
    f["dd_252"] = close / close.rolling(252, min_periods=60).max() - 1
    lv = np.log1p(volume.reindex_like(close).fillna(0))
    f["volz_21"] = (lv - lv.rolling(21).mean()) / lv.rolling(21).std().replace(0, np.nan)
    for n in (21, 63, 126):
        f[f"xs_rank_{n}"] = f[f"ret_{n}" if n != 126 else "ret_126"].rank(axis=1, pct=True)
    b = close[bench]
    f["rel_bench_21"] = f["ret_21"].sub(b.pct_change(21, fill_method=None), axis=0)
    f["rel_bench_63"] = f["ret_63"].sub(b.pct_change(63, fill_method=None), axis=0)
    mkt = {
        "mkt_ret_21": b.pct_change(21, fill_method=None),
        "mkt_vol_21": b.pct_change(fill_method=None).rolling(21).std() * np.sqrt(252),
        "mkt_sma200_gap": b / b.rolling(200).mean() - 1,
    }
    for k, s in mkt.items():
        f[k] = pd.DataFrame({t: s for t in close.columns}, index=close.index)

    fwd = close.shift(-horizon) / close - 1
    excess = fwd.sub(fwd.mean(axis=1), axis=0)

    cols = {k: v.stack(future_stack=True) for k, v in f.items()}
    cols["fwd_ret"] = fwd.stack(future_stack=True)
    cols["excess"] = excess.stack(future_stack=True)
    panel = pd.concat(cols, axis=1)
    panel.index.names = ["date", "ticker"]
    panel = panel.replace([np.inf, -np.inf], np.nan)
    panel = panel.dropna(subset=["ret_63", "vol_21", "sma50_gap"])
    panel["label"] = (panel["excess"] > 0).astype(float).where(panel["excess"].notna())
    return panel

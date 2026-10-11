"""
Market data ingestion, indicators and the five filter gates.

Each gate returns a GateResult so the engine can log exactly why it stayed
FLAT. Data comes from Yahoo Finance via yfinance; fetches are isolated in
MarketFeed so gates can be unit-tested with synthetic DataFrames.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Optional

import numpy as np
import pandas as pd

from config import settings as cfg

log = logging.getLogger(__name__)


@dataclass
class GateResult:
    name: str
    passed: bool
    reason: str
    metrics: dict = field(default_factory=dict)


# ==========================================================================
# Indicators
# ==========================================================================
def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["Close"].shift(1)
    return pd.concat(
        [df["High"] - df["Low"],
         (df["High"] - prev_close).abs(),
         (df["Low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Wilder ATR."""
    return true_range(df).ewm(alpha=1 / n, adjust=False).mean()


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Wilder ADX."""
    up = df["High"].diff()
    down = -df["Low"].diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr = true_range(df).ewm(alpha=1 / n, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / tr
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / tr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean()


def bb_bandwidth(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.Series:
    mid = sma(close, n)
    sd = close.rolling(n).std(ddof=0)
    return (2 * k * sd) / mid


# ==========================================================================
# 2-hour bar construction
# ==========================================================================
def _bucket_close(ts: pd.Timestamp) -> Optional[time]:
    """Map an intraday bar start time to the 2H bar close it belongs to."""
    t = ts.time()
    for close in cfg.BAR_CLOSES:
        if t < close:
            return close
    return None


def resample_2h(intraday: pd.DataFrame, now: datetime) -> pd.DataFrame:
    """
    Aggregate (BKK-localised) intraday bars into session-aligned 2H bars that
    close at 11:45 / 14:15 / 16:15 / 17:00. Bars whose close is still in the
    future relative to `now` are dropped. Adds a `slot` column (HH:MM).
    """
    if intraday.empty:
        return intraday
    df = intraday.copy()
    closes = [_bucket_close(ts) for ts in df.index]
    df = df[[c is not None for c in closes]]
    closes = [c for c in closes if c is not None]
    bar_end = [
        cfg.BKK.localize(datetime.combine(ts.date(), c))
        for ts, c in zip(df.index, closes)
    ]
    df["bar_end"] = bar_end
    out = df.groupby("bar_end").agg(
        Open=("Open", "first"), High=("High", "max"), Low=("Low", "min"),
        Close=("Close", "last"), Volume=("Volume", "sum"),
    )
    out = out[out.index <= now]
    out["slot"] = [ts.strftime("%H:%M") for ts in out.index]
    return out


def slot_normalised_rvol(bars: pd.DataFrame, lookback: int = 20) -> pd.Series:
    """Volume / mean volume of the same slot over the prior `lookback` sessions."""
    rvol = pd.Series(np.nan, index=bars.index)
    for slot, grp in bars.groupby("slot"):
        base = grp["Volume"].shift(1).rolling(lookback, min_periods=5).mean()
        rvol.loc[grp.index] = grp["Volume"] / base.replace(0, np.nan)
    return rvol


# ==========================================================================
# Gates
# ==========================================================================
def gate_macro_calendar(now: datetime) -> GateResult:
    d, t = now.date(), now.time()
    if d in cfg.BOT_MPC_DATES and cfg.BOT_MPC_BLACKOUT_START <= t <= cfg.BOT_MPC_BLACKOUT_END:
        return GateResult("Macro Calendar", False,
                          f"BOT MPC blackout {cfg.BOT_MPC_BLACKOUT_START:%H:%M}-"
                          f"{cfg.BOT_MPC_BLACKOUT_END:%H:%M} BKK")
    us_event = d in cfg.US_EVENT_DATES
    reason = ("US CPI/FOMC day: entry allowed, but flatten by "
              f"{cfg.EVENT_FLATTEN_BY:%H:%M} if Tranche 1 unbanked") if us_event else "No event blackout"
    return GateResult("Macro Calendar", True, reason, {"us_event_day": us_event})


def gate_intermarket(es_return_pct: Optional[float], usdthb_5d_pct: Optional[float]) -> GateResult:
    m = {"es_pct": es_return_pct, "usdthb_5d_pct": usdthb_5d_pct}
    if es_return_pct is None or usdthb_5d_pct is None:
        return GateResult("Intermarket", False, "Intermarket data unavailable", m)
    if es_return_pct <= cfg.ES_VETO_PCT:
        return GateResult("Intermarket", False,
                          f"ES Asian delta {es_return_pct:+.2f}% <= {cfg.ES_VETO_PCT:.2f}%", m)
    if usdthb_5d_pct > cfg.USDTHB_VETO_PCT:
        return GateResult("Intermarket", False,
                          f"USD/THB 5d {usdthb_5d_pct:+.2f}% > +{cfg.USDTHB_VETO_PCT:.2f}% (capital flight)", m)
    return GateResult("Intermarket", True,
                      f"ES {es_return_pct:+.2f}% | USD/THB 5d {usdthb_5d_pct:+.2f}%", m)


def gate_daily_regime(daily: pd.DataFrame) -> GateResult:
    if len(daily) < cfg.DAILY_SMA + 5:
        return GateResult("Daily Regime", False, f"Need {cfg.DAILY_SMA}+ daily bars, have {len(daily)}")
    close = daily["Close"]
    e20 = ema(close, cfg.DAILY_EMA)
    s200 = sma(close, cfg.DAILY_SMA)
    a14 = atr(daily, cfg.ATR_PERIOD)
    adx14 = adx(daily, cfg.ADX_PERIOD)

    c, e, s, a, x = (float(v.iloc[-1]) for v in (close, e20, s200, a14, adx14))
    slope = float(e20.iloc[-1] - e20.iloc[-1 - cfg.EMA_SLOPE_LOOKBACK])
    ext = (c - e) / a if a > 0 else float("inf")
    m = {"close": c, "ema20": e, "sma200": s, "daily_atr": a, "adx": x,
         "ema_slope": slope, "extension_atr": ext}

    if not (c > e and c > s):
        return GateResult("Daily Regime", False,
                          f"Close {c:.1f} not above EMA20 {e:.1f} and SMA200 {s:.1f}", m)
    if slope <= cfg.EMA_SLOPE_MIN_PTS:
        return GateResult("Daily Regime", False,
                          f"EMA20 3d slope {slope:+.2f} <= +{cfg.EMA_SLOPE_MIN_PTS} pts (chop)", m)
    if ext > cfg.EXTENSION_MAX_ATR:
        return GateResult("Daily Regime", False,
                          f"Extension {ext:.2f}x ATR > {cfg.EXTENSION_MAX_ATR}x (exhausted)", m)
    if x <= cfg.ADX_MIN:
        return GateResult("Daily Regime", False, f"ADX {x:.1f} <= {cfg.ADX_MIN}", m)
    return GateResult("Daily Regime", True,
                      f"Trend OK: slope {slope:+.2f}, ext {ext:.2f}x, ADX {x:.1f}", m)


def gate_breadth(changes: dict[str, Optional[float]]) -> GateResult:
    valid = {k: v for k, v in changes.items() if v is not None}
    adv = sum(1 for v in valid.values() if v > 0.0)
    m = {"advancers": adv, "total": len(changes), "changes": changes}
    detail = ", ".join(f"{k.replace('.BK', '')} {v:+.2f}%" for k, v in valid.items())
    if adv >= cfg.BREADTH_MIN_ADVANCERS:
        return GateResult("Mega-Cap Breadth", True, f"{adv}/{len(changes)} advancing ({detail})", m)
    return GateResult("Mega-Cap Breadth", False,
                      f"Only {adv}/{len(changes)} advancing, need {cfg.BREADTH_MIN_ADVANCERS} ({detail})", m)


def gate_wave_trigger(bars: pd.DataFrame, now: datetime) -> GateResult:
    if len(bars) < max(cfg.TWO_H_EMA, cfg.BB_PERIOD + cfg.BB_WIDTH_SMA) + 1:
        return GateResult("2H Wave Trigger", False, f"Not enough 2H bars ({len(bars)})")

    last_end = bars.index[-1]
    slot_ok = any(last_end.time() == s for s in cfg.ENTRY_SLOTS)
    if not slot_ok or last_end.date() != now.date():
        return GateResult("2H Wave Trigger", False,
                          f"Latest closed bar {last_end:%Y-%m-%d %H:%M} is not a "
                          "today 11:45/14:15 entry slot")

    e20 = ema(bars["Close"], cfg.TWO_H_EMA)
    vol_ma = sma(bars["Volume"], cfg.VOL_MA_PERIOD)
    rvol = slot_normalised_rvol(bars, cfg.VOL_MA_PERIOD)
    bw = bb_bandwidth(bars["Close"], cfg.BB_PERIOD, cfg.BB_STD)
    bw_sma = sma(bw, cfg.BB_WIDTH_SMA)
    atr2h = atr(bars, cfg.ATR_PERIOD)

    b = bars.iloc[-1]
    o, h, l, c, v = (float(b[k]) for k in ("Open", "High", "Low", "Close", "Volume"))
    ema_v = float(e20.iloc[-1])
    wick = min(o, c) - l
    m = {"open": o, "high": h, "low": l, "close": c, "ema20_2h": ema_v,
         "wick": wick, "volume": v, "vol_ma": float(vol_ma.iloc[-1]),
         "rvol": float(rvol.iloc[-1]) if pd.notna(rvol.iloc[-1]) else None,
         "bb_width": float(bw.iloc[-1]), "bb_width_sma": float(bw_sma.iloc[-1]),
         "atr_2h": float(atr2h.iloc[-1]), "slot": b["slot"]}

    if bars["Volume"].tail(cfg.VOL_MA_PERIOD).sum() <= 0:
        return GateResult("2H Wave Trigger", False, "Volume data unavailable from feed", m)
    if not l <= ema_v:
        return GateResult("2H Wave Trigger", False, f"No pullback: Low {l:.1f} > 2H EMA20 {ema_v:.1f}", m)
    if not c > ema_v:
        return GateResult("2H Wave Trigger", False, f"No reclaim: Close {c:.1f} <= 2H EMA20 {ema_v:.1f}", m)
    if not wick > cfg.WICK_MIN_PTS:
        return GateResult("2H Wave Trigger", False, f"Lower wick {wick:.2f} <= {cfg.WICK_MIN_PTS} pts", m)
    if not v > m["vol_ma"]:
        return GateResult("2H Wave Trigger", False, f"Volume {v:,.0f} <= 20-bar MA {m['vol_ma']:,.0f}", m)
    if m["rvol"] is None or m["rvol"] < cfg.RVOL_MIN:
        return GateResult("2H Wave Trigger", False, f"Slot RVol {m['rvol']} < {cfg.RVOL_MIN}", m)
    if not m["bb_width"] > m["bb_width_sma"]:
        return GateResult("2H Wave Trigger", False, "Bollinger bandwidth not expanding", m)
    return GateResult("2H Wave Trigger", True,
                      f"EMA20 test+reclaim, wick {wick:.2f}, RVol {m['rvol']:.2f}, BB expanding", m)


# ==========================================================================
# Data access
# ==========================================================================
class MarketFeed:
    """Thin yfinance wrapper. All timestamps are returned in Asia/Bangkok."""

    def __init__(self):
        import yfinance as yf  # imported lazily so gates are testable offline
        self.yf = yf

    def _hist(self, symbol: str, period: str, interval: str) -> pd.DataFrame:
        try:
            df = self.yf.Ticker(symbol).history(period=period, interval=interval, auto_adjust=False)
        except Exception as exc:  # network / symbol errors
            log.warning("yfinance %s %s/%s failed: %s", symbol, period, interval, exc)
            return pd.DataFrame()
        if df.empty:
            log.warning("yfinance returned no data for %s %s/%s", symbol, period, interval)
            return df
        df.index = df.index.tz_convert(cfg.BKK) if df.index.tz else df.index.tz_localize(cfg.BKK)
        return df[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])

    def daily(self) -> pd.DataFrame:
        return self._hist(cfg.S50_PROXY_SYMBOL, "2y", "1d")

    def two_hour(self, now: datetime) -> pd.DataFrame:
        return resample_2h(self._hist(cfg.S50_PROXY_SYMBOL, "60d", "15m"), now)

    def es_asian_return(self, now: datetime) -> Optional[float]:
        df = self._hist(cfg.ES_SYMBOL, "5d", "5m")
        if df.empty:
            return None
        anchor = cfg.BKK.localize(datetime.combine(now.date(), cfg.ES_SESSION_ANCHOR))
        before = df[df.index <= anchor]
        upto_now = df[df.index <= now]
        if before.empty or upto_now.empty:
            return None
        ref, last = float(before["Close"].iloc[-1]), float(upto_now["Close"].iloc[-1])
        return (last / ref - 1) * 100

    def usdthb_change(self) -> Optional[float]:
        df = self._hist(cfg.USDTHB_SYMBOL, "1mo", "1d")
        n = cfg.USDTHB_LOOKBACK_DAYS
        if len(df) < n + 1:
            return None
        return (float(df["Close"].iloc[-1]) / float(df["Close"].iloc[-1 - n]) - 1) * 100

    def breadth_changes(self, now: datetime) -> dict[str, Optional[float]]:
        out: dict[str, Optional[float]] = {}
        for sym in cfg.BREADTH_SYMBOLS:
            daily = self._hist(sym, "10d", "1d")
            intra = self._hist(sym, "2d", "5m")
            prior = daily[daily.index.date < now.date()]
            today = intra[(intra.index.date == now.date()) & (intra.index <= now)]
            if prior.empty or today.empty:
                out[sym] = None
                continue
            out[sym] = (float(today["Close"].iloc[-1]) / float(prior["Close"].iloc[-1]) - 1) * 100
        return out

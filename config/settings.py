"""
Code2Cash Engine v3 — central configuration.

Everything tunable lives here: secrets (read from the environment), TFEX S50
contract specs, gate thresholds, bracket parameters and the macro event
calendar. Any value can be overridden with an environment variable of the
same name where noted.
"""
from __future__ import annotations

import os
from datetime import date, time
from pathlib import Path

import pytz

# --------------------------------------------------------------------------
# Paths & timezone
# --------------------------------------------------------------------------
ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
STATE_DIR = ROOT_DIR / "state"
STATE_FILE = STATE_DIR / "engine_state.json"

BKK = pytz.timezone("Asia/Bangkok")


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw not in (None, "") else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------
# Secrets (GitHub Actions secrets / local env)
# --------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Send a short "stayed FLAT, here's why" message on every vetoed run.
NOTIFY_ON_VETO = _env_bool("NOTIFY_ON_VETO", True)

# --------------------------------------------------------------------------
# Account & contract specs
# --------------------------------------------------------------------------
ACCOUNT_EQUITY = _env_float("ACCOUNT_EQUITY", 1_000_000.0)  # THB
EQUITY_PER_CONTRACT = 100_000.0       # ฿1.0M -> 10 cts, ฿800k -> 8 cts
MAX_CONTRACTS = 10

CONTRACT_ROOT = "S50"
CONTRACT_MULTIPLIER = 200.0           # THB per index point
COMMISSION_RT = 80.0                  # THB round-turn per contract
TICK_SIZE = 0.1
INITIAL_MARGIN_PER_CONTRACT = 17_500.0  # approx; ~฿175k for 10 cts
ROLL_BUSINESS_DAYS_BEFORE_EXPIRY = 8  # roll window is 7–10 trading days

# --------------------------------------------------------------------------
# Data symbols (Yahoo Finance)
# --------------------------------------------------------------------------
# Yahoo has no TFEX futures feed, so the SET50 cash index is used as the
# price proxy for S50 signals. Override if you have a better source.
S50_PROXY_SYMBOL = os.getenv("S50_PROXY_SYMBOL", "^SET50.BK")
ES_SYMBOL = "ES=F"
USDTHB_SYMBOL = "THB=X"
BREADTH_SYMBOLS = ["DELTA.BK", "PTT.BK", "PTTEP.BK", "BDMS.BK", "CPALL.BK"]

# --------------------------------------------------------------------------
# Session schedule (Bangkok time)
# --------------------------------------------------------------------------
# 2-hour bar boundaries. A bar is labelled by its close time.
BAR_CLOSES = [time(11, 45), time(14, 15), time(16, 15), time(17, 0)]
ENTRY_SLOTS = [time(11, 45), time(14, 15)]   # 16:15 entries are banned
MANAGEMENT_SLOT = time(16, 20)               # pre-close lifecycle check
SLOT_TOLERANCE_MIN = 75                      # GH cron can start late
ES_SESSION_ANCHOR = time(9, 0)               # Asian-session reference

# --------------------------------------------------------------------------
# Gate 2 — intermarket
# --------------------------------------------------------------------------
ES_VETO_PCT = -0.60          # veto if ES intraday return <= this
USDTHB_VETO_PCT = 0.30       # veto if 5d USD/THB change > this
USDTHB_LOOKBACK_DAYS = 5

# --------------------------------------------------------------------------
# Gate 3 — daily regime
# --------------------------------------------------------------------------
DAILY_EMA = 20
DAILY_SMA = 200
EMA_SLOPE_LOOKBACK = 3
EMA_SLOPE_MIN_PTS = 0.80
EXTENSION_MAX_ATR = 2.5
ATR_PERIOD = 14
ADX_PERIOD = 14
ADX_MIN = 20.0

# --------------------------------------------------------------------------
# Gate 4 — mega-cap breadth
# --------------------------------------------------------------------------
BREADTH_MIN_ADVANCERS = 3

# --------------------------------------------------------------------------
# Gate 5 — 2H wave trigger
# --------------------------------------------------------------------------
TWO_H_EMA = 20
WICK_MIN_PTS = 0.20
VOL_MA_PERIOD = 20
RVOL_MIN = 1.15
BB_PERIOD = 20
BB_STD = 2.0
BB_WIDTH_SMA = 20

# --------------------------------------------------------------------------
# Sizing — ATR volatility matrix (daily ATR -> fraction of base size)
# --------------------------------------------------------------------------
ATR_SIZING_MATRIX = [
    (11.0, 1.0),          # Daily ATR <= 11.0  -> 10 cts on ฿1.0M
    (15.0, 0.7),          # 11.0 < ATR <= 15.0 ->  7 cts
    (float("inf"), 0.5),  # ATR > 15.0         ->  5 cts
]
BANK_FRACTION = 0.4       # Tranche 1 share of total (rounded up, max 4)
BANK_MAX_CONTRACTS = 4

# --------------------------------------------------------------------------
# Bracket parameters (points)
# --------------------------------------------------------------------------
ENTRY_BUFFER_PTS = 0.4
HARD_STOP_PTS = 4.5
T1_MIN_PTS = 5.0
T1_MAX_PTS = 8.0
T1_ATR_MULT = 1.2
T2A_TARGET_PTS = 12.0
CHANDELIER_ACTIVATION_PTS = 10.0
CHANDELIER_ATR_MULT = 1.5
STAGNATION_BARS = 6
WEEKEND_FLATTEN_BY = time(16, 45)
EVENT_FLATTEN_BY = time(16, 40)
MAX_RISK_PCT_EQUITY = 1.0

# --------------------------------------------------------------------------
# Circuit breakers
# --------------------------------------------------------------------------
DAILY_LOCKOUT_STOPS = 2
WEEKLY_LOCKOUT_STOPS = 3

# --------------------------------------------------------------------------
# Macro calendar (Bangkok trading dates)
# --------------------------------------------------------------------------
# IMPORTANT: verify these against the official calendars each year
#   BOT MPC: https://www.bot.or.th  (Monetary Policy -> MPC meeting schedule)
#   FOMC:    https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
#   US CPI:  https://www.bls.gov/schedule/news_release/cpi.htm
# Extra dates can be added at runtime without a code change via
# EXTRA_BOT_MPC_DATES / EXTRA_US_EVENT_DATES (comma-separated YYYY-MM-DD).

BOT_MPC_DATES: set[date] = {
    # TODO(verify): 2026 BOT MPC announcement dates
    date(2026, 2, 25),
    date(2026, 4, 29),
    date(2026, 6, 24),
    date(2026, 8, 19),
    date(2026, 10, 7),
    date(2026, 12, 16),
}
BOT_MPC_BLACKOUT_START = time(13, 0)
BOT_MPC_BLACKOUT_END = time(14, 15)

# US CPI / FOMC: the Bangkok session *before* the US release is the day we
# must not carry an unbanked position overnight. FOMC statements land at
# ~01:00–02:00 BKK the next morning, so the BKK date is the US meeting's
# final day. CPI prints at 19:30/20:30 BKK on the same date.
US_EVENT_DATES: set[date] = {
    # FOMC 2026 (decision days)
    date(2026, 1, 28), date(2026, 3, 18), date(2026, 4, 29),
    date(2026, 6, 17), date(2026, 7, 29), date(2026, 9, 16),
    date(2026, 10, 28), date(2026, 12, 9),
    # TODO(verify): US CPI 2026 release dates
    date(2026, 1, 13), date(2026, 2, 11), date(2026, 3, 11),
    date(2026, 4, 10), date(2026, 5, 12), date(2026, 6, 10),
    date(2026, 7, 14), date(2026, 8, 12), date(2026, 9, 11),
    date(2026, 10, 14), date(2026, 11, 12), date(2026, 12, 10),
}


def _parse_dates(raw: str | None) -> set[date]:
    out: set[date] = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if part:
            out.add(date.fromisoformat(part))
    return out


BOT_MPC_DATES |= _parse_dates(os.getenv("EXTRA_BOT_MPC_DATES"))
US_EVENT_DATES |= _parse_dates(os.getenv("EXTRA_US_EVENT_DATES"))

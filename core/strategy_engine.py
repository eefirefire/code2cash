"""
Strategy state machine, ATR sizing and the 3-tier bracket lifecycle.

Phase 1 executes orders manually from Telegram, so the engine cannot see real
fills. It "shadow-tracks" the position instead: every run it replays the new
2H bars against the bracket (stop first, then targets — conservative) and
sends management alerts (breakeven ratchet, chandelier trail, time/weekend/
event flattening). State persists in state/engine_state.json between runs.

States:  FLAT -> OPEN (Tranche 1 unbanked) -> BANKED (runners live) -> FLAT
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd

from config import settings as cfg

MONTH_CODES = {3: "H", 6: "M", 9: "U", 12: "Z"}


# ==========================================================================
# Helpers
# ==========================================================================
def round_tick(px: float) -> float:
    return round(round(px / cfg.TICK_SIZE) * cfg.TICK_SIZE, 1)


def _last_business_day(year: int, month: int) -> date:
    d = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1))
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _business_days_between(a: date, b: date) -> int:
    return max(0, len(pd.bdate_range(a, b)) - 1)


def front_contract(today: date) -> str:
    """Active quarterly S50 series, rolled N business days before expiry."""
    y, m = today.year, today.month
    for _ in range(5):
        qm = ((m - 1) // 3 + 1) * 3
        expiry = _last_business_day(y, qm)
        if today <= expiry and _business_days_between(today, expiry) > cfg.ROLL_BUSINESS_DAYS_BEFORE_EXPIRY:
            return f"{cfg.CONTRACT_ROOT}{MONTH_CODES[qm]}{y % 100:02d}"
        m = qm + 1
        if m > 12:
            m, y = 1, y + 1
    raise RuntimeError("could not resolve front contract")


# ==========================================================================
# Sizing
# ==========================================================================
@dataclass
class Sizing:
    total: int
    t1: int
    t2a: int
    t2b: int
    atr_factor: float


def size_position(equity: float, daily_atr: float) -> Sizing:
    base = min(cfg.MAX_CONTRACTS, int(equity // cfg.EQUITY_PER_CONTRACT))
    factor = next(f for cap, f in cfg.ATR_SIZING_MATRIX if daily_atr <= cap)
    total = max(1, round(base * factor))
    t1 = min(cfg.BANK_MAX_CONTRACTS, math.ceil(total * cfg.BANK_FRACTION), total)
    runners = total - t1
    t2a = runners // 2
    t2b = runners - t2a
    return Sizing(total, t1, t2a, t2b, factor)


# ==========================================================================
# Bracket plan
# ==========================================================================
@dataclass
class BracketPlan:
    contract: str
    ref_price: float
    entry_limit: float
    hard_stop: float
    t1_price: float
    t1_pts: float
    t2a_price: float
    chandelier_activation: float
    sizing: Sizing
    atr_2h: float
    daily_atr: float
    risk_thb: float
    t1_net_thb: float
    t2a_net_thb: float

    @property
    def risk_pct(self) -> float:
        return abs(self.risk_thb) / cfg.ACCOUNT_EQUITY * 100


def build_bracket(ref_price: float, atr_2h: float, daily_atr: float, today: date,
                  equity: float = cfg.ACCOUNT_EQUITY) -> BracketPlan:
    sz = size_position(equity, daily_atr)
    t1_pts = round_tick(min(cfg.T1_MAX_PTS, max(cfg.T1_MIN_PTS, cfg.T1_ATR_MULT * atr_2h)))
    mult, comm = cfg.CONTRACT_MULTIPLIER, cfg.COMMISSION_RT
    return BracketPlan(
        contract=front_contract(today),
        ref_price=round_tick(ref_price),
        entry_limit=round_tick(ref_price + cfg.ENTRY_BUFFER_PTS),
        hard_stop=round_tick(ref_price - cfg.HARD_STOP_PTS),
        t1_price=round_tick(ref_price + t1_pts),
        t1_pts=t1_pts,
        t2a_price=round_tick(ref_price + cfg.T2A_TARGET_PTS),
        chandelier_activation=round_tick(ref_price + cfg.CHANDELIER_ACTIVATION_PTS),
        sizing=sz,
        atr_2h=atr_2h,
        daily_atr=daily_atr,
        risk_thb=-(sz.total * cfg.HARD_STOP_PTS * mult + sz.total * comm),
        t1_net_thb=sz.t1 * t1_pts * mult - sz.t1 * comm,
        t2a_net_thb=sz.t2a * cfg.T2A_TARGET_PTS * mult - sz.t2a * comm,
    )


# ==========================================================================
# Persistent state
# ==========================================================================
@dataclass
class Position:
    contract: str
    entry: float
    entry_time: str
    stop: float
    t1_price: float
    t2a_price: float
    t1_qty: int
    t2a_qty: int
    t2b_qty: int
    atr_2h: float
    last_bar: str
    highest_high: float
    bars_held: int = 0
    t1_filled: bool = False
    t2a_filled: bool = False
    chandelier_active: bool = False
    realised_pts_x_qty: float = 0.0  # sum(points * contracts) of closed legs

    @property
    def open_qty(self) -> int:
        return ((0 if self.t1_filled else self.t1_qty)
                + (0 if self.t2a_filled else self.t2a_qty) + self.t2b_qty)


@dataclass
class EngineState:
    status: str = "FLAT"                       # FLAT | OPEN | BANKED
    position: Optional[Position] = None
    stopouts: list[str] = field(default_factory=list)   # ISO timestamps
    lockout_until: Optional[str] = None
    trade_log: list[dict] = field(default_factory=list)

    # ---- persistence ----
    @classmethod
    def load(cls) -> "EngineState":
        if not cfg.STATE_FILE.exists():
            return cls()
        raw = json.loads(cfg.STATE_FILE.read_text(encoding="utf-8"))
        pos = raw.get("position")
        raw["position"] = Position(**pos) if pos else None
        return cls(**raw)

    def save(self) -> None:
        cfg.STATE_DIR.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        data["trade_log"] = data["trade_log"][-200:]
        cfg.STATE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # ---- circuit breakers ----
    def locked_out(self, now: datetime) -> Optional[str]:
        if self.lockout_until and now < datetime.fromisoformat(self.lockout_until):
            return f"Circuit breaker lockout until {self.lockout_until[:16].replace('T', ' ')} BKK"
        return None

    def register_stopout(self, now: datetime) -> None:
        self.stopouts.append(now.isoformat())
        today = [s for s in self.stopouts if datetime.fromisoformat(s).date() == now.date()]
        week_start = now.date() - timedelta(days=now.weekday())
        week = [s for s in self.stopouts if datetime.fromisoformat(s).date() >= week_start]
        self.stopouts = week
        if len(week) >= cfg.WEEKLY_LOCKOUT_STOPS:
            monday = week_start + timedelta(days=7)
            self.lockout_until = cfg.BKK.localize(datetime(monday.year, monday.month, monday.day, 8, 30)).isoformat()
        elif len(today) >= cfg.DAILY_LOCKOUT_STOPS:
            nxt = now.date() + timedelta(days=1)
            self.lockout_until = cfg.BKK.localize(datetime(nxt.year, nxt.month, nxt.day, 8, 30)).isoformat()


# ==========================================================================
# Lifecycle
# ==========================================================================
@dataclass
class Event:
    kind: str        # ENTRY | T1_FILL | T2A_FILL | STOP | TRAIL | FLATTEN
    message: str
    price: Optional[float] = None


def open_position(state: EngineState, plan: BracketPlan, bar_end: pd.Timestamp) -> None:
    state.status = "OPEN"
    state.position = Position(
        contract=plan.contract, entry=plan.ref_price, entry_time=bar_end.isoformat(),
        stop=plan.hard_stop, t1_price=plan.t1_price, t2a_price=plan.t2a_price,
        t1_qty=plan.sizing.t1, t2a_qty=plan.sizing.t2a, t2b_qty=plan.sizing.t2b,
        atr_2h=plan.atr_2h, last_bar=bar_end.isoformat(), highest_high=plan.ref_price,
    )


def _close(state: EngineState, now: datetime, exit_px: float, reason: str) -> Event:
    p = state.position
    assert p is not None
    p.realised_pts_x_qty += (exit_px - p.entry) * p.open_qty
    qty_total = p.t1_qty + p.t2a_qty + p.t2b_qty
    net = p.realised_pts_x_qty * cfg.CONTRACT_MULTIPLIER - qty_total * cfg.COMMISSION_RT
    state.trade_log.append({
        "contract": p.contract, "entry": p.entry, "entry_time": p.entry_time,
        "exit_time": now.isoformat(), "exit_reason": reason,
        "t1_filled": p.t1_filled, "net_thb": round(net, 2),
    })
    state.status, state.position = "FLAT", None
    return Event("FLATTEN" if reason != "STOP" else "STOP",
                 f"{reason}: exit {p.open_qty} cts @ {exit_px:.1f} | trade net ฿{net:,.0f}", exit_px)


def update_position(state: EngineState, bars: pd.DataFrame, now: datetime) -> list[Event]:
    """Replay 2H bars closed since the last update against the bracket."""
    p = state.position
    if p is None:
        return []
    events: list[Event] = []
    new_bars = bars[bars.index > pd.Timestamp(p.last_bar)]
    atr_series = None
    if len(bars) > cfg.ATR_PERIOD:
        from core.market_feed import atr
        atr_series = atr(bars, cfg.ATR_PERIOD)

    for ts, b in new_bars.iterrows():
        p.bars_held += 1
        p.last_bar = ts.isoformat()
        if atr_series is not None:
            p.atr_2h = float(atr_series.loc[ts])

        # 1) Stop first (worst case inside the bar)
        if b["Low"] <= p.stop:
            was_be = p.t1_filled
            ev = _close(state, now, p.stop, "BREAKEVEN/TRAIL STOP" if was_be else "STOP")
            if not was_be:
                state.register_stopout(now)
            events.append(ev)
            return events

        # 2) Targets
        if not p.t1_filled and b["High"] >= p.t1_price:
            p.t1_filled = True
            p.realised_pts_x_qty += (p.t1_price - p.entry) * p.t1_qty
            p.stop = max(p.stop, p.entry)
            state.status = "BANKED"
            events.append(Event("T1_FILL",
                                f"Tranche 1 banked {p.t1_qty} cts @ {p.t1_price:.1f}. "
                                f"Ratchet stop on {p.t2a_qty + p.t2b_qty} runners to {p.entry:.1f} (breakeven)",
                                p.t1_price))
        if p.t1_filled and not p.t2a_filled and b["High"] >= p.t2a_price:
            p.t2a_filled = True
            p.realised_pts_x_qty += (p.t2a_price - p.entry) * p.t2a_qty
            events.append(Event("T2A_FILL", f"Tranche 2A filled {p.t2a_qty} cts @ {p.t2a_price:.1f}",
                                p.t2a_price))

        # 3) Chandelier trail for 2B
        p.highest_high = max(p.highest_high, float(b["High"]))
        if p.highest_high - p.entry >= cfg.CHANDELIER_ACTIVATION_PTS:
            new_stop = round_tick(p.highest_high - cfg.CHANDELIER_ATR_MULT * p.atr_2h)
            if new_stop > p.stop:
                first = not p.chandelier_active
                p.chandelier_active = True
                p.stop = new_stop
                events.append(Event("TRAIL",
                                    f"{'Chandelier ACTIVATED' if first else 'Chandelier raised'}: "
                                    f"move stop on {p.open_qty} open cts to {new_stop:.1f} "
                                    f"(HH {p.highest_high:.1f} - {cfg.CHANDELIER_ATR_MULT}xATR {p.atr_2h:.2f})",
                                    new_stop))
        if p.open_qty == 0:
            events.append(_close(state, now, float(b["Close"]), "ALL TRANCHES COMPLETE"))
            return events

    # 4) Time-based exits (only matter while Tranche 1 is unbanked)
    if state.position and not p.t1_filled:
        last_px = float(bars["Close"].iloc[-1])
        t = now.time()
        if p.bars_held >= cfg.STAGNATION_BARS and t >= cfg.MANAGEMENT_SLOT:
            events.append(_close(state, now, last_px, f"TIME-DECAY ({p.bars_held} bars, T1 unbanked)"))
        elif now.weekday() == 4 and t >= cfg.MANAGEMENT_SLOT:
            events.append(_close(state, now, last_px,
                                 f"FRIDAY WEEKEND RULE (flatten before {cfg.WEEKEND_FLATTEN_BY:%H:%M})"))
        elif now.date() in cfg.US_EVENT_DATES and t >= cfg.MANAGEMENT_SLOT:
            events.append(_close(state, now, last_px,
                                 f"US CPI/FOMC OVERNIGHT BAN (flatten by {cfg.EVENT_FLATTEN_BY:%H:%M})"))
    return events

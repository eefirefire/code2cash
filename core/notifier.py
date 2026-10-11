"""
Telegram alert formatting (HTML parse mode) and dispatch.

Order plans use Settrade Streaming order-ticket wording so they can be keyed
in directly.
"""
from __future__ import annotations

import html
import logging
from datetime import datetime
from typing import Iterable, Optional

import requests

from config import settings as cfg
from core.market_feed import GateResult
from core.strategy_engine import BracketPlan, EngineState, Event

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/sendMessage"
RULE = "----------------------------------------"


def esc(s: object) -> str:
    return html.escape(str(s), quote=False)


def px(v: float) -> str:
    return f"{v:,.1f}"


def thb(v: float) -> str:
    return f"{'-' if v < 0 else ''}฿{abs(v):,.0f}"


# ==========================================================================
# Formatters
# ==========================================================================
def format_entry(plan: BracketPlan, slot: str, gates: list[GateResult]) -> str:
    g = {r.name: r for r in gates}
    inter = g["Intermarket"].metrics
    breadth = g["Mega-Cap Breadth"].metrics
    sz = plan.sizing
    runners = sz.t2a + sz.t2b
    event_note = g["Macro Calendar"].metrics.get("us_event_day")

    lines = [
        f"🟢 <b>[Code2Cash v3 Alert] {esc(plan.contract)} LONG SWING</b>",
        f"• Session Slot: {esc(slot)} BKK (2-Hour Bar Close)",
        f"• Account Equity: {thb(cfg.ACCOUNT_EQUITY)} THB | Size: {sz.total} Contracts",
        f"• Ref Price: {px(plan.ref_price)} | Daily ATR: {plan.daily_atr:.1f} pts | 2H ATR: {plan.atr_2h:.2f}",
        f"• Intermarket: US ES: {inter['es_pct']:+.2f}% | USD/THB 5d: {inter['usdthb_5d_pct']:+.2f}%",
        f"• Breadth Gate: {breadth['advancers']}/{breadth['total']} Top Stocks Advancing ✅",
    ]
    if event_note:
        lines.append(f"⚠️ US CPI/FOMC day — flatten by {cfg.EVENT_FLATTEN_BY:%H:%M} if Tranche 1 unbanked")
    lines += [
        RULE,
        "<b>SETTRADE STREAMING ORDER PLAN:</b>",
        f"1. <b>BUY {sz.total} Contracts {esc(plan.contract)}</b>",
        f"   👉 Limit @ {px(plan.entry_limit)} (Ask + {cfg.ENTRY_BUFFER_PTS} buffer)",
        f"2. <b>CONDITIONAL HARD STOP</b>: Last &lt;= {px(plan.hard_stop)}",
        f"   👉 Close ALL {sz.total} MP (-{cfg.HARD_STOP_PTS} pts | Max risk {thb(plan.risk_thb)} "
        f"= {plan.risk_pct:.2f}% equity)",
        f"3. <b>TRANCHE 1 (Bank Target)</b>: Limit Sell {sz.t1} cts @ {px(plan.t1_price)} "
        f"(+{plan.t1_pts:.1f} pts, net {thb(plan.t1_net_thb)})",
        f"   👉 On fill: Ratchet all remaining {runners} runners to {px(plan.ref_price)} (Breakeven)",
    ]
    if sz.t2a:
        lines.append(f"4. <b>TRANCHE 2A (Fixed Runner)</b>: Limit Sell {sz.t2a} cts @ {px(plan.t2a_price)} "
                     f"(+{cfg.T2A_TARGET_PTS:.1f} pts, net {thb(plan.t2a_net_thb)})")
    lines += [
        f"5. <b>TRANCHE 2B (Uncapped Runner)</b>: {sz.t2b} cts trailed via ATR Chandelier "
        f"(Slice 1 ct / 30s)",
        f"   👉 Activates at {px(plan.chandelier_activation)} (+{cfg.CHANDELIER_ACTIVATION_PTS:.0f} pts): "
        f"Stop = HH - {cfg.CHANDELIER_ATR_MULT} x 2H ATR",
        RULE,
        f"⏱ Time-decay exit after {cfg.STAGNATION_BARS} 2H bars if T1 unbanked | "
        f"Friday: flatten before {cfg.WEEKEND_FLATTEN_BY:%H:%M} if T1 unbanked",
    ]
    return "\n".join(lines)


def format_veto(slot: str, now: datetime, gates: list[GateResult]) -> str:
    lines = [f"⚪ <b>[Code2Cash v3] FLAT — no entry</b>",
             f"• Slot: {esc(slot)} BKK | {now:%a %d %b %Y %H:%M}"]
    for r in gates:
        lines.append(f"{'✅' if r.passed else '❌'} {esc(r.name)}: {esc(r.reason)}")
    return "\n".join(lines)


def format_events(contract: str, events: Iterable[Event], state: EngineState) -> str:
    icon = {"T1_FILL": "💰", "T2A_FILL": "🎯", "TRAIL": "📈", "STOP": "🛑", "FLATTEN": "🔻"}
    lines = [f"🔔 <b>[Code2Cash v3] {esc(contract)} POSITION UPDATE</b>"]
    for ev in events:
        lines.append(f"{icon.get(ev.kind, '•')} {esc(ev.message)}")
    p = state.position
    if p:
        lines += [RULE,
                  f"Open: {p.open_qty} cts | Stop: {px(p.stop)} | HH: {px(p.highest_high)} | "
                  f"Bars held: {p.bars_held}"]
    else:
        lines += [RULE, "Status: FLAT ✅ — confirm all orders are cancelled in Streaming"]
    lines.append("<i>Shadow-tracked from 2H bars — reconcile with your actual fills.</i>")
    return "\n".join(lines)


def format_status(state: EngineState, slot: str, note: str) -> str:
    p = state.position
    if not p:
        return f"⚪ <b>[Code2Cash v3]</b> {esc(slot)} — {esc(note)}"
    return (f"🔵 <b>[Code2Cash v3] {esc(p.contract)} holding</b> ({esc(state.status)})\n"
            f"Open: {p.open_qty} cts @ {px(p.entry)} | Stop: {px(p.stop)} | "
            f"T1 {'✅' if p.t1_filled else px(p.t1_price)} | Bars held: {p.bars_held}\n{esc(note)}")


# ==========================================================================
# Dispatch
# ==========================================================================
def send(text: str, token: Optional[str] = None, chat_id: Optional[str] = None,
         dry_run: bool = False) -> bool:
    token = token or cfg.TELEGRAM_BOT_TOKEN
    chat_id = chat_id or cfg.TELEGRAM_CHAT_ID
    if dry_run or not token or not chat_id:
        if not dry_run:
            log.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — printing instead")
        print("\n" + text + "\n")
        return dry_run
    try:
        r = requests.post(API.format(token=token), timeout=20, json={
            "chat_id": chat_id, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })
    except requests.RequestException as exc:
        log.error("Telegram send failed: %s", exc)
        return False
    if not r.ok:
        log.error("Telegram API error %s: %s", r.status_code, r.text[:300])
    return r.ok

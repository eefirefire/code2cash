"""
Code2Cash Engine v3 — main entry point (invoked by GitHub Actions).

    python engine_worker.py                 # evaluate the current slot
    python engine_worker.py --dry-run       # print alerts, don't send or save state
    python engine_worker.py --mode manage   # lifecycle checks only, no new entries
    python engine_worker.py --reset-state   # wipe shadow position / lockouts
    python engine_worker.py --loop          # run locally on the BKK schedule
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta

from config import settings as cfg
from core import notifier
from core.market_feed import (GateResult, MarketFeed, atr, gate_breadth, gate_daily_regime,
                              gate_intermarket, gate_macro_calendar, gate_wave_trigger)
from core.strategy_engine import EngineState, build_bracket, open_position, update_position

log = logging.getLogger("engine")


def resolve_slot(now: datetime) -> str:
    """Name the scheduled slot this run belongs to (GH cron can start late)."""
    tol = timedelta(minutes=cfg.SLOT_TOLERANCE_MIN)
    for slot in [*cfg.ENTRY_SLOTS, cfg.MANAGEMENT_SLOT]:
        start = cfg.BKK.localize(datetime.combine(now.date(), slot))
        if start <= now <= start + tol:
            return slot.strftime("%H:%M")
    return "manual"


def evaluate_entry(feed: MarketFeed, now, bars, daily) -> tuple[list[GateResult], bool]:
    """Run the five gates in order; stop at the first veto."""
    results: list[GateResult] = []
    checks = [
        lambda: gate_macro_calendar(now),
        lambda: gate_intermarket(feed.es_asian_return(now), feed.usdthb_change()),
        lambda: gate_daily_regime(daily),
        lambda: gate_breadth(feed.breadth_changes(now)),
        lambda: gate_wave_trigger(bars, now),
    ]
    for check in checks:
        r = check()
        results.append(r)
        log.info("[%s] %s — %s", "PASS" if r.passed else "VETO", r.name, r.reason)
        if not r.passed:
            return results, False
    return results, True


def run_once(mode: str = "auto", dry_run: bool = False, now: datetime | None = None) -> int:
    now = now or datetime.now(cfg.BKK)
    slot = resolve_slot(now)
    log.info("Run at %s BKK | slot=%s | mode=%s | dry_run=%s", now.strftime("%Y-%m-%d %H:%M"),
             slot, mode, dry_run)

    if now.weekday() >= 5:
        log.info("Weekend — nothing to do.")
        return 0

    state = EngineState.load()
    feed = MarketFeed()
    bars = feed.two_hour(now)
    if bars.empty or bars.index[-1].date() != now.date():
        log.info("No 2H bars for today — market holiday or feed unavailable. Staying idle.")
        return 0

    def send(text: str) -> None:
        notifier.send(text, dry_run=dry_run)

    # ---- 1) manage an open shadow position ----
    if state.position:
        contract = state.position.contract
        events = update_position(state, bars, now)
        for ev in events:
            log.info("EVENT %s: %s", ev.kind, ev.message)
        if events:
            send(notifier.format_events(contract, events, state))
        elif slot == cfg.MANAGEMENT_SLOT.strftime("%H:%M"):
            send(notifier.format_status(state, slot, "Pre-close check: no action required."))

    # ---- 2) look for a new entry ----
    entry_slots = {s.strftime("%H:%M") for s in cfg.ENTRY_SLOTS}
    may_enter = mode == "auto" and (slot in entry_slots or slot == "manual")
    if may_enter and state.status == "FLAT":
        lock = state.locked_out(now)
        if lock:
            log.info(lock)
            if cfg.NOTIFY_ON_VETO:
                send(notifier.format_status(state, slot, lock))
        else:
            daily = feed.daily()
            gates, ok = evaluate_entry(feed, now, bars, daily)
            if ok:
                daily_atr = gates[2].metrics["daily_atr"]
                atr_2h = float(atr(bars, cfg.ATR_PERIOD).iloc[-1])
                ref = float(bars["Close"].iloc[-1])
                plan = build_bracket(ref, atr_2h, daily_atr, now.date())
                if plan.risk_pct > cfg.MAX_RISK_PCT_EQUITY:
                    log.warning("Risk %.2f%% exceeds cap — skipping", plan.risk_pct)
                    send(notifier.format_status(state, slot,
                                                f"Signal skipped: risk {plan.risk_pct:.2f}% > cap"))
                else:
                    open_position(state, plan, bars.index[-1])
                    send(notifier.format_entry(plan, bars["slot"].iloc[-1], gates))
            elif cfg.NOTIFY_ON_VETO:
                send(notifier.format_veto(slot, now, gates))

    if dry_run:
        log.info("Dry run — state not saved.")
    else:
        state.save()
    return 0


def run_loop(mode: str, dry_run: bool) -> None:
    import time as _time
    import schedule

    for slot in [*cfg.ENTRY_SLOTS, cfg.MANAGEMENT_SLOT]:
        at = (datetime.combine(datetime.today(), slot) + timedelta(minutes=2)).strftime("%H:%M")
        for day in ("monday", "tuesday", "wednesday", "thursday", "friday"):
            getattr(schedule.every(), day).at(at, "Asia/Bangkok").do(run_once, mode=mode, dry_run=dry_run)
    log.info("Local scheduler started (%d jobs). Ctrl+C to stop.", len(schedule.get_jobs()))
    while True:
        schedule.run_pending()
        _time.sleep(20)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Code2Cash Engine v3 — TFEX S50 swing runner")
    ap.add_argument("--mode", choices=["auto", "manage"], default="auto",
                    help="auto = manage + new entries; manage = lifecycle only")
    ap.add_argument("--dry-run", action="store_true", help="print alerts, don't send or save state")
    ap.add_argument("--reset-state", action="store_true", help="clear shadow position and lockouts")
    ap.add_argument("--loop", action="store_true", help="run continuously on the BKK schedule")
    ap.add_argument("--now", help="override clock, e.g. '2026-10-09 11:50' (BKK) for testing")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.reset_state:
        EngineState().save()
        log.info("State reset to FLAT.")
        return 0
    if args.loop:
        run_loop(args.mode, args.dry_run)
        return 0
    now = cfg.BKK.localize(datetime.strptime(args.now, "%Y-%m-%d %H:%M")) if args.now else None
    return run_once(args.mode, args.dry_run, now)


if __name__ == "__main__":
    sys.exit(main())

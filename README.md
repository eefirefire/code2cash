# Code2Cash Engine v3 — TFEX S50 Institutional Swing System

Automated signal engine for **TFEX SET50 Index Futures (S50)**. It runs a
**Long-Only** trend-following swing strategy on a **฿1,000,000** baseline
(10-contract footprint), evaluates 2-hour bars twice a day on GitHub Actions,
and sends step-by-step **Settrade Streaming** order plans to Telegram.

| Target | Value |
|---|---|
| Net annualised ROE | ~39% |
| Max peak-equity drawdown | < 1.75% |
| Win + scratch rate | > 83% |

> These are design targets from the spec, not backtested results from this code.

## Repository layout

```
.github/workflows/tfex_runner.yml   GitHub Actions cron (11:45, 14:15, 16:20 BKK) + manual dispatch
config/settings.py                  Secrets, contract specs, gate thresholds, macro calendar
core/market_feed.py                 yfinance ingestion, indicators, the 5 filter gates
core/strategy_engine.py             State machine, ATR sizing, 3-tier bracket lifecycle
core/notifier.py                    Telegram HTML formatter + dispatcher
data/system_architecture_rules_v3.csv      Rulebook (v3)
data/trade_parameters_calculator_v3.csv    Bracket calculator, generated from the engine
state/engine_state.json             Shadow position, lockouts, trade log (committed by the runner)
engine_worker.py                    CLI entry point
test_telegram.py                    Bot connectivity check
```

## Signal pipeline

Gates are checked in order. The first one that fails vetoes the entry and the
reason is logged (and sent to Telegram if `NOTIFY_ON_VETO=true`).

1. **Macro calendar.** Blocks entries 13:00–14:15 BKK on BOT MPC dates. On US
   CPI/FOMC days it allows entries but flattens at the 16:20 check if
   Tranche 1 hasn't filled.
2. **Intermarket.** Vetoes when ES=F since 09:00 BKK is ≤ −0.60%, or when
   USD/THB (THB=X) has risen more than +0.30% over 5 days.
3. **Daily regime.** Needs Close > EMA20 and Close > SMA200, a 3-day EMA20
   slope above +0.8 pts, (Close − EMA20) ≤ 2.5 × ATR14, and ADX14 > 20.
4. **Mega-cap breadth.** At least 3 of DELTA, PTT, PTTEP, BDMS and CPALL must
   be up on the session.
5. **2H wave trigger.** Runs only on the 11:45 or 14:15 bar close. Needs
   Low ≤ EMA20 < Close, a lower wick > 0.20 pts, Volume > its 20-bar MA, a
   slot-normalised RVol ≥ 1.15, and BB(20,2) bandwidth above its 20-bar SMA.

## Bracket (฿1.0M, daily ATR ≤ 11)

| Leg | Size | Rule |
|---|---|---|
| Hard stop | 10 | Entry − 4.5 pts → max risk −฿9,800 net (0.98%) |
| Tranche 1 (bank) | 4 | Entry + clamp(1.2 × 2H ATR, 5.0, 8.0). On fill, all runners move to breakeven |
| Tranche 2A (fixed) | 3 | Entry + 12.0 pts → +฿6,960 net |
| Tranche 2B (runner) | 3 | From +10 pts, chandelier stop = HH − 1.5 × 2H ATR |
| Time decay | all | Flatten after ≥ 6 2H bars if T1 is still unbanked |
| Friday rule | all | Flatten before 16:45 Friday if T1 is still unbanked |

The ATR sizing matrix scales size down to 7 contracts when daily ATR is between
11 and 15, and to 5 contracts above 15. Circuit breakers: 2 stop-outs in a day
lock entries until 08:30 the next day; 3 in a week lock them until Monday 08:30.
The contract (e.g. `S50Z26`) rolls to the next quarterly series 8 business days
before expiry.

### Shadow tracking

Phase 1 orders are placed by hand, so the engine can't see real fills. Each run
replays new 2H bars against the bracket instead. It checks the stop first
(conservative), then the targets, then the chandelier trail. It sends
ratchet/trail/flatten instructions, then the workflow commits the updated
`state/engine_state.json`. If your actual fills differ, reset the state from
the manual dispatch (`reset_state: true`).

## Setup

1. **Create the Telegram bot.** Talk to [@BotFather](https://t.me/BotFather) and
   run `/newbot`. Send your bot a message, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` to read your `chat.id`.
2. **Test it locally:**
   ```bash
   pip install -r requirements.txt
   TELEGRAM_BOT_TOKEN=xxx TELEGRAM_CHAT_ID=yyy python test_telegram.py
   ```
3. **Add GitHub secrets.** In Settings → Secrets and variables → Actions, add
   `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
4. **Enable Actions.** Run **TFEX S50 Runner** once via *Run workflow* with
   `dry_run: true` and check the log.

### Schedule (Mon–Fri)

| UTC | BKK | Purpose |
|---|---|---|
| 04:45 | 11:45 | 2H close: manage the position and evaluate an entry |
| 07:15 | 14:15 | 2H close: manage the position and evaluate an entry |
| 09:20 | 16:20 | Pre-close lifecycle check: time-decay, Friday and CPI/FOMC flattening (no entries) |

The 16:20 run is an addition to the spec's two runs. Without it, nothing could
enforce the 16:40 and 16:45 flatten deadlines. GitHub cron can start 5–20
minutes late. Each run still works out which slot it belongs to (75-minute
tolerance).

### CLI

```bash
python engine_worker.py                      # evaluate the current slot
python engine_worker.py --dry-run            # print alerts; don't send or save state
python engine_worker.py --mode manage        # lifecycle only, no new entries
python engine_worker.py --now "2026-10-09 11:50" --dry-run   # replay a past slot
python engine_worker.py --reset-state        # back to FLAT, clear lockouts
python engine_worker.py --loop               # run locally on the BKK schedule
```

### Configuration (env vars)

| Variable | Default | Purpose |
|---|---|---|
| `ACCOUNT_EQUITY` | `1000000` | Sizing base (฿100k per contract, max 10) |
| `S50_PROXY_SYMBOL` | `^SET50.BK` | Yahoo symbol used as the S50 price proxy |
| `NOTIFY_ON_VETO` | `true` | Send a "stayed FLAT" message with the veto reason |
| `EXTRA_BOT_MPC_DATES` | – | Comma-separated `YYYY-MM-DD` to add MPC dates |
| `EXTRA_US_EVENT_DATES` | – | Comma-separated `YYYY-MM-DD` to add CPI/FOMC dates |

## Known limitations

- **Price proxy.** Yahoo Finance has no TFEX futures, so signals come from the
  SET50 cash index. Futures trade at a basis to it, so check the live S50
  quote before you send the limit order. Yahoo's intraday Thai data can also
  lag about 15 minutes.
- **Volume.** If the feed reports zero index volume, Gate 5 vetoes rather than
  guesses.
- **Macro calendar.** The 2026 BOT MPC and US CPI dates in
  `config/settings.py` are marked `TODO(verify)`. Confirm them against
  bot.or.th and bls.gov. FOMC dates come from the Fed's published 2026
  calendar.
- **Rollover.** Rolls happen on the calendar rule only. The "next-series
  volume ≥ 70%" check needs a TFEX feed.

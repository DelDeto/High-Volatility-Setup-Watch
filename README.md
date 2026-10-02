# Market Opportunity Scanner — Freqtrade migration

This branch rebuilds the former High Volatility Setup Watch on top of the official Freqtrade stable Docker image.

## Objective

Scan a broad **Gate USDT perpetual crypto** universe and keep only setups with:

- strong multi-timeframe structure (4H / 1H / 15M)
- entry close to a meaningful structural zone
- structural stop and target
- attractive risk/reward
- volume confirmation
- volatility-aware entry requirements
- long and short support

High volatility is now a **regime**, not the first filter. This avoids excluding good normal-volatility pullbacks and avoids chasing extreme moves.

## Why Gate

The original migration targeted Binance Futures, but GitHub-hosted runners received HTTP 451 from Binance because of geo-IP restrictions. Gate futures was then tested from the same GitHub Actions environment and returned the USDT perpetual market successfully.

The live pairlist starts from all active Gate USDT perpetuals, then removes the five non-crypto `contract_type` classes observed from Gate metadata: `stocks`, `indices`, `commodities`, `forex`, and `metals`. The connectivity inspection found 584 crypto perpetual markets with an empty classification, so the volume ranking is applied only after the TradFi contracts are removed.

## Safety model

- Default mode: **dry-run**
- Default leverage: 1x
- No API key is required for public market-data scanning in dry-run.
- Live trading should not be enabled until the strategy has passed backtests, lookahead checks, dry-run, and outcome review.

The old MEXC implementation is preserved in branch `legacy-high-vol-v1`.

## Architecture

```text
Gate USDT perpetual futures
        ↓
All active USDT perpetual crypto markets
        ↓
Top 150 by quote volume
        ↓
FAST SCAN: 1H on all 150
        ↓
Top 40 liquidity guaranteed
+ highest setup-potential pairs
        ↓
DEEP SCAN: 80 pairs
15M + 1H + 4H
        ↓
V2 setup engine
Trend pullback / Breakout-retest
Sweep reversal / High-vol continuation
        ↓
Structural Entry / SL / TP
        ↓
READY / DEVELOPING
        ↓
Cross-market Opportunity Value
        ↓
TOP 10
        ↓
Telegram + JSON/Markdown snapshot
```

V3.5 is the active selection/reporting layer over the V2 structural engine. The V2 rules are frozen in branch `v2-baseline` and also copied to `MarketOpportunityStrategyV2.py` for direct comparison.

## Opportunity score

V2 uses the same 100-point framework, but setup confirmation is stricter and structural risk is now used by the actual Freqtrade stoploss:

- 20: 4H structure
- 10: 1H structure
- 20: location quality
- 20: entry confirmation
- 10: volume participation
- 15: structural RR
- 5: entry proximity

Thresholds become stricter as volatility rises.

| Regime | Approx. 24H range | Min score | Min RR |
|---|---:|---:|---:|
| NORMAL | < 8% | 80 | 2.0R |
| HIGH | 8–15% | 82 | 2.2R |
| VERY_HIGH | 15–25% | 85 | 2.5R |
| EXTREME | >= 25% | 90 | 3.0R |

## Setup types

- `trend_pullback`
- `breakout_retest`
- `sweep_reversal`
- `high_vol_continuation`

A pair may qualify through any one of these paths. RR is calculated only after structure, location and invalidation are defined.

### V2 changes

- naked liquidity sweeps no longer trigger reversal entries;
- sweep reversal requires a recent sweep plus 15m micro-structure CHoCH;
- reversals cannot directly fight a fully aligned opposing 4H trend;
- short reversal requires a higher score and higher minimum RR than V1;
- trend pullback can use either 1H demand/supply or the 1H EMA20 pullback location;
- high-volatility continuation requires a recent breakout and retest proximity;
- structural invalidation now includes a minimum 15m ATR noise buffer;
- Freqtrade `custom_stoploss()` now uses the same structural stop model used by the RR engine.

This fixes the main V1 mismatch where RR was calculated against a structural stop but the backtest was still using a fixed 8% emergency stop.


## V3 cross-market ranking

V3 does **not** change the V2 entry rules yet. It ranks the outputs across the market so a high score on one pair is compared against every other candidate at the same scan.

For each side:

```text
opportunity_value
= score
+ 4 × min(RR, 5)
- 5 × min(entry_distance_ATR, 2)
```

Ranking priority is:

1. `READY` before `DEVELOPING`;
2. higher `opportunity_value`;
3. higher RR;
4. higher setup score;
5. better liquidity rank.

`DEVELOPING` means the setup exists and is near the V2 gate, but is not yet allowed to become an entry:

- score can be at most 8 points below the required score;
- RR must be at least 80% of the regime-specific required RR;
- location quality must already be valid.

If both LONG and SHORT qualify on the same symbol, V3 suppresses the pair when the two directions are too close in opportunity value. If one direction is clearly stronger, only that side is kept.

### Two-stage universe

To keep GitHub Actions practical without reducing the market to only a handful of coins:

- all eligible Gate USDT perpetuals are discovered;
- top 150 by quote volume enter the fast 1H scan;
- top 40 by liquidity are always retained for deep scan;
- the rest of the 80-pair deep scan is filled by 1H setup-potential ranking;
- 15m + 1H + 4H analysis is then applied to those 80 pairs.

This keeps broad-market discovery while controlling API load.

### V3 outputs

Every scan writes:

- `user_data/v3_output/latest.json`: full machine-readable snapshot;
- `user_data/v3_output/latest.md`: human-readable Top-N report;
- Telegram: READY setups plus clearly labeled DEVELOPING watchlist items when Telegram secrets are configured.

Each ranked setup includes:

```text
rank
coin
LONG / SHORT
READY / DEVELOPING
setup type
entry
structural SL
structural TP
RR
score
volatility regime
24H range
entry distance in ATR
opportunity value
```

No real orders are placed by the V3 scanner.


## V3.1 Outcome Tracker

V3.1 adds deterministic forward-outcome tracking. It is intentionally separate from the signal engine so the original V2/V3 setup rules remain measurable.

### What counts as one setup?

A setup is identified by:

```text
symbol + direction + setup type
```

while that setup is still open/being monitored. Repeated scanner hits do **not** create new records. After a terminal outcome, the same symbol/direction/setup is suppressed for a 12-hour cooldown before it can become a new unique setup.

### State machine

```text
DEVELOPING
   ↓
MONITORING
   ↓
READY
   ↓
PENDING_ENTRY
   ↓
ACTIVE
   ↓
TP / SL / TIMEOUT / AMBIGUOUS
```

Rules:

- DEVELOPING is tracked but is not counted as a completed trade outcome.
- When a setup first becomes READY, Entry / SL / TP / RR are frozen for that setup.
- Entry must be touched within 6 hours or the setup becomes EXPIRED.
- Once active, the tracker follows 15m closed candles for up to 168 hours.
- TP = planned RR result.
- SL = -1R.
- TIMEOUT records mark-to-market R after the maximum holding window.
- If entry and an exit level, or TP and SL, are touched within the same 15m candle and ordering cannot be known, the result is AMBIGUOUS rather than guessed.

### Metrics stored automatically

Each READY setup can accumulate:

```text
entry_time
MFE_R
MAE_R
TP / SL / TIMEOUT
Actual_R
hold_hours
setup type
LONG / SHORT
volatility regime
score
initial RR
entry distance
```

The summary aggregates:

- unique setups;
- READY unique setups;
- completed outcomes;
- TP / SL counts;
- win rate on TP/SL outcomes;
- average R;
- profit factor in R;
- results by setup type;
- results by side;
- results by volatility regime.

Persistent files:

- `user_data/v3_state/outcomes.json`
- `user_data/v3_state/outcome_summary.json`
- `user_data/v3_state/delivery.json`
- `user_data/v3_state/scan_state.json`

Production runs restore and persist these runtime files on the dedicated `v3-state` branch. Keeping mutable state off `main` prevents code merges, reruns from older SHAs, and state commits from creating rebase conflicts. The `v3-state` branch is not a scanner trigger.

### Telegram deduplication

Telegram is now driven by the outcome tracker, not directly by every scanner snapshot. V3.2 also attaches 4H + 1H charts to new READY or promoted-to-READY signals.

It sends only meaningful events such as:

- a new unique READY/DEVELOPING setup;
- DEVELOPING promoted to READY;
- a terminal TP / SL / TIMEOUT / AMBIGUOUS outcome.

A setup that remains unchanged across multiple 15-minute scans is not re-alerted.


## V3.2 Visual Signal Pack

V3.2 adds automatic visual context for **READY** setups without changing any entry rule.

For each new READY setup, or a DEVELOPING setup promoted to READY, the system generates:

```text
4H chart = market context
1H chart = setup / entry context
```

Both charts include:

- closed Gate candlesticks;
- EMA20 / EMA50 / EMA200;
- latest supply / demand reference;
- frozen Entry / SL / TP;
- setup name, side, RR, score, volatility regime and `signal_id`;
- expected direction arrow.

Telegram behavior for a READY setup becomes:

```text
READY text alert
        ↓
4H CONTEXT image
        ↓
1H SETUP / ENTRY image
```

DEVELOPING setups continue to receive text-only alerts. This keeps Telegram lighter while making READY signals easy to inspect visually.

Charts are not committed into Git history. They are uploaded with the workflow artifact under `user_data/v3_charts/<signal_id>_.../` and retained for 90 days. The corresponding outcome record stores the generated chart paths and timestamp so charts remain linked to the setup ID used by the Outcome Tracker.

This visual layer is descriptive only. It does not approve, reject or modify a trade signal.


## V3.3 Self-healing Watchdog

V3.3 adds a second automation layer whose only job is to make the production scanner recover from missed or failed GitHub schedules.

Primary scanner:

```text
:07 / :22 / :37 / :52
        ↓
V3.2 market scan
        ↓
Outcome Tracker
        ↓
Telegram
```

Watchdog:

```text
:00 / :15 / :30 / :45
        ↓
Inspect latest production schedule/workflow_dispatch run
        ↓
Healthy and <=20 min old?
   YES → do nothing
   NO  → Telegram watchdog alert
          ↓
        workflow_dispatch replacement V3.2 scan
          ↓
        verify completion for up to 9 minutes
          ↓
        Telegram RECOVERED / FAILED / TIMEOUT
```

A queued or in-progress production scan younger than 20 minutes is treated as healthy, so a merely delayed GitHub runner is not unnecessarily duplicated.

The watchdog is deliberately staggered from the primary scanner. If one expected 15-minute primary trigger is missed, the next watchdog check can detect that the most recent production run has become stale and start a replacement scan.

This layer also distinguishes CI/push runs from real production runs: only `schedule` and `workflow_dispatch` runs count as scanner heartbeat.

The watchdog cannot make GitHub's infrastructure mathematically infallible. If GitHub Actions itself is unavailable for both workflows, neither can run. Within GitHub Actions, however, V3.3 converts a single missed primary cron from silent failure into an automatically detected and retried event, with Telegram visibility.


## V3.4 Free Redundancy

V3.4 adds reliability without changing the trading strategy.

### 1. Missed-slot catch-up

Every successful production cycle writes a canonical 15-minute heartbeat slot. Before the next production scan the workflow compares the current slot with the last completed slot.

If one or more slots were missed:

```text
last completed: 09:00
current slot:   09:45
        ↓
catch-up 09:15
catch-up 09:30
        ↓
normal 09:45 scan
```

Catch-up scans use the historical closed-candle cutoff (`--as-of`) rather than current candles, so a recovered signal is reconstructed from the information that existed at that missed slot. Up to 8 missed slots (2 hours) are replayed automatically. Older missed slots are explicitly reported as truncated rather than silently treated as recovered.

### 2. Durable Telegram delivery

Telegram notifications now use a persistent at-least-once delivery ledger:

```text
event detected
   ↓
PENDING saved to delivery.json
   ↓
Telegram send
   ├─ confirmed → SENT
   └─ timeout/error → remains PENDING
                         ↓
                    next cycle retries
```

This prevents a transient Telegram failure from becoming a permanently missed alert after signal deduplication. A process crash can theoretically cause a duplicate alert if Telegram accepted the message immediately before state persistence; V3.4 intentionally prefers a rare duplicate over silently missing a signal.

### 3. Stronger GitHub watchdog

The V3.4 watchdog checks three independent health indicators:

- recent production workflow status;
- scanner heartbeat age from the dedicated `v3-state` branch;
- age of the oldest pending Telegram delivery from `v3-state`.

If state is stale it dispatches a replacement production scan. That replacement runs the same catch-up planner and flushes pending Telegram notifications.

### 4. Independent Supabase watchdog

GitHub also syncs each successful heartbeat to Supabase when the repository secrets `SUPABASE_PROJECT_URL` and `SUPABASE_SECRET_KEY` are configured.

Supabase Cron can invoke `supabase/functions/scanner-watchdog` every 10 minutes. Because this schedule runs outside GitHub Actions, it can notify Telegram when GitHub itself stops scheduling both the primary scanner and its GitHub-hosted watchdog.

External watchdog files:

- `supabase/migrations/202610020001_v34_watchdog.sql`
- `supabase/functions/scanner-watchdog/index.ts`
- `supabase/V34_SETUP.md`

The external watchdog does not make trading decisions. When GitHub returns, the missed-slot catch-up mechanism reconstructs recent missed scans and the delivery ledger retries unsent alerts.

## V3.5 Quality Calibration

V3.5 was built after auditing the PONS legacy loss and A/B testing the first
quality-gate proposal against the 90-day V2 baseline. The first experiment
proved that globally vetoing nearby 15m obstacles / generic ATR contraction
removed too many profitable trend pullbacks, so those cutoffs were **not**
promoted to production entry rules.

### Active V3.5 rules

- **4H opposition veto:** a setup cannot become actionable while a fully
  aligned 4H structure points in the opposite direction.
- **High-vol continuation expansion:** `high_vol_continuation` requires 15m ATR
  expansion >= **1.05x** its 96-candle median, in addition to breakout,
  structure, volume and proximity conditions.
- **Obstacle-to-target is telemetry, not a global veto:** the scanner records
  the nearest prior 15m swing clearance in R, but does not reject ordinary
  trend pullbacks solely because that swing is nearby.
- **Generic ATR expansion is telemetry:** normal trend pullbacks are not
  rejected solely because short-term ATR is contracting.

### Telegram semantics

`DEVELOPING` is explicitly rendered as **WATCHLIST ONLY**. Entry / SL / TP are
shown only after a setup becomes `READY`. This directly prevents the PONS-type
failure mode where a near-threshold setup can look like an executable trade.

### Calibration telemetry

Every ranked setup and frozen READY outcome stores:

- `atr_expansion`
- `obstacle_clearance_r`
- `quality_gates`

The outcome summary segments results by ATR-expansion and obstacle-clearance
buckets. These features can therefore be promoted into future entry gates only
after forward/out-of-sample evidence supports a cutoff.

### 90-day A/B safeguard

The first V3.5 hard-gate experiment reduced the research sample from 85 to 30
trades and degraded profit factor, so it was rejected. The revised calibration
version restores the 85-trade sample while retaining the PONS safety/telemetry
changes. This is intentionally conservative: V3.5 should not become "better"
merely by deleting trades from the same calibration window.

## Quick start

Requirements: Docker + Docker Compose.

```bash
cp .env.example .env
docker compose pull

# Validate the strategy
docker compose run --rm freqtrade list-strategies \
  --strategy-path /freqtrade/user_data/strategies

# Inspect the current dynamic Gate crypto futures universe
docker compose run --rm freqtrade test-pairlist \
  --config /freqtrade/user_data/config.json

# Test the V3 ranking logic without network access
docker compose run --rm --entrypoint python freqtrade \
  /freqtrade/user_data/v3/market_ranker.py --self-test

# Run a live V3 snapshot without Telegram
docker compose run --rm --entrypoint python freqtrade \
  /freqtrade/user_data/v3/market_ranker.py \
  --universe 150 --deep-limit 80 --top-n 10

# Start continuous Freqtrade V2 dry-run
docker compose up -d

# Follow logs
docker compose logs -f
```

## Telegram

The scheduled V3 GitHub Action uses repository secrets `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. It sends a message only when at least one READY/DEVELOPING opportunity exists.

Freqtrade itself also supports environment-variable overrides. In `.env`:

```bash
FREQTRADE__TELEGRAM__ENABLED=true
FREQTRADE__TELEGRAM__TOKEN=...
FREQTRADE__TELEGRAM__CHAT_ID=...
```

Keep `.env` out of Git.

## Backtesting

For reproducibility, `config-backtest.json` uses a static seed pairlist instead of the live VolumePairList.

```bash
docker compose run --rm freqtrade download-data \
  --config /freqtrade/user_data/config-backtest.json \
  --timeframes 15m 1h 4h --days 90

docker compose run --rm freqtrade backtesting \
  --config /freqtrade/user_data/config-backtest.json \
  --strategy MarketOpportunityStrategy
```

A manual GitHub Actions backtest workflow is included. The branch also contains `Research Backtest V2 - 90d`, which runs a 90-day study on the static 20-pair research universe whenever the strategy or backtest config changes.

## Validation path

1. V2 baseline frozen;
2. V3 ranker self-test in CI;
3. scheduled Gate Top-150 → Top-80 → Top-10 snapshot;
4. compare READY/DEVELOPING forward outcomes;
5. collect at least 100–200 ranked setups;
6. calibrate by setup × side × volatility regime × RR bucket × entry distance;
7. run out-of-sample / walk-forward checks;
8. only then consider changing entry rules or enabling live execution.

## Upstream

Runtime engine: [Freqtrade](https://github.com/freqtrade/freqtrade) via the official `freqtradeorg/freqtrade:stable` Docker image.

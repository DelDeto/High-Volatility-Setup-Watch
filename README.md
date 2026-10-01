# Market Opportunity Scanner — Freqtrade migration

This branch rebuilds the former High Volatility Setup Watch on top of the official Freqtrade stable Docker image.

## Objective

Scan a broad Binance USDT-M futures universe and keep only setups with:

- strong multi-timeframe structure (4H / 1H / 15M)
- entry close to a meaningful structural zone
- structural stop and target
- attractive risk/reward
- volume confirmation
- volatility-aware entry requirements
- long and short support

High volatility is now a **regime**, not the first filter. This avoids excluding good normal-volatility pullbacks and avoids chasing extreme moves.

## Safety model

- Default mode: **dry-run**
- Default leverage: 1x
- No API key is required for public market-data scanning in dry-run.
- Live trading should not be enabled until the strategy has passed backtests, lookahead checks, dry-run, and outcome review.

The old MEXC implementation is preserved in branch `legacy-high-vol-v1`.

## Architecture

```text
Binance USDT-M futures
        ↓
VolumePairList (top 150)
        ↓
Age / spread filters
        ↓
15M base strategy
   + 1H context
   + 4H regime
        ↓
Trend pullback
Breakout/retest
Sweep reversal
High-vol continuation
        ↓
Structural Entry / SL / TP
        ↓
RR + Opportunity Score
        ↓
READY signals
        ↓
Freqtrade dry-run / backtest / Telegram
```

## Opportunity score

The first migration version uses a 100-point framework:

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

## Quick start

Requirements: Docker + Docker Compose.

```bash
cp .env.example .env
docker compose pull

# Validate the strategy
docker compose run --rm freqtrade list-strategies \
  --strategy-path /freqtrade/user_data/strategies

# Inspect the current dynamic Binance futures universe
docker compose run --rm freqtrade test-pairlist \
  --config /freqtrade/user_data/config.json

# Start continuous dry-run
docker compose up -d

# Follow logs
docker compose logs -f
```

## Telegram

Freqtrade supports environment-variable overrides. In `.env`:

```bash
FREQTRADE__TELEGRAM__ENABLED=true
FREQTRADE__TELEGRAM__TOKEN=...
FREQTRADE__TELEGRAM__CHAT_ID=...
```

Keep `.env` out of Git.

## Backtesting

For reproducibility, `config-backtest.json` uses a static seed pairlist instead of the live VolumePairList. Freqtrade notes that dynamic pairlists in backtests reflect current market conditions and can make results non-reproducible.

Run locally:

```bash
docker compose run --rm freqtrade download-data \
  --config /freqtrade/user_data/config-backtest.json \
  --timeframes 15m 1h 4h --days 180

docker compose run --rm freqtrade backtesting \
  --config /freqtrade/user_data/config-backtest.json \
  --strategy MarketOpportunityStrategy
```

A manual GitHub Actions backtest workflow is also included.

## What this version does not do yet

This migration intentionally does **not** auto-enable real-money execution. The next validation milestones are:

1. validate the strategy container loads cleanly;
2. backtest on a reproducible static pairlist;
3. run lookahead analysis;
4. run dry-run continuously;
5. collect 50–100 closed setups;
6. calibrate score and RR thresholds from actual outcomes;
7. only then consider live execution.

## Upstream

Runtime engine: [Freqtrade](https://github.com/freqtrade/freqtrade) via the official `freqtradeorg/freqtrade:stable` Docker image.

# High Volatility Setup Watch

Automated scanner for liquid MEXC USDT perpetual **crypto** contracts with unusually large daily movement and attractive PA/SMC trade structure.

## Goal

This repository is deliberately separate from Market Setup Watch.

- Market Setup Watch: clean, controlled PA/SMC setups.
- High Volatility Setup Watch: large-range / expansion setups with higher RR potential.

## Pipeline

```text
MEXC crypto universe
→ liquidity + spread filter
→ 24H range >= 10%
→ rank high-vol candidates
→ full 4H / 1H / 15M PA-SMC
→ ATR expansion + volume expansion
→ OI participation
→ deterministic trade plan
→ RR + entry proximity
→ high-vol scoring
→ correlation suppression
→ Telegram + VVV-style 15M chart
→ outcome tracking
```

## Buckets

### MOMENTUM_READY
Momentum direction agrees with the trade, execution is ready, TP1 RR >= 2R, price is close enough to entry, MTF is not conflicting, and score >= 80.

### PULLBACK_READY
Execution is ready near a valid pullback/sweep/retest area with TP1 RR >= 2R and score >= 78.

### HIGH_VOL_DEVELOPING
Promising high-volatility structure with TP1 RR >= 1.5R and score >= 68, but not yet ready.

### OVEREXTENDED
The setup must still be structurally valid: score >= 60, TP1 RR >= 1.5R, MTF not conflicting, an active trade plan, and at least one meaningful PA/SMC confirmation. Only then, if live price is >= 0.75 ATR away from the intended entry, it is classified as OVEREXTENDED. Low-quality distant setups are ignored.

### INSUFFICIENT_HISTORY
Newly listed contracts with fewer than the required 360 closed candles on a required timeframe are reported separately from API/fetch errors. They are not treated as scanner failures.

## Volatility regimes

- HIGH: 10% to <15% 24H range
- VERY_HIGH: 15% to <25%
- EXTREME: >=25%

Entry proximity becomes stricter as volatility rises.

## Market-quality gates

- 24H turnover >= 2M USDT
- spread <= 40 bps when bid/ask data is available
- 24H range >= 10%
- crypto-only heuristic excludes stock, oil, metals and index-style contracts
- top 60 high-volatility candidates receive the full PA/SMC scan

## Schedule

GitHub Actions runs at minutes:

```text
11, 26, 41, 56
```

of every hour, staggered from Market Setup Watch.

## GitHub Secrets

Create these repository secrets:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

A separate Telegram bot/chat is recommended so High Volatility alerts do not mix with the normal Market Setup Watch feed.

## Outcome journal

`high_vol/outcomes.json` tracks:

- entry touched / expired
- WIN / LOSS / AMBIGUOUS
- MFE / MAE in R
- 1H / 4H / 24H R checkpoints

This data can later be used to calibrate the high-volatility thresholds from actual performance.

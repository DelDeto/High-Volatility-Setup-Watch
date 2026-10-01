from pathlib import Path

BASE_URL = "https://api.mexc.com"
QUOTE_COIN = "USDT"

# Stage 1: find liquid contracts already moving.
MIN_24H_TURNOVER_USDT = 2_000_000.0
MAX_SPREAD_BPS = 40.0
MIN_24H_RANGE_PCT = 10.0
MAX_FULL_SCAN_SYMBOLS = 60

HISTORY_LIMIT = 420
MIN_HISTORY_REQUIRED = 360

# High-volatility tradeability.
MIN_DEVELOPING_RR = 1.5
MIN_READY_RR = 2.0
MOMENTUM_READY_MIN_SCORE = 80
PULLBACK_READY_MIN_SCORE = 78
DEVELOPING_MIN_SCORE = 68
OVEREXTENDED_DISTANCE_ATR = 0.75

# Entry distance is stricter for extreme movers.
NORMAL_READY_DISTANCE_ATR = 0.25
VERY_HIGH_READY_DISTANCE_ATR = 0.18
EXTREME_READY_DISTANCE_ATR = 0.12

MAX_TELEGRAM_SETUPS = 8

STATE_PATH = Path("high_vol/state.json")
OUTCOME_PATH = Path("high_vol/outcomes.json")
REPORT_PATH = Path("output/high_vol_report.json")
TEXT_PATH = Path("output/high_vol_update.txt")

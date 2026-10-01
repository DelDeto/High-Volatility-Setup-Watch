import math

import pandas as pd

from .config import (
    DEVELOPING_MIN_SCORE,
    EXTREME_READY_DISTANCE_ATR,
    MIN_DEVELOPING_RR,
    MIN_READY_RR,
    MOMENTUM_READY_MIN_SCORE,
    NORMAL_READY_DISTANCE_ATR,
    OVEREXTENDED_DISTANCE_ATR,
    OVEREXTENDED_MIN_SCORE,
    PULLBACK_READY_MIN_SCORE,
    VERY_HIGH_READY_DISTANCE_ATR,
)


def _range_pct(ticker):
    high = (ticker or {}).get("high_24h")
    low = (ticker or {}).get("low_24h")
    if high is None or low in (None, 0):
        return None
    return max(0.0, (float(high) - float(low)) / float(low) * 100.0)


def _volatility_regime(range_pct):
    if range_pct is None:
        return "UNKNOWN"
    if range_pct < 15:
        return "HIGH"
    if range_pct < 25:
        return "VERY_HIGH"
    return "EXTREME"


def _true_range(frame):
    prev_close = frame["close"].shift(1)
    return pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - prev_close).abs(),
            (frame["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)


def _expansion_metrics(frame):
    tr = _true_range(frame)
    atr14 = tr.rolling(14, min_periods=5).mean()
    recent_atr = float(atr14.iloc[-1]) if not atr14.empty else None
    baseline = atr14.tail(96).median()

    atr_expansion = None
    if recent_atr and baseline and not math.isnan(float(baseline)) and float(baseline) > 0:
        atr_expansion = recent_atr / float(baseline)

    volume = frame["volume"].astype(float)
    recent_vol = volume.tail(8).mean()
    base_vol = volume.iloc[-40:-8].mean() if len(volume) >= 40 else volume.mean()
    volume_expansion = None
    if base_vol and not math.isnan(float(base_vol)) and float(base_vol) > 0:
        volume_expansion = float(recent_vol) / float(base_vol)

    return {
        "atr_expansion": atr_expansion,
        "volume_expansion": volume_expansion,
    }


def _entry_distance_atr(plan, live_price, atr):
    if not plan.get("active") or live_price is None or not atr:
        return None

    entry = plan.get("entry_zone", {})
    lower = entry.get("lower")
    upper = entry.get("upper")
    if lower is None or upper is None:
        return None

    lower = float(lower)
    upper = float(upper)
    live_price = float(live_price)

    if lower <= live_price <= upper:
        return 0.0

    return min(abs(live_price - lower), abs(live_price - upper)) / max(float(atr), 1e-9)


def _ready_distance_limit(regime):
    if regime == "EXTREME":
        return EXTREME_READY_DISTANCE_ATR
    if regime == "VERY_HIGH":
        return VERY_HIGH_READY_DISTANCE_ATR
    return NORMAL_READY_DISTANCE_ATR


def _zone_points(analysis_15m, direction):
    zone = (
        analysis_15m.get("nearest_demand")
        if direction == "long"
        else analysis_15m.get("nearest_supply")
    ) or {}

    return {
        "A+": 16,
        "A": 14,
        "B": 10,
        "C": 4,
    }.get(zone.get("grade"), 0)


def score_high_vol_setup(
    analysis_4h,
    analysis_1h,
    analysis_15m,
    alignment,
    plan,
    ticker,
    frame_15m,
):
    setup = analysis_15m.get("setup", {}) or {}
    direction = plan.get("direction") if plan.get("active") else None
    live_price = (ticker or {}).get("last_price")
    atr = analysis_15m.get("atr")
    first_rr = plan.get("first_target_rr")

    range_pct = _range_pct(ticker)
    regime = _volatility_regime(range_pct)
    expansion = _expansion_metrics(frame_15m)
    atr_expansion = expansion["atr_expansion"]
    volume_expansion = expansion["volume_expansion"]
    distance_atr = _entry_distance_atr(plan, live_price, atr)

    breakdown = {}
    breakdown["24h_range"] = (
        8 if range_pct is not None and range_pct >= 10
        else 0
    )
    if range_pct is not None and range_pct >= 15:
        breakdown["24h_range"] += 4
    if range_pct is not None and range_pct >= 25:
        breakdown["24h_range"] += 2

    if atr_expansion is None:
        breakdown["atr_expansion"] = 0
    elif atr_expansion >= 1.5:
        breakdown["atr_expansion"] = 14
    elif atr_expansion >= 1.2:
        breakdown["atr_expansion"] = 10
    elif atr_expansion >= 1.0:
        breakdown["atr_expansion"] = 5
    else:
        breakdown["atr_expansion"] = -4

    if volume_expansion is None:
        breakdown["volume_expansion"] = 0
    elif volume_expansion >= 1.8:
        breakdown["volume_expansion"] = 12
    elif volume_expansion >= 1.3:
        breakdown["volume_expansion"] = 8
    elif volume_expansion >= 1.0:
        breakdown["volume_expansion"] = 4
    else:
        breakdown["volume_expansion"] = -3

    setup_score = int(setup.get("score", 0) or 0)
    breakdown["setup_completeness"] = min(16, setup_score * 4)
    breakdown["displacement"] = 10 if setup.get("displacement") else -8
    breakdown["structure"] = 9 if setup.get("structure") else -8
    breakdown["sweep"] = 7 if setup.get("sweep") else 0
    breakdown["retest"] = 7 if setup.get("retest") else 0
    breakdown["zone_quality"] = _zone_points(analysis_15m, direction) if direction else 0

    alignment_label = alignment.get("label")
    breakdown["mtf_alignment"] = {
        "ALIGNED": 8,
        "PARTIAL": 3,
        "CONFLICT": -18,
    }.get(alignment_label, 0)

    if first_rr is None:
        breakdown["rr"] = -8
    elif first_rr >= 3.0:
        breakdown["rr"] = 14
    elif first_rr >= 2.0:
        breakdown["rr"] = 10
    elif first_rr >= 1.5:
        breakdown["rr"] = 5
    else:
        breakdown["rr"] = -14

    participation = ((ticker or {}).get("participation_context") or {}).get("regime")
    if participation == "LONG_BUILD":
        breakdown["participation"] = 6 if direction == "long" else -4
    elif participation == "SHORT_BUILD":
        breakdown["participation"] = 6 if direction == "short" else -4
    elif participation == "DELEVERAGING":
        breakdown["participation"] = -2
    else:
        breakdown["participation"] = 0

    change_24h = (ticker or {}).get("change_rate_24h")
    momentum_aligned = False
    if change_24h is not None and direction:
        momentum_aligned = (
            (direction == "long" and float(change_24h) > 0)
            or (direction == "short" and float(change_24h) < 0)
        )
    breakdown["momentum_alignment"] = 6 if momentum_aligned else 0

    if distance_atr is None:
        breakdown["entry_proximity"] = 0
    elif distance_atr == 0:
        breakdown["entry_proximity"] = 8
    elif distance_atr <= 0.15:
        breakdown["entry_proximity"] = 6
    elif distance_atr <= 0.35:
        breakdown["entry_proximity"] = 3
    elif distance_atr >= OVEREXTENDED_DISTANCE_ATR:
        breakdown["entry_proximity"] = -12
    else:
        breakdown["entry_proximity"] = 0

    score = max(0, min(100, sum(breakdown.values())))

    ready_distance = _ready_distance_limit(regime)
    rr_ready = first_rr is not None and first_rr >= MIN_READY_RR
    rr_developing = first_rr is not None and first_rr >= MIN_DEVELOPING_RR
    execution_ready = bool(plan.get("execution_ready"))

    overextended = (
        distance_atr is not None
        and distance_atr >= OVEREXTENDED_DISTANCE_ATR
        and rr_developing
        and alignment_label != "CONFLICT"
        and score >= OVEREXTENDED_MIN_SCORE
        and plan.get("active")
        and (
            bool(setup.get("structure"))
            or bool(setup.get("displacement"))
            or bool(setup.get("retest"))
            or bool(setup.get("sweep"))
        )
    )

    momentum_ready = (
        execution_ready
        and rr_ready
        and distance_atr is not None
        and distance_atr <= ready_distance
        and momentum_aligned
        and alignment_label != "CONFLICT"
        and score >= MOMENTUM_READY_MIN_SCORE
        and not overextended
    )

    pullback_ready = (
        execution_ready
        and rr_ready
        and distance_atr is not None
        and distance_atr <= ready_distance
        and bool(setup.get("retest") or setup.get("sweep"))
        and alignment_label != "CONFLICT"
        and score >= PULLBACK_READY_MIN_SCORE
        and not overextended
    )

    developing = (
        rr_developing
        and alignment_label != "CONFLICT"
        and score >= DEVELOPING_MIN_SCORE
        and not overextended
    )

    if momentum_ready:
        bucket = "MOMENTUM_READY"
    elif pullback_ready:
        bucket = "PULLBACK_READY"
    elif overextended:
        bucket = "OVEREXTENDED"
    elif developing:
        bucket = "HIGH_VOL_DEVELOPING"
    else:
        bucket = "IGNORE"

    return {
        "score": score,
        "bucket": bucket,
        "entry_distance_atr": distance_atr,
        "score_breakdown": breakdown,
        "volatility": {
            "range_24h_pct": range_pct,
            "regime": regime,
            "atr_expansion": atr_expansion,
            "volume_expansion": volume_expansion,
            "ready_distance_limit_atr": ready_distance,
            "momentum_aligned": momentum_aligned,
        },
    }

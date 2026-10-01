import json
from datetime import datetime, timezone

import pandas as pd

from .config import OUTCOME_PATH


TRACK_BUCKETS = (
    "MOMENTUM_READY",
    "PULLBACK_READY",
    "HIGH_VOL_DEVELOPING",
)


def load_outcomes():
    if not OUTCOME_PATH.exists():
        return []
    try:
        data = json.loads(OUTCOME_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_outcomes(records):
    OUTCOME_PATH.write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _signal_id(item):
    plan = item.get("trade_plan") or {}
    entry = plan.get("entry_zone") or {}
    return "|".join([
        str(item.get("symbol")),
        str(item.get("direction")),
        str(round(float(entry.get("lower") or 0), 8)),
        str(round(float(entry.get("upper") or 0), 8)),
        str(round(float(plan.get("stop_loss") or 0), 8)),
    ])


def register_candidates(records, results, generated_at):
    existing = {row.get("signal_id") for row in records}

    for item in results:
        if item.get("bucket") not in TRACK_BUCKETS:
            continue

        plan = item.get("trade_plan") or {}
        entry = plan.get("entry_zone") or {}
        targets = plan.get("targets") or []
        if not plan.get("active"):
            continue

        signal_id = _signal_id(item)
        if signal_id in existing:
            continue

        records.append({
            "signal_id": signal_id,
            "created_at_utc": generated_at,
            "symbol": item.get("symbol"),
            "direction": item.get("direction"),
            "bucket": item.get("bucket"),
            "score": item.get("score"),
            "volatility": item.get("volatility"),
            "entry_lower": entry.get("lower"),
            "entry_upper": entry.get("upper"),
            "stop_loss": plan.get("stop_loss"),
            "tp1": targets[0].get("price") if targets else None,
            "first_target_rr": plan.get("first_target_rr"),
            "status": "PENDING_ENTRY",
            "entry_time_utc": None,
            "closed_at_utc": None,
            "outcome": None,
            "mfe_r": None,
            "mae_r": None,
            "checkpoints_r": {"1h": None, "4h": None, "24h": None},
        })
        existing.add(signal_id)

    return records


def _ts(value):
    try:
        return pd.Timestamp(value)
    except Exception:
        return None


def update_outcomes(records, frames_by_symbol):
    now = datetime.now(timezone.utc)

    for row in records:
        if row.get("outcome") in ("WIN", "LOSS", "AMBIGUOUS", "EXPIRED"):
            continue

        frame = (frames_by_symbol.get(row.get("symbol")) or {}).get("15M")
        if frame is None or frame.empty:
            continue

        created = _ts(row.get("created_at_utc"))
        if created is None:
            continue

        close_times = frame.index + pd.Timedelta(minutes=15)
        candles = frame.loc[close_times > created]
        if candles.empty:
            continue

        lower = float(row["entry_lower"])
        upper = float(row["entry_upper"])
        stop = float(row["stop_loss"])
        tp1 = float(row["tp1"]) if row.get("tp1") is not None else None
        direction = row.get("direction")
        entry_mid = (lower + upper) / 2.0
        risk = abs(entry_mid - stop)
        if risk <= 0:
            continue

        entry_time = _ts(row.get("entry_time_utc"))
        active = candles

        if row.get("status") == "PENDING_ENTRY":
            touched = candles[
                (candles["low"] <= upper)
                & (candles["high"] >= lower)
            ]
            if touched.empty:
                age_hours = (now - created.to_pydatetime()).total_seconds() / 3600
                if age_hours >= 12:
                    row["status"] = "CLOSED"
                    row["outcome"] = "EXPIRED"
                    row["closed_at_utc"] = now.isoformat()
                continue

            entry_time = touched.index[0]
            row["entry_time_utc"] = entry_time.isoformat()
            row["status"] = "ACTIVE"
            active = candles.loc[candles.index >= entry_time]
        elif entry_time is not None:
            active = candles.loc[candles.index >= entry_time]

        best_r = 0.0
        worst_r = 0.0

        for ts, candle in active.iterrows():
            high = float(candle["high"])
            low = float(candle["low"])

            if direction == "long":
                favorable = (high - entry_mid) / risk
                adverse = (entry_mid - low) / risk
                hit_stop = low <= stop
                hit_tp = tp1 is not None and high >= tp1
            else:
                favorable = (entry_mid - low) / risk
                adverse = (high - entry_mid) / risk
                hit_stop = high >= stop
                hit_tp = tp1 is not None and low <= tp1

            best_r = max(best_r, favorable)
            worst_r = max(worst_r, adverse)

            if hit_stop and hit_tp:
                row["status"] = "CLOSED"
                row["outcome"] = "AMBIGUOUS"
                row["closed_at_utc"] = ts.isoformat()
                break
            if hit_tp:
                row["status"] = "CLOSED"
                row["outcome"] = "WIN"
                row["closed_at_utc"] = ts.isoformat()
                break
            if hit_stop:
                row["status"] = "CLOSED"
                row["outcome"] = "LOSS"
                row["closed_at_utc"] = ts.isoformat()
                break

        row["mfe_r"] = round(best_r, 3)
        row["mae_r"] = round(worst_r, 3)

        if entry_time is not None:
            checkpoints = row.setdefault(
                "checkpoints_r",
                {"1h": None, "4h": None, "24h": None},
            )
            for label, hours in (("1h", 1), ("4h", 4), ("24h", 24)):
                if checkpoints.get(label) is not None:
                    continue
                target = entry_time + pd.Timedelta(hours=hours)
                close_times = active.index + pd.Timedelta(minutes=15)
                eligible = active.loc[close_times >= target]
                if eligible.empty:
                    continue
                close_price = float(eligible.iloc[0]["close"])
                value = (
                    (close_price - entry_mid) / risk
                    if direction == "long"
                    else (entry_mid - close_price) / risk
                )
                checkpoints[label] = round(value, 3)

    return records

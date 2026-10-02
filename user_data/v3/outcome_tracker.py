from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import ccxt

from chart_generator import generate_ready_charts, telegram_send_photo
from delivery_ledger import (
    enqueue,
    load_delivery,
    mark_attempt,
    mark_failed,
    mark_sent,
    pending_items,
    save_delivery,
    stats as delivery_stats,
)


TERMINAL = {"TP", "SL", "TIMEOUT", "EXPIRED", "AMBIGUOUS"}
TRADE_TERMINAL = {"TP", "SL", "TIMEOUT", "AMBIGUOUS"}
TF_MS = 15 * 60 * 1000


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utc_now()).isoformat()


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def finite(value: Any, default: float = 0.0) -> float:
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def retry(callable_, *args, attempts: int = 3, **kwargs):
    last = None
    for idx in range(attempts):
        try:
            return callable_(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            last = exc
            if idx + 1 < attempts:
                time.sleep(1.25 * (idx + 1))
    raise last


def build_exchange():
    exchange = ccxt.gate(
        {
            "enableRateLimit": True,
            "timeout": 20000,
            "options": {"defaultType": "swap"},
        }
    )
    exchange.load_markets()
    return exchange


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def setup_key(item: dict[str, Any]) -> str:
    return "|".join(
        [
            str(item.get("symbol", "")),
            str(item.get("side", "")).upper(),
            str(item.get("setup", "")),
        ]
    )


def new_signal_id(item: dict[str, Any], created_at: str) -> str:
    raw = f"{setup_key(item)}|{created_at}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def same_open_setup(record: dict[str, Any], item: dict[str, Any]) -> bool:
    return (
        record.get("tracking_status") not in TERMINAL
        and record.get("symbol") == item.get("symbol")
        and record.get("side") == item.get("side")
        and record.get("setup") == item.get("setup")
    )


def cooldown_match(
    record: dict[str, Any],
    item: dict[str, Any],
    now: datetime,
    cooldown_hours: int,
) -> bool:
    if record.get("tracking_status") not in TERMINAL:
        return False
    if (
        record.get("symbol") != item.get("symbol")
        or record.get("side") != item.get("side")
        or record.get("setup") != item.get("setup")
    ):
        return False
    terminal_at = parse_dt(record.get("closed_at") or record.get("expired_at"))
    return bool(terminal_at and now - terminal_at < timedelta(hours=cooldown_hours))


def freeze_plan(record: dict[str, Any], item: dict[str, Any], now: datetime) -> None:
    record["scanner_status"] = "READY"
    record["tracking_status"] = "PENDING_ENTRY"
    record["ready_at"] = record.get("ready_at") or iso(now)
    record["entry"] = finite(item.get("entry"))
    record["stop"] = finite(item.get("stop"))
    record["target"] = finite(item.get("target"))
    record["rr"] = finite(item.get("rr"))
    record["score"] = int(item.get("score", 0))
    record["required_score"] = int(item.get("required_score", 0))
    record["required_rr"] = finite(item.get("required_rr"))
    record["volatility_regime"] = item.get("volatility_regime")
    record["range_24h_pct"] = finite(item.get("range_24h_pct"))
    record["entry_distance_atr"] = finite(item.get("entry_distance_atr"))
    record["atr_expansion"] = finite(item.get("atr_expansion"), 1.0)
    record["obstacle_clearance_r"] = finite(item.get("obstacle_clearance_r"), 0.0)
    record["quality_gates"] = item.get("quality_gates", "PASS")
    record["opportunity_value"] = finite(item.get("opportunity_value"))
    record["rank_at_ready"] = item.get("rank")
    risk = abs(record["entry"] - record["stop"])
    record["risk"] = risk
    record["plan_valid"] = bool(
        risk > 0
        and (
            (
                record["side"] == "LONG"
                and record["stop"] < record["entry"] < record["target"]
            )
            or (
                record["side"] == "SHORT"
                and record["target"] < record["entry"] < record["stop"]
            )
        )
    )


def create_record(item: dict[str, Any], now: datetime) -> dict[str, Any]:
    created_at = iso(now)
    status = str(item.get("status", "DEVELOPING")).upper()
    record = {
        "signal_id": new_signal_id(item, created_at),
        "symbol": item.get("symbol"),
        "side": str(item.get("side", "")).upper(),
        "setup": item.get("setup"),
        "created_at": created_at,
        "first_seen_at": created_at,
        "last_seen_at": created_at,
        "scanner_status": status,
        "tracking_status": "MONITORING" if status == "DEVELOPING" else "PENDING_ENTRY",
        "initial_rank": item.get("rank"),
        "latest_rank": item.get("rank"),
        "latest_score": int(item.get("score", 0)),
        "latest_rr": finite(item.get("rr")),
        "latest_opportunity_value": finite(item.get("opportunity_value")),
        "latest_atr_expansion": finite(item.get("atr_expansion"), 1.0),
        "latest_obstacle_clearance_r": finite(item.get("obstacle_clearance_r"), 0.0),
        "ready_at": None,
        "entry": None,
        "stop": None,
        "target": None,
        "risk": None,
        "rr": None,
        "score": None,
        "required_score": None,
        "required_rr": None,
        "volatility_regime": item.get("volatility_regime"),
        "range_24h_pct": finite(item.get("range_24h_pct")),
        "entry_distance_atr": finite(item.get("entry_distance_atr")),
        "atr_expansion": None,
        "obstacle_clearance_r": None,
        "quality_gates": item.get("quality_gates", "PASS"),
        "opportunity_value": finite(item.get("opportunity_value")),
        "rank_at_ready": None,
        "plan_valid": None,
        "entry_time": None,
        "last_checked_at": None,
        "last_processed_candle_ts": None,
        "mfe_r": None,
        "mae_r": None,
        "closed_at": None,
        "expired_at": None,
        "outcome": None,
        "actual_r": None,
        "exit_price": None,
        "hold_hours": None,
        "charts": {},
        "chart_generated_at": None,
        "notes": [],
    }
    if status == "READY":
        freeze_plan(record, item, now)
    return record


def update_seen_record(
    record: dict[str, Any],
    item: dict[str, Any],
    now: datetime,
) -> tuple[bool, bool]:
    """Return (changed, promoted_to_ready)."""
    changed = False
    promoted = False
    now_iso = iso(now)

    if record.get("last_seen_at") != now_iso:
        record["last_seen_at"] = now_iso
        changed = True

    for key, value in (
        ("latest_rank", item.get("rank")),
        ("latest_score", int(item.get("score", 0))),
        ("latest_rr", finite(item.get("rr"))),
        ("latest_opportunity_value", finite(item.get("opportunity_value"))),
        ("latest_atr_expansion", finite(item.get("atr_expansion"), 1.0)),
        ("latest_obstacle_clearance_r", finite(item.get("obstacle_clearance_r"), 0.0)),
    ):
        if record.get(key) != value:
            record[key] = value
            changed = True

    status = str(item.get("status", "")).upper()
    if record.get("scanner_status") != status:
        record["scanner_status"] = status
        changed = True

    if (
        status == "READY"
        and record.get("tracking_status") == "MONITORING"
    ):
        freeze_plan(record, item, now)
        promoted = True
        changed = True

    return changed, promoted


def ingest_snapshot(
    state: dict[str, Any],
    snapshot: dict[str, Any],
    now: datetime,
    cooldown_hours: int,
) -> dict[str, list[dict[str, Any]]]:
    records = state.setdefault("records", [])
    events = {"new": [], "promoted": [], "suppressed": []}

    for item in snapshot.get("top_opportunities", []):
        match = next((r for r in records if same_open_setup(r, item)), None)
        if match:
            _, promoted = update_seen_record(match, item, now)
            if promoted:
                events["promoted"].append(match)
            continue

        recent_terminal = next(
            (r for r in reversed(records) if cooldown_match(r, item, now, cooldown_hours)),
            None,
        )
        if recent_terminal:
            recent_terminal["last_seen_at"] = iso(now)
            events["suppressed"].append(recent_terminal)
            continue

        record = create_record(item, now)
        records.append(record)
        events["new"].append(record)

    state["updated_at"] = iso(now)
    return events


def closed_candles(
    exchange,
    symbol: str,
    since_ms: int | None,
    limit: int = 1000,
) -> list[list[float]]:
    rows = retry(exchange.fetch_ohlcv, symbol, timeframe="15m", since=since_ms, limit=limit)
    now_ms = exchange.milliseconds()
    return [row for row in rows if row[0] + TF_MS <= now_ms - 2000]


def candle_crosses(level: float, low: float, high: float) -> bool:
    return low <= level <= high


def update_excursions(record: dict[str, Any], low: float, high: float) -> None:
    entry = finite(record["entry"])
    risk = finite(record["risk"])
    if risk <= 0:
        return

    if record["side"] == "LONG":
        favorable = (high - entry) / risk
        adverse = (low - entry) / risk
    else:
        favorable = (entry - low) / risk
        adverse = (entry - high) / risk

    current_mfe = record.get("mfe_r")
    current_mae = record.get("mae_r")
    record["mfe_r"] = max(favorable, finite(current_mfe, 0.0))
    record["mae_r"] = min(adverse, finite(current_mae, 0.0))


def finish_trade(
    record: dict[str, Any],
    outcome: str,
    exit_price: float | None,
    actual_r: float | None,
    when: datetime,
) -> None:
    record["tracking_status"] = outcome
    record["outcome"] = outcome
    record["closed_at"] = iso(when)
    record["exit_price"] = exit_price
    record["actual_r"] = actual_r
    entry_time = parse_dt(record.get("entry_time"))
    if entry_time:
        record["hold_hours"] = round((when - entry_time).total_seconds() / 3600, 3)


def process_ready_record(
    record: dict[str, Any],
    exchange,
    now: datetime,
    entry_window_hours: int,
    max_hold_hours: int,
) -> list[dict[str, Any]]:
    events = []
    if record.get("tracking_status") not in {"PENDING_ENTRY", "ACTIVE"}:
        return events
    if not record.get("plan_valid"):
        finish_trade(record, "AMBIGUOUS", None, None, now)
        record["notes"].append("invalid_frozen_trade_plan")
        events.append({"type": "AMBIGUOUS", "record": record})
        return events

    ready_at = parse_dt(record.get("ready_at"))
    last_ts = record.get("last_processed_candle_ts")
    since_ms = int(last_ts) + TF_MS if last_ts is not None else int(ready_at.timestamp() * 1000)
    rows = closed_candles(exchange, record["symbol"], since_ms)

    entry = finite(record["entry"])
    stop = finite(record["stop"])
    target = finite(record["target"])
    risk = finite(record["risk"])

    for row in rows:
        ts, _, high, low, close, _ = row
        candle_time = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        high = finite(high)
        low = finite(low)
        close = finite(close)
        record["last_processed_candle_ts"] = int(ts)

        if record["tracking_status"] == "PENDING_ENTRY":
            if candle_crosses(entry, low, high):
                record["entry_time"] = iso(candle_time)
                record["tracking_status"] = "ACTIVE"
                events.append({"type": "ENTRY", "record": record})

                stop_hit_same = low <= stop if record["side"] == "LONG" else high >= stop
                target_hit_same = high >= target if record["side"] == "LONG" else low <= target
                if stop_hit_same or target_hit_same:
                    record["notes"].append("entry_and_exit_level_touched_same_15m_candle")
                    finish_trade(record, "AMBIGUOUS", None, None, candle_time)
                    events.append({"type": "AMBIGUOUS", "record": record})
                    break
                update_excursions(record, low, high)
            continue

        if record["tracking_status"] == "ACTIVE":
            update_excursions(record, low, high)
            if record["side"] == "LONG":
                stop_hit = low <= stop
                target_hit = high >= target
            else:
                stop_hit = high >= stop
                target_hit = low <= target

            if stop_hit and target_hit:
                record["notes"].append("tp_and_sl_touched_same_15m_candle")
                finish_trade(record, "AMBIGUOUS", None, None, candle_time)
                events.append({"type": "AMBIGUOUS", "record": record})
                break
            if target_hit:
                finish_trade(record, "TP", target, finite(record["rr"]), candle_time)
                events.append({"type": "TP", "record": record})
                break
            if stop_hit:
                finish_trade(record, "SL", stop, -1.0, candle_time)
                events.append({"type": "SL", "record": record})
                break

    record["last_checked_at"] = iso(now)

    if record["tracking_status"] == "PENDING_ENTRY" and ready_at:
        if now - ready_at >= timedelta(hours=entry_window_hours):
            record["tracking_status"] = "EXPIRED"
            record["outcome"] = "EXPIRED"
            record["expired_at"] = iso(now)
            record["actual_r"] = None
            events.append({"type": "EXPIRED", "record": record})

    if record["tracking_status"] == "ACTIVE":
        entry_time = parse_dt(record.get("entry_time"))
        if entry_time and now - entry_time >= timedelta(hours=max_hold_hours):
            ticker = retry(exchange.fetch_ticker, record["symbol"])
            price = finite(ticker.get("last"))
            if price > 0 and risk > 0:
                actual_r = (
                    (price - entry) / risk
                    if record["side"] == "LONG"
                    else (entry - price) / risk
                )
                finish_trade(record, "TIMEOUT", price, actual_r, now)
                events.append({"type": "TIMEOUT", "record": record})

    return events


def expire_stale_developing(
    state: dict[str, Any],
    now: datetime,
    stale_hours: int,
) -> list[dict[str, Any]]:
    events = []
    for record in state.get("records", []):
        if record.get("tracking_status") != "MONITORING":
            continue
        last_seen = parse_dt(record.get("last_seen_at"))
        if last_seen and now - last_seen >= timedelta(hours=stale_hours):
            record["tracking_status"] = "EXPIRED"
            record["outcome"] = "EXPIRED"
            record["expired_at"] = iso(now)
            events.append({"type": "EXPIRED", "record": record})
    return events


def group_stats(records: list[dict[str, Any]], key_fn) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        groups[str(key_fn(record))].append(record)

    out = {}
    for key, items in groups.items():
        completed = [
            r for r in items
            if r.get("tracking_status") in {"TP", "SL", "TIMEOUT"}
            and r.get("actual_r") is not None
        ]
        tp_sl = [r for r in items if r.get("tracking_status") in {"TP", "SL"}]
        r_values = [finite(r.get("actual_r")) for r in completed]
        gross_win = sum(x for x in r_values if x > 0)
        gross_loss = abs(sum(x for x in r_values if x < 0))
        out[key] = {
            "records": len(items),
            "completed": len(completed),
            "wins_tp": sum(1 for r in items if r.get("tracking_status") == "TP"),
            "losses_sl": sum(1 for r in items if r.get("tracking_status") == "SL"),
            "win_rate_tp_sl": (
                sum(1 for r in tp_sl if r.get("tracking_status") == "TP") / len(tp_sl)
                if tp_sl else None
            ),
            "average_r": sum(r_values) / len(r_values) if r_values else None,
            "profit_factor_r": gross_win / gross_loss if gross_loss > 0 else None,
        }
    return out


def atr_expansion_bucket(record: dict[str, Any]) -> str:
    value = finite(
        record.get("atr_expansion"),
        finite(record.get("latest_atr_expansion"), 0.0),
    )
    if value < 0.90:
        return "<0.90x"
    if value < 1.05:
        return "0.90-1.04x"
    if value < 1.25:
        return "1.05-1.24x"
    return ">=1.25x"


def obstacle_bucket(record: dict[str, Any]) -> str:
    value = finite(
        record.get("obstacle_clearance_r"),
        finite(record.get("latest_obstacle_clearance_r"), 0.0),
    )
    if value < 0.75:
        return "<0.75R"
    if value < 1.00:
        return "0.75-0.99R"
    if value < 1.50:
        return "1.00-1.49R"
    return ">=1.50R"


def build_summary(state: dict[str, Any]) -> dict[str, Any]:
    records = state.get("records", [])
    counts = defaultdict(int)
    for record in records:
        counts[record.get("tracking_status", "UNKNOWN")] += 1

    completed = [
        r for r in records
        if r.get("tracking_status") in {"TP", "SL", "TIMEOUT"}
        and r.get("actual_r") is not None
    ]
    r_values = [finite(r.get("actual_r")) for r in completed]
    gross_win = sum(x for x in r_values if x > 0)
    gross_loss = abs(sum(x for x in r_values if x < 0))
    tp_sl = [r for r in records if r.get("tracking_status") in {"TP", "SL"}]

    return {
        "version": "V3.5",
        "generated_at": iso(),
        "unique_setups": len(records),
        "ready_unique": sum(1 for r in records if r.get("ready_at")),
        "tracking_counts": dict(sorted(counts.items())),
        "completed_trade_outcomes": len(completed),
        "tp": counts["TP"],
        "sl": counts["SL"],
        "timeouts": counts["TIMEOUT"],
        "ambiguous": counts["AMBIGUOUS"],
        "expired": counts["EXPIRED"],
        "win_rate_tp_sl": (
            counts["TP"] / len(tp_sl) if tp_sl else None
        ),
        "average_r": sum(r_values) / len(r_values) if r_values else None,
        "profit_factor_r": gross_win / gross_loss if gross_loss > 0 else None,
        "by_setup": group_stats(records, lambda r: r.get("setup", "unknown")),
        "by_side": group_stats(records, lambda r: r.get("side", "unknown")),
        "by_volatility": group_stats(
            records, lambda r: r.get("volatility_regime", "unknown")
        ),
        "by_atr_expansion": group_stats(records, atr_expansion_bucket),
        "by_obstacle_clearance": group_stats(records, obstacle_bucket),
    }


def price_fmt(value: Any) -> str:
    x = finite(value)
    a = abs(x)
    if a >= 1000:
        return f"{x:,.2f}"
    if a >= 1:
        return f"{x:.4f}"
    if a >= 0.01:
        return f"{x:.6f}"
    return f"{x:.8f}"


def telegram_send(text: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id or not text:
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": text[:3900], "disable_web_page_preview": "true"}
    ).encode()
    req = urllib.request.Request(url, data=payload, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
        body = json.loads(resp.read().decode("utf-8"))
        if not body.get("ok"):
            raise RuntimeError(f"Telegram send failed: {body}")
    return True


def ensure_ready_charts(
    exchange,
    record: dict[str, Any],
    chart_dir: Path,
) -> bool:
    if record.get("tracking_status") not in {
        "PENDING_ENTRY", "ACTIVE", "TP", "SL", "TIMEOUT", "AMBIGUOUS"
    }:
        return False
    charts = record.get("charts") or {}
    existing = all(Path(p).exists() for p in charts.values()) if charts else False
    if existing and {"1h", "4h"}.issubset(charts):
        return False

    generated = generate_ready_charts(exchange, record, chart_dir)
    record["charts"] = generated
    record["chart_generated_at"] = iso()
    return True


def ready_chart_caption(record: dict[str, Any], timeframe: str) -> str:
    label = "4H CONTEXT" if timeframe == "4h" else "1H SETUP / ENTRY"
    return (
        f"{label} | {record['symbol']} {record['side']}\n"
        f"{record['setup']} | RR {finite(record.get('rr')):.2f}R | "
        f"Score {record.get('score')} | ID {record.get('signal_id')}"
    )


def signal_alert(record: dict[str, Any], promoted: bool = False) -> str:
    label = "PROMOTED TO READY" if promoted else record.get("scanner_status", "NEW SETUP")
    lines = [
        f"🚀 V3.5 {label}",
        f"{record['symbol']} {record['side']} | {record['setup']}",
    ]
    if record.get("tracking_status") == "PENDING_ENTRY":
        lines.extend(
            [
                f"Entry {price_fmt(record['entry'])}",
                f"SL {price_fmt(record['stop'])} | TP {price_fmt(record['target'])}",
                f"RR {finite(record['rr']):.2f}R | Score {record.get('score')}",
                f"Regime {record.get('volatility_regime')} | Rank #{record.get('latest_rank')}",
                f"ID {record['signal_id']}",
            ]
        )
    else:
        lines.extend(
            [
                f"Status DEVELOPING / WATCHLIST | Rank #{record.get('latest_rank')}",
                "WATCHLIST ONLY — no entry/SL/TP until promoted to READY",
                f"Latest RR {finite(record.get('latest_rr')):.2f}R | Score {record.get('latest_score')}",
                f"ID {record['signal_id']}",
            ]
        )
    return "\n".join(lines)


def outcome_alert(event: dict[str, Any]) -> str:
    record = event["record"]
    kind = event["type"]
    if kind not in {"TP", "SL", "TIMEOUT", "AMBIGUOUS"}:
        return ""
    lines = [
        f"📊 V3.5 OUTCOME {kind}",
        f"{record['symbol']} {record['side']} | {record['setup']}",
        f"ID {record['signal_id']}",
    ]
    if record.get("actual_r") is not None:
        lines.append(f"Actual R: {finite(record['actual_r']):+.2f}R")
    if record.get("mfe_r") is not None:
        lines.append(
            f"MFE {finite(record['mfe_r']):+.2f}R | MAE {finite(record['mae_r']):+.2f}R"
        )
    return "\n".join(lines)


def find_record(state: dict[str, Any], signal_id: str) -> dict[str, Any] | None:
    return next(
        (r for r in state.get("records", []) if r.get("signal_id") == signal_id),
        None,
    )


def enqueue_notifications(
    delivery: dict[str, Any],
    ingest_events: dict[str, list[dict[str, Any]]],
    outcome_events: list[dict[str, Any]],
) -> None:
    for record in ingest_events["new"]:
        sid = record["signal_id"]
        enqueue(
            delivery,
            key=f"signal:{sid}:new",
            kind="signal_text",
            signal_id=sid,
            text=signal_alert(record, promoted=False),
        )
        if record.get("tracking_status") == "PENDING_ENTRY":
            for tf in ("4h", "1h"):
                enqueue(
                    delivery,
                    key=f"signal:{sid}:chart:{tf}",
                    kind="signal_chart",
                    signal_id=sid,
                    timeframe=tf,
                )

    for record in ingest_events["promoted"]:
        sid = record["signal_id"]
        enqueue(
            delivery,
            key=f"signal:{sid}:ready",
            kind="signal_text",
            signal_id=sid,
            text=signal_alert(record, promoted=True),
        )
        for tf in ("4h", "1h"):
            enqueue(
                delivery,
                key=f"signal:{sid}:chart:{tf}",
                kind="signal_chart",
                signal_id=sid,
                timeframe=tf,
            )

    for event in outcome_events:
        message = outcome_alert(event)
        if not message:
            continue
        record = event["record"]
        sid = record["signal_id"]
        kind = event["type"]
        enqueue(
            delivery,
            key=f"outcome:{sid}:{kind}",
            kind="outcome_text",
            signal_id=sid,
            text=message,
        )


def flush_deliveries(
    delivery: dict[str, Any],
    state: dict[str, Any],
    exchange,
    chart_dir: Path,
) -> dict[str, int]:
    sent = 0
    failed = 0

    for item in pending_items(delivery):
        mark_attempt(item)
        try:
            if item.get("kind") == "signal_chart":
                record = find_record(state, str(item.get("signal_id")))
                if not record:
                    raise RuntimeError("signal record not found for chart delivery")
                ensure_ready_charts(exchange, record, chart_dir)
                timeframe = str(item.get("timeframe"))
                path = (record.get("charts") or {}).get(timeframe)
                if not path:
                    raise RuntimeError(f"chart path unavailable for {timeframe}")
                ok = telegram_send_photo(
                    path,
                    ready_chart_caption(record, timeframe),
                )
            else:
                ok = telegram_send(str(item.get("text") or ""))

            if not ok:
                raise RuntimeError("Telegram delivery returned false")
            mark_sent(item)
            sent += 1
        except Exception as exc:  # noqa: BLE001
            mark_failed(item, exc)
            failed += 1

    return {"sent": sent, "failed": failed}


def run_tracker(args) -> dict[str, Any]:
    observed_at = parse_dt(args.observed_at) if args.observed_at else None
    now = observed_at or utc_now()
    snapshot = load_json(Path(args.snapshot), {"top_opportunities": []})
    state_path = Path(args.state)
    summary_path = Path(args.summary)
    delivery_path = Path(args.delivery_state)
    state = load_json(
        state_path,
        {
            "version": "V3.5",
            "created_at": iso(now),
            "updated_at": iso(now),
            "records": [],
        },
    )
    delivery = load_delivery(delivery_path)

    state["version"] = "V3.5"
    exchange = build_exchange()
    outcome_events: list[dict[str, Any]] = []

    if not args.ingest_only:
        live_now = utc_now()
        for record in state.get("records", []):
            if record.get("tracking_status") in {"PENDING_ENTRY", "ACTIVE"}:
                try:
                    outcome_events.extend(
                        process_ready_record(
                            record,
                            exchange,
                            live_now,
                            args.entry_window_hours,
                            args.max_hold_hours,
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    record.setdefault("notes", []).append(
                        f"tracker_error:{type(exc).__name__}:{str(exc)[:160]}"
                    )

        outcome_events.extend(
            expire_stale_developing(state, live_now, args.developing_stale_hours)
        )

    ingest_events = ingest_snapshot(
        state, snapshot, now, args.cooldown_hours
    )

    chart_records = []
    for record in [*ingest_events["new"], *ingest_events["promoted"]]:
        if record.get("tracking_status") == "PENDING_ENTRY":
            try:
                if ensure_ready_charts(exchange, record, Path(args.chart_dir)):
                    chart_records.append(record)
            except Exception as exc:  # noqa: BLE001
                record.setdefault("notes", []).append(
                    f"chart_error:{type(exc).__name__}:{str(exc)[:160]}"
                )

    enqueue_notifications(delivery, ingest_events, outcome_events)

    state["updated_at"] = iso(utc_now())
    summary = build_summary(state)
    save_json(state_path, state)
    save_json(summary_path, summary)
    save_delivery(delivery_path, delivery)

    send_result = {"sent": 0, "failed": 0}
    if args.telegram:
        send_result = flush_deliveries(
            delivery,
            state,
            exchange,
            Path(args.chart_dir),
        )
        save_json(state_path, state)
        save_delivery(delivery_path, delivery)

    d_stats = delivery_stats(delivery)
    print(
        "OUTCOME_TRACKER:",
        f"unique={summary['unique_setups']}",
        f"ready_unique={summary['ready_unique']}",
        f"completed={summary['completed_trade_outcomes']}",
        f"tp={summary['tp']}",
        f"sl={summary['sl']}",
        f"ambiguous={summary['ambiguous']}",
        f"ingest_only={args.ingest_only}",
    )
    print(
        "INGEST:",
        f"new={len(ingest_events['new'])}",
        f"promoted={len(ingest_events['promoted'])}",
        f"duplicate_suppressed={len(ingest_events['suppressed'])}",
    )
    print(
        "DELIVERY:",
        f"pending={d_stats['pending']}",
        f"sent_total={d_stats['sent']}",
        f"sent_now={send_result['sent']}",
        f"failed_now={send_result['failed']}",
    )

    return {
        "summary": summary,
        "new": len(ingest_events["new"]),
        "promoted": len(ingest_events["promoted"]),
        "suppressed": len(ingest_events["suppressed"]),
        "outcome_events": len(outcome_events),
        "charts_generated": len(chart_records),
        "delivery": d_stats,
        "delivery_attempt": send_result,
    }


def self_test() -> None:
    now = datetime(2026, 10, 2, 7, 0, tzinfo=timezone.utc)
    ready = {
        "rank": 1,
        "symbol": "TEST/USDT:USDT",
        "side": "LONG",
        "status": "READY",
        "setup": "trend_pullback",
        "entry": 100.0,
        "stop": 98.0,
        "target": 106.0,
        "rr": 3.0,
        "score": 90,
        "required_score": 80,
        "required_rr": 2.0,
        "volatility_regime": "NORMAL",
        "range_24h_pct": 5.0,
        "entry_distance_atr": 0.2,
        "opportunity_value": 101.0,
        "atr_expansion": 1.10,
        "obstacle_clearance_r": 1.25,
        "quality_gates": "PASS",
    }
    state = {"version": "V3.5", "records": []}
    first = ingest_snapshot(state, {"top_opportunities": [ready]}, now, 12)
    assert len(first["new"]) == 1
    assert state["records"][0]["tracking_status"] == "PENDING_ENTRY"

    second = ingest_snapshot(
        state,
        {"top_opportunities": [ready]},
        now + timedelta(minutes=15),
        12,
    )
    assert len(second["new"]) == 0
    assert len(state["records"]) == 1

    developing = dict(ready)
    developing.update(
        {
            "symbol": "DEV/USDT:USDT",
            "status": "DEVELOPING",
            "score": 75,
            "rr": 1.8,
        }
    )
    ingest_snapshot(
        state,
        {"top_opportunities": [developing]},
        now,
        12,
    )
    dev_record = next(r for r in state["records"] if r["symbol"].startswith("DEV"))
    assert dev_record["tracking_status"] == "MONITORING"

    developing["status"] = "READY"
    developing["score"] = 82
    developing["rr"] = 2.2
    promoted = ingest_snapshot(
        state,
        {"top_opportunities": [developing]},
        now + timedelta(minutes=30),
        12,
    )
    assert len(promoted["promoted"]) == 1
    assert dev_record["tracking_status"] == "PENDING_ENTRY"
    assert dev_record["entry"] == 100.0

    print("OUTCOME_SELF_TEST_OK")


def main() -> int:
    parser = argparse.ArgumentParser(description="V3.5 deterministic outcome tracker")
    parser.add_argument("--snapshot", default="/freqtrade/user_data/v3_output/latest.json")
    parser.add_argument("--state", default="/freqtrade/user_data/v3_state/outcomes.json")
    parser.add_argument("--summary", default="/freqtrade/user_data/v3_state/outcome_summary.json")
    parser.add_argument(
        "--delivery-state",
        default="/freqtrade/user_data/v3_state/delivery.json",
    )
    parser.add_argument("--chart-dir", default="/freqtrade/user_data/v3_charts")
    parser.add_argument("--entry-window-hours", type=int, default=6)
    parser.add_argument("--max-hold-hours", type=int, default=168)
    parser.add_argument("--developing-stale-hours", type=int, default=12)
    parser.add_argument("--cooldown-hours", type=int, default=12)
    parser.add_argument("--telegram", action="store_true")
    parser.add_argument(
        "--observed-at",
        help="UTC ISO timestamp used for historical catch-up signal discovery",
    )
    parser.add_argument(
        "--ingest-only",
        action="store_true",
        help="Ingest a historical snapshot without updating outcomes",
    )
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return 0

    run_tracker(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())

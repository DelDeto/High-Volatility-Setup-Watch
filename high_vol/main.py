import json
import math
from datetime import datetime, timezone

from .chart import render_setup_chart
from .config import (
    MAX_FULL_SCAN_SYMBOLS,
    MAX_SPREAD_BPS,
    MIN_24H_RANGE_PCT,
    MIN_24H_TURNOVER_USDT,
    REPORT_PATH,
    STATE_PATH,
)
from .correlation import suppress_correlated
from .mexc_market import (
    fetch_many_frames,
    get_all_tickers,
    get_contract_universe,
)
from .notifier import send_telegram
from .outcomes import (
    load_outcomes,
    register_candidates,
    save_outcomes,
    update_outcomes,
)
from .participation import apply_participation_context
from .scanner import analyze_symbol


NON_CRYPTO_MARKERS = (
    "STOCK",
    "USOIL",
    "UKOIL",
    "XAU_",
    "XAUT_",
    "SILVER_",
    "ALUMINUM_",
    "COPPER_",
    "NGAS_",
    "SPY_",
    "SPX500_",
    "NAS100_",
    "US30_",
    "EUR_",
)


def _is_crypto_symbol(symbol):
    return not any(marker in symbol.upper() for marker in NON_CRYPTO_MARKERS)


def _range_pct(ticker):
    if not ticker:
        return None
    high = ticker.get("high_24h")
    low = ticker.get("low_24h")
    if high is None or low in (None, 0):
        return None
    return max(0.0, (float(high) - float(low)) / float(low) * 100.0)


def _turnover(ticker):
    if not ticker:
        return 0.0
    value = ticker.get("turnover_24h")
    if value is not None:
        return max(0.0, float(value))
    volume = ticker.get("volume_24h")
    price = ticker.get("last_price")
    if volume is not None and price is not None:
        return max(0.0, float(volume) * float(price))
    return 0.0


def _quality_ok(ticker):
    if not ticker or ticker.get("last_price") is None:
        return False, "missing_price"

    spread = ticker.get("spread_bps")
    if spread is not None and spread > MAX_SPREAD_BPS:
        return False, "wide_spread"

    return True, None


def _load_state():
    if not STATE_PATH.exists():
        return {"signatures": [], "hold_vol_snapshot": {}}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        data.setdefault("signatures", [])
        data.setdefault("hold_vol_snapshot", {})
        return data
    except Exception:
        return {"signatures": [], "hold_vol_snapshot": {}}


def _alert_eligible(item):
    return (
        item.get("bucket")
        in ("MOMENTUM_READY", "PULLBACK_READY", "HIGH_VOL_DEVELOPING")
        and not item.get("correlation_suppressed", False)
    )


def _signature(item):
    plan = item.get("trade_plan") or {}
    entry = plan.get("entry_zone") or {}
    return "|".join(
        str(value)
        for value in [
            item.get("symbol"),
            item.get("bucket"),
            item.get("direction"),
            round(float(entry.get("lower") or 0), 8),
            round(float(entry.get("upper") or 0), 8),
            round(float(plan.get("stop_loss") or 0), 8),
        ]
    )


def _should_notify(previous, results):
    important = [item for item in results if _alert_eligible(item)]
    if not important:
        return False

    old = set(previous.get("signatures", []))
    new = {_signature(item) for item in important}

    if any(
        item.get("bucket") in ("MOMENTUM_READY", "PULLBACK_READY")
        and _signature(item) not in old
        for item in important
    ):
        return True

    return new != old


def main():
    generated_at = datetime.now(timezone.utc).isoformat()
    previous = _load_state()

    universe = get_contract_universe()
    tickers = get_all_tickers()

    hold_snapshot = apply_participation_context(
        tickers,
        previous_hold_vol=previous.get("hold_vol_snapshot", {}),
    )

    candidates = []
    rejections = {}

    for symbol in universe:
        if not _is_crypto_symbol(symbol):
            rejections[symbol] = "non_crypto"
            continue

        ticker = tickers.get(symbol)
        ok, reason = _quality_ok(ticker)
        if not ok:
            rejections[symbol] = reason
            continue

        turnover = _turnover(ticker)
        if turnover < MIN_24H_TURNOVER_USDT:
            rejections[symbol] = "low_turnover"
            continue

        range_pct = _range_pct(ticker)
        if range_pct is None or range_pct < MIN_24H_RANGE_PCT:
            rejections[symbol] = "range_below_threshold"
            continue

        # Favor both movement and actual tradeability.
        ranking = range_pct * (1.0 + math.log10(max(turnover, 1.0)) / 10.0)
        candidates.append((symbol, ranking, range_pct, turnover))

    candidates.sort(key=lambda row: row[1], reverse=True)

    scan_symbols = [
        row[0] for row in candidates[:MAX_FULL_SCAN_SYMBOLS]
    ]

    print(
        f"Universe={len(universe)} | "
        f"high_vol_candidates={len(candidates)} | "
        f"full_scan={len(scan_symbols)}"
    )

    frames_by_symbol, fetch_errors, insufficient_history = fetch_many_frames(
        scan_symbols,
        workers=6,
    )

    outcomes = load_outcomes()
    outcomes = update_outcomes(outcomes, frames_by_symbol)

    results = []
    analysis_errors = {}

    for symbol in scan_symbols:
        frames = frames_by_symbol.get(symbol)
        if not frames:
            continue
        try:
            item = analyze_symbol(
                symbol,
                frames,
                tickers.get(symbol, {}),
            )
            if item.get("bucket") != "IGNORE":
                results.append(item)
        except Exception as exc:
            analysis_errors[symbol] = str(exc)

    priority = {
        "MOMENTUM_READY": 0,
        "PULLBACK_READY": 1,
        "HIGH_VOL_DEVELOPING": 2,
        "OVEREXTENDED": 3,
    }
    results.sort(
        key=lambda item: (
            priority.get(item.get("bucket"), 9),
            -float(item.get("score", 0)),
            -float((item.get("volatility") or {}).get("range_24h_pct") or 0),
        )
    )

    suppress_correlated(results, frames_by_symbol)

    counts = {
        bucket: sum(item.get("bucket") == bucket for item in results)
        for bucket in (
            "MOMENTUM_READY",
            "PULLBACK_READY",
            "HIGH_VOL_DEVELOPING",
            "OVEREXTENDED",
        )
    }
    alert_counts = {
        bucket: sum(
            item.get("bucket") == bucket
            and not item.get("correlation_suppressed", False)
            for item in results
        )
        for bucket in counts
    }
    suppressed = sum(
        bool(item.get("correlation_suppressed"))
        for item in results
    )

    outcomes = register_candidates(outcomes, results, generated_at)
    save_outcomes(outcomes)

    report = {
        "generated_at_utc": generated_at,
        "exchange": "MEXC",
        "market": "USDT perpetual crypto",
        "engine": "High Volatility PA/SMC V1",
        "universe_count": len(universe),
        "high_vol_candidate_count": len(candidates),
        "full_scan_count": len(scan_symbols),
        "counts": counts,
        "alert_counts": alert_counts,
        "correlation_suppressed_count": suppressed,
        "setups": results,
        "prefilter_rejections": rejections,
        "fetch_error_count": len(fetch_errors),
        "insufficient_history_count": len(insufficient_history),
        "fetch_errors": fetch_errors,
        "insufficient_history": insufficient_history,
        "analysis_errors": analysis_errors,
        "outcome_count": len(outcomes),
    }

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    chart_paths = []
    important = [item for item in results if _alert_eligible(item)]

    for item in important[:8]:
        symbol = item.get("symbol")
        frames = frames_by_symbol.get(symbol)
        if not frames:
            continue
        try:
            path = render_setup_chart(
                symbol,
                frames["15M"],
                item,
            )
            chart_paths.append({
                "symbol": symbol,
                "bucket": item.get("bucket"),
                "score": item.get("score"),
                "direction": item.get("direction"),
                "path": str(path),
            })
        except Exception as exc:
            analysis_errors[f"{symbol}:chart"] = str(exc)

    should_notify = _should_notify(previous, results)
    if should_notify:
        send_telegram(report, chart_paths=chart_paths)
    else:
        print("High Vol Watch: no actionable setup change; Telegram suppressed.")

    STATE_PATH.write_text(
        json.dumps(
            {
                "updated_at_utc": generated_at,
                "signatures": [
                    _signature(item)
                    for item in results
                    if _alert_eligible(item)
                ],
                "counts": counts,
                "alert_counts": alert_counts,
                "hold_vol_snapshot": hold_snapshot,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(
        "High Vol completed: "
        f"MOMENTUM={counts['MOMENTUM_READY']} "
        f"PULLBACK={counts['PULLBACK_READY']} "
        f"DEVELOPING={counts['HIGH_VOL_DEVELOPING']} "
        f"OVEREXTENDED={counts['OVEREXTENDED']} "
        f"INSUFFICIENT_HISTORY={len(insufficient_history)} "
        f"FETCH_ERRORS={len(fetch_errors)} "
        f"SUPPRESSED={suppressed}"
    )


if __name__ == "__main__":
    main()

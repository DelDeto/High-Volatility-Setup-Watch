from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import ccxt
import numpy as np
import pandas as pd
import talib.abstract as ta


BLOCKED_CONTRACT_TYPES = {"stocks", "indices", "commodities", "forex", "metals"}
TF_MS = {"15m": 15 * 60 * 1000, "1h": 60 * 60 * 1000, "4h": 4 * 60 * 60 * 1000}
REGIME_LABEL = {0: "NORMAL", 1: "HIGH", 2: "VERY_HIGH", 3: "EXTREME"}


@dataclass
class Opportunity:
    symbol: str
    side: str
    status: str
    setup: str
    entry: float
    stop: float
    target: float
    rr: float
    score: int
    required_score: int
    required_rr: float
    volatility_regime: str
    volatility_regime_id: int
    range_24h_pct: float
    entry_distance_atr: float
    volume_ratio: float
    opportunity_value: float
    quote_volume: float
    volume_rank: int
    conflict_resolved: bool = False
    rank: int | None = None


def finite(x: Any, default: float = 0.0) -> float:
    try:
        value = float(x)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def price_fmt(value: float) -> str:
    value = finite(value)
    a = abs(value)
    if a >= 1000:
        return f"{value:,.2f}"
    if a >= 1:
        return f"{value:.4f}"
    if a >= 0.01:
        return f"{value:.6f}"
    return f"{value:.8f}"


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


def closed_ohlcv(exchange, symbol: str, timeframe: str, limit: int = 240) -> pd.DataFrame:
    rows = retry(exchange.fetch_ohlcv, symbol, timeframe=timeframe, limit=limit)
    if not rows:
        raise ValueError(f"No OHLCV returned for {symbol} {timeframe}")
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    now_ms = exchange.milliseconds()
    tf_ms = TF_MS[timeframe]
    df = df[(df["timestamp"] + tf_ms) <= (now_ms - 2000)].copy()
    if df.empty:
        raise ValueError(f"No closed candles for {symbol} {timeframe}")
    return df.reset_index(drop=True)


def add_context_indicators(df: pd.DataFrame, zone_window: int) -> pd.DataFrame:
    out = df.copy()
    out["ema20"] = ta.EMA(out, timeperiod=20)
    out["ema50"] = ta.EMA(out, timeperiod=50)
    out["ema200"] = ta.EMA(out, timeperiod=200)
    out["atr"] = ta.ATR(out, timeperiod=14)
    out["demand"] = out["low"].rolling(zone_window, min_periods=zone_window).min().shift(1)
    out["supply"] = out["high"].rolling(zone_window, min_periods=zone_window).max().shift(1)
    out["bull_structure"] = (
        (out["ema20"] > out["ema50"]) & (out["ema50"] > out["ema200"])
    ).astype(int)
    out["bear_structure"] = (
        (out["ema20"] < out["ema50"]) & (out["ema50"] < out["ema200"])
    ).astype(int)
    return out


def fast_scan_features(df_1h: pd.DataFrame) -> dict[str, float]:
    d = add_context_indicators(df_1h, 20)
    d["volume_mean20"] = d["volume"].rolling(20, min_periods=20).mean()
    d["volume_ratio"] = d["volume"] / d["volume_mean20"].replace(0, np.nan)
    d["range_24h"] = (
        d["high"].rolling(24, min_periods=24).max()
        / d["low"].rolling(24, min_periods=24).min().replace(0, np.nan)
        - 1
    )
    row = d.iloc[-1]
    atr = finite(row["atr"], np.nan)
    if not math.isfinite(atr) or atr <= 0:
        raise ValueError("Invalid 1h ATR")

    ema_dist = abs(finite(row["close"]) - finite(row["ema20"])) / atr
    zone_dist = min(
        abs(finite(row["close"]) - finite(row["demand"])) / atr,
        abs(finite(row["supply"]) - finite(row["close"])) / atr,
    )
    structure = max(int(row["bull_structure"]), int(row["bear_structure"]))
    volume_ratio = finite(row["volume_ratio"])
    range_24h = finite(row["range_24h"])

    potential = (
        30 * structure
        + 20 * int(ema_dist <= 1.0)
        + 20 * int(zone_dist <= 1.0)
        + 10 * int(volume_ratio >= 1.10)
        + 10 * int(0.03 <= range_24h <= 0.25)
        + min(range_24h * 40, 10)
    )
    return {
        "potential": float(potential),
        "volume_ratio_1h": volume_ratio,
        "range_24h_1h": range_24h,
        "bull_structure_1h": int(row["bull_structure"]),
        "bear_structure_1h": int(row["bear_structure"]),
    }


def rr_score(rr: float) -> int:
    if rr >= 3.0:
        return 15
    if rr >= 2.5:
        return 12
    if rr >= 2.0:
        return 8
    return 0


def regime_thresholds(regime: int) -> tuple[float, int, float]:
    if regime == 3:
        return 0.30, 90, 3.0
    if regime == 2:
        return 0.45, 85, 2.5
    if regime == 1:
        return 0.60, 82, 2.2
    return 0.75, 80, 2.0


def analyze_symbol(
    symbol: str,
    df_15m: pd.DataFrame,
    df_1h: pd.DataFrame,
    df_4h: pd.DataFrame,
    quote_volume: float,
    volume_rank: int,
) -> list[Opportunity]:
    if len(df_15m) < 210 or len(df_1h) < 210 or len(df_4h) < 210:
        return []

    h1 = add_context_indicators(df_1h, 20)
    h4 = add_context_indicators(df_4h, 24)

    d = df_15m.copy()
    d["ema20"] = ta.EMA(d, timeperiod=20)
    d["ema50"] = ta.EMA(d, timeperiod=50)
    d["atr"] = ta.ATR(d, timeperiod=14)
    d["rsi"] = ta.RSI(d, timeperiod=14)
    d["volume_mean20"] = d["volume"].rolling(20, min_periods=20).mean()
    d["volume_ratio"] = d["volume"] / d["volume_mean20"].replace(0, np.nan)
    d["prior_high20"] = d["high"].rolling(20, min_periods=20).max().shift(1)
    d["prior_low20"] = d["low"].rolling(20, min_periods=20).min().shift(1)
    d["micro_high5"] = d["high"].rolling(5, min_periods=5).max().shift(1)
    d["micro_low5"] = d["low"].rolling(5, min_periods=5).min().shift(1)
    d["range_24h"] = (
        d["high"].rolling(96, min_periods=96).max()
        / d["low"].rolling(96, min_periods=96).min().replace(0, np.nan)
        - 1
    )

    d["sweep_long"] = (d["low"] < d["prior_low20"]) & (d["close"] > d["prior_low20"])
    d["sweep_short"] = (d["high"] > d["prior_high20"]) & (d["close"] < d["prior_high20"])
    d["breakout_long"] = d["close"] > d["prior_high20"]
    d["breakout_short"] = d["close"] < d["prior_low20"]
    d["recent_breakout_long"] = (
        d["breakout_long"].shift(1).rolling(4, min_periods=1).max().fillna(0) > 0
    )
    d["recent_breakout_short"] = (
        d["breakout_short"].shift(1).rolling(4, min_periods=1).max().fillna(0) > 0
    )
    d["reclaim_long"] = (d["close"] > d["ema20"]) & (d["close"].shift(1) <= d["ema20"].shift(1))
    d["reclaim_short"] = (d["close"] < d["ema20"]) & (d["close"].shift(1) >= d["ema20"].shift(1))
    d["ema_touch_long"] = (
        (d["low"] <= d["ema20"]) & (d["close"] > d["ema20"]) & (d["close"] > d["open"])
    )
    d["ema_touch_short"] = (
        (d["high"] >= d["ema20"]) & (d["close"] < d["ema20"]) & (d["close"] < d["open"])
    )
    d["recent_sweep_long"] = d["sweep_long"].rolling(4, min_periods=1).max().fillna(0) > 0
    d["recent_sweep_short"] = d["sweep_short"].rolling(4, min_periods=1).max().fillna(0) > 0
    d["choch_long"] = d["recent_sweep_long"] & (d["close"] > d["micro_high5"]) & (d["close"] > d["ema20"])
    d["choch_short"] = d["recent_sweep_short"] & (d["close"] < d["micro_low5"]) & (d["close"] < d["ema20"])

    row = d.iloc[-1]
    r1 = h1.iloc[-1]
    r4 = h4.iloc[-1]

    required_values = [
        row["close"], row["atr"], row["range_24h"], r1["ema20"], r1["atr"],
        r1["demand"], r1["supply"], r4["demand"], r4["supply"],
    ]
    if not all(math.isfinite(finite(x, np.nan)) for x in required_values):
        return []

    entry = finite(row["close"])
    atr = finite(row["atr"])
    atr_1h = finite(r1["atr"])
    range_24h = finite(row["range_24h"])
    volume_ratio = finite(row["volume_ratio"])

    if range_24h >= 0.25:
        regime = 3
    elif range_24h >= 0.15:
        regime = 2
    elif range_24h >= 0.08:
        regime = 1
    else:
        regime = 0

    max_dist, required_score, required_rr = regime_thresholds(regime)

    demand_1h = finite(r1["demand"])
    supply_1h = finite(r1["supply"])
    ema20_1h = finite(r1["ema20"])
    demand_4h = finite(r4["demand"])
    supply_4h = finite(r4["supply"])

    long_entry_dist = abs(entry - demand_1h) / atr
    short_entry_dist = abs(supply_1h - entry) / atr
    ema_1h_dist = abs(entry - ema20_1h) / atr
    long_pullback_dist = min(long_entry_dist, ema_1h_dist)
    short_pullback_dist = min(short_entry_dist, ema_1h_dist)

    long_zone_stop = demand_1h - 0.20 * atr_1h
    short_zone_stop = supply_1h + 0.20 * atr_1h
    long_stop = min(long_zone_stop, entry - 0.75 * atr)
    short_stop = max(short_zone_stop, entry + 0.75 * atr)

    long_target = supply_1h if supply_1h > entry else supply_4h
    short_target = demand_1h if demand_1h < entry else demand_4h

    long_risk = entry - long_stop
    short_risk = short_stop - entry
    long_rr = (long_target - entry) / long_risk if long_risk > 0 and long_target > entry else float("nan")
    short_rr = (entry - short_target) / short_risk if short_risk > 0 and short_target < entry else float("nan")

    bull_1h = int(r1["bull_structure"]) == 1
    bear_1h = int(r1["bear_structure"]) == 1
    bull_4h = int(r4["bull_structure"]) == 1
    bear_4h = int(r4["bear_structure"]) == 1

    long_location = long_entry_dist <= max_dist
    short_location = short_entry_dist <= max_dist
    long_pullback_location = long_pullback_dist <= max_dist
    short_pullback_location = short_pullback_dist <= max_dist

    recent_breakout_long = bool(row["recent_breakout_long"])
    recent_breakout_short = bool(row["recent_breakout_short"])
    reclaim_long = bool(row["reclaim_long"])
    reclaim_short = bool(row["reclaim_short"])
    ema_touch_long = bool(row["ema_touch_long"])
    ema_touch_short = bool(row["ema_touch_short"])
    choch_long = bool(row["choch_long"])
    choch_short = bool(row["choch_short"])

    prior_high20 = finite(row["prior_high20"])
    prior_low20 = finite(row["prior_low20"])

    long_breakout_retest = (
        recent_breakout_long
        and bull_1h
        and abs(entry - prior_high20) / atr <= 0.55
        and entry >= prior_high20
        and volume_ratio >= 1.10
    )
    short_breakout_retest = (
        recent_breakout_short
        and bear_1h
        and abs(entry - prior_low20) / atr <= 0.55
        and entry <= prior_low20
        and volume_ratio >= 1.10
    )

    long_trend_pullback = bull_4h and bull_1h and long_pullback_location and (reclaim_long or ema_touch_long)
    short_trend_pullback = bear_4h and bear_1h and short_pullback_location and (reclaim_short or ema_touch_short)

    long_reversal = (
        choch_long
        and not bear_4h
        and long_location
        and volume_ratio >= 1.20
        and finite(row["rsi"]) <= 58
    )
    short_reversal = (
        choch_short
        and not bull_4h
        and short_location
        and volume_ratio >= 1.25
        and finite(row["rsi"]) >= 42
    )

    long_high_vol = (
        regime >= 1
        and bull_1h
        and recent_breakout_long
        and entry > finite(row["ema20"])
        and volume_ratio >= 1.40
        and abs(entry - prior_high20) / atr <= 0.70
    )
    short_high_vol = (
        regime >= 1
        and bear_1h
        and recent_breakout_short
        and entry < finite(row["ema20"])
        and volume_ratio >= 1.40
        and abs(entry - prior_low20) / atr <= 0.70
    )

    long_setup_name = next(
        (name for ok, name in [
            (long_trend_pullback, "trend_pullback"),
            (long_breakout_retest, "breakout_retest"),
            (long_reversal, "sweep_reversal"),
            (long_high_vol, "high_vol_continuation"),
        ] if ok),
        "none",
    )
    short_setup_name = next(
        (name for ok, name in [
            (short_trend_pullback, "trend_pullback"),
            (short_breakout_retest, "breakout_retest"),
            (short_reversal, "sweep_reversal"),
            (short_high_vol, "high_vol_continuation"),
        ] if ok),
        "none",
    )

    long_confirmation = reclaim_long or ema_touch_long or choch_long
    short_confirmation = reclaim_short or ema_touch_short or choch_short
    long_location_quality = long_location or long_pullback_location or long_breakout_retest
    short_location_quality = short_location or short_pullback_location or short_breakout_retest

    long_score = (
        20 * int(bull_4h)
        + 10 * int(bull_1h)
        + 20 * int(long_location_quality)
        + 20 * int(long_confirmation or long_breakout_retest)
        + 10 * int(volume_ratio >= 1.20)
        + rr_score(long_rr)
        + 5 * int(min(long_entry_dist, long_pullback_dist) <= 0.35)
    )
    short_score = (
        20 * int(bear_4h)
        + 10 * int(bear_1h)
        + 20 * int(short_location_quality)
        + 20 * int(short_confirmation or short_breakout_retest)
        + 10 * int(volume_ratio >= 1.20)
        + rr_score(short_rr)
        + 5 * int(min(short_entry_dist, short_pullback_dist) <= 0.35)
    )

    long_req_score = max(required_score, 90 if long_setup_name == "sweep_reversal" else required_score)
    short_req_score = max(required_score, 92 if short_setup_name == "sweep_reversal" else required_score)
    long_req_rr = max(required_rr, 2.5 if long_setup_name == "sweep_reversal" else required_rr)
    short_req_rr = max(required_rr, 2.8 if short_setup_name == "sweep_reversal" else required_rr)

    opportunities: list[Opportunity] = []

    def build(
        side: str,
        setup: str,
        score: int,
        req_score: int,
        rr: float,
        req_rr: float,
        stop: float,
        target: float,
        location_quality: bool,
        distance: float,
    ) -> None:
        if setup == "none" or not math.isfinite(rr) or not location_quality:
            return
        if score >= req_score and rr >= req_rr:
            status = "READY"
        elif score >= req_score - 8 and rr >= req_rr * 0.80:
            status = "DEVELOPING"
        else:
            return

        opportunity_value = (
            score
            + min(max(rr, 0), 5) * 4
            - min(max(distance, 0), 2) * 5
        )
        opportunities.append(
            Opportunity(
                symbol=symbol,
                side=side,
                status=status,
                setup=setup,
                entry=entry,
                stop=stop,
                target=target,
                rr=rr,
                score=int(score),
                required_score=int(req_score),
                required_rr=float(req_rr),
                volatility_regime=REGIME_LABEL[regime],
                volatility_regime_id=regime,
                range_24h_pct=range_24h * 100,
                entry_distance_atr=distance,
                volume_ratio=volume_ratio,
                opportunity_value=opportunity_value,
                quote_volume=quote_volume,
                volume_rank=volume_rank,
            )
        )

    build(
        "LONG", long_setup_name, long_score, long_req_score, long_rr, long_req_rr,
        long_stop, long_target, long_location_quality, min(long_entry_dist, long_pullback_dist)
    )
    build(
        "SHORT", short_setup_name, short_score, short_req_score, short_rr, short_req_rr,
        short_stop, short_target, short_location_quality, min(short_entry_dist, short_pullback_dist)
    )
    return opportunities


def market_quote_volume(ticker: dict[str, Any]) -> float:
    qv = finite(ticker.get("quoteVolume"))
    if qv > 0:
        return qv
    info = ticker.get("info") or {}
    for key in ("volume_24h_quote", "volume_24h_usd", "quote_volume", "volume_usd"):
        value = finite(info.get(key))
        if value > 0:
            return value
    base = finite(ticker.get("baseVolume"))
    last = finite(ticker.get("last"))
    return base * last if base > 0 and last > 0 else 0.0


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


def eligible_markets(exchange) -> list[str]:
    out = []
    for symbol, market in exchange.markets.items():
        if market.get("active") is False:
            continue
        if not market.get("swap"):
            continue
        if market.get("quote") != "USDT":
            continue
        if not market.get("linear", True):
            continue
        info = market.get("info") or {}
        contract_type = str(info.get("contract_type") or "").lower()
        if contract_type in BLOCKED_CONTRACT_TYPES:
            continue
        out.append(symbol)
    return out


def choose_deep_candidates(
    top_symbols: list[str],
    fast_rows: dict[str, dict[str, float]],
    deep_limit: int,
) -> list[str]:
    if deep_limit >= len(top_symbols):
        return top_symbols
    guaranteed = top_symbols[: min(40, deep_limit)]
    remaining = [s for s in top_symbols if s not in guaranteed]
    ranked = sorted(
        remaining,
        key=lambda s: (
            fast_rows.get(s, {}).get("potential", -1),
            -top_symbols.index(s),
        ),
        reverse=True,
    )
    need = max(0, deep_limit - len(guaranteed))
    return guaranteed + ranked[:need]


def resolve_direction_conflicts(opportunities: list[Opportunity]) -> tuple[list[Opportunity], int]:
    by_symbol: dict[str, list[Opportunity]] = {}
    for item in opportunities:
        by_symbol.setdefault(item.symbol, []).append(item)

    resolved: list[Opportunity] = []
    conflicts = 0
    for symbol, items in by_symbol.items():
        if len(items) == 1:
            resolved.append(items[0])
            continue
        items.sort(
            key=lambda x: (
                2 if x.status == "READY" else 1,
                x.opportunity_value,
                x.rr,
            ),
            reverse=True,
        )
        first, second = items[0], items[1]
        if first.status == second.status and abs(first.opportunity_value - second.opportunity_value) < 5:
            conflicts += 1
            continue
        first.conflict_resolved = True
        resolved.append(first)
        conflicts += 1
    return resolved, conflicts


def rank_opportunities(opportunities: list[Opportunity], top_n: int) -> list[Opportunity]:
    ranked = sorted(
        opportunities,
        key=lambda x: (
            2 if x.status == "READY" else 1,
            x.opportunity_value,
            x.rr,
            x.score,
            -x.volume_rank,
        ),
        reverse=True,
    )[:top_n]
    for idx, item in enumerate(ranked, start=1):
        item.rank = idx
    return ranked


def report_text(snapshot: dict[str, Any]) -> str:
    lines = [
        "# Market Opportunity Scanner V3",
        "",
        f"Generated: {snapshot['generated_at']}",
        f"Exchange: Gate USDT perpetual",
        f"Universe: {snapshot['universe_scanned']} liquid pairs",
        f"Deep scan: {snapshot['deep_scanned']} pairs",
        f"READY: {snapshot['ready_count']} | DEVELOPING: {snapshot['developing_count']}",
        f"Failures: {snapshot['failure_count']} | Direction conflicts: {snapshot['direction_conflicts']}",
        "",
    ]
    items = snapshot["top_opportunities"]
    if not items:
        lines.append("No READY/DEVELOPING setups passed the V3 ranking gates.")
        return "\n".join(lines)

    lines.append("## Top opportunities")
    lines.append("")
    for item in items:
        lines.extend(
            [
                f"### #{item['rank']} {item['symbol']} {item['side']} — {item['status']}",
                f"- Setup: {item['setup']}",
                f"- Entry: {price_fmt(item['entry'])}",
                f"- SL: {price_fmt(item['stop'])}",
                f"- TP: {price_fmt(item['target'])}",
                f"- RR: {item['rr']:.2f}R",
                f"- Score: {item['score']} / required {item['required_score']}",
                f"- Volatility: {item['volatility_regime']} ({item['range_24h_pct']:.1f}% / 24h)",
                f"- Entry distance: {item['entry_distance_atr']:.2f} ATR",
                f"- Opportunity value: {item['opportunity_value']:.1f}",
                "",
            ]
        )
    return "\n".join(lines)


def telegram_text(snapshot: dict[str, Any]) -> str:
    items = snapshot["top_opportunities"]
    if not items:
        return ""
    lines = [
        "🚀 MARKET OPPORTUNITY V3",
        f"Gate | READY {snapshot['ready_count']} | DEV {snapshot['developing_count']}",
        "",
    ]
    for item in items:
        icon = "🟢" if item["status"] == "READY" else "🟡"
        lines.extend(
            [
                f"{icon} #{item['rank']} {item['symbol']} {item['side']} | {item['setup']}",
                f"E {price_fmt(item['entry'])} | SL {price_fmt(item['stop'])} | TP {price_fmt(item['target'])}",
                f"RR {item['rr']:.2f}R | S {item['score']} | {item['volatility_regime']} | OV {item['opportunity_value']:.1f}",
                "",
            ]
        )
    return "\n".join(lines).strip()


def telegram_send(text: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("TELEGRAM_SKIPPED: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing")
        return False
    if not text:
        print("TELEGRAM_SKIPPED: no READY/DEVELOPING opportunities")
        return False

    chunks: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        candidate = block if not current else current + "\n\n" + block
        if len(candidate) <= 3800:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = block
    if current:
        chunks.append(current)

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chunk in chunks:
        payload = urllib.parse.urlencode(
            {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": "true"}
        ).encode()
        req = urllib.request.Request(url, data=payload, method="POST")
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
            body = json.loads(resp.read().decode("utf-8"))
            if not body.get("ok"):
                raise RuntimeError(f"Telegram send failed: {body}")
    print(f"TELEGRAM_SENT: {len(chunks)} message(s)")
    return True


def run_scan(args) -> dict[str, Any]:
    exchange = build_exchange()
    markets = eligible_markets(exchange)
    market_set = set(markets)

    tickers = retry(exchange.fetch_tickers)
    volume_rows = []
    for symbol, ticker in tickers.items():
        if symbol not in market_set:
            continue
        qv = market_quote_volume(ticker)
        if qv <= 0:
            continue
        volume_rows.append((symbol, qv))
    volume_rows.sort(key=lambda x: x[1], reverse=True)
    selected = volume_rows[: args.universe]
    top_symbols = [x[0] for x in selected]
    quote_volume = {symbol: qv for symbol, qv in selected}
    volume_rank = {symbol: idx for idx, symbol in enumerate(top_symbols, start=1)}

    print(f"UNIVERSE: {len(markets)} eligible Gate crypto perpetuals")
    print(f"LIQUID_UNIVERSE: {len(top_symbols)}")

    fast_rows: dict[str, dict[str, float]] = {}
    cached_1h: dict[str, pd.DataFrame] = {}
    failures: list[dict[str, str]] = []

    for idx, symbol in enumerate(top_symbols, start=1):
        try:
            df_1h = closed_ohlcv(exchange, symbol, "1h", 240)
            cached_1h[symbol] = df_1h
            fast_rows[symbol] = fast_scan_features(df_1h)
        except Exception as exc:  # noqa: BLE001
            failures.append({"symbol": symbol, "stage": "fast_1h", "error": str(exc)[:240]})
        if idx % 25 == 0:
            print(f"FAST_SCAN: {idx}/{len(top_symbols)}")

    deep_symbols = choose_deep_candidates(
        [s for s in top_symbols if s in cached_1h],
        fast_rows,
        args.deep_limit,
    )
    print(f"DEEP_UNIVERSE: {len(deep_symbols)}")

    raw_opportunities: list[Opportunity] = []
    for idx, symbol in enumerate(deep_symbols, start=1):
        try:
            df_15m = closed_ohlcv(exchange, symbol, "15m", 240)
            df_4h = closed_ohlcv(exchange, symbol, "4h", 240)
            raw_opportunities.extend(
                analyze_symbol(
                    symbol,
                    df_15m,
                    cached_1h[symbol],
                    df_4h,
                    quote_volume.get(symbol, 0.0),
                    volume_rank.get(symbol, 9999),
                )
            )
        except Exception as exc:  # noqa: BLE001
            failures.append({"symbol": symbol, "stage": "deep", "error": str(exc)[:240]})
        if idx % 20 == 0:
            print(f"DEEP_SCAN: {idx}/{len(deep_symbols)}")

    resolved, conflict_count = resolve_direction_conflicts(raw_opportunities)
    top = rank_opportunities(resolved, args.top_n)

    ready_count = sum(1 for x in resolved if x.status == "READY")
    developing_count = sum(1 for x in resolved if x.status == "DEVELOPING")
    snapshot = {
        "version": "V3",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "exchange": "gate",
        "market": "USDT perpetual",
        "eligible_market_count": len(markets),
        "universe_scanned": len(top_symbols),
        "fast_scan_success": len(cached_1h),
        "deep_scanned": len(deep_symbols),
        "ready_count": ready_count,
        "developing_count": developing_count,
        "direction_conflicts": conflict_count,
        "failure_count": len(failures),
        "top_opportunities": [asdict(x) for x in top],
        "all_opportunities": [asdict(x) for x in resolved],
        "failures": failures,
    }
    return snapshot


def self_test() -> None:
    items = [
        Opportunity("A/USDT:USDT", "LONG", "DEVELOPING", "trend_pullback", 1, .9, 1.3, 3, 88, 90, 2.5, "NORMAL", 0, 5, .2, 1.2, 99, 10_000, 1),
        Opportunity("B/USDT:USDT", "SHORT", "READY", "trend_pullback", 1, 1.1, .7, 3, 85, 82, 2.2, "HIGH", 1, 10, .3, 1.5, 96, 9_000, 2),
        Opportunity("C/USDT:USDT", "LONG", "READY", "breakout_retest", 1, .9, 1.4, 4, 90, 85, 2.5, "VERY_HIGH", 2, 17, .1, 1.6, 105, 8_000, 3),
    ]
    ranked = rank_opportunities(items, 3)
    assert ranked[0].symbol == "C/USDT:USDT"
    assert ranked[1].symbol == "B/USDT:USDT"
    assert ranked[2].status == "DEVELOPING"
    print("SELF_TEST_OK")


def main() -> int:
    parser = argparse.ArgumentParser(description="V3 cross-market Gate opportunity ranker")
    parser.add_argument("--universe", type=int, default=150)
    parser.add_argument("--deep-limit", type=int, default=80)
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--output-dir", default="/freqtrade/user_data/v3_output")
    parser.add_argument("--telegram", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return 0

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    snapshot = run_scan(args)
    json_path = output_dir / "latest.json"
    md_path = output_dir / "latest.md"

    json_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(report_text(snapshot), encoding="utf-8")

    print(report_text(snapshot))
    if args.telegram:
        telegram_send(telegram_text(snapshot))
    return 0


if __name__ == "__main__":
    sys.exit(main())

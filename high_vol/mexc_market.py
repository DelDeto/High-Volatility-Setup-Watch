import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests

from .config import (
    BASE_URL,
    HISTORY_LIMIT,
    MIN_HISTORY_REQUIRED,
    QUOTE_COIN,
)

INTERVAL_MAP = {
    "15m": "Min15",
    "1h": "Min60",
    "4h": "Hour4",
}

INTERVAL_SECONDS = {
    "15m": 15 * 60,
    "1h": 60 * 60,
    "4h": 4 * 60 * 60,
}

# MEXC futures kline endpoints are sensitive to burst traffic. All workers
# share this limiter so concurrent symbol fetches do not create a request
# storm. ~4 requests/sec keeps the scan well within the workflow timeout.
_REQUEST_LOCK = threading.Lock()
_LAST_REQUEST_AT = 0.0
_MIN_REQUEST_INTERVAL = 0.25


class InsufficientHistoryError(RuntimeError):
    def __init__(self, symbol, interval, available, required):
        self.symbol = symbol
        self.interval = interval
        self.available = int(available)
        self.required = int(required)
        super().__init__(
            f"{symbol} {interval}: only {available} closed candles; "
            f"need {required}"
        )


def _throttle():
    global _LAST_REQUEST_AT
    with _REQUEST_LOCK:
        now = time.monotonic()
        wait = _MIN_REQUEST_INTERVAL - (now - _LAST_REQUEST_AT)
        if wait > 0:
            time.sleep(wait)
        _LAST_REQUEST_AT = time.monotonic()


def _get_json(path, params=None, timeout=15, retries=4):
    last_error = None

    for attempt in range(retries + 1):
        _throttle()

        try:
            response = requests.get(
                BASE_URL + path,
                params=params,
                timeout=timeout,
            )
            response.raise_for_status()
            payload = response.json()

            if isinstance(payload, dict) and payload.get("success") is False:
                code = payload.get("code")
                message = str(payload.get("message") or "")

                # MEXC commonly returns code 510 for burst/rate-limit events.
                if (
                    code == 510
                    or "too frequent" in message.lower()
                ) and attempt < retries:
                    time.sleep(1.0 * (2 ** attempt))
                    continue

                raise RuntimeError(
                    f"MEXC API error for {path}: {payload}"
                )

            return payload

        except requests.RequestException as exc:
            last_error = exc
            if attempt >= retries:
                raise
            time.sleep(1.0 * (2 ** attempt))

    raise RuntimeError(
        f"MEXC request failed for {path}: {last_error}"
    )


def get_contract_universe():
    """Return tradable USDT futures symbols from MEXC contract detail."""
    payload = _get_json("/api/v1/contract/detail")
    rows = payload.get("data", []) if isinstance(payload, dict) else []

    symbols = []
    for row in rows:
        symbol = row.get("symbol")
        quote = (
            row.get("quoteCoin")
            or row.get("settleCoin")
            or ""
        )

        if not symbol or str(quote).upper() != QUOTE_COIN:
            continue

        state = row.get("state")
        if state in (2, 3, 4, "2", "3", "4"):
            continue

        symbols.append(symbol)

    return sorted(set(symbols))


def get_all_tickers():
    payload = _get_json("/api/v1/contract/ticker")
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    if isinstance(rows, dict):
        rows = [rows]

    output = {}
    for row in rows:
        symbol = row.get("symbol")
        if not symbol:
            continue

        def f(*keys):
            for key in keys:
                value = row.get(key)
                if value not in (None, ""):
                    try:
                        return float(value)
                    except (TypeError, ValueError):
                        pass
            return None

        bid = f("bid1", "bid1Price", "bidPrice")
        ask = f("ask1", "ask1Price", "askPrice")
        last = f("lastPrice", "last_price")

        spread_bps = None
        if bid is not None and ask is not None and bid > 0 and ask >= bid:
            mid = (bid + ask) / 2
            if mid > 0:
                spread_bps = (ask - bid) / mid * 10_000

        output[symbol] = {
            "symbol": symbol,
            "last_price": last,
            "bid": bid,
            "ask": ask,
            "spread_bps": spread_bps,
            "high_24h": f("high24Price", "high24h"),
            "low_24h": f("lower24Price", "low24h"),
            "change_rate_24h": f("riseFallRate", "changeRate"),
            "hold_vol": f("holdVol", "hold_volume"),
            "funding_rate": f("fundingRate", "funding_rate"),
            "turnover_24h": f(
                "amount24",
                "turnover24",
                "turnover24h",
                "amount24h",
            ),
            "volume_24h": f("volume24", "volume24h", "volume"),
        }

    return output


def _parse_kline(payload):
    data = payload.get("data", {}) if isinstance(payload, dict) else {}

    if isinstance(data, list):
        rows = []
        for item in data:
            if isinstance(item, dict):
                rows.append(item)
            elif isinstance(item, (list, tuple)) and len(item) >= 6:
                rows.append({
                    "time": item[0],
                    "open": item[1],
                    "high": item[2],
                    "low": item[3],
                    "close": item[4],
                    "vol": item[5],
                })
        frame = pd.DataFrame(rows)
    else:
        times = data.get("time", [])
        frame = pd.DataFrame({
            "time": times,
            "open": data.get("open", []),
            "high": data.get("high", []),
            "low": data.get("low", []),
            "close": data.get("close", []),
            "volume": data.get("vol", data.get("volume", [])),
        })

    if frame.empty:
        return frame

    if "volume" not in frame.columns and "vol" in frame.columns:
        frame["volume"] = frame["vol"]

    frame["time"] = pd.to_datetime(
        frame["time"].astype(float),
        unit="s",
        utc=True,
    )

    for column in ["open", "high", "low", "close", "volume"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    frame = (
        frame[["time", "open", "high", "low", "close", "volume"]]
        .dropna()
        .drop_duplicates(subset=["time"])
        .sort_values("time")
        .set_index("time")
    )

    return frame


def get_closed_klines(symbol, interval, limit=HISTORY_LIMIT):
    if interval not in INTERVAL_MAP:
        raise ValueError(f"Unsupported interval: {interval}")

    now_seconds = int(time.time())
    request_limit = limit + 5
    end = now_seconds
    start = end - INTERVAL_SECONDS[interval] * (request_limit + 4)

    payload = _get_json(
        f"/api/v1/contract/kline/{symbol}",
        params={
            "interval": INTERVAL_MAP[interval],
            "start": start,
            "end": end,
        },
    )

    frame = _parse_kline(payload)
    if frame.empty:
        raise RuntimeError(f"No kline data for {symbol} {interval}")

    now = pd.Timestamp.now(tz="UTC")
    delta = pd.to_timedelta(INTERVAL_SECONDS[interval], unit="s")
    frame = frame.loc[(frame.index + delta) <= now].tail(limit)

    if len(frame) < MIN_HISTORY_REQUIRED:
        raise InsufficientHistoryError(
            symbol,
            interval,
            len(frame),
            MIN_HISTORY_REQUIRED,
        )

    return frame


def fetch_symbol_frames(symbol):
    return {
        "4H": get_closed_klines(symbol, "4h"),
        "1H": get_closed_klines(symbol, "1h"),
        "15M": get_closed_klines(symbol, "15m"),
    }


def fetch_many_frames(symbols, workers=6):
    output = {}
    errors = {}
    insufficient_history = {}

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(fetch_symbol_frames, symbol): symbol
            for symbol in symbols
        }
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                output[symbol] = future.result()
            except InsufficientHistoryError as exc:
                insufficient_history[symbol] = {
                    "status": "INSUFFICIENT_HISTORY",
                    "interval": exc.interval,
                    "available": exc.available,
                    "required": exc.required,
                }
            except Exception as exc:
                errors[symbol] = str(exc)

    return output, errors, insufficient_history

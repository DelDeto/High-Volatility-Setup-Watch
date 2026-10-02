from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import ccxt
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import numpy as np
import pandas as pd
import talib.abstract as ta


TF_MS = {"1h": 60 * 60 * 1000, "4h": 4 * 60 * 60 * 1000}


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


def closed_ohlcv(exchange, symbol: str, timeframe: str, limit: int = 180) -> pd.DataFrame:
    rows = retry(exchange.fetch_ohlcv, symbol, timeframe=timeframe, limit=limit)
    if not rows:
        raise ValueError(f"No OHLCV for {symbol} {timeframe}")
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    now_ms = exchange.milliseconds()
    df = df[(df["timestamp"] + TF_MS[timeframe]) <= now_ms - 2000].copy()
    if len(df) < 60:
        raise ValueError(f"Insufficient closed candles for {symbol} {timeframe}")
    df["date"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df.reset_index(drop=True)


def add_chart_indicators(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    out = df.copy()
    out["ema20"] = ta.EMA(out, timeperiod=20)
    out["ema50"] = ta.EMA(out, timeperiod=50)
    out["ema200"] = ta.EMA(out, timeperiod=200)
    zone_window = 20 if timeframe == "1h" else 24
    out["demand"] = out["low"].rolling(zone_window, min_periods=zone_window).min().shift(1)
    out["supply"] = out["high"].rolling(zone_window, min_periods=zone_window).max().shift(1)
    return out


def _draw_candles(ax, df: pd.DataFrame) -> None:
    width = 0.62
    for i, row in df.iterrows():
        o, h, l, c = (finite(row[x]) for x in ("open", "high", "low", "close"))
        up = c >= o
        color = "#16a34a" if up else "#dc2626"
        ax.vlines(i, l, h, color=color, linewidth=0.8, alpha=0.9)
        body_low = min(o, c)
        body_h = abs(c - o)
        if body_h == 0:
            body_h = max(abs(c) * 0.0002, 1e-10)
        ax.add_patch(
            Rectangle(
                (i - width / 2, body_low),
                width,
                body_h,
                facecolor=color,
                edgecolor=color,
                linewidth=0.6,
                alpha=0.88,
            )
        )


def _safe_level(ax, value: Any, label: str, color: str, linestyle: str = "--") -> None:
    level = finite(value, float("nan"))
    if math.isfinite(level) and level > 0:
        ax.axhline(level, color=color, linestyle=linestyle, linewidth=1.35, alpha=0.95)
        ax.text(
            1.002,
            level,
            f" {label} {level:.8g}",
            transform=ax.get_yaxis_transform(),
            va="center",
            fontsize=8,
            color=color,
        )


def generate_chart(
    exchange,
    record: dict[str, Any],
    timeframe: str,
    output_path: Path,
    candles: int = 120,
) -> Path:
    raw = closed_ohlcv(exchange, record["symbol"], timeframe, max(candles + 220, 340))
    df = add_chart_indicators(raw, timeframe).tail(candles).reset_index(drop=True)

    fig, ax = plt.subplots(figsize=(13.5, 7.5))
    _draw_candles(ax, df)

    x = np.arange(len(df))
    ax.plot(x, df["ema20"], linewidth=1.05, label="EMA20")
    ax.plot(x, df["ema50"], linewidth=1.05, label="EMA50")
    ax.plot(x, df["ema200"], linewidth=1.05, label="EMA200")

    last = df.iloc[-1]
    _safe_level(ax, last.get("demand"), "Demand", "#2563eb", ":")
    _safe_level(ax, last.get("supply"), "Supply", "#9333ea", ":")
    _safe_level(ax, record.get("entry"), "ENTRY", "#111827", "--")
    _safe_level(ax, record.get("stop"), "SL", "#dc2626", "--")
    _safe_level(ax, record.get("target"), "TP", "#16a34a", "--")

    side = record.get("side", "")
    setup = str(record.get("setup", "")).replace("_", " ").title()
    rr = finite(record.get("rr"))
    score = record.get("score")
    regime = record.get("volatility_regime")
    signal_id = record.get("signal_id", "")

    ax.set_title(
        f"{record['symbol']} | {side} | {setup} | {timeframe.upper()}\n"
        f"RR {rr:.2f}R | Score {score} | {regime} | ID {signal_id}",
        fontsize=12,
        fontweight="bold",
    )
    ax.set_ylabel("Price")
    ax.grid(True, alpha=0.16)
    ax.legend(loc="upper left", ncol=3, fontsize=8)

    tick_count = min(8, len(df))
    tick_idx = np.linspace(0, len(df) - 1, tick_count, dtype=int)
    tick_labels = [
        df.iloc[i]["date"].strftime("%m-%d\n%H:%M")
        for i in tick_idx
    ]
    ax.set_xticks(tick_idx)
    ax.set_xticklabels(tick_labels, fontsize=8)

    entry = finite(record.get("entry"))
    target = finite(record.get("target"))
    if entry > 0 and target > 0:
        arrow_start = max(2, len(df) - 14)
        arrow_end = len(df) - 3
        ax.annotate(
            "",
            xy=(arrow_end, target),
            xytext=(arrow_start, entry),
            arrowprops={"arrowstyle": "->", "linewidth": 1.8},
        )

    fig.text(
        0.01,
        0.012,
        "Context chart generated automatically from closed Gate candles. Not a trade instruction.",
        fontsize=8,
        alpha=0.7,
    )
    fig.tight_layout(rect=(0, 0.03, 0.94, 1))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=140, bbox_inches="tight")
    plt.close(fig)
    return output_path


def generate_ready_charts(
    exchange,
    record: dict[str, Any],
    output_dir: Path,
) -> dict[str, str]:
    signal_id = record["signal_id"]
    symbol_slug = str(record["symbol"]).replace("/", "_").replace(":", "_")
    folder = output_dir / f"{signal_id}_{symbol_slug}"
    one_h = generate_chart(exchange, record, "1h", folder / "1h.png")
    four_h = generate_chart(exchange, record, "4h", folder / "4h.png")
    return {"1h": str(one_h), "4h": str(four_h)}


def _multipart_body(fields: dict[str, str], file_field: str, file_path: Path) -> tuple[bytes, str]:
    boundary = "----V32ChartBoundary7MA4YWxkTrZu0gW"
    chunks: list[bytes] = []
    for key, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(),
                str(value).encode("utf-8"),
                b"\r\n",
            ]
        )
    data = file_path.read_bytes()
    chunks.extend(
        [
            f"--{boundary}\r\n".encode(),
            (
                f'Content-Disposition: form-data; name="{file_field}"; '
                f'filename="{file_path.name}"\r\n'
            ).encode(),
            b"Content-Type: image/png\r\n\r\n",
            data,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    return b"".join(chunks), boundary


def telegram_send_photo(photo_path: str, caption: str = "") -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    path = Path(photo_path)
    if not token or not chat_id or not path.exists():
        return False

    body, boundary = _multipart_body(
        {"chat_id": chat_id, "caption": caption[:1000]},
        "photo",
        path,
    )
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendPhoto",
        data=body,
        method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        result = json.loads(resp.read().decode("utf-8"))
        if not result.get("ok"):
            raise RuntimeError(f"Telegram sendPhoto failed: {result}")
    return True


def self_test(output_dir: Path) -> None:
    dates = pd.date_range("2026-01-01", periods=340, freq="h", tz="UTC")
    base = np.linspace(90, 110, len(dates))
    df = pd.DataFrame(
        {
            "timestamp": (dates.astype("int64") // 10**6),
            "open": base,
            "high": base + 1.2,
            "low": base - 1.1,
            "close": base + np.sin(np.arange(len(base)) / 5) * 0.5,
            "volume": np.linspace(1000, 1500, len(base)),
            "date": dates,
        }
    )

    class FakeExchange:
        def milliseconds(self):
            return int(datetime(2026, 2, 1, tzinfo=timezone.utc).timestamp() * 1000)

        def fetch_ohlcv(self, symbol, timeframe="1h", limit=180):
            ms = TF_MS[timeframe]
            end = self.milliseconds() - ms
            rows = []
            price = 100.0
            for i in range(limit):
                ts = end - (limit - i) * ms
                drift = i * 0.03
                o = price + drift
                c = o + math.sin(i / 5) * 0.4
                rows.append([ts, o, max(o, c) + 0.7, min(o, c) - 0.7, c, 1000 + i])
            return rows

    record = {
        "signal_id": "selftest12345678",
        "symbol": "TEST/USDT:USDT",
        "side": "LONG",
        "setup": "trend_pullback",
        "entry": 105.0,
        "stop": 102.0,
        "target": 111.0,
        "rr": 2.0,
        "score": 88,
        "volatility_regime": "NORMAL",
    }
    charts = generate_ready_charts(FakeExchange(), record, output_dir)
    assert Path(charts["1h"]).exists()
    assert Path(charts["4h"]).exists()
    assert Path(charts["1h"]).stat().st_size > 1000
    assert Path(charts["4h"]).stat().st_size > 1000
    print("CHART_SELF_TEST_OK")


def main() -> int:
    parser = argparse.ArgumentParser(description="V3.2 READY setup chart generator")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--output-dir", default="/freqtrade/user_data/v3_charts")
    args = parser.parse_args()
    if args.self_test:
        self_test(Path(args.output_dir))
        return 0
    print("Use chart_generator through outcome_tracker for READY setups.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

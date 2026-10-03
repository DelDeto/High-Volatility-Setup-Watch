from __future__ import annotations

import argparse
import importlib.util
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
import numpy as np
import pandas as pd


TF_MS = {"1h": 60 * 60 * 1000, "4h": 4 * 60 * 60 * 1000}

# Market-Setup-Watch visual system.
BG = "#0B0E11"
PANEL = "#11161D"
GRID = "#20262E"
TEXT = "#EAECEF"
MUTED = "#8A94A6"
GREEN = "#0ECB81"
RED = "#F6465D"
YELLOW = "#F0B90B"
BLUE = "#2B7FFF"
WHITE = "#D8DEE9"

CHART_STYLE_VERSION = "market-scan-v1"


def finite(value: Any, default: float = 0.0) -> float:
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def price_fmt(value: Any) -> str:
    x = finite(value)
    a = abs(x)
    if a >= 1000:
        return f"{x:,.2f}"
    if a >= 1:
        return f"{x:.4f}".rstrip("0").rstrip(".")
    if a >= 0.01:
        return f"{x:.6f}".rstrip("0").rstrip(".")
    return f"{x:.8f}".rstrip("0").rstrip(".")


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


def closed_ohlcv(
    exchange,
    symbol: str,
    timeframe: str,
    limit: int = 180,
    as_of_ms: int | None = None,
) -> pd.DataFrame:
    rows = retry(exchange.fetch_ohlcv, symbol, timeframe=timeframe, limit=limit)
    if not rows:
        raise ValueError(f"No OHLCV for {symbol} {timeframe}")
    df = pd.DataFrame(
        rows,
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )
    cutoff_ms = (
        as_of_ms if as_of_ms is not None else exchange.milliseconds() - 2000
    )
    df = df[(df["timestamp"] + TF_MS[timeframe]) <= cutoff_ms].copy()
    if len(df) < 60:
        raise ValueError(f"Insufficient closed candles for {symbol} {timeframe}")
    df["date"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df.reset_index(drop=True)


def add_chart_context(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    out = df.copy()

    zone_window = 20 if timeframe == "1h" else 24
    out["demand"] = (
        out["low"].rolling(zone_window, min_periods=zone_window).min().shift(1)
    )
    out["supply"] = (
        out["high"].rolling(zone_window, min_periods=zone_window).max().shift(1)
    )

    prev_close = out["close"].shift(1)
    true_range = pd.concat(
        [
            out["high"] - out["low"],
            (out["high"] - prev_close).abs(),
            (out["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    out["atr14"] = true_range.rolling(14, min_periods=14).mean()

    typical = (out["high"] + out["low"] + out["close"]) / 3.0
    volume = out["volume"].astype(float).fillna(0.0)
    out["vwap"] = np.nan
    anchor = max(0, len(out) - 96)
    pv = (typical.iloc[anchor:] * volume.iloc[anchor:]).cumsum()
    vv = volume.iloc[anchor:].cumsum().replace(0, np.nan)
    out.loc[out.index[anchor:], "vwap"] = pv / vv

    return out


def _mpl():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    return plt, Rectangle


def _style_axis(ax) -> None:
    ax.set_facecolor(BG)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.tick_params(colors=MUTED, labelsize=8, length=0)
    ax.grid(True, color=GRID, linewidth=0.55, alpha=0.58)
    ax.yaxis.tick_right()
    ax.yaxis.set_label_position("right")


def _draw_candles(ax, df: pd.DataFrame) -> None:
    _, Rectangle = _mpl()
    width = 0.64

    for i, row in df.iterrows():
        o = finite(row["open"])
        h = finite(row["high"])
        l = finite(row["low"])
        c = finite(row["close"])
        color = GREEN if c >= o else RED

        ax.vlines(i, l, h, color=color, linewidth=0.72, alpha=0.96, zorder=3)

        body_low = min(o, c)
        body_h = abs(c - o)
        if body_h == 0:
            body_h = max(abs(c) * 0.00015, 1e-10)

        ax.add_patch(
            Rectangle(
                (i - width / 2, body_low),
                width,
                body_h,
                facecolor=color,
                edgecolor=color,
                linewidth=0.55,
                alpha=0.96,
                zorder=4,
            )
        )


def _price_tag(ax, price: float, last_open: float) -> None:
    color = GREEN if price >= last_open else RED
    ax.axhline(
        price,
        color=color,
        linewidth=0.8,
        linestyle=(0, (2, 2)),
        alpha=0.8,
        zorder=2,
    )
    ax.text(
        1.002,
        price,
        f" {price_fmt(price)} ",
        transform=ax.get_yaxis_transform(),
        ha="left",
        va="center",
        fontsize=8,
        color=BG,
        clip_on=False,
        bbox=dict(
            boxstyle="square,pad=0.22",
            facecolor=color,
            edgecolor=color,
            linewidth=0,
        ),
        zorder=10,
    )


def _draw_context_zone(
    ax,
    level: Any,
    atr: float,
    side: str,
    x_left: float,
    x_right: float,
) -> None:
    price = finite(level, float("nan"))
    if not math.isfinite(price) or price <= 0:
        return

    thickness = max(atr * 0.18, abs(price) * 0.00035)
    if side == "supply":
        lower, upper = price - thickness, price
        color = RED
        label = "Supply context"
        label_color = "#FF8A97"
    else:
        lower, upper = price, price + thickness
        color = GREEN
        label = "Demand context"
        label_color = "#5AF0B0"

    ax.fill_between(
        [x_left, x_right],
        lower,
        upper,
        color=color,
        alpha=0.08,
        zorder=0,
    )
    ax.hlines(
        [lower, upper],
        xmin=x_left,
        xmax=x_right,
        color=color,
        linewidth=0.75,
        alpha=0.58,
        zorder=1,
    )
    ax.text(
        x_right - 0.8,
        (lower + upper) / 2,
        f"{label}  {price_fmt(lower)}–{price_fmt(upper)}",
        ha="right",
        va="center",
        fontsize=7.0,
        color=label_color,
        bbox=dict(
            boxstyle="round,pad=0.22",
            facecolor=BG,
            edgecolor=color,
            linewidth=0.45,
            alpha=0.78,
        ),
        zorder=7,
    )


def _draw_vwap(ax, df: pd.DataFrame) -> tuple[float | None, str]:
    values = df["vwap"].to_numpy(dtype=float)
    valid = np.isfinite(values)

    if valid.any():
        x = np.arange(len(df))
        ax.plot(
            x[valid],
            values[valid],
            color=BLUE,
            linewidth=1.3,
            alpha=0.96,
            zorder=5,
        )

        last_idx = np.where(valid)[0][-1]
        last_vwap = float(values[last_idx])
        ax.text(
            last_idx,
            last_vwap,
            " VWAP ",
            ha="left",
            va="bottom",
            fontsize=7,
            color=WHITE,
            bbox=dict(
                boxstyle="round,pad=0.18",
                facecolor="#153C72",
                edgecolor=BLUE,
                linewidth=0.6,
                alpha=0.94,
            ),
            zorder=8,
        )
    else:
        last_vwap = None

    current = finite(df.iloc[-1]["close"])
    relation = "N/A"
    if last_vwap is not None:
        relation = "ABOVE" if current >= last_vwap else "BELOW"

    return last_vwap, relation


def _draw_trade_map(
    ax,
    record: dict[str, Any],
    x_now: float,
    x_future: float,
) -> None:
    entry = finite(record.get("entry"), float("nan"))
    stop = finite(record.get("stop"), float("nan"))
    target = finite(record.get("target"), float("nan"))

    if not all(math.isfinite(v) and v > 0 for v in (entry, stop, target)):
        return

    ax.hlines(
        entry,
        xmin=max(0, x_now - 22),
        xmax=x_future,
        color=GREEN,
        linewidth=1.0,
        linestyle=(0, (5, 3)),
        alpha=0.95,
        zorder=4,
    )
    ax.text(
        x_future,
        entry,
        f" ENTRY {price_fmt(entry)} ",
        ha="right",
        va="center",
        fontsize=7.2,
        color=BG,
        bbox=dict(
            boxstyle="round,pad=0.2",
            facecolor=GREEN,
            edgecolor=GREEN,
            linewidth=0.5,
            alpha=0.96,
        ),
        zorder=10,
    )

    ax.hlines(
        stop,
        xmin=max(0, x_now - 22),
        xmax=x_future,
        color=RED,
        linewidth=0.9,
        linestyle=(0, (5, 4)),
        alpha=0.92,
        zorder=4,
    )
    ax.text(
        x_future,
        stop,
        f" SL / INVALIDATION {price_fmt(stop)} ",
        ha="right",
        va="top" if record.get("side") == "LONG" else "bottom",
        fontsize=7.0,
        color=RED,
        bbox=dict(
            boxstyle="round,pad=0.18",
            facecolor=BG,
            edgecolor=RED,
            linewidth=0.6,
            alpha=0.92,
        ),
        zorder=10,
    )

    ax.hlines(
        target,
        xmin=x_now + 1,
        xmax=x_future,
        color=GREEN,
        linewidth=0.9,
        linestyle=(0, (6, 4)),
        alpha=0.88,
        zorder=4,
    )
    ax.text(
        x_future,
        target,
        f" TP {price_fmt(target)} ",
        ha="right",
        va="bottom",
        fontsize=7.2,
        color=BG,
        bbox=dict(
            boxstyle="round,pad=0.18",
            facecolor=GREEN,
            edgecolor=GREEN,
            linewidth=0.5,
            alpha=0.96,
        ),
        zorder=10,
    )


def _draw_preferred_scenario(
    ax,
    record: dict[str, Any],
    current_price: float,
    x_now: float,
) -> None:
    entry = finite(record.get("entry"), float("nan"))
    target = finite(record.get("target"), float("nan"))
    if not math.isfinite(entry) or not math.isfinite(target):
        return

    side = str(record.get("side") or "").upper()
    color = GREEN if side == "LONG" else RED

    xs = np.array([x_now, x_now + 4, x_now + 15], dtype=float)
    ys = np.array([current_price, entry, target], dtype=float)
    dense_x = np.linspace(xs.min(), xs.max(), 120)
    dense_y = np.interp(dense_x, xs, ys)

    ax.plot(
        dense_x,
        dense_y,
        color=color,
        linewidth=2.0,
        alpha=0.90,
        zorder=6,
    )
    ax.annotate(
        "",
        xy=(dense_x[-1], dense_y[-1]),
        xytext=(dense_x[-12], dense_y[-12]),
        arrowprops=dict(
            arrowstyle="-|>",
            color=color,
            lw=2.0,
            mutation_scale=17,
        ),
        zorder=7,
    )

    text = (
        "Preferred scenario\n"
        + (
            "pullback/retest entry → bullish continuation"
            if side == "LONG"
            else "retest entry → bearish continuation"
        )
    )
    ax.text(
        x_now + 3.5,
        (entry + target) / 2,
        text,
        ha="left",
        va="center",
        fontsize=7.4,
        color=color,
        bbox=dict(
            boxstyle="round,pad=0.32",
            facecolor=PANEL,
            edgecolor=color,
            linewidth=0.7,
            alpha=0.94,
        ),
        zorder=9,
    )


def _draw_side_panel(
    fig,
    record: dict[str, Any],
    timeframe: str,
    current_price: float,
    vwap_value: float | None,
    vwap_relation: str,
) -> None:
    panel = fig.add_axes([0.825, 0.105, 0.155, 0.775])
    panel.set_facecolor(PANEL)
    panel.set_xticks([])
    panel.set_yticks([])
    for spine in panel.spines.values():
        spine.set_visible(False)

    side = str(record.get("side") or "-").upper()
    side_color = GREEN if side == "LONG" else RED
    setup = str(record.get("setup") or "-").replace("_", " ").upper()
    status = str(record.get("scanner_status") or "READY").upper()

    rows = [
        ("STATUS", status, GREEN if status == "READY" else YELLOW),
        ("SIDE", side, side_color),
        ("SETUP", setup, TEXT),
        ("TIMEFRAME", timeframe.upper(), TEXT),
        ("SCORE", str(record.get("score") or "-"), TEXT),
        ("RR", f"{finite(record.get('rr')):.2f}R", TEXT),
        ("ENTRY DIST", f"{finite(record.get('entry_distance_atr')):.2f} ATR", TEXT),
        ("ATR EXP", f"{finite(record.get('atr_expansion'), 1.0):.2f}x", TEXT),
        ("CLEAR PATH", f"{finite(record.get('obstacle_clearance_r')):.2f}R", TEXT),
        ("REGIME", str(record.get("volatility_regime") or "-"), TEXT),
        ("VWAP", vwap_relation, BLUE),
    ]

    panel.text(
        0.08,
        0.96,
        "TRADE MAP",
        ha="left",
        va="top",
        fontsize=11,
        fontweight="bold",
        color=TEXT,
    )
    panel.text(
        0.08,
        0.915,
        price_fmt(current_price),
        ha="left",
        va="top",
        fontsize=15,
        fontweight="bold",
        color=side_color,
    )

    y = 0.84
    for label, value, color in rows:
        panel.text(
            0.08,
            y,
            label,
            ha="left",
            va="top",
            fontsize=6.7,
            color=MUTED,
        )
        panel.text(
            0.08,
            y - 0.032,
            value,
            ha="left",
            va="top",
            fontsize=8.1,
            fontweight="bold" if label in {"STATUS", "SIDE", "RR"} else "normal",
            color=color,
            wrap=True,
        )
        y -= 0.073

    if vwap_value is not None:
        panel.text(
            0.08,
            0.055,
            f"VWAP {price_fmt(vwap_value)}",
            ha="left",
            va="bottom",
            fontsize=7.0,
            color=MUTED,
        )


def generate_chart(
    exchange,
    record: dict[str, Any],
    timeframe: str,
    output_path: Path,
    candles: int = 160,
) -> Path:
    signal_time = record.get("ready_at") or record.get("created_at")
    as_of_ms = None
    if signal_time:
        dt = datetime.fromisoformat(str(signal_time).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        as_of_ms = int(dt.timestamp() * 1000)

    raw = closed_ohlcv(
        exchange,
        record["symbol"],
        timeframe,
        max(candles + 40, 220),
        as_of_ms=as_of_ms,
    )
    df = add_chart_context(raw, timeframe).tail(candles).reset_index(drop=True)

    plt, _ = _mpl()
    fig, ax = plt.subplots(figsize=(16, 9))
    fig.patch.set_facecolor(BG)
    _style_axis(ax)
    _draw_candles(ax, df)

    fig.subplots_adjust(
        left=0.055,
        right=0.80,
        top=0.88,
        bottom=0.105,
    )

    last = df.iloc[-1]
    current_price = finite(last["close"])
    last_open = finite(last["open"])
    atr = finite(last.get("atr14"), max(abs(current_price) * 0.005, 1e-10))
    x_now = len(df) - 1
    x_future = x_now + 18
    ax.set_xlim(-1, x_future + 2)

    _draw_context_zone(
        ax,
        last.get("supply"),
        atr,
        "supply",
        max(0, x_now - 68),
        x_future - 2,
    )
    _draw_context_zone(
        ax,
        last.get("demand"),
        atr,
        "demand",
        max(0, x_now - 68),
        x_future - 2,
    )

    vwap_value, vwap_relation = _draw_vwap(ax, df)
    _price_tag(ax, current_price, last_open)
    _draw_trade_map(ax, record, x_now, x_future)
    _draw_preferred_scenario(ax, record, current_price, x_now)
    _draw_side_panel(
        fig,
        record,
        timeframe,
        current_price,
        vwap_value,
        vwap_relation,
    )

    symbol = str(record["symbol"]).replace(":USDT", "")
    fig.text(
        0.058,
        0.955,
        f"{symbol}   {timeframe.upper()}",
        ha="left",
        va="top",
        fontsize=17,
        fontweight="bold",
        color=TEXT,
    )
    fig.text(
        0.24,
        0.955,
        price_fmt(current_price),
        ha="left",
        va="top",
        fontsize=18,
        fontweight="bold",
        color=GREEN if current_price >= last_open else RED,
    )

    header_bits = [
        f"O {price_fmt(last_open)}",
        f"H {price_fmt(last['high'])}",
        f"L {price_fmt(last['low'])}",
        f"C {price_fmt(current_price)}",
        "· Gate Futures",
    ]
    fig.text(
        0.058,
        0.925,
        "   ".join(header_bits),
        ha="left",
        va="top",
        fontsize=8.7,
        color=MUTED,
    )

    tick_count = min(8, len(df))
    tick_idx = np.linspace(0, len(df) - 1, tick_count, dtype=int)
    tick_labels = [
        df.iloc[i]["date"].strftime("%m-%d\n%H:%M")
        for i in tick_idx
    ]
    ax.set_xticks(tick_idx)
    ax.set_xticklabels(tick_labels, fontsize=8, color=MUTED)

    signal_id = record.get("signal_id", "")
    fig.text(
        0.058,
        0.035,
        f"V3.5 · {CHART_STYLE_VERSION} · Signal {signal_id} · closed candles only",
        ha="left",
        va="bottom",
        fontsize=7.3,
        color=MUTED,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(
        output_path,
        dpi=165,
        facecolor=BG,
        bbox_inches="tight",
    )
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


def _multipart_body(
    fields: dict[str, str],
    file_field: str,
    file_path: Path,
) -> tuple[bytes, str]:
    boundary = "----V35MarketScanChartBoundary7MA4YWxkTrZu0gW"
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


DEFAULT_TELEGRAM_GROUP_CHAT_ID = "-1003984243045"


def _telegram_chat_ids() -> list[str]:
    personal = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    group = os.getenv(
        "TELEGRAM_GROUP_CHAT_ID",
        DEFAULT_TELEGRAM_GROUP_CHAT_ID,
    ).strip()

    chat_ids: list[str] = []
    for chat_id in (personal, group):
        if chat_id and chat_id not in chat_ids:
            chat_ids.append(chat_id)
    return chat_ids


def telegram_send_photo(photo_path: str, caption: str = "") -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_ids = _telegram_chat_ids()
    path = Path(photo_path)

    if not token or not chat_ids or not path.exists():
        return False

    for chat_id in chat_ids:
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
                raise RuntimeError(
                    f"Telegram sendPhoto failed for {chat_id}: {result}"
                )

    return True


def dependency_report() -> dict[str, bool]:
    names = ["matplotlib", "PIL", "plotly", "kaleido", "cairosvg"]
    return {
        name: importlib.util.find_spec(name) is not None
        for name in names
    }


def self_test(output_dir: Path) -> None:
    print(
        "IMAGE_DEPENDENCIES",
        json.dumps(dependency_report(), sort_keys=True),
    )

    class FakeExchange:
        def milliseconds(self):
            return int(
                datetime(2026, 2, 1, tzinfo=timezone.utc).timestamp() * 1000
            )

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
                rows.append(
                    [
                        ts,
                        o,
                        max(o, c) + 0.7,
                        min(o, c) - 0.7,
                        c,
                        1000 + i,
                    ]
                )
            return rows

    record = {
        "signal_id": "selftest12345678",
        "symbol": "TEST/USDT:USDT",
        "side": "LONG",
        "setup": "trend_pullback",
        "scanner_status": "READY",
        "entry": 105.0,
        "stop": 102.0,
        "target": 111.0,
        "rr": 2.0,
        "score": 88,
        "volatility_regime": "NORMAL",
        "entry_distance_atr": 0.21,
        "atr_expansion": 1.14,
        "obstacle_clearance_r": 1.75,
    }

    charts = generate_ready_charts(FakeExchange(), record, output_dir)
    assert Path(charts["1h"]).exists()
    assert Path(charts["4h"]).exists()
    assert Path(charts["1h"]).stat().st_size > 1000
    assert Path(charts["4h"]).stat().st_size > 1000
    print("CHART_SELF_TEST_OK", CHART_STYLE_VERSION)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="V3.5 Market-Scan-style READY chart generator"
    )
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument(
        "--output-dir",
        default="/freqtrade/user_data/v3_charts",
    )
    args = parser.parse_args()

    if args.self_test:
        self_test(Path(args.output_dir))
        return 0

    print(
        "Use chart_generator through outcome_tracker for READY setups."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

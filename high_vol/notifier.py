import os

import requests

from .config import MAX_TELEGRAM_SETUPS, TEXT_PATH


def _fmt(value):
    if value is None:
        return "-"
    value = float(value)
    if abs(value) >= 1000:
        return f"{value:,.2f}"
    if abs(value) >= 1:
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return f"{value:.6f}".rstrip("0").rstrip(".")


def build_text(report):
    rows = [
        item
        for item in report.get("setups", [])
        if not item.get("correlation_suppressed", False)
        and item.get("bucket") != "OVEREXTENDED"
    ][:MAX_TELEGRAM_SETUPS]

    counts = report.get("alert_counts") or report.get("counts", {})

    lines = [
        "⚡ HIGH VOLATILITY SETUP WATCH",
        "=" * 32,
        f"Scan UTC: {report.get('generated_at_utc')}",
        (
            f"Universe: {report.get('universe_count', 0)} | "
            f"High-vol candidates: {report.get('high_vol_candidate_count', 0)} | "
            f"Full PA/SMC: {report.get('full_scan_count', 0)}"
        ),
        (
            f"MOMENTUM {counts.get('MOMENTUM_READY', 0)} | "
            f"PULLBACK {counts.get('PULLBACK_READY', 0)} | "
            f"DEVELOPING {counts.get('HIGH_VOL_DEVELOPING', 0)}"
        ),
        (
            f"OVEREXTENDED {counts.get('OVEREXTENDED', 0)} | "
            f"Corr suppressed {report.get('correlation_suppressed_count', 0)}"
        ),
    ]

    if not rows:
        lines += ["", "Không có high-vol setup đạt ngưỡng gửi cảnh báo."]
        return "\n".join(lines)

    icons = {
        "MOMENTUM_READY": "🚀",
        "PULLBACK_READY": "🎯",
        "HIGH_VOL_DEVELOPING": "⚡",
    }

    for i, item in enumerate(rows, start=1):
        plan = item.get("trade_plan") or {}
        entry = plan.get("entry_zone") or {}
        targets = plan.get("targets") or []
        vol = item.get("volatility") or {}
        ticker = item.get("ticker") or {}
        part = ticker.get("participation_context") or {}

        lines += [
            "",
            (
                f"{icons.get(item.get('bucket'), '•')} {i}. "
                f"{item.get('symbol')} · "
                f"{(item.get('direction') or '-').upper()} · "
                f"{item.get('score', 0)}/100"
            ),
            (
                f"{item.get('bucket')} | MTF {item.get('mtf_alignment')} | "
                f"24H range {vol.get('range_24h_pct', 0):.1f}% "
                f"[{vol.get('regime', '-')}]"
            ),
            (
                f"ATR expansion: "
                f"{vol.get('atr_expansion'):.2f}x "
                if vol.get("atr_expansion") is not None
                else "ATR expansion: - "
            ) + (
                f"| Volume: {vol.get('volume_expansion'):.2f}x"
                if vol.get("volume_expansion") is not None
                else "| Volume: -"
            ),
            (
                f"Entry: {_fmt(entry.get('lower'))} - "
                f"{_fmt(entry.get('upper'))}"
            ),
            f"SL: {_fmt(plan.get('stop_loss'))}",
        ]

        for target in targets[:3]:
            rr = target.get("rr")
            rr_text = f"{rr:.2f}R" if rr is not None else "-"
            lines.append(
                f"{target.get('name')}: {_fmt(target.get('price'))} [{rr_text}]"
            )

        distance = item.get("entry_distance_atr")
        lines += [
            (
                f"Entry distance: "
                f"{distance:.2f} ATR"
                if distance is not None
                else "Entry distance: -"
            ),
            (
                f"Participation: {part.get('regime', 'N/A')} | "
                f"HoldVol Δ "
                f"{part.get('hold_vol_change_pct'):+.2f}%"
                if part.get("hold_vol_change_pct") is not None
                else f"Participation: {part.get('regime', 'N/A')} | HoldVol Δ -"
            ),
        ]

    return "\n".join(lines)


def _split_text(text, max_chars=3800):
    if len(text) <= max_chars:
        return [text]

    chunks = []
    current = []
    for block in text.split("\n\n"):
        candidate = "\n\n".join(current + [block])
        if len(candidate) <= max_chars:
            current.append(block)
        else:
            if current:
                chunks.append("\n\n".join(current))
            current = [block]

    if current:
        chunks.append("\n\n".join(current))

    return chunks


def _send_photo(token, chat_id, chart):
    path = chart.get("path")
    if not path:
        return

    caption = (
        f"{chart.get('symbol')} | {chart.get('bucket')} | "
        f"{(chart.get('direction') or '-').upper()} | "
        f"Score {chart.get('score')}/100 | 15M"
    )

    with open(path, "rb") as handle:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendPhoto",
            data={"chat_id": chat_id, "caption": caption},
            files={"photo": handle},
            timeout=30,
        )

    if response.status_code != 200:
        raise RuntimeError(
            f"Telegram chart failed: HTTP {response.status_code} "
            f"{response.text[:500]}"
        )


def send_telegram(report, chart_paths=None):
    text = build_text(report)
    TEXT_PATH.parent.mkdir(parents=True, exist_ok=True)
    TEXT_PATH.write_text(text, encoding="utf-8")

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("Telegram skipped: missing secrets.")
        return False

    chunks = _split_text(text)
    for index, chunk in enumerate(chunks, start=1):
        if len(chunks) > 1:
            chunk = f"[{index}/{len(chunks)}]\n" + chunk

        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Telegram send failed: HTTP {response.status_code} "
                f"{response.text[:500]}"
            )

    chart_paths = chart_paths or []
    for chart in chart_paths:
        _send_photo(token, chat_id, chart)

    print(
        f"High Vol Telegram sent "
        f"({len(chunks)} text, {len(chart_paths)} chart(s))."
    )
    return True

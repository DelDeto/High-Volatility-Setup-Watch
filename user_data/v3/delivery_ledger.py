from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_delivery(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": "V3.4", "updated_at": iso_now(), "items": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        data = {}
    data.setdefault("version", "V3.4")
    data.setdefault("items", {})
    data["version"] = "V3.4"
    return data


def save_delivery(path: Path, state: dict[str, Any]) -> None:
    state["version"] = "V3.4"
    state["updated_at"] = iso_now()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def enqueue(
    state: dict[str, Any],
    *,
    key: str,
    kind: str,
    signal_id: str,
    text: str | None = None,
    timeframe: str | None = None,
) -> dict[str, Any]:
    items = state.setdefault("items", {})
    if key in items:
        return items[key]
    item = {
        "key": key,
        "kind": kind,
        "signal_id": signal_id,
        "timeframe": timeframe,
        "text": text,
        "status": "PENDING",
        "attempts": 0,
        "created_at": iso_now(),
        "last_attempt_at": None,
        "sent_at": None,
        "last_error": None,
    }
    items[key] = item
    return item


def pending_items(state: dict[str, Any]) -> list[dict[str, Any]]:
    items = [
        item
        for item in state.get("items", {}).values()
        if item.get("status") != "SENT"
    ]
    return sorted(items, key=lambda x: x.get("created_at") or "")


def mark_attempt(item: dict[str, Any]) -> None:
    item["attempts"] = int(item.get("attempts", 0)) + 1
    item["last_attempt_at"] = iso_now()


def mark_sent(item: dict[str, Any]) -> None:
    item["status"] = "SENT"
    item["sent_at"] = iso_now()
    item["last_error"] = None


def mark_failed(item: dict[str, Any], exc: Exception | str) -> None:
    item["status"] = "PENDING"
    item["last_error"] = str(exc)[:500]


def stats(state: dict[str, Any]) -> dict[str, int]:
    items = list(state.get("items", {}).values())
    return {
        "total": len(items),
        "sent": sum(1 for x in items if x.get("status") == "SENT"),
        "pending": sum(1 for x in items if x.get("status") != "SENT"),
    }


def self_test() -> None:
    state = {"items": {}}
    x = enqueue(
        state,
        key="signal:abc:new",
        kind="signal_text",
        signal_id="abc",
        text="hello",
    )
    enqueue(
        state,
        key="signal:abc:new",
        kind="signal_text",
        signal_id="abc",
        text="hello",
    )
    assert len(state["items"]) == 1
    assert len(pending_items(state)) == 1
    mark_attempt(x)
    mark_failed(x, "temporary")
    assert stats(state)["pending"] == 1
    mark_attempt(x)
    mark_sent(x)
    assert stats(state)["sent"] == 1
    print("DELIVERY_LEDGER_SELF_TEST_OK")


if __name__ == "__main__":
    self_test()

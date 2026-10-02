from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SLOT_MINUTES = 15


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def canonical_slot(dt: datetime) -> datetime:
    dt = dt.astimezone(timezone.utc).replace(second=0, microsecond=0)
    return dt.replace(minute=(dt.minute // SLOT_MINUTES) * SLOT_MINUTES)


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "version": "V3.4",
            "last_completed_slot": None,
            "last_completed_at": None,
            "last_run_id": None,
            "last_event": None,
            "total_catchup_slots": 0,
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        data = {}
    data.setdefault("version", "V3.4")
    data.setdefault("last_completed_slot", None)
    data.setdefault("last_completed_at", None)
    data.setdefault("last_run_id", None)
    data.setdefault("last_event", None)
    data.setdefault("total_catchup_slots", 0)
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def build_plan(state: dict[str, Any], now: datetime, max_catchup: int) -> dict[str, Any]:
    current = canonical_slot(now)
    last = parse_dt(state.get("last_completed_slot"))
    missed: list[datetime] = []

    if last:
        cursor = canonical_slot(last) + timedelta(minutes=SLOT_MINUTES)
        while cursor < current:
            missed.append(cursor)
            cursor += timedelta(minutes=SLOT_MINUTES)

    truncated = 0
    if len(missed) > max_catchup:
        truncated = len(missed) - max_catchup
        missed = missed[-max_catchup:]

    return {
        "version": "V3.4",
        "planned_at": iso(now),
        "current_slot": iso(current),
        "last_completed_slot": state.get("last_completed_slot"),
        "missed_slots": [iso(x) for x in missed],
        "missed_count": len(missed),
        "truncated_older_slots": truncated,
    }


def command_plan(args) -> None:
    state = load_state(Path(args.state))
    now = parse_dt(args.now) if args.now else utc_now()
    plan = build_plan(state, now, args.max_catchup)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        "V34_SCAN_PLAN",
        f"current={plan['current_slot']}",
        f"last={plan['last_completed_slot']}",
        f"catchup={plan['missed_count']}",
        f"truncated={plan['truncated_older_slots']}",
    )


def command_complete(args) -> None:
    path = Path(args.state)
    state = load_state(path)
    slot = canonical_slot(parse_dt(args.slot) or utc_now())
    state["version"] = "V3.4"
    state["last_completed_slot"] = iso(slot)
    state["last_completed_at"] = iso(utc_now())
    state["last_run_id"] = args.run_id or None
    state["last_event"] = args.event or None
    state["total_catchup_slots"] = int(state.get("total_catchup_slots", 0)) + max(
        0, args.catchup_count
    )
    save_state(path, state)
    print("V34_HEARTBEAT_COMPLETED", state["last_completed_slot"])


def self_test() -> None:
    state = {
        "last_completed_slot": "2026-10-02T08:00:00+00:00",
        "total_catchup_slots": 0,
    }
    now = datetime(2026, 10, 2, 8, 37, tzinfo=timezone.utc)
    plan = build_plan(state, now, 8)
    assert plan["current_slot"] == "2026-10-02T08:30:00+00:00"
    assert plan["missed_slots"] == ["2026-10-02T08:15:00+00:00"]

    state2 = {"last_completed_slot": None}
    plan2 = build_plan(state2, now, 8)
    assert plan2["missed_slots"] == []
    print("SCAN_LEDGER_SELF_TEST_OK")


def main() -> int:
    parser = argparse.ArgumentParser(description="V3.4 scan heartbeat and catch-up planner")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("plan")
    p.add_argument("--state", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--now")
    p.add_argument("--max-catchup", type=int, default=8)

    c = sub.add_parser("complete")
    c.add_argument("--state", required=True)
    c.add_argument("--slot", required=True)
    c.add_argument("--run-id")
    c.add_argument("--event")
    c.add_argument("--catchup-count", type=int, default=0)

    sub.add_parser("self-test")

    args = parser.parse_args()
    if args.command == "plan":
        command_plan(args)
    elif args.command == "complete":
        command_complete(args)
    else:
        self_test()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import json
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def build_payload(scan_state: dict[str, Any], delivery: dict[str, Any]) -> dict[str, Any]:
    pending = sum(
        1
        for item in delivery.get("items", {}).values()
        if item.get("status") != "SENT"
    )
    return {
        "service": "v3-scanner",
        "last_completed_slot": scan_state.get("last_completed_slot"),
        "last_completed_at": scan_state.get("last_completed_at"),
        "run_id": scan_state.get("last_run_id"),
        "status": "healthy",
        "pending_delivery": pending,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def sync(
    project_url: str,
    secret_key: str,
    payload: dict[str, Any],
) -> bool:
    if not project_url or not secret_key:
        print("SUPABASE_HEARTBEAT_SKIPPED missing credentials")
        return False

    url = project_url.rstrip("/") + "/rest/v1/scanner_heartbeat?on_conflict=service"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "apikey": secret_key,
            "Authorization": f"Bearer {secret_key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
        if resp.status not in {200, 201, 204}:
            raise RuntimeError(f"Supabase heartbeat HTTP {resp.status}")
    print(
        "SUPABASE_HEARTBEAT_OK",
        payload.get("last_completed_slot"),
        f"pending={payload.get('pending_delivery')}",
    )
    return True


def self_test() -> None:
    payload = build_payload(
        {
            "last_completed_slot": "2026-10-02T09:30:00+00:00",
            "last_completed_at": "2026-10-02T09:37:00+00:00",
            "last_run_id": "123",
        },
        {
            "items": {
                "a": {"status": "SENT"},
                "b": {"status": "PENDING"},
            }
        },
    )
    assert payload["service"] == "v3-scanner"
    assert payload["pending_delivery"] == 1
    assert payload["last_run_id"] == "123"
    print("SUPABASE_HEARTBEAT_SELF_TEST_OK")


def main() -> int:
    parser = argparse.ArgumentParser(description="V3.4 Supabase heartbeat sync")
    parser.add_argument("--scan-state", required=True)
    parser.add_argument("--delivery-state", required=True)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return 0

    scan_state = load_json(Path(args.scan_state), {})
    delivery = load_json(Path(args.delivery_state), {"items": {}})
    payload = build_payload(scan_state, delivery)
    sync(
        os.getenv("SUPABASE_PROJECT_URL", "").strip(),
        os.getenv("SUPABASE_SECRET_KEY", "").strip(),
        payload,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

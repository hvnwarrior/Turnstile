#!/usr/bin/env python3
"""Turnstile demo: a full offboarding, end to end, on the real app.

Narrative, in order:
1. Unsigned webhook bounced (401) — signature checks fail closed.
2. Incomplete intake refused (422) with the exact missing fields —
   refusal over fabrication, visible over HTTP.
3. Signed, complete intake accepted; checklist built with human-
   authored risk tiers.
4. Attempt to execute a gated task without approval -> 403. The gate
   is structural: it does not care who is asking or how nicely.
5. Priya approves the device wipe as a named human; it executes.
6. The rest of the checklist walks to completion, gates first.
7. The revocation report: complete, with per-task attribution, and
   one rejection preserved forever as an exception with its reason.

Run:  python scripts/demo.py [--fresh]
Requires the API server running (uvicorn app.main:app --port 8000
from backend/) unless --offline is passed, which drives the services
directly without HTTP.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

BASE_URL = "http://127.0.0.1:8000"

SECRET = "whsec_demo_secret"

OFFBOARDING = {
    "type": "employee.terminated",
    "person_name": "Sam Ray",
    "requested_by": "bamboohr-sync",
    "details": {
        "department": "Sales",
        "manager": "Priya",
        "last_day": "2026-12-04",
        "has_company_device": True,
        "has_building_access": True,
    },
}


def banner(text: str) -> None:
    print(f"\n{'=' * 62}\n{text}\n{'=' * 62}")


def pretty(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true",
                        help="drive services directly, no HTTP server")
    args = parser.parse_args()

    if args.offline:
        return run_offline()

    try:
        import requests
    except ImportError:
        print("pip install requests  (or use --offline)")
        return 1

    import requests as rq

    def sign(body: bytes) -> dict:
        import hashlib
        import hmac
        digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        return {"X-Hub-Signature-256": digest}

    s = rq.Session()

    # 1. -------------------------------------------------------------
    banner("1. Unsigned webhook -> bounced (fail closed)")
    response = s.post(f"{BASE_URL}/hris/events",
                      json=OFFBOARDING)
    print(f"HTTP {response.status_code} — {response.json()['detail']}")

    # 2. -------------------------------------------------------------
    banner("2. Incomplete intake -> refused with exact missing fields")
    incomplete = {**OFFBOARDING, "details": {"department": "Sales"}}
    body = json.dumps(incomplete).encode()
    response = s.post(f"{BASE_URL}/hris/events", content=body,
                      headers=sign(body))
    detail = response.json()["detail"]
    print(f"HTTP {response.status_code}")
    print(pretty(detail))

    # 3. -------------------------------------------------------------
    banner("3. Signed, complete intake -> checklist built")
    body = json.dumps(OFFBOARDING).encode()
    response = s.post(f"{BASE_URL}/hris/events", content=body,
                      headers=sign(body))
    result = response.json()
    event_id = result["event_id"]
    print(pretty(result))

    # 4. -------------------------------------------------------------
    banner("4. Try to wipe the device WITHOUT approval -> 403")
    response = s.post(f"{BASE_URL}/events/{event_id}/tasks/reclaim_device/execute", json={})
    print(f"HTTP {response.status_code} — {response.json()['detail']}")

    # 5. -------------------------------------------------------------
    banner("5. Priya (named human) approves; the wipe executes")
    s.post(f"{BASE_URL}/events/{event_id}/tasks/reclaim_device/approve",
           json={"approver": "Priya", "note": "departure confirmed"})
    response = s.post(f"{BASE_URL}/events/{event_id}/tasks/reclaim_device/execute",
                      json={"actor": "automation"})
    task = next(t for t in response.json()["tasks"]
                if t["id"] == "reclaim_device")
    print(f"reclaim_device: {task['status']} "
          f"(approved by {task['decided_by']}: {task['decision_note']})")

    # 6. -------------------------------------------------------------
    banner("6. Walk the rest of the checklist, gates first")
    for _round in range(12):
        advance = s.post(f"{BASE_URL}/events/{event_id}/advance").json()

        for task in advance["awaiting_approval"]:
            if task["id"] == "deactivate_badge":
                # 6a. A deliberate rejection, preserved forever.
                r = s.post(
                    f"{BASE_URL}/events/{event_id}/tasks/deactivate_badge/reject",
                    json={"rejector": "Priya",
                          "reason": "legal hold active on badge data"},
                )
                print(f"  REJECTED deactivate_badge: {r.status_code}")
            else:
                s.post(f"{BASE_URL}/events/{event_id}/tasks/{task['id']}/approve",
                       json={"approver": "Priya"})
                print(f"  approved {task['id']}")

        for task in advance["cleared"]:
            r = s.post(f"{BASE_URL}/events/{event_id}/tasks/{task['id']}/execute",
                       json={"actor": "automation"})
            print(f"  executed {task['id']}: {r.status_code}")

        done = all(
            t["status"] in ("completed", "rejected")
            for t in s.get(f"{BASE_URL}/events/{event_id}").json()["tasks"]
        )
        if done:
            break

    # 7. -------------------------------------------------------------
    banner("7. Revocation report: the ledger is the proof")
    report = s.get(f"{BASE_URL}/events/{event_id}/revocation-report").json()
    print(pretty(report))

    return 0


def run_offline() -> int:
    """Same narrative, no HTTP: drive the wired services directly."""

    from app.config import AppSettings
    from app.dependencies import build_services

    settings = AppSettings(
        database_path="data/demo-turnstile.db",
        audit_log_path="data/demo-audit.jsonl",
        hris_webhook_secret=SECRET,
        hris_require_signature=True,
    )
    services = build_services(settings)

    import hashlib
    import hmac

    def sign(body: bytes) -> str:
        return hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()

    banner("1. Unsigned webhook -> refused")
    import json as _json
    try:
        services["hris_intake"].ingest(
            _json.dumps(OFFBOARDING).encode(), None
        )
        print("ERROR: accepted unsigned")
        return 1
    except Exception as error:
        print(f"Refused: {error}")

    banner("2. Incomplete intake -> refused with missing list")
    incomplete = {**OFFBOARDING, "details": {"department": "Sales"}}
    result = services["hris_intake"].ingest(
        _json.dumps(incomplete).encode(),
        sign(_json.dumps(incomplete).encode()),
    )
    print(pretty(result))

    banner("3. Complete intake -> checklist built")
    body = _json.dumps(OFFBOARDING).encode()
    result = services["hris_intake"].ingest(body, sign(body))
    event_id = result["event_id"]
    print(pretty(result))

    engine = services["engine"]
    store = services["store"]
    event = store.load_event(event_id)
    engine.next_batch(event)

    banner("4. Execute gated task without approval -> blocked")
    try:
        engine.execute(event, "reclaim_device", actor="automation")
        print("ERROR: executed without approval")
        return 1
    except PermissionError as error:
        print(f"Blocked: {error}")

    banner("5. Named human approves; task executes")
    engine.approve(event, "reclaim_device", approver="Priya",
                   note="departure confirmed")
    engine.execute(event, "reclaim_device", actor="automation")
    print("reclaim_device completed")

    banner("6. Walk the checklist; reject the badge with a reason")
    for _round in range(12):
        engine.next_batch(event)

        for task in list(event.tasks):
            if task.status.value == "awaiting_approval":
                if task.id == "deactivate_badge":
                    engine.reject(event, "deactivate_badge",
                                  rejector="Priya",
                                  reason="legal hold active on badge data")
                    print("  rejected deactivate_badge (legal hold)")
                else:
                    engine.approve(event, task.id, approver="Priya")
                    print(f"  approved {task.id}")

        progressed = False
        for task in list(event.tasks):
            if task.status.value in ("ready", "approved"):
                engine.execute(event, task.id, actor="automation")
                print(f"  executed {task.id}")
                progressed = True

        if not progressed and all(
            t.status.value in ("completed", "rejected")
            for t in event.tasks
        ):
            break

    store.save_task_states(event)

    banner("7. Revocation report")
    report = services["revocation_audit"].generate(event)
    print(pretty(report.to_dict()))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""API integration tests: intake -> decisions -> revocation report.

Runs the real FastAPI app against a temp database, exercising the HTTP
surface end to end.
"""

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app.config import AppSettings
from app.dependencies import build_services
from app.main import create_app


SECRET = "whsec_api_test"


@pytest.fixture
def client(tmp_path):
    settings = AppSettings(
        database_path=str(tmp_path / "turnstile.db"),
        audit_log_path=str(tmp_path / "audit.jsonl"),
        hris_webhook_secret=SECRET,
        hris_require_signature=True,
    )

    app = create_app()
    app.state.turnstile_services = build_services(settings)

    with TestClient(app) as test_client:
        yield test_client


def sign(body: bytes) -> str:
    return hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


OFFBOARDING = {
    "type": "employee.terminated",
    "person_name": "Sam Ray",
    "requested_by": "bamboohr-sync",
    "details": {
        "department": "Sales",
        "manager": "Priya",
        "last_day": "2026-12-04",
        "has_company_device": True,
    },
}


def post_hris(client, payload):
    body = json.dumps(payload).encode()
    return client.post(
        "/hris/events", content=body,
        headers={"X-Hub-Signature-256": sign(body)},
    )


def test_full_offboarding_via_api(client):
    # 1. HRIS webhook accepted.
    response = post_hris(client, OFFBOARDING)
    assert response.status_code == 200
    event_id = response.json()["event_id"]

    # 2. Unsigned webhook rejected.
    body = json.dumps(OFFBOARDING).encode()
    unsigned = client.post("/hris/events", content=body)
    assert unsigned.status_code == 401

    # 3. AUTO tasks execute.
    advance = client.post(f"/events/{event_id}/advance")
    cleared = {t["id"] for t in advance.json()["cleared"]}
    assert cleared == {"transfer_files", "set_mail_forwarding"}

    for task_id in ("transfer_files", "set_mail_forwarding"):
        done = client.post(
            f"/events/{event_id}/tasks/{task_id}/execute",
            json={"actor": "automation"},
        )
        assert done.status_code == 200

    # 4. Executing an unapproved gated task -> 403.
    forbidden = client.post(
        f"/events/{event_id}/tasks/reclaim_device/execute", json={}
    )
    assert forbidden.status_code == 403

    # 5. Approve the device wipe as a named human, then execute.
    approved = client.post(
        f"/events/{event_id}/tasks/reclaim_device/approve",
        json={"approver": "Priya", "note": "departure confirmed"},
    )
    assert approved.status_code == 200

    done = client.post(
        f"/events/{event_id}/tasks/reclaim_device/execute",
        json={"actor": "automation"},
    )
    assert done.status_code == 200

    # 6. SSO disable is gated and only surfaces after its AUTO deps
    # complete; advance should now expose it for approval.
    advance = client.post(f"/events/{event_id}/advance")
    awaiting = {t["id"] for t in advance.json()["awaiting_approval"]}
    assert awaiting == {"disable_sso_account"}

    client.post(
        f"/events/{event_id}/tasks/disable_sso_account/approve",
        json={"approver": "it-admin"},
    )
    client.post(
        f"/events/{event_id}/tasks/disable_sso_account/execute", json={}
    )

    # 7. Remaining tasks: revoke_remote_access (gated, after SSO),
    # revoke_saas_licenses (AUTO, after SSO), run_revocation_audit
    # (fan-in). advance() first: approvals attach to runnable tasks.
    client.post(f"/events/{event_id}/advance")
    client.post(
        f"/events/{event_id}/tasks/revoke_remote_access/approve",
        json={"approver": "it-admin"},
    )
    client.post(f"/events/{event_id}/tasks/revoke_remote_access/execute", json={})
    client.post(f"/events/{event_id}/tasks/revoke_saas_licenses/execute", json={})

    client.post(f"/events/{event_id}/advance")
    client.post(f"/events/{event_id}/tasks/run_revocation_audit/execute", json={})

    # 8. Final report: complete, with attribution.
    report = client.get(f"/events/{event_id}/revocation-report")
    assert report.status_code == 200
    data = report.json()
    assert data["verdict"] == "complete"
    ledger = {e["task_id"]: e for e in data["task_ledger"]}
    assert ledger["reclaim_device"]["decided_by"] == "Priya"      # manager-owned
    assert ledger["disable_sso_account"]["decided_by"] == "it-admin"  # IT-owned
    assert ledger["disable_sso_account"]["risk_tier"] == "approval_required"


def test_refusal_returns_422_with_missing_list(client):
    body = json.dumps({
        "type": "employee.terminated",
        "person_name": "X",
        "requested_by": "hr",
        "details": {},
    }).encode()

    response = client.post(
        "/hris/events", content=body,
        headers={"X-Hub-Signature-256": sign(body)},
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["reason"] == "missing_intake_data"
    assert sorted(detail["missing"]) == ["department", "last_day", "manager"]


def test_rejection_flows_into_report(client):
    response = post_hris(client, OFFBOARDING)
    event_id = response.json()["event_id"]

    rejected = client.post(
        f"/events/{event_id}/tasks/reclaim_device/reject",
        json={"rejector": "Priya", "reason": "device already wiped"},
    )
    assert rejected.status_code == 200

    report = client.get(f"/events/{event_id}/revocation-report")
    data = report.json()
    assert data["verdict"] in ("exceptions_found", "in_progress")
    assert any("already wiped" in e for e in data["exceptions"])


def test_revocation_report_rejects_onboarding(client):
    body = json.dumps({
        "type": "employee.created",
        "person_name": "Jordan Lee",
        "requested_by": "recruiting",
        "details": {
            "role": "CS", "department": "Sales",
            "manager": "Priya", "start_date": "2026-12-07",
        },
    }).encode()

    response = client.post(
        "/hris/events", content=body,
        headers={"X-Hub-Signature-256": sign(body)},
    )
    event_id = response.json()["event_id"]

    report = client.get(f"/events/{event_id}/revocation-report")
    assert report.status_code == 422


def test_unknown_event_404(client):
    response = client.get("/events/999")
    assert response.status_code == 404


def test_list_events(client):
    post_hris(client, OFFBOARDING)
    response = client.get("/events")
    assert response.status_code == 200
    events = response.json()["events"]
    assert len(events) == 1
    assert events[0]["person_name"] == "Sam Ray"

"""HRIS intake tests: signature fail-closed, refusal passthrough,
persist + first batch evaluation."""

import hashlib
import hmac
import json

import pytest

from app.models import EventType, RiskTier, TaskStatus
from app.services.audit_service import AuditLogService
from app.services.checklist_builder import ChecklistBuilder
from app.services.event_store import EventStore
from app.services.execution_engine import (
    DryRunExecutor,
    ExecutionEngine,
)
from app.services.hris_intake import (
    HrisIntakeService,
    SignatureError,
    UnknownHrisEventError,
)


SECRET = "whsec_test"


def make_service(tmp_path, require_signature=True):
    builder = ChecklistBuilder(clock=lambda: "2026-11-25T10:00:00+00:00")
    engine = ExecutionEngine(
        executor=DryRunExecutor(),
        audit_service=AuditLogService(str(tmp_path / "audit.jsonl")),
    )
    store = EventStore(str(tmp_path / "turnstile.db"))

    return HrisIntakeService(
        builder=builder,
        engine=engine,
        store=store,
        webhook_secret=SECRET,
        require_signature=require_signature,
    )


def sign(body: bytes) -> str:
    return hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


VALID_OFFBOARDING = {
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


def test_valid_signed_webhook_accepted(tmp_path):
    service = make_service(tmp_path)
    body = json.dumps(VALID_OFFBOARDING).encode()

    result = service.ingest(body, sign(body))

    assert result["accepted"] is True
    assert result["event_type"] == "offboarding"
    assert result["event_id"] >= 1
    # transfer_files/set_mail_forwarding are AUTO and dependency-free.
    ready_ids = {t["id"] for t in result["ready_tasks"]}
    assert ready_ids == {"transfer_files", "set_mail_forwarding"}
    # Approvals attach to runnable tasks only: reclaim_device is gated
    # and dependency-free, so it awaits approval at intake.
    # disable_sso_account stays PENDING until its AUTO dependencies
    # complete — approving it early would just go stale.
    awaiting_ids = {t["id"] for t in result["awaiting_approval"]}
    assert awaiting_ids == {"reclaim_device"}


def test_missing_signature_rejected(tmp_path):
    service = make_service(tmp_path)
    body = json.dumps(VALID_OFFBOARDING).encode()

    with pytest.raises(SignatureError):
        service.ingest(body, None)


def test_invalid_signature_rejected(tmp_path):
    service = make_service(tmp_path)
    body = json.dumps(VALID_OFFBOARDING).encode()

    with pytest.raises(SignatureError):
        service.ingest(body, "deadbeef")


def test_signature_fails_closed_without_secret(tmp_path):
    service = make_service(tmp_path)
    service.webhook_secret = ""  # misconfiguration

    body = json.dumps(VALID_OFFBOARDING).encode()

    with pytest.raises(RuntimeError):
        service.ingest(body, sign(body))


def test_unsigned_allowed_when_signature_check_disabled(tmp_path):
    service = make_service(tmp_path, require_signature=False)
    body = json.dumps(VALID_OFFBOARDING).encode()

    result = service.ingest(body, None)

    assert result["accepted"] is True


def test_missing_intake_data_refused_with_list(tmp_path):
    service = make_service(tmp_path)
    body = json.dumps({
        "type": "employee.terminated",
        "person_name": "Sam Ray",
        "requested_by": "bamboohr-sync",
        "details": {},
    }).encode()

    result = service.ingest(body, sign(body))

    assert result["accepted"] is False
    assert result["reason"] == "missing_intake_data"
    assert sorted(result["missing"]) == ["department", "last_day", "manager"]


def test_unknown_hris_event_type(tmp_path):
    service = make_service(tmp_path)
    body = json.dumps({"type": "employee.retired", "person_name": "X"}).encode()

    with pytest.raises(UnknownHrisEventError):
        service.ingest(body, sign(body))


def test_event_persisted_and_reloadable(tmp_path):
    service = make_service(tmp_path)
    body = json.dumps(VALID_OFFBOARDING).encode()

    result = service.ingest(body, sign(body))

    event = service.store.load_event(result["event_id"])
    assert event.person_name == "Sam Ray"
    assert event.task_by_id("disable_sso_account").is_gated


def test_invalid_json_handled(tmp_path):
    service = make_service(tmp_path)

    result = service.ingest(b"{not json", sign(b"{not json"))

    assert result["accepted"] is False
    assert result["reason"] == "invalid_json"

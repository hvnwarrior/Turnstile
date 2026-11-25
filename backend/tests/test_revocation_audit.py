"""Revocation audit tests: the ledger is the proof."""

import pytest

from app.models import EventType, PersonEvent, RiskTier, Task, TaskStatus
from app.services.revocation_audit import (
    RevocationAuditService,
    VERDICT_COMPLETE,
    VERDICT_EXCEPTIONS,
    VERDICT_IN_PROGRESS,
)


def make_event(tasks):
    return PersonEvent(
        id=7,
        event_type=EventType.OFFBOARDING,
        person_name="Sam Ray",
        requested_by="hr",
        created_at="2026-11-25T09:00:00+00:00",
        tasks=tasks,
    )


def completed(task_id, tier=RiskTier.AUTO):
    task = Task(id=task_id, title=task_id, risk_tier=tier)
    task.status = TaskStatus.COMPLETED
    task.executed_by = "automation"
    return task


def make_service():
    return RevocationAuditService(clock=lambda: "2026-11-25T17:00:00+00:00")


def test_complete_offboarding_reports_complete():
    event = make_event([
        completed("transfer_files"),
        completed("disable_sso_account", RiskTier.APPROVAL_REQUIRED),
        completed("revoke_remote_access", RiskTier.APPROVAL_REQUIRED),
    ])
    event.tasks[1].decided_by = "Priya"
    event.tasks[2].decided_by = "Priya"

    report = make_service().generate(event)

    assert report.verdict == VERDICT_COMPLETE
    assert report.exceptions == []
    assert report.outstanding == []
    assert len(report.task_ledger) == 3
    # Approval attribution survives in the ledger.
    sso = next(e for e in report.task_ledger if e["task_id"] == "disable_sso_account")
    assert sso["decided_by"] == "Priya"
    assert sso["risk_tier"] == "approval_required"


def test_rejected_task_is_an_exception_not_a_failure_to_run():
    """A rejection is a legitimate outcome that must be visible forever."""

    event = make_event([
        completed("transfer_files"),
    ])
    rejected = Task(
        id="reclaim_device",
        title="Wipe device",
        risk_tier=RiskTier.APPROVAL_REQUIRED,
    )
    rejected.status = TaskStatus.REJECTED
    rejected.decided_by = "Priya"
    rejected.decision_note = "legal hold active"
    event.tasks.append(rejected)

    report = make_service().generate(event)

    assert report.verdict == VERDICT_EXCEPTIONS
    assert len(report.exceptions) == 1
    assert "REJECTED by Priya" in report.exceptions[0]
    assert "legal hold active" in report.exceptions[0]


def test_failed_task_is_reported_with_reason():
    event = make_event([completed("transfer_files")])
    failed = Task(id="disable_sso_account", title="Disable SSO", risk_tier=RiskTier.APPROVAL_REQUIRED)
    failed.status = TaskStatus.FAILED
    failed.failure_reason = "directory API timeout"
    event.tasks.append(failed)

    report = make_service().generate(event)

    assert report.verdict == VERDICT_EXCEPTIONS
    assert "directory API timeout" in report.exceptions[0]


def test_incomplete_event_names_outstanding_tasks():
    event = make_event([
        completed("transfer_files"),
        Task(id="disable_sso_account", title="Disable SSO", risk_tier=RiskTier.APPROVAL_REQUIRED),
    ])

    report = make_service().generate(event)

    assert report.verdict == VERDICT_IN_PROGRESS
    assert report.outstanding == ["disable_sso_account (pending)"]
    assert report.exceptions == []

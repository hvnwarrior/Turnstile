"""Execution engine tests: the gate is structural, not advisory."""

import pytest

from app.models import EventType, PersonEvent, RiskTier, Task, TaskStatus
from app.services.execution_engine import (
    DryRunExecutor,
    ExecutionEngine,
)


class AuditRecorder:
    def __init__(self):
        self.events = []

    def record(self, event_type, actor, details=None):
        self.events.append({"event_type": event_type, "actor": actor, "details": details or {}})


def make_event(tasks):
    return PersonEvent(
        id=1,
        event_type=EventType.OFFBOARDING,
        person_name="departing employee",
        requested_by="hr",
        created_at="2026-11-25T09:00:00+00:00",
        tasks=tasks,
    )


def test_auto_task_runs_without_human():
    event = make_event([
        Task(id="t1", title="Archive mailbox", risk_tier=RiskTier.AUTO),
    ])
    engine = ExecutionEngine(audit_service=AuditRecorder())

    batch = engine.next_batch(event)
    assert [t.id for t in batch] == ["t1"]

    result = engine.execute(event, "t1", actor="automation")
    assert event.tasks[0].status == TaskStatus.COMPLETED
    assert result["dry_run"] is True


def test_gated_task_never_returns_in_next_batch():
    event = make_event([
        Task(id="t1", title="Revoke VPN access", risk_tier=RiskTier.APPROVAL_REQUIRED),
    ])
    engine = ExecutionEngine(audit_service=AuditRecorder())

    batch = engine.next_batch(event)

    assert batch == []
    assert event.tasks[0].status == TaskStatus.AWAITING_APPROVAL


def test_execute_blocked_without_approval():
    event = make_event([
        Task(id="t1", title="Revoke VPN access", risk_tier=RiskTier.APPROVAL_REQUIRED),
    ])
    engine = ExecutionEngine(audit_service=AuditRecorder())
    engine.next_batch(event)

    with pytest.raises(PermissionError):
        engine.execute(event, "t1", actor="automation")

    assert event.tasks[0].status == TaskStatus.AWAITING_APPROVAL


def test_approval_then_execution_completes_gated_task():
    event = make_event([
        Task(id="t1", title="Revoke VPN access", risk_tier=RiskTier.APPROVAL_REQUIRED),
    ])
    audit = AuditRecorder()
    engine = ExecutionEngine(audit_service=audit)
    engine.next_batch(event)

    engine.approve(event, "t1", approver="sam", note="departure confirmed")
    engine.execute(event, "t1", actor="automation")

    task = event.tasks[0]
    assert task.status == TaskStatus.COMPLETED
    assert task.decided_by == "sam"
    assert task.executed_by == "automation"

    kinds = [e["event_type"] for e in audit.events]
    assert "task_approved" in kinds and "task_executed" in kinds
    assert audit.events[0]["actor"] == "sam"


def test_dependencies_block_execution():
    event = make_event([
        Task(id="t1", title="Disable SSO account", risk_tier=RiskTier.APPROVAL_REQUIRED),
        Task(id="t2", title="Transfer files", risk_tier=RiskTier.AUTO, depends_on=["t1"]),
    ])
    engine = ExecutionEngine(audit_service=AuditRecorder())
    engine.next_batch(event)

    with pytest.raises(ValueError):
        engine.execute(event, "t2", actor="automation")


def test_approval_blocked_until_dependencies_complete():
    event = make_event([
        Task(id="t1", title="Disable SSO account", risk_tier=RiskTier.APPROVAL_REQUIRED),
        Task(id="t2", title="Transfer files", risk_tier=RiskTier.AUTO, depends_on=["t1"]),
    ])
    engine = ExecutionEngine(audit_service=AuditRecorder())
    engine.next_batch(event)

    with pytest.raises(ValueError):
        engine.approve(event, "t2", approver="sam")


def test_rejection_is_terminal_and_recorded():
    event = make_event([
        Task(id="t1", title="Wipe device", risk_tier=RiskTier.APPROVAL_REQUIRED),
    ])
    audit = AuditRecorder()
    engine = ExecutionEngine(audit_service=audit)
    engine.next_batch(event)

    engine.reject(event, "t1", rejector="sam", reason="legal hold active")

    task = event.tasks[0]
    assert task.status == TaskStatus.REJECTED
    assert task.decision_note == "legal hold active"

    with pytest.raises(ValueError):
        engine.approve(event, "t1", approver="pat")


def test_auto_task_cannot_be_approved_or_rejected():
    event = make_event([
        Task(id="t1", title="Add to group", risk_tier=RiskTier.AUTO),
    ])
    engine = ExecutionEngine(audit_service=AuditRecorder())
    engine.next_batch(event)

    with pytest.raises(ValueError):
        engine.approve(event, "t1", approver="sam")
    with pytest.raises(ValueError):
        engine.reject(event, "t1", rejector="sam", reason="no")


def test_unknown_task_errors():
    event = make_event([])
    engine = ExecutionEngine(audit_service=AuditRecorder())

    with pytest.raises(KeyError):
        engine.execute(event, "nope", actor="automation")


def test_dry_run_executor_records_actions():
    event = make_event([
        Task(id="t1", title="Archive mailbox", risk_tier=RiskTier.AUTO),
    ])
    executor = DryRunExecutor()
    engine = ExecutionEngine(executor=executor, audit_service=AuditRecorder())

    engine.next_batch(event)
    engine.execute(event, "t1", actor="automation")

    assert len(executor.actions) == 1
    assert executor.actions[0]["task_id"] == "t1"
    assert executor.actions[0]["executed_by"] == "automation"

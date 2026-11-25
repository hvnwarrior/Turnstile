"""Notification tests: informed assignees, provable in the log."""

from app.models import EventType, PersonEvent, RiskTier, Task, TaskStatus
from app.services.audit_service import AuditLogService
from app.services.checklist_builder import ChecklistBuilder
from app.services.execution_engine import ExecutionEngine
from app.services.notification_service import NotificationService


class RecordingNotifier:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send(self, recipient, message):
        self.sent.append((recipient, message))


class AuditRecorder:
    def __init__(self):
        self.events = []

    def record(self, event_type, actor, details=None):
        self.events.append(
            {"event_type": event_type, "actor": actor, "details": details or {}}
        )


def make_engine(notifier=None, audit=None):
    audit = audit or AuditRecorder()
    notifications = NotificationService(
        notifier=notifier or RecordingNotifier(), audit_service=audit
    )
    return ExecutionEngine(
        audit_service=audit, notification_service=notifications
    )


def make_event(tasks):
    return PersonEvent(
        id=9,
        event_type=EventType.OFFBOARDING,
        person_name="Sam Ray",
        requested_by="hr",
        created_at="2026-11-25T09:00:00+00:00",
        tasks=tasks,
    )


def test_assignees_notified_when_task_awaits_approval():
    notifier = RecordingNotifier()
    engine = make_engine(notifier=notifier)

    gated = Task(
        id="reclaim_device", title="Wipe and reclaim company device",
        risk_tier=RiskTier.APPROVAL_REQUIRED, assignees=["Priya", "it-admin"],
    )
    event = make_event([gated])

    engine.next_batch(event)  # gated -> awaiting_approval (notify fires)

    recipients = [r for r, _ in notifier.sent]
    assert recipients == ["Priya", "it-admin"]
    assert "needs your approval" in notifier.sent[0][1]


def test_repeated_advance_never_re_notifies():
    notifier = RecordingNotifier()
    engine = make_engine(notifier=notifier)

    gated = Task(
        id="reclaim_device", title="Wipe device",
        risk_tier=RiskTier.APPROVAL_REQUIRED, assignees=["Priya"],
    )
    event = make_event([gated])

    engine.next_batch(event)
    engine.next_batch(event)
    engine.next_batch(event)

    assert len(notifier.sent) == 1  # fired once, on the transition


def test_decision_notifies_other_assignees_not_the_actor():
    notifier = RecordingNotifier()
    audit = AuditRecorder()
    engine = make_engine(notifier=notifier, audit=audit)

    gated = Task(
        id="deactivate_badge", title="Deactivate badge",
        risk_tier=RiskTier.APPROVAL_REQUIRED, assignees=["Priya", "it-admin"],
    )
    event = make_event([gated])

    engine.next_batch(event)
    notifier.sent.clear()  # ignore the approval-request notifications

    engine.approve(event, "deactivate_badge", approver="Priya",
                   note="departure confirmed")

    # Only the co-assignee hears about it — the actor already knows.
    recipients = [r for r, _ in notifier.sent]
    assert recipients == ["it-admin"]
    assert "approved by Priya" in notifier.sent[0][1]
    assert "departure confirmed" in notifier.sent[0][1]


def test_rejection_notifies_co_assignees_with_reason():
    notifier = RecordingNotifier()
    engine = make_engine(notifier=notifier)

    gated = Task(
        id="reclaim_device", title="Wipe device",
        risk_tier=RiskTier.APPROVAL_REQUIRED, assignees=["Priya", "it-admin"],
    )
    event = make_event([gated])

    engine.next_batch(event)
    notifier.sent.clear()

    engine.reject(event, "reclaim_device", rejector="it-admin",
                  reason="legal hold active")

    assert any("rejected by it-admin" in m for _, m in notifier.sent)
    assert any("legal hold active" in m for _, m in notifier.sent)


def test_every_notification_is_in_the_audit_log():
    audit = AuditRecorder()
    engine = make_engine(audit=audit)

    gated = Task(
        id="reclaim_device", title="Wipe device",
        risk_tier=RiskTier.APPROVAL_REQUIRED, assignees=["Priya", "it-admin"],
    )
    event = make_event([gated])

    engine.next_batch(event)
    engine.approve(event, "reclaim_device", approver="Priya")

    sent = [
        e for e in audit.events
        if e["event_type"] == "notification_sent"
    ]
    # 2 requests (both assignees) + 1 decision notice (co-assignee)
    assert len(sent) == 3

    kinds = [e["details"]["kind"] for e in sent]
    assert kinds.count("approval_requested") == 2
    assert kinds.count("decision_approved") == 1

    by_recipient = [(e["details"]["recipient"], e["details"]["kind"]) for e in sent]
    assert ("Priya", "approval_requested") in by_recipient
    assert ("it-admin", "decision_approved") in by_recipient


def test_end_to_end_via_intake_produces_logged_notifications(tmp_path):
    """Full pipeline: build assigns roles; first batch notifies; the
    audit log can answer 'were the assignees ever told?'"""

    import json
    from pathlib import Path

    audit_log = AuditLogService(str(tmp_path / "audit.jsonl"))
    notifier = RecordingNotifier()

    from app.services.checklist_builder import ChecklistBuilder
    from app.services.execution_engine import ExecutionEngine
    from app.services.notification_service import NotificationService

    notifications = NotificationService(
        notifier=notifier, audit_service=audit_log
    )
    engine = ExecutionEngine(
        audit_service=audit_log, notification_service=notifications
    )
    builder = ChecklistBuilder(
        clock=lambda: "2026-11-25T10:00:00+00:00",
        default_approver="it-admin",
    )

    result = builder.build(
        EventType.OFFBOARDING, "Sam Ray", "hr",
        {
            "department": "Sales", "manager": "Priya",
            "last_day": "2026-12-04", "has_company_device": True,
        },
    )
    event = result.event

    engine.next_batch(event)

    # reclaim_device is manager-assigned -> Priya notified.
    reclaim = event.task_by_id("reclaim_device")
    assert reclaim.status == TaskStatus.AWAITING_APPROVAL

    log = audit_log.read_all()
    # AuditLogService flattens details into top-level log keys.
    requests = [
        e for e in log
        if e["event_type"] == "notification_sent"
        and e["kind"] == "approval_requested"
    ]
    assert any(r["recipient"] == "Priya" for r in requests)

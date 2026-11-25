"""ChecklistBuilder tests: refusal over fabrication.

Defining behaviors:
- Missing intake fields -> explicit refusal listing exactly what's
  needed, never a partial checklist.
- Templates carry human-authored risk tiers: irreversible actions are
  APPROVAL_REQUIRED in the registry, regardless of who calls build().
- Conditional tasks skip when their flag is absent — a destructive
  task is skipped, never assumed.
- The revocation audit fans in: it depends on every other included
  task, so it can only run when everything else finished.
"""

import pytest

from app.models import EventType, RiskTier, TaskStatus
from app.services.checklist_builder import ChecklistBuilder
from app.services.execution_engine import ExecutionEngine


class AuditRecorder:
    def record(self, event_type, actor, details=None):
        pass


ONBOARDING_PAYLOAD = {
    "role": "Customer Success",
    "department": "Sales",
    "manager": "Priya",
    "start_date": "2026-12-07",
    "needs_laptop": True,
}

OFFBOARDING_PAYLOAD = {
    "department": "Sales",
    "manager": "Priya",
    "last_day": "2026-12-04",
    "has_company_device": True,
    "has_building_access": True,
}


@pytest.fixture
def builder():
    return ChecklistBuilder(clock=lambda: "2026-11-25T10:00:00+00:00")


def test_build_success_onboarding(builder):
    result = builder.build(
        EventType.ONBOARDING, "Jordan Lee", "recruiting", ONBOARDING_PAYLOAD
    )

    assert result.ok
    assert result.event.person_name == "Jordan Lee"
    assert result.missing == []
    ids = [t.id for t in result.event.tasks]
    assert "create_account" in ids
    assert "hand_off_credentials" in ids  # gated task present


def test_refusal_lists_every_missing_field(builder):
    result = builder.build(
        EventType.OFFBOARDING,
        "Jordan Lee",
        "",  # missing requester
        {"department": "Sales"},  # manager + last_day missing
    )

    assert not result.ok
    assert sorted(result.missing) == ["last_day", "manager", "requested_by"]
    assert "Refusing" in result.message
    assert result.event is None


def test_refusal_not_partial_checklist(builder):
    result = builder.build(EventType.ONBOARDING, "", "recruiting", {})

    assert not result.ok
    # No half-built event leaks out.
    assert result.event is None


def test_unknown_event_type_refuses(builder):
    result = builder.build("sideways", "Jordan Lee", "hr", {})

    assert not result.ok
    assert "Unknown event type" in result.message


def test_conditional_task_skipped_when_flag_absent(builder):
    result = builder.build(
        EventType.ONBOARDING, "Jordan Lee", "recruiting",
        {**ONBOARDING_PAYLOAD, "needs_laptop": False},
    )

    ids = [t.id for t in result.event.tasks]
    assert "prepare_laptop" not in ids
    assert "install_role_software" not in ids


def test_offboarding_irreversible_tasks_are_gated(builder):
    result = builder.build(
        EventType.OFFBOARDING, "Sam Ray", "hr", OFFBOARDING_PAYLOAD
    )

    gated = {t.id for t in result.event.tasks if t.is_gated}
    assert gated == {
        "disable_sso_account",
        "revoke_remote_access",
        "reclaim_device",
        "deactivate_badge",
    }


def test_revocation_audit_fans_in(builder):
    result = builder.build(
        EventType.OFFBOARDING, "Sam Ray", "hr", OFFBOARDING_PAYLOAD
    )

    audit_task = result.event.task_by_id("run_revocation_audit")
    others = {t.id for t in result.event.tasks} - {"run_revocation_audit"}

    assert set(audit_task.depends_on) == others


def test_full_offboarding_run_through_engine(builder):
    """End-to-end: intake -> checklist -> gated execution completes,
    with gated tasks requiring approval at each step."""

    result = builder.build(
        EventType.OFFBOARDING, "Sam Ray", "hr", OFFBOARDING_PAYLOAD
    )
    engine = ExecutionEngine(audit_service=AuditRecorder())
    event = result.event

    completed = set()

    # Walk the checklist to completion; bounded iterations as safety.
    for _ in range(50):
        batch = engine.next_batch(event)

        if not batch:
            awaiting = [
                t for t in event.tasks
                if t.status == TaskStatus.AWAITING_APPROVAL
            ]
            if not awaiting:
                break
            for task in awaiting:
                engine.approve(event, task.id, approver="Priya")
            continue

        for task in batch:
            engine.execute(event, task.id, actor="automation")

        completed |= {t.id for t in event.tasks if t.status == TaskStatus.COMPLETED}

    statuses = {t.id: t.status for t in event.tasks}
    assert all(s == TaskStatus.COMPLETED for s in statuses.values())
    assert "disable_sso_account" in completed

    # Every gated task got its approval from a named human.
    for task in event.tasks:
        if task.is_gated:
            assert task.decided_by == "Priya"


def test_built_checklist_dependencies_resolve(builder):
    """Every depends_on references an included task (no dangling deps)."""

    for payload in (ONBOARDING_PAYLOAD, OFFBOARDING_PAYLOAD):
        result = builder.build(
            EventType.OFFBOARDING, "X", "hr", payload
        ) if "last_day" in payload else builder.build(
            EventType.ONBOARDING, "X", "hr", payload
        )
        ids = {t.id for t in result.event.tasks}
        for task in result.event.tasks:
            assert set(task.depends_on) <= ids, (
                f"dangling dependency {task.id} -> {task.depends_on}"
            )

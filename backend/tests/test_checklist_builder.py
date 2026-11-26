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

import hashlib
import hmac

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
    with each gated task approved by one of ITS assignees."""

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
                # The first assignee acts — they are authorized by
                # construction.
                engine.approve(event, task.id, approver=task.assignees[0])
            continue

        for task in batch:
            engine.execute(event, task.id, actor="automation")

        completed |= {t.id for t in event.tasks if t.status == TaskStatus.COMPLETED}

    statuses = {t.id: t.status for t in event.tasks}
    assert all(s == TaskStatus.COMPLETED for s in statuses.values())
    assert "disable_sso_account" in completed

    # Every gated task got its approval from a NAMED ASSIGNEE.
    for task in event.tasks:
        if task.is_gated:
            assert task.decided_by in task.assignees


def test_non_assignee_cannot_approve(builder):
    """The gate knows WHO is allowed: the manager can wipe the device,
    but the manager cannot disable SSO (assigned to the IT admin)."""

    from app.services.execution_engine import ExecutionEngine as Engine

    result = builder.build(
        EventType.OFFBOARDING, "Sam Ray", "hr", OFFBOARDING_PAYLOAD
    )
    engine = Engine(audit_service=AuditRecorder())
    event = result.event

    engine.next_batch(event)

    # reclaim_device is assigned to the manager (Priya)...
    reclaim = event.task_by_id("reclaim_device")
    assert reclaim.assignees == ["Priya"]
    engine.approve(event, "reclaim_device", approver="Priya")

    # Unblock the SSO disable (finish its AUTO dependencies) so it is
    # legitimately awaiting approval — then the AUTHORIZATION rule is
    # the only thing that can stop Priya.
    for task_id in ("transfer_files", "set_mail_forwarding"):
        engine.execute(event, task_id, actor="automation")
    engine.next_batch(event)

    sso = event.task_by_id("disable_sso_account")
    assert sso.assignees == ["it-admin"]
    assert sso.status == TaskStatus.AWAITING_APPROVAL
    with pytest.raises(PermissionError):
        engine.approve(event, "disable_sso_account", approver="Priya")

    # And the assignee succeeds where the non-assignee was refused.
    engine.approve(event, "disable_sso_account", approver="it-admin")
    assert sso.status == TaskStatus.APPROVED


def test_missing_manager_refuses_owned_tasks(builder):
    """Device wipe is manager-owned: no manager in intake -> refusal,
    because an irreversible step must not float ownerless."""

    payload = {k: v for k, v in OFFBOARDING_PAYLOAD.items() if k != "manager"}
    result = builder.build(EventType.OFFBOARDING, "Sam Ray", "hr", payload)

    # Refusal happens at intake-validation level for manager itself...
    if not result.ok:
        assert "manager" in result.missing
        return

    # ...or, if manager were optional, the owned task must refuse.
    assert any("approver:" in m for m in result.missing)


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


# ----------------------------------------------------------------------
# Fallback approver: no resolvable assignee -> IT, visibly
# ----------------------------------------------------------------------

NO_MANAGER_ONBOARDING = {
    "role": "Customer Success",
    "department": "Sales",
    # manager ABSENT: role resolution finds no handle
    "start_date": "2026-12-07",
}

NO_MANAGER_OFFBOARDING = {
    "department": "Sales",
    # manager ABSENT
    "last_day": "2026-12-04",
    "has_company_device": True,
    "has_building_access": True,
}


@pytest.fixture
def fallback_builder():
    return ChecklistBuilder(
        clock=lambda: "2026-11-25T10:00:00+00:00",
        fallback_approver="it-admin",
    )


def test_fallback_assigns_unowned_gated_tasks(fallback_builder):
    """No manager in intake: the manager-owned gated task is assigned
    to the fallback approver instead of refusing the build."""

    result = fallback_builder.build(
        EventType.OFFBOARDING, "Sam Ray", "hr", NO_MANAGER_OFFBOARDING
    )

    assert result.ok
    assert result.missing == []

    reclaim = result.event.task_by_id("reclaim_device")
    # Previously unowned; now IT's, by explicit substitution.
    assert reclaim.assignees == ["it-admin"]
    assert result.substitutions == [
        {
            "task_id": "reclaim_device",
            "assigned_to": "it-admin",
            "reason": "no_resolvable_assignee",
        }
    ]


def test_fallback_used_across_templates(fallback_builder):
    """Onboarding survives a missing manager too. Note: credential
    handoff has roles (manager, it_admin) — IT resolves, so the task
    is assigned WITHOUT a substitution; only manager-ONLY gates
    produce substitutions (like offboarding's reclaim_device)."""

    result = fallback_builder.build(
        EventType.ONBOARDING, "Jordan Lee", "recruiting", NO_MANAGER_ONBOARDING
    )

    assert result.ok
    handoff = result.event.task_by_id("hand_off_credentials")
    assert handoff.assignees == ["it-admin"]
    assert result.substitutions == []


def test_no_fallback_still_refuses_unowned_gated_tasks(builder):
    """Default builder (no fallback configured): the original fail-
    closed stance is unchanged."""

    result = builder.build(
        EventType.OFFBOARDING, "Sam Ray", "hr", NO_MANAGER_OFFBOARDING
    )

    assert not result.ok
    # Refused at intake validation: manager is still a required field
    # when no fallback is configured.
    assert "manager" in result.missing


def test_fallback_substitution_audited(tmp_path):
    """Substitutions surface through HRIS intake AND the audit log."""

    from app.services.audit_service import AuditLogService
    from app.services.event_store import EventStore
    from app.services.execution_engine import DryRunExecutor, ExecutionEngine
    from app.services.hris_intake import HrisIntakeService
    import json as jsonlib

    audit = AuditLogService(str(tmp_path / "audit.jsonl"))
    builder = ChecklistBuilder(
        clock=lambda: "2026-11-25T10:00:00+00:00",
        fallback_approver="it-admin",
    )
    service = HrisIntakeService(
        builder=builder,
        engine=ExecutionEngine(executor=DryRunExecutor(), audit_service=audit),
        store=EventStore(str(tmp_path / "db.sqlite")),
        webhook_secret="whsec_test",
        require_signature=True,
        audit_service=audit,
    )

    body = jsonlib.dumps(
        {
            "type": "employee.terminated",
            "person_name": "Sam Ray",
            "requested_by": "hr",
            "details": NO_MANAGER_OFFBOARDING,
        }
    ).encode()

    signature = hmac.new(b"whsec_test", body, hashlib.sha256).hexdigest()
    response = service.ingest(body, signature)

    assert response["accepted"] is True
    assert response["assignee_substitutions"] == [
        {
            "task_id": "reclaim_device",
            "assigned_to": "it-admin",
            "reason": "no_resolvable_assignee",
        }
    ]

    with (tmp_path / "audit.jsonl").open(encoding="utf-8") as fh:
        events = [jsonlib.loads(line) for line in fh if line.strip()]
    fallback_events = [
        e for e in events if e["event_type"] == "assignee_fallback"
    ]
    assert len(fallback_events) == 1
    # Flattened detail keys, per the audit-log convention.
    assert fallback_events[0]["task_id"] == "reclaim_device"
    assert fallback_events[0]["assigned_to"] == "it-admin"
    assert fallback_events[0]["reason"] == "no_resolvable_assignee"


def test_fallback_does_not_touch_resolved_assignees(fallback_builder):
    """Tasks that already resolve fine (e.g. disable_sso_account, IT
    only) are untouched: no substitution noise."""

    result = fallback_builder.build(
        EventType.OFFBOARDING, "Sam Ray", "hr", NO_MANAGER_OFFBOARDING
    )

    assert result.event.task_by_id("disable_sso_account").assignees == [
        "it-admin"
    ]
    substituted_ids = {s["task_id"] for s in result.substitutions}
    assert "disable_sso_account" not in substituted_ids

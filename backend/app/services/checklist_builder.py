"""Intake validation and checklist construction.

Refusal over fabrication, applied to onboarding/offboarding:

- If required intake fields are missing, build() REFUSES and returns
  the exact list of what's missing. It never builds a partial checklist
  with guessed values: a guessed last_day or wrong manager is worse
  than no checklist.
- Checklists come from human-authored templates. Risk tiers and
  dependencies are declared in code, reviewed like any other code —
  the system never infers risk from free text.
- Conditional tasks (device reclaim, badge deactivation) are driven by
  explicit boolean flags in the intake payload, with sane defaults
  (absent = skip a destructive task; presence is asserted by the
  requester, never assumed).

A future LLM advisor may SUGGEST additional tasks, but only by picking
from these same task specs (closed vocabulary) — it never assigns risk
tiers or invents dependencies. That advisor lives in its own module.
"""

from dataclasses import dataclass, field

from app.models import EventType, PersonEvent, RiskTier, Task


# ----------------------------------------------------------------------
# Intake requirements
# ----------------------------------------------------------------------

REQUIRED_INTAKE_FIELDS = {
    EventType.ONBOARDING: ["role", "department", "manager", "start_date"],
    EventType.OFFBOARDING: ["department", "manager", "last_day"],
}


# ----------------------------------------------------------------------
# Task specs: human-authored, code-reviewed, closed vocabulary
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class TaskSpec:
    id: str
    title: str
    risk_tier: RiskTier
    depends_on: tuple[str, ...] = ()
    # Who is accountable for approving this task, as ROLES resolved at
    # build time: "manager" (from intake) and/or "it_admin" (from
    # settings). Roles, not handles, so assignments follow the data.
    assignee_roles: tuple[str, ...] = ()
    # Included only when this boolean intake flag is truthy. Absent or
    # falsy means the task is skipped — with defaults chosen so that a
    # destructive task is skipped rather than assumed.
    requires_flag: str | None = None
    # Days relative to the event's anchor date (start_date for
    # onboarding, last_day for offboarding) by which this task is due.
    # Negative = before the anchor (prep work), positive = after
    # (cleanup/grace). None = no deadline declared for this task.
    due_offset_days: int | None = None

    @property
    def is_gated(self) -> bool:
        return self.risk_tier == RiskTier.APPROVAL_REQUIRED


ONBOARDING_TEMPLATE: tuple[TaskSpec, ...] = (
    TaskSpec(
        id="create_account",
        due_offset_days=-3,
        title="Create directory account",
        risk_tier=RiskTier.AUTO,
    ),
    TaskSpec(
        id="add_department_groups",
        due_offset_days=-2,
        title="Add to department mailing lists and groups",
        risk_tier=RiskTier.AUTO,
        depends_on=("create_account",),
    ),
    TaskSpec(
        id="prepare_laptop",
        due_offset_days=-2,
        title="Prepare and assign laptop",
        risk_tier=RiskTier.AUTO,
        depends_on=("create_account",),
        requires_flag="needs_laptop",
    ),
    TaskSpec(
        id="install_role_software",
        due_offset_days=-1,
        title="Install role-specific software",
        risk_tier=RiskTier.AUTO,
        depends_on=("prepare_laptop",),
        requires_flag="needs_laptop",
    ),
    TaskSpec(
        # Credential handoff gives a third party access to sensitive
        # material: gated on purpose.
        id="hand_off_credentials",
        due_offset_days=0,
        title="Release initial credentials to manager for first-day handoff",
        risk_tier=RiskTier.APPROVAL_REQUIRED,
        depends_on=("create_account",),
        assignee_roles=("manager", "it_admin"),
    ),
    TaskSpec(
        id="schedule_orientation",
        due_offset_days=0,
        title="Schedule orientation with manager",
        risk_tier=RiskTier.AUTO,
    ),
)


OFFBOARDING_TEMPLATE: tuple[TaskSpec, ...] = (
    TaskSpec(
        id="transfer_files",
        due_offset_days=0,
        title="Transfer ownership of files and shared drives",
        risk_tier=RiskTier.AUTO,
    ),
    TaskSpec(
        id="set_mail_forwarding",
        due_offset_days=0,
        title="Set mail forwarding/auto-reply to manager",
        risk_tier=RiskTier.AUTO,
    ),
    TaskSpec(
        # Killing the SSO account is the master switch: gated.
        id="disable_sso_account",
        due_offset_days=1,
        title="Disable SSO account",
        risk_tier=RiskTier.APPROVAL_REQUIRED,
        depends_on=("transfer_files", "set_mail_forwarding"),
        assignee_roles=("it_admin",),
    ),
    TaskSpec(
        id="revoke_remote_access",
        due_offset_days=1,
        title="Revoke VPN and remote access",
        risk_tier=RiskTier.APPROVAL_REQUIRED,
        depends_on=("disable_sso_account",),
        assignee_roles=("it_admin",),
    ),
    TaskSpec(
        # Re-licensing is easy; revoking a license is reversible.
        id="revoke_saas_licenses",
        due_offset_days=3,
        title="Revoke SaaS licenses",
        risk_tier=RiskTier.AUTO,
        depends_on=("disable_sso_account",),
    ),
    TaskSpec(
        id="reclaim_device",
        due_offset_days=5,
        title="Wipe and reclaim company device",
        risk_tier=RiskTier.APPROVAL_REQUIRED,
        requires_flag="has_company_device",
        assignee_roles=("manager",),
    ),
    TaskSpec(
        id="deactivate_badge",
        due_offset_days=5,
        title="Deactivate building badge",
        risk_tier=RiskTier.APPROVAL_REQUIRED,
        requires_flag="has_building_access",
        assignee_roles=("it_admin", "manager"),
    ),
    # Fan-in: added at build time, depends on every other included task.
    TaskSpec(
        id="run_revocation_audit",
        due_offset_days=7,
        title="Verify all access revoked and produce audit report",
        risk_tier=RiskTier.AUTO,
    ),
)


TEMPLATES = {
    EventType.ONBOARDING: ONBOARDING_TEMPLATE,
    EventType.OFFBOARDING: OFFBOARDING_TEMPLATE,
}


# ----------------------------------------------------------------------
# Builder
# ----------------------------------------------------------------------

@dataclass
class BuildResult:
    """Either a built event, or a refusal with the exact missing data."""

    event: PersonEvent | None = None
    missing: list[str] = field(default_factory=list)
    message: str = ""
    # Gated tasks that resolved to no assignee and were assigned to the
    # fallback approver instead: [{"task_id", "assigned_to", "reason"}].
    # Present only when fallback is configured; audited by the caller.
    substitutions: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.event is not None


def _truthy(value) -> bool:
    return bool(value)


def _anchor_date(event_type: EventType, details: dict) -> str | None:
    """The date everything else is measured against: start_date for
    onboarding, last_day for offboarding. Returned as-is (ISO string
    from intake) or None when absent/unparseable — a task with no
    anchor gets no fabricated deadline."""

    from datetime import date

    key = (
        "start_date"
        if event_type == EventType.ONBOARDING
        else "last_day"
    )
    raw = str(details.get(key, "")).strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError:
        return None


class ChecklistBuilder:
    def __init__(
        self,
        clock=None,
        default_approver: str = "it-admin",
        fallback_approver: str = "",
    ) -> None:
        """clock: zero-arg callable returning an ISO timestamp string;
        defaults to utcnow. default_approver: the handle substituted for
        the "it_admin" role. fallback_approver: when a gated task
        resolves to no assignee (e.g. manager missing from intake),
        assign it to this handle instead of refusing the whole build.
        Empty (default) keeps the fail-closed refusal. Injectable for
        tests."""

        from datetime import datetime, timezone

        self.clock = clock or (
            lambda: datetime.now(timezone.utc).isoformat()
        )
        self.default_approver = default_approver
        self.fallback_approver = fallback_approver

    def build(
        self,
        event_type: EventType,
        person_name: str,
        requested_by: str,
        details: dict,
    ) -> BuildResult:
        """Validate intake, then instantiate the checklist.

        Refuses with a complete missing-field list when intake is
        incomplete; never partially builds.
        """

        if not isinstance(event_type, EventType):
            return BuildResult(
                missing=[],
                message=(
                    f"Unknown event type '{event_type}': expected "
                    "'onboarding' or 'offboarding'. Refusing to build."
                ),
            )

        template = TEMPLATES[event_type]
        required = list(REQUIRED_INTAKE_FIELDS[event_type])

        if self.fallback_approver:
            # A configured fallback absorbs an unresolvable manager:
            # manager-owned gates route to IT (audited, never silent),
            # so a missing manager no longer blocks the whole
            # checklist. Without a fallback, manager stays required.
            required = [f for f in required if f != "manager"]

        missing = [
            field_name
            for field_name in ([*REQUIRED_INTAKE, *required])
            if not _truthy(_intake_value(field_name, person_name, requested_by, details))
        ]

        if missing:
            return BuildResult(
                missing=missing,
                message=(
                    f"Refusing to build {event_type.value} checklist: "
                    f"missing required data: {', '.join(missing)}. "
                    "A partial checklist would embed guesses into "
                    "access decisions."
                ),
            )

        tasks, unowned, substitutions = self._instantiate(
            event_type, template, details
        )

        if unowned:
            # An irreversible step with no accountable human is a
            # refusal, same stance as missing intake data.
            return BuildResult(
                missing=[f"approver:{task_id}" for task_id in unowned],
                message=(
                    "Refusing to build: irreversible step(s) with no "
                    f"accountable human: {', '.join(unowned)}. Configure "
                    "an approver or name a manager."
                ),
            )

        return BuildResult(
            event=PersonEvent(
                id=0,  # assigned by persistence layer
                event_type=event_type,
                person_name=person_name,
                requested_by=requested_by,
                created_at=self.clock(),
                tasks=tasks,
            ),
            substitutions=substitutions,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _resolve_assignees(self, roles, details: dict) -> list[str]:
        resolved: list[str] = []

        for role in roles:
            if role == "manager":
                handle = details.get("manager", "")
            elif role == "it_admin":
                handle = self.default_approver
            else:
                handle = ""

            if handle and handle not in resolved:
                resolved.append(handle)

        return resolved

    def _instantiate(self, event_type: EventType, template, details: dict):
        """Returns (tasks, unowned_gated_ids, substitutions)."""

        included_specs = [
            spec
            for spec in template
            if spec.requires_flag is None
            or _truthy(details.get(spec.requires_flag))
        ]

        included_ids = [spec.id for spec in included_specs]

        tasks: list[Task] = []
        unowned: list[str] = []
        substitutions: list[dict] = []

        anchor = _anchor_date(event_type, details)

        for spec in included_specs:
            dependencies = list(spec.depends_on)

            if spec.id == "run_revocation_audit":
                # Depends on everything else in the checklist.
                dependencies = [
                    task_id
                    for task_id in included_ids
                    if task_id != spec.id
                ]

            assignees = self._resolve_assignees(spec.assignee_roles, details)

            if spec.is_gated and not assignees:
                if self.fallback_approver:
                    # No accountable human resolvable from intake ->
                    # an explicit, AUDITED assignment to the fallback
                    # handle, so the decision has a name (IT) while a
                    # better owner can be sorted out. Never silent.
                    assignees = [self.fallback_approver]
                    substitutions.append(
                        {
                            "task_id": spec.id,
                            "assigned_to": self.fallback_approver,
                            "reason": "no_resolvable_assignee",
                        }
                    )
                else:
                    unowned.append(spec.id)

            due_date = None
            if anchor is not None and spec.due_offset_days is not None:
                from datetime import date, timedelta

                due_date = (
                    date.fromisoformat(anchor)
                    + timedelta(days=spec.due_offset_days)
                ).isoformat()

            tasks.append(
                Task(
                    id=spec.id,
                    title=spec.title,
                    risk_tier=spec.risk_tier,
                    depends_on=dependencies,
                    assignees=assignees,
                    due_date=due_date,
                )
            )

        return tasks, unowned, substitutions


REQUIRED_INTAKE = ["person_name", "requested_by"]


def _intake_value(field_name, person_name, requested_by, details):
    if field_name == "person_name":
        return person_name
    if field_name == "requested_by":
        return requested_by
    return details.get(field_name)

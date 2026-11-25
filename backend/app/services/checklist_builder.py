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
    # Included only when this boolean intake flag is truthy. Absent or
    # falsy means the task is skipped — with defaults chosen so that a
    # destructive task is skipped rather than assumed.
    requires_flag: str | None = None


ONBOARDING_TEMPLATE: tuple[TaskSpec, ...] = (
    TaskSpec(
        id="create_account",
        title="Create directory account",
        risk_tier=RiskTier.AUTO,
    ),
    TaskSpec(
        id="add_department_groups",
        title="Add to department mailing lists and groups",
        risk_tier=RiskTier.AUTO,
        depends_on=("create_account",),
    ),
    TaskSpec(
        id="prepare_laptop",
        title="Prepare and assign laptop",
        risk_tier=RiskTier.AUTO,
        depends_on=("create_account",),
        requires_flag="needs_laptop",
    ),
    TaskSpec(
        id="install_role_software",
        title="Install role-specific software",
        risk_tier=RiskTier.AUTO,
        depends_on=("prepare_laptop",),
        requires_flag="needs_laptop",
    ),
    TaskSpec(
        # Credential handoff gives a third party access to sensitive
        # material: gated on purpose.
        id="hand_off_credentials",
        title="Release initial credentials to manager for first-day handoff",
        risk_tier=RiskTier.APPROVAL_REQUIRED,
        depends_on=("create_account",),
    ),
    TaskSpec(
        id="schedule_orientation",
        title="Schedule orientation with manager",
        risk_tier=RiskTier.AUTO,
    ),
)


OFFBOARDING_TEMPLATE: tuple[TaskSpec, ...] = (
    TaskSpec(
        id="transfer_files",
        title="Transfer ownership of files and shared drives",
        risk_tier=RiskTier.AUTO,
    ),
    TaskSpec(
        id="set_mail_forwarding",
        title="Set mail forwarding/auto-reply to manager",
        risk_tier=RiskTier.AUTO,
    ),
    TaskSpec(
        # Killing the SSO account is the master switch: gated.
        id="disable_sso_account",
        title="Disable SSO account",
        risk_tier=RiskTier.APPROVAL_REQUIRED,
        depends_on=("transfer_files", "set_mail_forwarding"),
    ),
    TaskSpec(
        id="revoke_remote_access",
        title="Revoke VPN and remote access",
        risk_tier=RiskTier.APPROVAL_REQUIRED,
        depends_on=("disable_sso_account",),
    ),
    TaskSpec(
        # Re-licensing is easy; revoking a license is reversible.
        id="revoke_saas_licenses",
        title="Revoke SaaS licenses",
        risk_tier=RiskTier.AUTO,
        depends_on=("disable_sso_account",),
    ),
    TaskSpec(
        id="reclaim_device",
        title="Wipe and reclaim company device",
        risk_tier=RiskTier.APPROVAL_REQUIRED,
        requires_flag="has_company_device",
    ),
    TaskSpec(
        id="deactivate_badge",
        title="Deactivate building badge",
        risk_tier=RiskTier.APPROVAL_REQUIRED,
        requires_flag="has_building_access",
    ),
    # Fan-in: added at build time, depends on every other included task.
    TaskSpec(
        id="run_revocation_audit",
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

    @property
    def ok(self) -> bool:
        return self.event is not None


def _truthy(value) -> bool:
    return bool(value)


class ChecklistBuilder:
    def __init__(self, clock=None) -> None:
        """clock: zero-arg callable returning an ISO timestamp string;
        defaults to utcnow. Injectable for tests."""

        from datetime import datetime, timezone

        self.clock = clock or (
            lambda: datetime.now(timezone.utc).isoformat()
        )

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
        required = REQUIRED_INTAKE_FIELDS[event_type]

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

        tasks = self._instantiate(template, details)

        return BuildResult(
            event=PersonEvent(
                id=0,  # assigned by persistence layer
                event_type=event_type,
                person_name=person_name,
                requested_by=requested_by,
                created_at=self.clock(),
                tasks=tasks,
            ),
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _instantiate(self, template, details: dict) -> list[Task]:
        included_specs = [
            spec
            for spec in template
            if spec.requires_flag is None
            or _truthy(details.get(spec.requires_flag))
        ]

        included_ids = [spec.id for spec in included_specs]

        tasks: list[Task] = []

        for spec in included_specs:
            dependencies = list(spec.depends_on)

            if spec.id == "run_revocation_audit":
                # Depends on everything else in the checklist.
                dependencies = [
                    task_id
                    for task_id in included_ids
                    if task_id != spec.id
                ]

            tasks.append(
                Task(
                    id=spec.id,
                    title=spec.title,
                    risk_tier=spec.risk_tier,
                    depends_on=dependencies,
                )
            )

        return tasks


REQUIRED_INTAKE = ["person_name", "requested_by"]


def _intake_value(field_name, person_name, requested_by, details):
    if field_name == "person_name":
        return person_name
    if field_name == "requested_by":
        return requested_by
    return details.get(field_name)

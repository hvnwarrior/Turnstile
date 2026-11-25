"""Turnstile domain model: hires, departures, and their checklists.

Core idea: onboarding/offboarding decomposes into ordered tasks. Some
tasks are safe to execute automatically (e.g., adding a user to a group);
others are irreversible or high blast-radius (revoking access, wiping
devices) and MUST be approved by a named human first. Risk is declared
per task type, never inferred from free text.

Every task records what happened and who decided it — mirroring
TriageBot's "every path is auditable" principle.
"""

from dataclasses import dataclass, field
from enum import Enum


class EventType(str, Enum):
    ONBOARDING = "onboarding"
    OFFBOARDING = "offboarding"


class RiskTier(str, Enum):
    """What it takes for a task to run.

    AUTO: reversible, low blast radius. Executes without a human.
    APPROVAL_REQUIRED: destructive, irreversible, or accesses sensitive
    data. A named human must approve before execution. Refusing to run
    without approval is the default, not a special case.
    """

    AUTO = "auto"
    APPROVAL_REQUIRED = "approval_required"


class TaskStatus(str, Enum):
    PENDING = "pending"          # waiting for its dependencies
    READY = "ready"              # dependencies met; AUTO tasks run now
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"        # human said yes, not yet executed
    REJECTED = "rejected"        # human said no (recorded, permanent)
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Task:
    """One step of an onboarding or offboarding run."""

    id: str
    title: str
    risk_tier: RiskTier
    depends_on: list[str] = field(default_factory=list)  # task ids
    status: TaskStatus = TaskStatus.PENDING
    executed_by: str | None = None      # "automation" or a human handle
    decided_by: str | None = None       # approver handle, if gated
    decision_note: str | None = None   # why the human said yes/no
    failure_reason: str | None = None

    @property
    def is_gated(self) -> bool:
        return self.risk_tier == RiskTier.APPROVAL_REQUIRED

    def runnable_now(self) -> bool:
        return self.status in (TaskStatus.READY, TaskStatus.APPROVED)

    def terminal(self) -> bool:
        return self.status in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.REJECTED,
        )


@dataclass
class PersonEvent:
    """A hire or a departure, with its dependency-ordered checklist."""

    id: int
    event_type: EventType
    person_name: str
    requested_by: str             # e.g., HR or the hiring manager
    created_at: str
    tasks: list[Task] = field(default_factory=list)
    missing_data: list[str] = field(default_factory=list)  # refusal grounds

    def task_by_id(self, task_id: str) -> Task | None:
        for task in self.tasks:
            if task.id == task_id:
                return task
        return None

    def next_ready(self) -> list[Task]:
        """Tasks whose dependencies are all complete and are runnable."""

        ready: list[Task] = []
        done = {
            task.id
            for task in self.tasks
            if task.status == TaskStatus.COMPLETED
        }

        for task in self.tasks:
            if task.status == TaskStatus.PENDING and all(
                dep in done for dep in task.depends_on
            ):
                ready.append(task)

        return ready

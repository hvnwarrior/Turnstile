"""Task execution with approval gates enforced by structure.

Rules the engine makes impossible to bypass:

- An APPROVAL_REQUIRED task cannot run until a named human approves it.
  Approving a gated task marks it APPROVED; a second, separate call
  executes it. There is no code path from AWAITING_APPROVAL to COMPLETED
  in one step, so no refactor can accidentally skip the gate.
- Approval and execution are attributed to named actors, recorded on
  the task and in the audit log.
- Dependencies are respected: a task whose prerequisites are not all
  COMPLETED cannot be executed, approved, or returned by next_batch().
- Rejection is terminal and recorded with the reason.

Execution itself goes through a TaskExecutor the caller provides. The
default executor is a dry-run recorder (suitable for demos and tests);
production executors wrap Google Workspace / Entra ID / Jamf APIs.
"""

from app.models import (
    EventType,
    PersonEvent,
    RiskTier,
    Task,
    TaskStatus,
)


class DryRunExecutor:
    """Records would-be actions instead of performing them.

    The demo story: Turnstile proposes the exact commands; a human
    approves; the executor layer is swapped for the real integration
    without touching approval logic.
    """

    def __init__(self) -> None:
        self.actions: list[dict] = []

    def execute(self, event: PersonEvent, task: Task, actor: str) -> dict:
        # Actor is passed explicitly: at this point the task hasn't been
        # marked completed yet, so task.executed_by is still None.
        record = {
            "event_id": event.id,
            "task_id": task.id,
            "title": task.title,
            "executed_by": actor,
            "dry_run": True,
        }
        self.actions.append(record)
        return record


class ExecutionEngine:
    def __init__(self, executor=None, audit_service=None) -> None:
        self.executor = executor or DryRunExecutor()
        self.audit_service = audit_service

    # ------------------------------------------------------------------
    # Progressing a run
    # ------------------------------------------------------------------

    def next_batch(self, event: PersonEvent) -> list[Task]:
        """Mark newly unblocked tasks and return what can happen now.

        Pending tasks with all dependencies complete become READY
        (AUTO) or AWAITING_APPROVAL (gated). What is returned:
        - AUTO ready tasks: cleared to execute.
        - APPROVED gated tasks: cleared to execute.
        Awaiting-approval tasks are deliberately NOT returned here;
        they must go through approve() first.
        """

        cleared: list[Task] = []

        for task in event.next_ready():
            if task.is_gated:
                task.status = TaskStatus.AWAITING_APPROVAL
            else:
                task.status = TaskStatus.READY
                cleared.append(task)

        done = {
            task.id
            for task in event.tasks
            if task.status == TaskStatus.COMPLETED
        }

        for task in event.tasks:
            if task.status == TaskStatus.APPROVED and all(
                dep in done for dep in task.depends_on
            ):
                cleared.append(task)

        return cleared

    def execute(self, event: PersonEvent, task_id: str, actor: str) -> dict:
        """Run one cleared task. Gated tasks must already be APPROVED."""

        task = self._require_task(event, task_id)

        if task.is_gated and task.status != TaskStatus.APPROVED:
            raise PermissionError(
                f"Task '{task_id}' requires human approval before execution."
            )

        if not task.is_gated and task.status != TaskStatus.READY:
            raise ValueError(
                f"Task '{task_id}' is not ready to execute (status: "
                f"{task.status.value})."
            )

        if not self._dependencies_met(event, task):
            raise ValueError(
                f"Task '{task_id}' has incomplete dependencies."
            )

        result = self.executor.execute(event, task, actor)

        task.status = TaskStatus.COMPLETED
        task.executed_by = actor

        self._audit(
            "task_executed",
            actor=actor,
            event=event,
            task=task,
            details={"dry_run": result.get("dry_run", False)},
        )

        return result

    # ------------------------------------------------------------------
    # Human decisions
    # ------------------------------------------------------------------

    def approve(
        self,
        event: PersonEvent,
        task_id: str,
        approver: str,
        note: str | None = None,
    ) -> Task:
        """A named human clears a gated task for execution."""

        task = self._require_task(event, task_id)

        if not task.is_gated:
            raise ValueError(
                f"Task '{task_id}' is not approval-gated; nothing to approve."
            )

        if task.status not in (TaskStatus.AWAITING_APPROVAL, TaskStatus.APPROVED):
            raise ValueError(
                f"Task '{task_id}' cannot be approved in status "
                f"'{task.status.value}'."
            )

        if not self._dependencies_met(event, task):
            raise ValueError(
                f"Task '{task_id}' has incomplete dependencies."
            )

        task.status = TaskStatus.APPROVED
        task.decided_by = approver
        task.decision_note = note

        self._audit(
            "task_approved",
            actor=approver,
            event=event,
            task=task,
            details={"note": note},
        )

        return task

    def reject(
        self,
        event: PersonEvent,
        task_id: str,
        rejector: str,
        reason: str,
    ) -> Task:
        """Refuse a gated task. Terminal and recorded."""

        task = self._require_task(event, task_id)

        if not task.is_gated:
            raise ValueError(
                f"Task '{task_id}' is not approval-gated; nothing to reject."
            )

        if task.terminal():
            raise ValueError(
                f"Task '{task_id}' is already terminal "
                f"({task.status.value})."
            )

        task.status = TaskStatus.REJECTED
        task.decided_by = rejector
        task.decision_note = reason

        self._audit(
            "task_rejected",
            actor=rejector,
            event=event,
            task=task,
            details={"reason": reason},
        )

        return task

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _require_task(self, event: PersonEvent, task_id: str) -> Task:
        task = event.task_by_id(task_id)
        if task is None:
            raise KeyError(f"No task '{task_id}' in event {event.id}.")
        return task

    def _dependencies_met(self, event: PersonEvent, task: Task) -> bool:
        done = {
            other.id
            for other in event.tasks
            if other.status == TaskStatus.COMPLETED
        }
        return all(dep in done for dep in task.depends_on)

    def _audit(
        self,
        event_type: str,
        actor: str,
        event: PersonEvent,
        task: Task,
        details: dict,
    ) -> None:
        if self.audit_service is None:
            return

        self.audit_service.record(
            event_type=event_type,
            actor=actor,
            details={
                "person_event_id": event.id,
                "event_type": event.event_type.value,
                "task_id": task.id,
                "task_title": task.title,
                "risk_tier": task.risk_tier.value,
                **details,
            },
        )

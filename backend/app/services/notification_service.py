"""Assignment notifications, logged as first-class audit facts.

When does someone learn they own a decision?

- The moment a gated task becomes runnable (its dependencies settle),
  every assignee is notified: this fires exactly once, on the PENDING
  -> AWAITING_APPROVAL transition, so repeated `advance` calls never
  spam anyone.
- The moment a co-assignee acts: on approve/reject, the OTHER
  assignees are told who decided and why. A silent approval in a
  two-person IT team is exactly the "silence is a failure condition"
  pattern Turnstile is built around — nobody should discover a wipe
  happened from the aftermath.

Every notification is recorded in the audit log: who was told, about
what, when, how. "We notified them" must be a provable claim, not an
assumption — the log records the attempt to inform.

Delivery sinks are injected (console printer by default; Slack DM /
email adapters are single-class additions, same seam as TriageBot's
NotificationChannel).
"""

from app.models import PersonEvent, Task, TaskStatus


class ConsoleNotifier:
    def send(self, recipient: str, message: str) -> None:
        print(f"[NOTIFY -> {recipient}] {message}")


class NotificationService:
    def __init__(self, notifier=None, audit_service=None) -> None:
        self.notifier = notifier or ConsoleNotifier()
        self.audit_service = audit_service

    # ------------------------------------------------------------------
    # Awaiting-approval: tell the assignees they own a decision
    # ------------------------------------------------------------------

    def notify_awaiting_approval(
        self, event: PersonEvent, task: Task
    ) -> None:
        message = (
            f"'{task.title}' for {event.person_name} "
            f"({event.event_type.value}) needs your approval — you are "
            f"an assignee. Approve or reject with a reason."
        )

        for recipient in task.assignees:
            self.notifier.send(recipient, message)
            self._record(event, task, recipient, "approval_requested", message)

    # ------------------------------------------------------------------
    # Decision made: tell the co-assignees
    # ------------------------------------------------------------------

    def notify_decision(
        self,
        event: PersonEvent,
        task: Task,
        decided_by: str,
        decision: str,          # "approved" | "rejected"
        note: str | None,
    ) -> None:
        reason = f" — {note}" if note else ""

        message = (
            f"'{task.title}' for {event.person_name} was {decision} by "
            f"{decided_by}{reason}."
        )

        for recipient in task.assignees:
            if recipient == decided_by:
                continue  # the actor knows; the others must learn

            self.notifier.send(recipient, message)
            self._record(event, task, recipient, f"decision_{decision}", message)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _record(
        self,
        event: PersonEvent,
        task: Task,
        recipient: str,
        kind: str,
        message: str,
    ) -> None:
        if self.audit_service is None:
            return

        self.audit_service.record(
            "notification_sent",
            actor="turnstile",
            details={
                "person_event_id": event.id,
                "task_id": task.id,
                "recipient": recipient,
                "kind": kind,
                "message": message,
            },
        )

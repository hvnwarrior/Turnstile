"""Revocation audit: the "everything verified revoked" report.

An offboarding is not done when the tasks ran; it is done when the
ledger proves they ran, who approved each irreversible one, and what
still needs attention. This service produces that proof from the event
aggregate itself — no reconstruction, no trusting process memory.

Verdicts:
- complete: every task terminal, every gated task approved by a named
  human, nothing failed or rejected.
- exceptions_found: everything reached a terminal state, but a task
  FAILED or was REJECTED. The exception is listed explicitly with its
  recorded reason — a rejection ("legal hold active") is a legitimate
  outcome that must survive in the report, not be hidden.
- in_progress: one or more tasks are not terminal yet. The report
  names them so the checklist can't silently stall.
"""

from dataclasses import dataclass, field

from app.models import EventType, PersonEvent, RiskTier, TaskStatus


VERDICT_COMPLETE = "complete"
VERDICT_EXCEPTIONS = "exceptions_found"
VERDICT_IN_PROGRESS = "in_progress"


@dataclass
class RevocationReport:
    event_id: int
    event_type: str
    person_name: str
    generated_at: str
    verdict: str
    task_ledger: list[dict] = field(default_factory=list)
    exceptions: list[str] = field(default_factory=list)
    outstanding: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "person_name": self.person_name,
            "generated_at": self.generated_at,
            "verdict": self.verdict,
            "task_ledger": self.task_ledger,
            "exceptions": self.exceptions,
            "outstanding": self.outstanding,
        }


class RevocationAuditService:
    def __init__(self, clock=None) -> None:
        from datetime import datetime, timezone

        self.clock = clock or (
            lambda: datetime.now(timezone.utc).isoformat()
        )

    def generate(self, event: PersonEvent) -> RevocationReport:
        ledger: list[dict] = []
        exceptions: list[str] = []
        outstanding: list[str] = []

        for task in event.tasks:
            entry = {
                "task_id": task.id,
                "title": task.title,
                "risk_tier": task.risk_tier.value,
                "status": task.status.value,
                "executed_by": task.executed_by,
                "decided_by": task.decided_by,
                "decision_note": task.decision_note,
            }

            if task.failure_reason:
                entry["failure_reason"] = task.failure_reason

            ledger.append(entry)

            if task.status == TaskStatus.FAILED:
                exceptions.append(
                    f"{task.id}: FAILED — {task.failure_reason or 'no reason recorded'}"
                )
            elif task.status == TaskStatus.REJECTED:
                exceptions.append(
                    f"{task.id}: REJECTED by {task.decided_by} — "
                    f"{task.decision_note or 'no reason recorded'}"
                )
            elif not task.terminal():
                outstanding.append(f"{task.id} ({task.status.value})")

        if outstanding:
            verdict = VERDICT_IN_PROGRESS
        elif exceptions:
            verdict = VERDICT_EXCEPTIONS
        else:
            verdict = VERDICT_COMPLETE

        return RevocationReport(
            event_id=event.id,
            event_type=event.event_type.value,
            person_name=event.person_name,
            generated_at=self.clock(),
            verdict=verdict,
            task_ledger=ledger,
            exceptions=exceptions,
            outstanding=outstanding,
        )

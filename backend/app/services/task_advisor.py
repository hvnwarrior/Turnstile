"""LLM task advisor: closed-vocabulary suggestions, nothing more.

What the LLM is allowed to do here, and what it is structurally
forbidden from doing:

ALLOWED
- Read the intake payload and the current checklist.
- Suggest additional tasks, expressed ONLY as registry task ids, each
  with a one-line rationale grounded in the intake details.

FORBIDDEN (enforced in code, not prompt etiquette)
- Inventing task ids: suggestions are intersected with the registry
  before anything happens; unknown ids are dropped and logged.
- Assigning risk tiers or dependencies: those come from the human-
  authored TaskSpec, always. The LLM's suggestion carries no authority
  of its own.
- Attaching tasks directly: suggestions are proposals. A human must
  accept each one; acceptance instantiates the registry spec, with
  its declared risk tier, as a normal PENDING task the engine then
  treats like any other.

If no API key is configured the advisor is disabled and returns no
suggestions — deterministic, honest absence rather than silent
fabrication. Any parse failure on the LLM response likewise yields an
empty suggestion list, never a crash or a guess.
"""

import json

from app.models import EventType, PersonEvent
from app.services.checklist_builder import TEMPLATES


ADVISOR_DISABLED = "advisor_disabled"
ADVISOR_OK = "ok"
ADVISOR_PARSE_FAILED = "parse_failed"


class TaskAdvisor:
    def __init__(self, llm_client=None, audit_service=None) -> None:
        """llm_client: object with a `complete(system, user) -> str`
        method. Injected for tests; production wires the Anthropic
        client. None => advisor disabled."""

        self.llm_client = llm_client
        self.audit_service = audit_service

    # ------------------------------------------------------------------
    # Suggestions
    # ------------------------------------------------------------------

    def suggest(self, event: PersonEvent) -> dict:
        """Propose additional registry tasks for this event.

        Returns {status, suggestions: [{id, rationale}], dropped: [...]}.
        Suggestions are validated against the registry and filtered to
        tasks not already in the checklist.
        """

        if self.llm_client is None:
            return {
                "status": ADVISOR_DISABLED,
                "suggestions": [],
                "dropped": [],
            }

        registry_ids = {
            spec.id: spec for spec in TEMPLATES[event.event_type]
        }
        current_ids = {task.id for task in event.tasks}
        available = [
            spec for spec_id, spec in registry_ids.items()
            if spec_id not in current_ids
        ]

        if not available:
            return {"status": ADVISOR_OK, "suggestions": [], "dropped": []}

        system = (
            "You suggest onboarding/offboarding checklist tasks for a "
            "small company's IT automation system. You may ONLY "
            "suggest tasks from this list, by exact id:\n"
            + "\n".join(f"- {spec.id}: {spec.title}" for spec in available)
            + "\n\nRespond with ONLY a JSON object: "
            '{"suggestions": [{"id": "<registry id>", '
            '"rationale": "<one line, grounded in the intake details>"}]}'
            "\nSuggest nothing if the checklist is already sufficient. "
            "Never invent ids. Never include fields other than id and "
            "rationale."
        )

        user = json.dumps({
            "event_type": event.event_type.value,
            "person_name": event.person_name,
            "requested_by": event.requested_by,
            "current_tasks": sorted(current_ids),
        })

        try:
            raw = self.llm_client.complete(system, user)
            parsed = json.loads(raw)
        except Exception:
            return {
                "status": ADVISOR_PARSE_FAILED,
                "suggestions": [],
                "dropped": [],
            }

        raw_suggestions = parsed.get("suggestions", [])
        if not isinstance(raw_suggestions, list):
            return {
                "status": ADVISOR_PARSE_FAILED,
                "suggestions": [],
                "dropped": [],
            }

        suggestions = []
        dropped = []
        seen_ids = set()

        for item in raw_suggestions:
            task_id = item.get("id") if isinstance(item, dict) else None
            rationale = (
                item.get("rationale", "") if isinstance(item, dict) else ""
            )

            if (
                task_id in registry_ids
                and task_id not in current_ids
                and task_id not in seen_ids
            ):
                suggestions.append({"id": task_id, "rationale": rationale})
                seen_ids.add(task_id)
            else:
                dropped.append(item)

        return {
            "status": ADVISOR_OK,
            "suggestions": suggestions,
            "dropped": dropped,
        }

    # ------------------------------------------------------------------
    # Human acceptance
    # ------------------------------------------------------------------

    def accept(
        self,
        event: PersonEvent,
        suggestion_id: str,
        accepted_by: str,
    ) -> bool:
        """A human accepts one suggestion: instantiate the registry spec.

        The task enters as PENDING with its registry-declared risk tier
        and dependencies. The engine takes it from there: AUTO runs in
        the next batch, gated tasks wait for approval like any other.
        Returns False (no-op) for unknown or duplicate suggestions —
        acceptance is also restricted to the closed vocabulary.
        """

        registry_ids = {spec.id: spec for spec in TEMPLATES[event.event_type]}
        spec = registry_ids.get(suggestion_id)

        if spec is None:
            return False
        if event.task_by_id(suggestion_id) is not None:
            return False

        from app.models import Task

        dependencies = list(spec.depends_on)

        if spec.id == "run_revocation_audit":
            dependencies = [task.id for task in event.tasks]

        event.tasks.append(
            Task(
                id=spec.id,
                title=spec.title,
                risk_tier=spec.risk_tier,
                depends_on=dependencies,
            )
        )

        if self.audit_service is not None:
            self.audit_service.record(
                "advisor_suggestion_accepted",
                actor=accepted_by,
                details={
                    "person_event_id": event.id,
                    "task_id": spec.id,
                    "risk_tier": spec.risk_tier.value,
                    "source": "llm_advisor",
                },
            )

        return True

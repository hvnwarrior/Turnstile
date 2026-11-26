"""HRIS webhook intake: validate the payload, refuse or build.

The mock HRIS (BambooHR-style) posts employee lifecycle events:

    POST /hris/events
    {"type": "employee.created" | "employee.terminated",
     "person_name": ..., "details": {...}}

Design rules, same family as TriageBot's refusal stance:

- Signature first: if TURNSTILE_HRIS_REQUIRE_SIGNATURE=true, an
  unsigned or badly-signed request is rejected before any parsing.
  Signature verification must fail CLOSED (bad or missing signature =
  reject), never open.
- Intake fields come from the payload, not from LLM guesses. If the
  required fields are absent, ChecklistBuilder refuses and the exact
  missing list goes back to the caller in the 422 response body.
- Builds are persisted and the first engine batch (next_batch()) is
  evaluated once at intake, so newly arrived events immediately show
  what can run now vs what awaits approval.
"""

import hashlib
import hmac
from datetime import datetime, timezone

from app.models import EventType, PersonEvent
from app.services.checklist_builder import (
    BuildResult,
    ChecklistBuilder,
)
from app.services.execution_engine import ExecutionEngine


EVENT_TYPE_MAP = {
    "employee.created": EventType.ONBOARDING,
    "employee.terminated": EventType.OFFBOARDING,
}


class SignatureError(Exception):
    """Missing or invalid HMAC signature."""


class UnknownHrisEventError(Exception):
    """HRIS event type we don't map to onboarding/offboarding."""


class HrisIntakeService:
    def __init__(
        self,
        builder: ChecklistBuilder,
        engine: ExecutionEngine,
        store,                      # EventStore: persist(event) -> PersonEvent
        webhook_secret: str = "",
        require_signature: bool = True,
        clock=None,
        audit_service=None,         # AuditLogService, for substitutions
    ) -> None:
        self.builder = builder
        self.engine = engine
        self.store = store
        self.webhook_secret = webhook_secret
        self.require_signature = require_signature
        self.audit_service = audit_service
        from datetime import datetime, timezone
        self.clock = clock or (
            lambda: datetime.now(timezone.utc).isoformat()
        )

    # ------------------------------------------------------------------
    # Signature
    # ------------------------------------------------------------------

    def verify_signature(self, raw_body: bytes, signature: str | None) -> None:
        """Fail CLOSED: no secret configured with require_signature=true
        is a configuration error, not a pass."""

        if not self.require_signature:
            return

        if not self.webhook_secret:
            raise RuntimeError(
                "TURNSTILE_HRIS_REQUIRE_SIGNATURE is true but no "
                "TURNSTILE_HRIS_WEBHOOK_SECRET is configured. Refusing "
                "to accept unsigned webhooks."
            )

        if not signature:
            raise SignatureError("Missing HMAC signature.")

        expected = hmac.new(
            self.webhook_secret.encode(), raw_body, hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(expected, signature):
            raise SignatureError("Invalid HMAC signature.")

    # ------------------------------------------------------------------
    # Ingest
    # ------------------------------------------------------------------

    def ingest(self, raw_body: bytes, signature: str | None) -> dict:
        """Parse, validate, build, persist. Returns an API-ready dict."""

        self.verify_signature(raw_body, signature)

        import json

        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError as error:
            return {
                "accepted": False,
                "reason": "invalid_json",
                "message": f"Payload is not valid JSON: {error}",
            }

        hris_type = payload.get("type")
        if hris_type not in EVENT_TYPE_MAP:
            raise UnknownHrisEventError(
                f"Unknown HRIS event type '{hris_type}'. Known types: "
                + ", ".join(sorted(EVENT_TYPE_MAP))
            )

        event_type = EVENT_TYPE_MAP[hris_type]
        person_name = payload.get("person_name", "")
        requested_by = payload.get("requested_by", "")
        details = payload.get("details", {})
        if not isinstance(details, dict):
            details = {}

        result = self.builder.build(
            event_type, person_name, requested_by, details
        )

        if not result.ok:
            # Refusal goes back to the HRIS/system: the system may want
            # to queue a human follow-up, but we never guess fields.
            return {
                "accepted": False,
                "reason": "missing_intake_data",
                "missing": result.missing,
                "message": result.message,
            }

        event = self.store.persist(result.event)

        # A fallback assignment is a judgment made by the system on
        # behalf of a human who was NOT resolvable: it must be as
        # provable as the approvals are. Never silent.
        for sub in result.substitutions:
            if self.audit_service is None:
                continue
            self.audit_service.record(
                event_type="assignee_fallback",
                actor="turnstile",
                details={
                    # NOTE: no "event_type" key here — AuditLogService
                    # FLATTENS details into top-level keys, and a key
                    # named event_type would clobber the record's own
                    # kind ("assignee_fallback").
                    "person_event_id": event.id,
                    "person_name": event.person_name,
                    "task_id": sub["task_id"],
                    "assigned_to": sub["assigned_to"],
                    "reason": sub["reason"],
                },
            )

        ready = self.engine.next_batch(event)
        # Persist the intake-time batch transitions (PENDING ->
        # READY / AWAITING_APPROVAL) so a reload reflects reality.
        self.store.save_task_states(event)

        return {
            "accepted": True,
            "event_id": event.id,
            "event_type": event.event_type.value,
            "person_name": event.person_name,
            "assignee_substitutions": result.substitutions,
            "ready_tasks": [
                {"id": t.id, "title": t.title} for t in ready
            ],
            "awaiting_approval": [
                {"id": t.id, "title": t.title}
                for t in event.tasks
                if t.status.value == "awaiting_approval"
                and all(
                    dep in event._settled() for dep in t.depends_on
                )
            ],
        }

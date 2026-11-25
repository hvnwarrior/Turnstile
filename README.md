# Turnstile

Employee onboarding and offboarding automation for small IT teams, with
human-in-the-loop execution.

Private work-in-progress. Core ideas:

- **Risk is declared, not inferred.** Every task carries a risk tier:
  `AUTO` (reversible, low blast radius) or `APPROVAL_REQUIRED`
  (irreversible, destructive, or sensitive). Gated tasks cannot execute
  until a named human approves them — the gate is enforced by
  structure, not convention.
- **Dependency-ordered checklists.** Tasks declare what must finish
  first; the engine surfaces only what can run now.
- **Refuse over fabricate.** If intake data is missing (start date,
  role, manager), Turnstile refuses to build the checklist and says
  exactly what it needs — mirroring TriageBot's stance.
- **Every path is auditable.** Approval, rejection, and execution are
  attributed to named actors and recorded in an append-only log.

## Status

Early development. Current components:

- `backend/app/models.py` — domain model (PersonEvent, Task, risk tiers)
- `backend/app/services/execution_engine.py` — approval gates,
  dependency ordering, dry-run executor
- `backend/app/services/checklist_builder.py` — intake validation with
  refusal-on-missing-data; human-authored task templates where risk
  tiers are declared in code, never inferred
- `backend/app/services/hris_intake.py` — HRIS webhook ingestion with
  HMAC signature verification that fails closed
- `backend/app/services/event_store.py` — SQLite persistence; an
  in-flight offboarding survives a restart
- `backend/app/services/audit_service.py` — append-only JSONL audit log
- `backend/tests/` — 28 tests covering gate semantics, refusal,
  signatures, and persistence

- `backend/app/services/revocation_audit.py` — the "everything
  verified revoked" report: per-task ledger with attribution, plus
  verdicts (complete / exceptions_found / in_progress) so a stalled
  offboarding can't hide
- `backend/app/services/task_advisor.py` — optional LLM advisor that
  suggests tasks ONLY from the human-authored registry (closed
  vocabulary); invented ids are dropped, risk tiers always come from
  the registry, and a human must accept each suggestion before it
  becomes a task

Still to come: API layer, demo script.

See also: [TriageBot](https://github.com/hvnwarrior/IT-copilot) — IT
support triage assistant with security escalation.

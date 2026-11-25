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

- `backend/app/services/revocation_audit.py` — the "everything
  verified revoked" report: per-task ledger with attribution, plus
  verdicts (complete / exceptions_found / in_progress) so a stalled
  offboarding can't hide
- `backend/app/services/task_advisor.py` — optional LLM advisor that
  suggests tasks ONLY from the human-authored registry (closed
  vocabulary); invented ids are dropped, risk tiers always come from
  the registry, and a human must accept each suggestion before it
  becomes a task

- `backend/app/api/routes.py` — FastAPI surface: HRIS intake,
  event inspection, approve/reject/execute, advisor endpoints, and
  the revocation report. Every mutation reloads from the store; the
  database is the source of truth, not process memory.
- `scripts/demo.py` — end-to-end offboarding narrative: unsigned
  webhook bounced, incomplete intake refused with the exact missing
  fields, gated task blocked without approval (403), named human
  approves, one deliberate rejection ("legal hold") preserved forever
  in the final ledger. Run `python scripts/demo.py --offline` from the
  repo root (no server needed).
- `backend/tests/` — 46 tests: gate semantics, refusal, fail-closed
  signatures, persistence, closed-vocabulary advisor, revocation
  verdicts, and full HTTP-flow integration tests

## Design semantics worth knowing

- **Dependencies settle, they don't have to succeed.** A rejected task
  releases its dependents — a legal hold must not deadlock the whole
  checklist — but each dependent keeps its own risk gate, so a
  rejected SSO-disable never auto-authorizes the VPN revoke.
- **Approvals attach to runnable tasks only.** A gated task surfaces
  for approval when it can actually run; early approvals would go
  stale in a queue.
- **`advance` is idempotent.** It answers "what can run now,"
  including tasks left READY by a crashed client — recovery, not
  re-triggering.

See also: [TriageBot](https://github.com/hvnwarrior/IT-copilot) — IT
support triage assistant with security escalation.

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
- `backend/tests/` — 10 tests covering the gate semantics

See also: [TriageBot](https://github.com/hvnwarrior/IT-copilot) — IT
support triage assistant with security escalation.

# Turnstile

Employee onboarding and offboarding automation for IT teams, with
human-in-the-loop execution. Turnstile builds a dependency-ordered
checklist for every hire and departure, runs the safe steps
automatically, and refuses to run the dangerous ones until a named human
approves them.

**Why it exists:** offboarding gaps — accounts left active after a
departure — are one of the most common security audit findings, and
they happen because the process lives in someone's head
or a stale spreadsheet. Turnstile makes the process executable,
orderable, and provable.

Private work-in-progress. See also: [TriageBot](https://github.com/hvnwarrior/IT-copilot) — IT support
triage assistant with security escalation.

## How it works

```
HRIS webhook ──▶ signature check ──▶ intake validation ──▶ checklist build
                  (fails closed)      (refuses on             │
                                      missing data)            ▼
                                                          SQLite store
                                                               │
   ┌───────────────────────────────────────────────────────────┘
   ▼
[advance]  ──▶ what can run now?
   │
   ├─ AUTO tasks ─────────────▶ executed by automation
   │
   └─ APPROVAL_REQUIRED ─────▶ await a named human
        tasks                      │
                                   ├─ approve ──▶ executed
                                   └─ reject ───▶ recorded forever,
                                                   releases dependents
   │
   ▼
revocation report: per-task ledger, attribution, verdict
```

1. **Intake.** The HRIS posts `employee.created` / `employee.terminated`
   webhooks. Turnstile verifies the HMAC signature first (missing or
   bad signature = reject, before any parsing). Required fields come
   from the payload, never from guesses; if anything is missing,
   Turnstile refuses and returns the exact list.
2. **Build.** A human-authored template instantiates the checklist.
   Risk tiers (`AUTO` / `APPROVAL_REQUIRED`) and dependencies are
   declared in code and reviewed like any other code — never inferred
   from free text.
3. **Advance.** The engine surfaces what can run now. AUTO tasks are
   cleared for automation; gated tasks wait for **their assignees** —
   named humans resolved at build time from roles: the manager comes
   from the intake payload, the IT admin from config (`reclaim_device`
   is manager-owned; `disable_sso_account` is IT-owned). Only assignees
   may approve or reject. When a task becomes runnable, its assignees
   are notified; when one of them decides, the *other* assignees are
   told who decided and why — and every notification is recorded in
   the audit log, so "we told them" is a provable claim.
4. **Execute.** Cleared tasks run through a pluggable executor. The
   default is a dry-run recorder for demos; production executors wrap
   Google Workspace / Entra ID / Jamf APIs without touching approval
   logic.
5. **Report.** The revocation report is the ledger of proof: every
   task, who executed it, who approved or rejected it and why, plus a
   verdict — `complete`, `exceptions_found`, or `in_progress` — so a
   stalled offboarding can't hide.

## Architecture

```
backend/app/
├── main.py                # FastAPI app factory
├── config.py              # settings; secrets from env only
├── dependencies.py        # single service-wiring point
├── models.py              # PersonEvent, Task, risk tiers, statuses
├── api/
│   └── routes.py          # HTTP surface; reloads from store per call
└── services/
    ├── hris_intake.py         # signature verify (fail closed), parse, route
    ├── checklist_builder.py   # intake validation, human-authored templates
    ├── execution_engine.py    # approval gates, dependency ordering
    ├── task_advisor.py        # optional LLM: registry-only suggestions
    ├── event_store.py         # SQLite persistence
    ├── revocation_audit.py    # report generation, verdicts
    └── audit_service.py       # append-only JSONL audit log
scripts/
└── demo.py                 # end-to-end offboarding narrative
```

The services are pure Python with injected dependencies (clock,
executor, LLM client, audit sink) — the API layer is thin, and all 46
tests run without a server.

## Running it

```bash
git clone https://github.com/hvnwarrior/Turnstile.git
cd Turnstile/backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-test.txt
cp ../.env.example ../.env   # then set TURNSTILE_HRIS_WEBHOOK_SECRET

# tests (no server, no API key)
python -m pytest

# API server
uvicorn app.main:app --reload --port 8000

# end-to-end demo (no server needed)
cd .. && python scripts/demo.py --offline
```

Environment variables are prefixed `TURNSTILE_`; see `.env.example`.
The demo runs fully offline — dry-run executor, no API key — and walks
through refusal, gating, approval, a deliberate rejection, and the
final report.

## API highlights

| Method & path | Purpose |
|---|---|
| `POST /hris/events` | HRIS webhook intake (HMAC header; 401 unsigned, 422 on refusal with missing-field list) |
| `GET /events/{id}` | Full event with per-task state |
| `POST /events/{id}/advance` | Idempotent: what can run now |
| `POST /events/{id}/tasks/{tid}/approve` | Named human clears a gated task |
| `POST /events/{id}/tasks/{tid}/reject` | Terminal, recorded with reason |
| `POST /events/{id}/tasks/{tid}/execute` | Run a cleared task; 403 if gated and unapproved |
| `POST /events/{id}/advisor/suggest` | LLM suggestions, registry-only |
| `GET /events/{id}/revocation-report` | Ledger with attribution + verdict |

Interactive docs at `/docs` once the server runs.

## Design principles

1. **Accountability has a name.** Every gated task carries assignees
   resolved from data you already have (manager from intake, IT admin
   from config) — never picked ad hoc per ticket. Only assignees may
   approve or reject, so the gate answers *who is allowed*, not just
   *that someone must*. By default, an irreversible step that resolves
   to no accountable human refuses the build. When
   `TURNSTILE_FALLBACK_APPROVER` is set, the step is assigned to that
   handle instead — IT becomes the accountable owner by name — and the
   substitution is recorded in the audit log (`assignee_fallback`) and
   echoed in the intake response, so routing to IT is a provable
   judgment, never a silent default.
2. **Silence is a failure condition — in onboarding too.** Assignees
   are notified when they own a decision and when a co-assignee acts,
   and every notification is logged. Decisions propagate to the whole
   responsible pair, not just the person who clicked.
3. **Risk is declared, not inferred.** Every task carries a
   human-authored risk tier: `AUTO` (reversible, low blast radius) or
   `APPROVAL_REQUIRED` (irreversible, destructive, or sensitive). The
   gate is enforced by structure — approval and execution are separate
   code paths, so no refactor can merge them.
4. **Refuse over fabricate.** Missing intake data means refusal with
   the exact list of what's needed. A guessed `last_day` or wrong
   manager embedded in an access decision is worse than no checklist.
5. **The LLM proposes; the registry disposes.** The optional advisor
   may suggest tasks only from the registry's closed vocabulary.
   Invented ids are dropped; risk tiers always come from the registry;
   a human must accept each suggestion. No API key configured — the
   advisor reports `advisor_disabled` and the system degrades to the
   deterministic checklist, never to fabrication.
6. **Stalls are failure conditions.** The revocation
   report names outstanding tasks and preserves rejections with their
   reasons ("legal hold active") — a deliberate stop stays visible
   forever, and nothing disappears into a backlog.
7. **Every path is auditable.** Intake, approval, rejection, and
   execution are attributed to named actors in an append-only log.
   `reclaim_device: approved by Priya — departure confirmed` is a
   first-class fact, not a debug log line.
8. **Fail closed.** Signature verification rejects before parsing; a
   misconfigured secret (required=true but secret unset) raises rather
   than silently accepting. Signature checks that fail open are worse
   than no check.

### Semantics the tests pin down

- **Dependencies settle, they don't have to succeed.** A rejected task
  releases its dependents — a legal hold must not deadlock the
  checklist — but each dependent keeps its own risk gate, so a
  rejected SSO-disable never auto-authorizes the VPN revoke.
- **Approvals attach to runnable tasks only.** Gated tasks surface for
  approval when they can actually run; early approvals would go
  stale in a queue.
- **`advance` is idempotent.** It answers "what can run now,"
  including tasks left READY by a crashed client — recovery, not
  re-triggering.

## Scope, limits, and what's next

Being explicit about what is real today and what is a seam:

**Real now**
- Full intake → gate → approval → audit → report pipeline, exercised
  end to end by tests and the offline demo
- Structural approval gates, settled-dependency semantics, fail-closed
  HMAC verification, persistence across restarts
- Optional LLM advisor, hardened behind the closed-vocabulary registry

**Deliberate seams (by design, not oversight)**
- The executor is a **dry-run recorder**: Turnstile records exactly
  what it *would* do (the Workspace / Entra ID / Jamf calls) instead of
  doing it. Swapping in a real executor is a single class implementing
  one method — the approval logic above it doesn't change. The demo
  story is "propose with precision, execute only with sign-off."
- The advisor runs on an injected client and ships disabled by default;
  wiring Anthropic is a config change, and the system works fully
  without it.
- Single-process SQLite, sized to the workload not the headcount:
  onboarding/offboarding events are low-volume even at large
  organizations, so one node comfortably serves hundreds of staff.
  Multi-instance deployment would swap the store, not the domain
  model — Postgres, a queue, nothing above it changes.

**Next**
- Minimal technician console (approve/reject UI over the API)
- A real executor for one IdP as the reference integration
- Reminder loop for offboardings sitting in `awaiting_approval` too
  long — stalls are failure conditions, so they should page, like
  TriageBot's security escalation

## Testing

59 tests, all offline: gate semantics, assignee authorization, refusal with exact missing
lists, fail-closed signatures, persistence and reload, the
closed-vocabulary advisor (including an LLM that tries to invent a
task and one that lies about risk tiers), revocation verdicts,
assignment notifications (fire-once, co-assignee decisions, logged),
the fallback approver (missing-manager gates route to IT, audited,
fail-closed by default), and full HTTP-flow integration tests through
the real FastAPI app.

```bash
cd backend && python -m pytest
```

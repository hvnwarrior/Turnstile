"""Turnstile API: HRIS intake, event inspection, human decisions.

Endpoint map:
- POST /hris/events                               mock-HRIS webhook intake
- GET  /events                                    list person events
- GET  /events/{id}                               event with task states
- POST /events/{id}/advance                       what can run now
- POST /events/{id}/tasks/{tid}/approve            human approval
- POST /events/{id}/tasks/{tid}/reject             human rejection
- POST /events/{id}/tasks/{tid}/execute            run a cleared task
- POST /events/{id}/advisor/suggest                LLM suggestions
- POST /events/{id}/advisor/accept                 human accepts one
- GET  /events/{id}/revocation-report              the offboarding ledger
- GET  /events/{id}/due-soon?within_days=N         tasks due soon /
                                                   overdue (scheduler feed)

Every mutation loads the event from the store, applies the action, and
persists task states back — the database is the source of truth, not
process memory. Nothing mutates a stale in-process copy.
"""

from fastapi import APIRouter, Header, HTTPException, Request

from app.dependencies import get_turnstile_services
from app.services.hris_intake import (
    SignatureError,
    UnknownHrisEventError,
)


router = APIRouter()


def _load_event(services, event_id: int):
    event = services["store"].load_event(event_id)
    if event is None:
        raise HTTPException(
            status_code=404, detail=f"No person event {event_id}."
        )
    return event


def _save(services, event) -> None:
    services["store"].save_task_states(event)


def _task_dicts(event) -> list[dict]:
    return [
        {
            "id": task.id,
            "title": task.title,
            "risk_tier": task.risk_tier.value,
            "status": task.status.value,
            "depends_on": list(task.depends_on),
            "assignees": list(task.assignees),
            "due_date": task.due_date,
            "executed_by": task.executed_by,
            "decided_by": task.decided_by,
            "decision_note": task.decision_note,
            "failure_reason": task.failure_reason,
        }
        for task in event.tasks
    ]


# ----------------------------------------------------------------------
# Intake
# ----------------------------------------------------------------------

@router.post("/hris/events")
async def hris_events(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
) -> dict:
    services = get_turnstile_services(request.app.state)
    raw = await request.body()

    try:
        result = services["hris_intake"].ingest(raw, x_hub_signature_256)
    except SignatureError as error:
        raise HTTPException(status_code=401, detail=str(error))
    except UnknownHrisEventError as error:
        raise HTTPException(status_code=422, detail=str(error))

    if not result["accepted"]:
        if result.get("reason") == "invalid_json":
            raise HTTPException(status_code=400, detail=result["message"])
        raise HTTPException(
            status_code=422,
            detail={
                "reason": result.get("reason"),
                "missing": result.get("missing", []),
                "message": result.get("message", ""),
            },
        )

    return result


# ----------------------------------------------------------------------
# Inspection
# ----------------------------------------------------------------------

@router.get("/events")
def list_events(request: Request) -> dict:
    services = get_turnstile_services(request.app.state)
    return {"events": services["store"].list_events()}


@router.get("/events/{event_id}")
def get_event(request: Request, event_id: int) -> dict:
    services = get_turnstile_services(request.app.state)
    event = _load_event(services, event_id)

    return {
        "id": event.id,
        "event_type": event.event_type.value,
        "person_name": event.person_name,
        "requested_by": event.requested_by,
        "created_at": event.created_at,
        "tasks": _task_dicts(event),
    }


@router.get("/events/{event_id}/due-soon")
def due_soon(
    request: Request, event_id: int, within_days: int = 2
) -> dict:
    """Unfinished tasks whose due_date falls within `within_days`
    days from today (or is already past). This is the feed a
    scheduler (cron, n8n) polls to re-page assignees — the reminder
    loop itself lives outside the API, on purpose. Only tasks that
    still need human attention are listed; anything settled
    (completed, rejected, failed) is exempt no matter how late."""
    from datetime import date, datetime, timezone

    services = get_turnstile_services(request.app.state)
    event = _load_event(services, event_id)

    today = datetime.now(timezone.utc).date()
    horizon = today.fromordinal(today.toordinal() + within_days)

    due = []
    for task in event.tasks:
        if task.terminal():
            continue
        if not task.due_date:
            continue
        try:
            task_due = date.fromisoformat(task.due_date)
        except ValueError:
            continue
        if task_due <= horizon:
            due.append(
                {
                    "id": task.id,
                    "title": task.title,
                    "status": task.status.value,
                    "due_date": task.due_date,
                    "overdue": task_due < today,
                    "assignees": list(task.assignees),
                }
            )

    return {
        "event_id": event_id,
        "person_name": event.person_name,
        "as_of": today.isoformat(),
        "within_days": within_days,
        "tasks": due,
    }


@router.post("/events/{event_id}/advance")
def advance(request: Request, event_id: int) -> dict:
    services = get_turnstile_services(request.app.state)
    event = _load_event(services, event_id)

    services["engine"].next_batch(event)

    # advance() answers "what can run now": newly promoted tasks plus
    # anything still READY/APPROVED from earlier rounds (e.g. a client
    # crashed before executing them). Idempotent view, not a one-shot.
    settled = event._settled()
    cleared = [
        task
        for task in event.tasks
        if task.status.value in ("ready", "approved")
        and all(dep in settled for dep in task.depends_on)
    ]

    _save(services, event)

    return {
        "event_id": event_id,
        "cleared": [
            {"id": t.id, "title": t.title, "risk_tier": t.risk_tier.value}
            for t in cleared
        ],
        "awaiting_approval": [
            {"id": t.id, "title": t.title, "assignees": list(t.assignees)}
            for t in event.tasks
            if t.status.value == "awaiting_approval"
            and all(dep in settled for dep in t.depends_on)
        ],
    }


# ----------------------------------------------------------------------
# Human decisions
# ----------------------------------------------------------------------

@router.post("/events/{event_id}/tasks/{task_id}/approve")
def approve_task(
    request: Request, event_id: int, task_id: str, body: dict
) -> dict:
    services = get_turnstile_services(request.app.state)
    event = _load_event(services, event_id)

    approver = body.get("approver", "")
    if not approver:
        raise HTTPException(status_code=422, detail="approver required")

    try:
        services["engine"].approve(
            event, task_id, approver=approver, note=body.get("note")
        )
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error))
    except (ValueError, KeyError) as error:
        raise HTTPException(status_code=422, detail=str(error))

    _save(services, event)
    return {"event_id": event_id, "tasks": _task_dicts(event)}


@router.post("/events/{event_id}/tasks/{task_id}/reject")
def reject_task(
    request: Request, event_id: int, task_id: str, body: dict
) -> dict:
    services = get_turnstile_services(request.app.state)
    event = _load_event(services, event_id)

    rejector = body.get("rejector", "")
    if not rejector:
        raise HTTPException(status_code=422, detail="rejector required")

    try:
        services["engine"].reject(
            event, task_id, rejector=rejector, reason=body.get("reason", "")
        )
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error))
    except (ValueError, KeyError) as error:
        raise HTTPException(status_code=422, detail=str(error))

    _save(services, event)
    return {"event_id": event_id, "tasks": _task_dicts(event)}


@router.post("/events/{event_id}/tasks/{task_id}/execute")
def execute_task(
    request: Request, event_id: int, task_id: str, body: dict
) -> dict:
    services = get_turnstile_services(request.app.state)
    event = _load_event(services, event_id)

    actor = body.get("actor", "automation")

    try:
        result = services["engine"].execute(event, task_id, actor=actor)
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error))
    except (ValueError, KeyError) as error:
        raise HTTPException(status_code=422, detail=str(error))

    _save(services, event)
    return {"event_id": event_id, "result": result, "tasks": _task_dicts(event)}


# ----------------------------------------------------------------------
# Advisor
# ----------------------------------------------------------------------

@router.post("/events/{event_id}/advisor/suggest")
def advisor_suggest(request: Request, event_id: int) -> dict:
    services = get_turnstile_services(request.app.state)
    event = _load_event(services, event_id)

    return services["advisor"].suggest(event)


@router.post("/events/{event_id}/advisor/accept")
def advisor_accept(
    request: Request, event_id: int, body: dict
) -> dict:
    services = get_turnstile_services(request.app.state)
    event = _load_event(services, event_id)

    accepted = services["advisor"].accept(
        event,
        body.get("task_id", ""),
        accepted_by=body.get("accepted_by", ""),
    )

    if not accepted:
        raise HTTPException(
            status_code=422,
            detail="Unknown or duplicate suggestion; only registry "
            "tasks not already in the checklist can be accepted.",
        )

    _save(services, event)
    return {"event_id": event_id, "tasks": _task_dicts(event)}


# ----------------------------------------------------------------------
# Reports
# ----------------------------------------------------------------------

@router.get("/events/{event_id}/revocation-report")
def revocation_report(request: Request, event_id: int) -> dict:
    services = get_turnstile_services(request.app.state)
    event = _load_event(services, event_id)

    if event.event_type.value != "offboarding":
        raise HTTPException(
            status_code=422,
            detail="Revocation reports exist for offboardings only.",
        )

    return services["revocation_audit"].generate(event).to_dict()

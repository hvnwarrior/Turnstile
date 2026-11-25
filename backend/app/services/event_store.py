"""Persistence for person events and their tasks (SQLite).

Stores event headers plus tasks with their current status and decision
attribution, so a restart doesn't lose an in-flight onboarding or an
offboarding waiting on approvals. load_event() reconstructs the full
PersonEvent aggregate for the engine.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.models import (
    EventType,
    PersonEvent,
    RiskTier,
    Task,
    TaskStatus,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS person_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    person_name TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    missing_data TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES person_events(id),
    task_id TEXT NOT NULL,
    title TEXT NOT NULL,
    risk_tier TEXT NOT NULL,
    depends_on TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    assignees TEXT NOT NULL DEFAULT '',
    executed_by TEXT,
    decided_by TEXT,
    decision_note TEXT,
    failure_reason TEXT,
    UNIQUE(event_id, task_id)
);
"""


class EventStore:
    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: FastAPI serves sync routes from a
        # worker thread pool. Single-process, single-writer usage; the
        # store is not shared between processes.
        self.conn = sqlite3.connect(
            db_path, check_same_thread=False, timeout=10
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

        # Migration for databases created before assignees existed.
        try:
            self.conn.execute(
                "ALTER TABLE tasks ADD COLUMN assignees TEXT NOT NULL DEFAULT ''"
            )
        except sqlite3.OperationalError:
            pass  # column already exists

        self.conn.commit()

    def persist(self, event: PersonEvent) -> PersonEvent:
        """Insert a newly built event; assigns and returns its id."""

        cursor = self.conn.execute(
            "INSERT INTO person_events "
            "(event_type, person_name, requested_by, created_at, missing_data) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                event.event_type.value,
                event.person_name,
                event.requested_by,
                event.created_at,
                ",".join(event.missing_data),
            ),
        )
        event.id = cursor.lastrowid

        for task in event.tasks:
            self.conn.execute(
                "INSERT INTO tasks "
                "(event_id, task_id, title, risk_tier, depends_on, status, "
                " assignees, executed_by, decided_by, decision_note, failure_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.id,
                    task.id,
                    task.title,
                    task.risk_tier.value,
                    ",".join(task.depends_on),
                    task.status.value,
                    ",".join(task.assignees),
                    task.executed_by,
                    task.decided_by,
                    task.decision_note,
                    task.failure_reason,
                ),
            )

        self.conn.commit()
        return event

    def load_event(self, event_id: int) -> PersonEvent | None:
        row = self.conn.execute(
            "SELECT * FROM person_events WHERE id = ?", (event_id,)
        ).fetchone()

        if row is None:
            return None

        tasks = [
            Task(
                id=trow["task_id"],
                title=trow["title"],
                risk_tier=RiskTier(trow["risk_tier"]),
                depends_on=[
                    dep for dep in trow["depends_on"].split(",") if dep
                ],
                status=TaskStatus(trow["status"]),
                assignees=[
                    part for part in (trow["assignees"] or "").split(",") if part
                ],
                executed_by=trow["executed_by"],
                decided_by=trow["decided_by"],
                decision_note=trow["decision_note"],
                failure_reason=trow["failure_reason"],
            )
            for trow in self.conn.execute(
                "SELECT * FROM tasks WHERE event_id = ? ORDER BY id",
                (event_id,),
            )
        ]

        return PersonEvent(
            id=row["id"],
            event_type=EventType(row["event_type"]),
            person_name=row["person_name"],
            requested_by=row["requested_by"],
            created_at=row["created_at"],
            tasks=tasks,
            missing_data=[
                part for part in row["missing_data"].split(",") if part
            ],
        )

    def save_task_states(self, event: PersonEvent) -> None:
        """Sync current task statuses/attribution back to storage."""

        for task in event.tasks:
            self.conn.execute(
                "UPDATE tasks SET status = ?, executed_by = ?, decided_by = ?, "
                "decision_note = ?, failure_reason = ? "
                "WHERE event_id = ? AND task_id = ?",
                (
                    task.status.value,
                    task.executed_by,
                    task.decided_by,
                    task.decision_note,
                    task.failure_reason,
                    event.id,
                    task.id,
                ),
            )

        self.conn.commit()

    def list_events(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, event_type, person_name, requested_by, created_at "
            "FROM person_events ORDER BY id DESC"
        ).fetchall()

        return [dict(row) for row in rows]

    def close(self) -> None:
        self.conn.close()

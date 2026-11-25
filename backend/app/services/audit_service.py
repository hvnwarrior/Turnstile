"""Append-only audit log for Turnstile (JSONL).

Same contract as TriageBot's AuditLogService: record(event_type,
actor, details) appended as one JSON line, nothing ever rewritten.
Every intake, approval, rejection, execution, and revocation check
lands here.
"""

import json
from pathlib import Path


class AuditLogService:
    def __init__(self, log_path: str) -> None:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        self.log_path = Path(log_path)

    def record(
        self,
        event_type: str,
        actor: str,
        details: dict | None = None,
    ) -> None:
        from datetime import datetime, timezone

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "actor": actor,
            **(details or {}),
        }

        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def read_all(self) -> list[dict]:
        if not self.log_path.exists():
            return []

        entries = []

        for line in self.log_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entries.append(json.loads(line))

        return entries

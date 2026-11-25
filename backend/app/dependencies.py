"""Service wiring for the FastAPI app. Built once at startup."""

from app.config import get_settings
from app.services.audit_service import AuditLogService
from app.services.checklist_builder import ChecklistBuilder
from app.services.event_store import EventStore
from app.services.execution_engine import DryRunExecutor, ExecutionEngine
from app.services.hris_intake import HrisIntakeService
from app.services.revocation_audit import RevocationAuditService
from app.services.task_advisor import TaskAdvisor


def build_services(settings=None) -> dict:
    """Single construction point; tests override paths via settings."""

    settings = settings or get_settings()

    audit = AuditLogService(settings.audit_log_path)
    store = EventStore(settings.database_path)
    engine = ExecutionEngine(executor=DryRunExecutor(), audit_service=audit)
    builder = ChecklistBuilder()
    hris_intake = HrisIntakeService(
        builder=builder,
        engine=engine,
        store=store,
        webhook_secret=settings.hris_webhook_secret,
        require_signature=settings.hris_require_signature,
    )
    advisor = TaskAdvisor(llm_client=None, audit_service=audit)
    revocation_audit = RevocationAuditService()

    return {
        "audit": audit,
        "store": store,
        "engine": engine,
        "hris_intake": hris_intake,
        "advisor": advisor,
        "revocation_audit": revocation_audit,
    }


def get_turnstile_services(app_state) -> dict:
    if not getattr(app_state, "turnstile_services", None):
        raise RuntimeError("Services not initialized on app.state.")
    return app_state.turnstile_services

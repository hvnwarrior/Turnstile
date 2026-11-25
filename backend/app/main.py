"""Turnstile FastAPI application."""

from fastapi import FastAPI

from app.api.routes import router
from app.dependencies import build_services


def create_app() -> FastAPI:
    app = FastAPI(
        title="Turnstile",
        description=(
            "Onboarding/offboarding automation with approval-gated "
            "execution. Risk tiers are human-authored; the LLM advisor "
            "suggests only from the registry; every decision is audited."
        ),
    )

    app.state.turnstile_services = build_services()
    app.include_router(router)
    return app


app = create_app()

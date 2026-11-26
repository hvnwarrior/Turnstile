"""Turnstile settings. Secrets come from the environment only."""

from functools import lru_cache

from pydantic_settings import BaseSettings


class AppSettings(BaseSettings):
    database_path: str = "data/turnstile.db"
    audit_log_path: str = "data/audit.jsonl"

    # Mock HRIS webhook
    hris_webhook_secret: str = ""       # HMAC secret; empty disables
    hris_require_signature: bool = True

    # Handle substituted for the "it_admin" assignee role
    default_approver: str = "it-admin"

    # When a gated task resolves to NO assignee (e.g. no manager in
    # intake), assign it to this handle instead of refusing the build.
    # Empty (default) keeps the fail-closed refusal.
    fallback_approver: str = ""

    # LLM advisor (added next slice)
    anthropic_api_key: str = ""

    model_config = {
        "env_prefix": "TURNSTILE_",
        "env_file": ".env",
        "extra": "ignore",
    }


@lru_cache
def get_settings() -> AppSettings:
    return AppSettings()


def turnstile_env_prefix() -> str:
    return "TURNSTILE_"

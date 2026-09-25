"""Central application configuration.

Every pod (app, sentinel, mcp, healer) imports the single `settings` instance
from this module. Values come from the process environment / a `.env` file
(see `.env.example`), never from hardcoded defaults for secrets.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Absolute, not ".env": this module is imported by code that can run with a
# cwd other than the repo root — notably healer/agent_free.py, which spawns
# the `claude` CLI (and, via .mcp.json, a nested `python -m mcp_server.server`
# that also imports this module) with cwd set to a fix worktree under
# worktrees/<name>/, which has no .env of its own. A relative "./.env" would
# silently resolve to nothing there and fall back to this class's defaults
# (e.g. the default DATABASE_URL), not the real configured one.
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    """Typed, validated application settings loaded from the environment."""

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Environment ---------------------------------------------------
    environment: str = "development"
    log_level: str = "INFO"

    # --- Database --------------------------------------------------------
    database_url: str = "postgresql+asyncpg://selfheal:selfheal@localhost:5432/selfheal"
    # Only read by tests/conftest.py (to point the whole process at a
    # throwaway DB before this module is ever imported) and documented here
    # so `.env` has one obvious place to declare it. Application code must
    # never read this field directly.
    test_database_url: str | None = None
    db_pool_size: int = 5
    db_max_overflow: int = 2

    # --- Pod ports ---------------------------------------------------------
    app_port: int = 8001
    sentinel_port: int = 8002
    mcp_port: int = 8003
    healer_port: int = 8000

    # --- AI backend selection ------------------------------------------------
    # Free mode (default): healer/agent_free.py drives the fix loop through the
    # local Claude Code CLI on the user's own subscription login, no API key.
    # API mode: healer/runtime_agent.py + healer/ci_agent.py via the `anthropic`
    # SDK, which requires anthropic_api_key below and is only imported lazily
    # (see healer/anthropic_client.py) since `anthropic` is an optional extra.
    use_claude_code: bool = True
    claude_cli_path: str | None = None  # None = look up "claude" on PATH
    claude_cli_timeout_s: int = 600
    claude_cli_max_turns: int = 30
    max_cli_calls_per_day: int = 50

    # --- Anthropic / Claude (API mode only) -----------------------------------
    anthropic_api_key: str | None = None
    anthropic_base_url: str | None = None  # e.g. https://openrouter.ai/api for OpenRouter
    anthropic_model: str = "claude-sonnet-5"

    # --- GitHub --------------------------------------------------------------
    github_token: str | None = None
    github_repo: str | None = None

    # --- Auth ------------------------------------------------------------
    admin_password_hash: str | None = None
    session_secret: str | None = None

    # --- Webhooks ------------------------------------------------------------
    healer_webhook_secret: str | None = None
    healer_webhook_url: str | None = None
    public_url: str | None = None

    # --- Cost control --------------------------------------------------------
    max_tokens_per_job: int = 150_000
    daily_budget_usd: Decimal = Decimal("2.00")
    chat_daily_budget_usd: Decimal = Decimal("1.00")

    # --- Safety guardrails -----------------------------------------------
    auto_merge: bool = False

    # --- Cloud deployment ------------------------------------------------
    deploy_host: str | None = None
    deploy_user: str = "deploy"
    deploy_ssh_key: str | None = None

    # --- Notifications (optional) ------------------------------------------
    slack_webhook_url: str | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_to: str | None = None

    # --- Rate limiting / lockout ------------------------------------------
    login_max_attempts: int = 5
    login_lockout_minutes: int = 15

    # --- Circuit breaker (safety guardrails) --------------------------------
    max_heal_attempts_per_fingerprint_24h: int = 3
    max_ci_fix_attempts_per_pr: int = 2
    max_heal_jobs_per_hour_global: int = 10

    # --- Patch limits --------------------------------------------------------
    max_patch_files: int = 3
    max_patch_changed_lines: int = 80

    webhook_replay_tolerance_seconds: int = Field(default=300, ge=1)

    # --- target_app (app-pod) demo/bug settings -----------------------------
    # Deliberately unreachable IP (RFC 5737-ish "likely blackholed" address)
    # so bug #6 (unhandled external API timeout) is deterministic and never
    # depends on a real third party being up or down.
    pricing_api_url: str = "http://10.255.255.1:8080"
    pricing_api_timeout_seconds: float = 5.0

    # --- sentinel-pod ingest / detection settings ---------------------------
    sentinel_ingest_url: str | None = None  # defaults to http://localhost:{sentinel_port}
    error_report_timeout_seconds: float = 2.0
    error_reoccurrence_threshold: int = 5
    contract_probe_interval_seconds: int = 300
    anomaly_eval_interval_seconds: int = 60
    anomaly_window_seconds: int = 300
    anomaly_baseline_window_seconds: int = 3600
    anomaly_min_requests_in_window: int = 20
    anomaly_error_rate_threshold: float = 0.05
    anomaly_latency_multiplier_threshold: float = 2.0
    anomaly_cooldown_seconds: int = 600

    @property
    def sentinel_base_url(self) -> str:
        """Base URL target_app's middleware/handler use to reach sentinel-pod's ingest API."""
        return self.sentinel_ingest_url or f"http://localhost:{self.sentinel_port}"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide cached Settings instance."""
    return Settings()


settings = get_settings()

"""SQLAlchemy 2.0 ORM models for every table in SPEC.md's DATABASE SCHEMA.

Table-by-table mapping to the spec:
  errors, error_occurrences, contract_violations, heal_jobs, fix_attempts,
  pipeline_runs, deployments, chat_sessions, chat_messages, rate_limits,
  login_attempts, daily_spend, anomalies, audit_log.

Design notes (documented per SPEC.md's "pick the simplest robust option"):
  - All primary keys are `BigInteger` identity columns; the system has no
    cross-service ID-generation requirement that would justify UUIDs.
  - `errors` and `contract_violations` carry a `status` column
    (open/resolved) even though SPEC.md doesn't spell out the enum values,
    because `list_open_errors()` (an MCP tool) needs something to filter on.
  - Money columns use `Numeric(10, 6)` to track sub-cent Claude API costs
    accurately.
"""

from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Enum as SAEnum

from core.db import Base


def _pg_enum(enum_cls: type[enum.StrEnum], name: str) -> SAEnum:
    """A Postgres ENUM bound to `enum_cls`, using `.value` (not `.name`) on the wire.

    SQLAlchemy's `Enum(SomePythonEnum)` sends the member *name* by default
    (e.g. "OPEN"), but the Postgres types created in the Alembic migrations
    use the lowercase `.value` strings (e.g. "open") — without
    `values_callable`, every insert/update fails with "invalid input value
    for enum ...".
    """
    return SAEnum(enum_cls, name=name, values_callable=lambda cls: [member.value for member in cls])


class TimestampMixin:
    """created_at column shared by every table."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class OpenResolvedStatus(enum.StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


class HealJobType(enum.StrEnum):
    RUNTIME_ERROR = "runtime_error"
    CONTRACT_VIOLATION = "contract_violation"
    CI_FAILURE = "ci_failure"


class HealJobStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PR_OPENED = "pr_opened"
    CI_FIXING = "ci_fixing"
    MERGED = "merged"
    DEPLOYED = "deployed"
    VERIFIED = "verified"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    PAUSED_BUDGET = "paused_budget"


class BudgetCategory(enum.StrEnum):
    HEALER = "healer"
    CHAT = "chat"


class MonitoredApp(TimestampMixin, Base):
    """A registered app the self-healing system watches (multi-app support).

    `apps/target_app` is registered as the first row (see
    `scripts/sync_monitored_apps.py` / `config/monitored_apps.yaml`) with
    identical behavior to the pre-multi-app hardcoded defaults. Every write
    scope (`propose_patch`), test/lint command and PR target repo comes from
    this table, looked up server-side from the heal_job's `app_id` — never
    trusted from a caller-supplied parameter, same principle as the
    `apps/target_app`-only restriction it replaces.
    """

    __tablename__ = "monitored_apps"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    language: Mapped[str] = mapped_column(String(50), nullable=False)
    #: Repo-relative path (e.g. "apps/target_app", "examples/node_app") --
    #: every app registered so far lives in a subdirectory of this same repo.
    local_repo_path: Mapped[str] = mapped_column(String(500), nullable=False)
    github_repo: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Repo-relative write-scope prefixes (e.g. ["apps/target_app/"]) --
    #: a list, not a single string, since a real app may span more than one
    #: directory (e.g. a frontend + backend pair under one app name).
    allowed_write_paths: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    test_command: Mapped[str] = mapped_column(Text, nullable=False)
    lint_command: Mapped[str | None] = mapped_column(Text, nullable=True)
    health_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    #: Per-app bearer token for `/ingest/error` and `/ingest/metric` --
    #: identifies which app an incoming report belongs to server-side
    #: (looked up by token), never from a caller-supplied app name/id.
    ingest_token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)


class Error(TimestampMixin, Base):
    """A deduplicated runtime error, identified by `fingerprint`."""

    __tablename__ = "errors"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    exception_type: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    traceback: Mapped[str] = mapped_column(Text, nullable=False)
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    function_name: Mapped[str] = mapped_column(String(255), nullable=False)
    request_context: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    git_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    status: Mapped[OpenResolvedStatus] = mapped_column(
        _pg_enum(OpenResolvedStatus, "error_status"),
        nullable=False,
        default=OpenResolvedStatus.OPEN,
        server_default=OpenResolvedStatus.OPEN.value,
    )
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    app_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("monitored_apps.id", ondelete="SET NULL"), nullable=True
    )

    occurrences: Mapped[list[ErrorOccurrence]] = relationship(
        back_populates="error", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_errors_status", "status"),
        Index("ix_errors_created_at", "created_at"),
        Index("ix_errors_app_id", "app_id"),
    )


class ErrorOccurrence(TimestampMixin, Base):
    """A single occurrence of an already-fingerprinted error."""

    __tablename__ = "error_occurrences"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    error_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("errors.id", ondelete="CASCADE"), nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    traceback: Mapped[str] = mapped_column(Text, nullable=False)
    request_context: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    error: Mapped[Error] = relationship(back_populates="occurrences")

    __table_args__ = (Index("ix_error_occurrences_error_id", "error_id"),)


class ContractViolation(TimestampMixin, Base):
    """A silent-bug detection: contract check that produced the wrong output."""

    __tablename__ = "contract_violations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    endpoint: Mapped[str] = mapped_column(String(512), nullable=False)
    expected: Mapped[str] = mapped_column(Text, nullable=False)
    actual: Mapped[str] = mapped_column(Text, nullable=False)
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[OpenResolvedStatus] = mapped_column(
        _pg_enum(OpenResolvedStatus, "error_status"),
        nullable=False,
        default=OpenResolvedStatus.OPEN,
        server_default=OpenResolvedStatus.OPEN.value,
    )
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    app_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("monitored_apps.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        Index("ix_contract_violations_status", "status"),
        Index("ix_contract_violations_created_at", "created_at"),
        Index("ix_contract_violations_app_id", "app_id"),
    )


class HealJob(TimestampMixin, Base):
    """A unit of self-healing work, consumed by the healer worker loop."""

    __tablename__ = "heal_jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    type: Mapped[HealJobType] = mapped_column(
        _pg_enum(HealJobType, "heal_job_type"), nullable=False
    )
    status: Mapped[HealJobStatus] = mapped_column(
        _pg_enum(HealJobStatus, "heal_job_status"),
        nullable=False,
        default=HealJobStatus.QUEUED,
        server_default=HealJobStatus.QUEUED.value,
    )
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    source_error_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("errors.id", ondelete="SET NULL"), nullable=True
    )
    source_contract_violation_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("contract_violations.id", ondelete="SET NULL"), nullable=True
    )
    source_pipeline_run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("pipeline_runs.id", ondelete="SET NULL"), nullable=True
    )
    branch_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    app_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("monitored_apps.id", ondelete="SET NULL"), nullable=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    fix_attempts: Mapped[list[FixAttempt]] = relationship(
        back_populates="heal_job", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_heal_jobs_fingerprint", "fingerprint"),
        Index("ix_heal_jobs_status", "status"),
        Index("ix_heal_jobs_type", "type"),
        Index("ix_heal_jobs_pr_number", "pr_number"),
        Index("ix_heal_jobs_created_at", "created_at"),
        Index("ix_heal_jobs_status_created_at", "status", "created_at"),
        Index("ix_heal_jobs_app_id", "app_id"),
    )


class FixAttempt(TimestampMixin, Base):
    """One agentic-loop iteration of a HealJob: a candidate diff + test evidence."""

    __tablename__ = "fix_attempts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    heal_job_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("heal_jobs.id", ondelete="CASCADE"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    root_cause: Mapped[str | None] = mapped_column(Text, nullable=True)
    diff: Mapped[str | None] = mapped_column(Text, nullable=True)
    test_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False, default=Decimal("0"))

    heal_job: Mapped[HealJob] = relationship(back_populates="fix_attempts")

    __table_args__ = (Index("ix_fix_attempts_heal_job_id", "heal_job_id"),)


class PipelineRun(TimestampMixin, Base):
    """A GitHub Actions workflow run, mirrored locally for the CI-fix loop and chat."""

    __tablename__ = "pipeline_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    workflow: Mapped[str] = mapped_column(String(255), nullable=False)
    branch: Mapped[str] = mapped_column(String(255), nullable=False)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sha: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    conclusion: Mapped[str | None] = mapped_column(String(32), nullable=True)
    failed_job: Mapped[str | None] = mapped_column(String(255), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_pipeline_runs_run_id", "run_id"),
        Index("ix_pipeline_runs_pr_number", "pr_number"),
        Index("ix_pipeline_runs_created_at", "created_at"),
    )


class Deployment(TimestampMixin, Base):
    """A single release: which sha, which env, and its rollout outcome."""

    __tablename__ = "deployments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    sha: Mapped[str] = mapped_column(String(40), nullable=False)
    env: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_deployments_env", "env"),
        Index("ix_deployments_created_at", "created_at"),
    )


class ChatSession(TimestampMixin, Base):
    """A logged-in chat session (single admin account)."""

    __tablename__ = "chat_sessions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_label: Mapped[str] = mapped_column(String(64), nullable=False, default="admin")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    messages: Mapped[list[ChatMessage]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class ChatMessage(TimestampMixin, Base):
    """One message in a chat session's history, with per-message token/cost tracking."""

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False, default=Decimal("0"))

    session: Mapped[ChatSession] = relationship(back_populates="messages")

    __table_args__ = (
        Index("ix_chat_messages_session_id", "session_id"),
        Index("ix_chat_messages_created_at", "created_at"),
    )


class RateLimit(TimestampMixin, Base):
    """Token-bucket state for in-process rate limiting, persisted in Postgres."""

    __tablename__ = "rate_limits"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    bucket: Mapped[str] = mapped_column(String(64), nullable=False)
    tokens: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    last_refill_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (UniqueConstraint("key", "bucket", name="uq_rate_limits_key_bucket"),)


class LoginAttempt(TimestampMixin, Base):
    """Every login attempt, used to enforce the 15-minute lockout after 5 failures."""

    __tablename__ = "login_attempts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ip_address: Mapped[str] = mapped_column(String(64), nullable=False)
    username: Mapped[str] = mapped_column(String(128), nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_login_attempts_ip_address", "ip_address"),
        Index("ix_login_attempts_attempted_at", "attempted_at"),
    )


class DailySpend(TimestampMixin, Base):
    """Running total of Claude API spend per UTC day per budget category."""

    __tablename__ = "daily_spend"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    day: Mapped[datetime] = mapped_column(Date, nullable=False)
    category: Mapped[BudgetCategory] = mapped_column(
        _pg_enum(BudgetCategory, "budget_category"), nullable=False
    )
    spend_usd: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False, default=Decimal("0"))
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (UniqueConstraint("day", "category", name="uq_daily_spend_day_category"),)


class Anomaly(TimestampMixin, Base):
    """A 5xx-rate or p95-latency anomaly alert (reported to chat, never auto-fixed)."""

    __tablename__ = "anomalies"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    metric_value: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    threshold: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_anomalies_created_at", "created_at"),)


class AuditLog(TimestampMixin, Base):
    """Immutable record of every consequential action taken by the system."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    heal_job_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("heal_jobs.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        Index("ix_audit_log_action", "action"),
        Index("ix_audit_log_created_at", "created_at"),
    )

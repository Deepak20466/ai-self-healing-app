"""Initial schema: errors, heal_jobs, pipeline, chat, safety and audit tables.

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

error_status_enum = postgresql.ENUM("open", "resolved", name="error_status", create_type=False)
heal_job_type_enum = postgresql.ENUM(
    "runtime_error",
    "contract_violation",
    "ci_failure",
    name="heal_job_type",
    create_type=False,
)
heal_job_status_enum = postgresql.ENUM(
    "queued",
    "running",
    "pr_opened",
    "ci_fixing",
    "merged",
    "deployed",
    "verified",
    "failed",
    "rolled_back",
    "paused_budget",
    name="heal_job_status",
    create_type=False,
)
budget_category_enum = postgresql.ENUM("healer", "chat", name="budget_category", create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    error_status_enum.create(bind, checkfirst=True)
    heal_job_type_enum.create(bind, checkfirst=True)
    heal_job_status_enum.create(bind, checkfirst=True)
    budget_category_enum.create(bind, checkfirst=True)

    op.create_table(
        "errors",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("exception_type", sa.String(255), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("traceback", sa.Text(), nullable=False),
        sa.Column("file_path", sa.String(1024), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("function_name", sa.String(255), nullable=False),
        sa.Column("request_context", postgresql.JSONB(), nullable=True),
        sa.Column("git_sha", sa.String(40), nullable=True),
        sa.Column("status", error_status_enum, nullable=False, server_default="open"),
        sa.Column("occurrence_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_errors_fingerprint", "errors", ["fingerprint"])
    op.create_index("ix_errors_status", "errors", ["status"])
    op.create_index("ix_errors_created_at", "errors", ["created_at"])

    op.create_table(
        "error_occurrences",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "error_id",
            sa.BigInteger(),
            sa.ForeignKey("errors.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("traceback", sa.Text(), nullable=False),
        sa.Column("request_context", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_error_occurrences_error_id", "error_occurrences", ["error_id"])

    op.create_table(
        "contract_violations",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("endpoint", sa.String(512), nullable=False),
        sa.Column("expected", sa.Text(), nullable=False),
        sa.Column("actual", sa.Text(), nullable=False),
        sa.Column("file_path", sa.String(1024), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("status", error_status_enum, nullable=False, server_default="open"),
        sa.Column("occurrence_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_contract_violations_fingerprint", "contract_violations", ["fingerprint"])
    op.create_index("ix_contract_violations_status", "contract_violations", ["status"])
    op.create_index("ix_contract_violations_created_at", "contract_violations", ["created_at"])

    op.create_table(
        "pipeline_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("workflow", sa.String(255), nullable=False),
        sa.Column("branch", sa.String(255), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("sha", sa.String(40), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("conclusion", sa.String(32), nullable=True),
        sa.Column("failed_job", sa.String(255), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_pipeline_runs_run_id", "pipeline_runs", ["run_id"])
    op.create_index("ix_pipeline_runs_pr_number", "pipeline_runs", ["pr_number"])
    op.create_index("ix_pipeline_runs_created_at", "pipeline_runs", ["created_at"])

    op.create_table(
        "heal_jobs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("type", heal_job_type_enum, nullable=False),
        sa.Column("status", heal_job_status_enum, nullable=False, server_default="queued"),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column(
            "source_error_id",
            sa.BigInteger(),
            sa.ForeignKey("errors.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "source_contract_violation_id",
            sa.BigInteger(),
            sa.ForeignKey("contract_violations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "source_pipeline_run_id",
            sa.BigInteger(),
            sa.ForeignKey("pipeline_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("branch_name", sa.String(255), nullable=True),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_heal_jobs_fingerprint", "heal_jobs", ["fingerprint"])
    op.create_index("ix_heal_jobs_status", "heal_jobs", ["status"])
    op.create_index("ix_heal_jobs_type", "heal_jobs", ["type"])
    op.create_index("ix_heal_jobs_pr_number", "heal_jobs", ["pr_number"])
    op.create_index("ix_heal_jobs_created_at", "heal_jobs", ["created_at"])
    op.create_index("ix_heal_jobs_status_created_at", "heal_jobs", ["status", "created_at"])

    op.create_table(
        "fix_attempts",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "heal_job_id",
            sa.BigInteger(),
            sa.ForeignKey("heal_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("root_cause", sa.Text(), nullable=True),
        sa.Column("diff", sa.Text(), nullable=True),
        sa.Column("test_output", sa.Text(), nullable=True),
        sa.Column("passed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cached_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_fix_attempts_heal_job_id", "fix_attempts", ["heal_job_id"])

    op.create_table(
        "deployments",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("sha", sa.String(40), nullable=False),
        sa.Column("env", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_deployments_env", "deployments", ["env"])
    op.create_index("ix_deployments_created_at", "deployments", ["created_at"])

    op.create_table(
        "chat_sessions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("user_label", sa.String(64), nullable=False, server_default="admin"),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "last_active_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )

    op.create_table(
        "chat_messages",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "session_id",
            sa.BigInteger(),
            sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cached_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_chat_messages_session_id", "chat_messages", ["session_id"])
    op.create_index("ix_chat_messages_created_at", "chat_messages", ["created_at"])

    op.create_table(
        "rate_limits",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("bucket", sa.String(64), nullable=False),
        sa.Column("tokens", sa.Numeric(12, 4), nullable=False),
        sa.Column(
            "last_refill_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("key", "bucket", name="uq_rate_limits_key_bucket"),
    )

    op.create_table(
        "login_attempts",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("ip_address", sa.String(64), nullable=False),
        sa.Column("username", sa.String(128), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column(
            "attempted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_login_attempts_ip_address", "login_attempts", ["ip_address"])
    op.create_index("ix_login_attempts_attempted_at", "login_attempts", ["attempted_at"])

    op.create_table(
        "daily_spend",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("category", budget_category_enum, nullable=False),
        sa.Column("spend_usd", sa.Numeric(10, 4), nullable=False, server_default="0"),
        sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("day", "category", name="uq_daily_spend_day_category"),
    )

    op.create_table(
        "anomalies",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("metric_value", sa.Numeric(10, 4), nullable=False),
        sa.Column("threshold", sa.Numeric(10, 4), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_anomalies_created_at", "anomalies", ["created_at"])

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=True),
        sa.Column(
            "heal_job_id",
            sa.BigInteger(),
            sa.ForeignKey("heal_jobs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_audit_log_action", "audit_log", ["action"])
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])


def downgrade() -> None:
    op.drop_table("audit_log")
    op.drop_table("anomalies")
    op.drop_table("daily_spend")
    op.drop_table("login_attempts")
    op.drop_table("rate_limits")
    op.drop_table("chat_messages")
    op.drop_table("chat_sessions")
    op.drop_table("deployments")
    op.drop_table("fix_attempts")
    op.drop_table("heal_jobs")
    op.drop_table("pipeline_runs")
    op.drop_table("contract_violations")
    op.drop_table("error_occurrences")
    op.drop_table("errors")

    bind = op.get_bind()
    budget_category_enum.drop(bind, checkfirst=True)
    heal_job_status_enum.drop(bind, checkfirst=True)
    heal_job_type_enum.drop(bind, checkfirst=True)
    error_status_enum.drop(bind, checkfirst=True)

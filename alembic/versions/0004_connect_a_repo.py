"""Connect-a-repo flow: `findings` table + new `monitored_apps` columns.

Adds `repo_url` (the clone URL for an app registered via the dashboard's
"Add app" flow, as opposed to `config/monitored_apps.yaml`), the
`auto_fix_high_severity` toggle (default off), and `last_scanned_at`/
`health_score` bookkeeping written by `core/scanner.py` after each scan.

`findings` mirrors `errors`/`contract_violations`'s existing dedup-by-
fingerprint shape (see 0001's tables) rather than inventing a new pattern.

Revision ID: 0004_connect_a_repo
Revises: 0003_monitored_apps
Create Date: 2026-09-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_connect_a_repo"
down_revision: str | None = "0003_monitored_apps"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FINDING_CATEGORY = sa.Enum("test", "lint", "type_check", "dependency", name="finding_category")
_FINDING_SEVERITY = sa.Enum("low", "medium", "high", "critical", name="finding_severity")
_FINDING_STATUS = sa.Enum("open", "fix_requested", "resolved", "ignored", name="finding_status")


def upgrade() -> None:
    op.add_column("monitored_apps", sa.Column("repo_url", sa.String(500), nullable=True))
    op.add_column(
        "monitored_apps",
        sa.Column(
            "auto_fix_high_severity",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )
    op.add_column(
        "monitored_apps",
        sa.Column("last_scanned_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("monitored_apps", sa.Column("health_score", sa.Integer(), nullable=True))

    # No explicit .create() calls: op.create_table()'s column DDL creates
    # each Enum type automatically on first use (found the hard way --
    # calling both .create() here *and* using the same Enum object as a
    # column type below double-creates it and fails with DuplicateObjectError).
    op.create_table(
        "findings",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "app_id",
            sa.BigInteger(),
            sa.ForeignKey("monitored_apps.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("category", _FINDING_CATEGORY, nullable=False),
        sa.Column("severity", _FINDING_SEVERITY, nullable=False),
        sa.Column("tool", sa.String(64), nullable=False),
        sa.Column("file_path", sa.String(1024), nullable=True),
        sa.Column("line_number", sa.Integer(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("status", _FINDING_STATUS, nullable=False, server_default="open"),
        sa.Column(
            "heal_job_id",
            sa.BigInteger(),
            sa.ForeignKey("heal_jobs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("occurrence_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("app_id", "fingerprint", name="uq_findings_app_id_fingerprint"),
    )
    op.create_index("ix_findings_app_id", "findings", ["app_id"])
    op.create_index("ix_findings_status", "findings", ["status"])
    op.create_index("ix_findings_severity", "findings", ["severity"])
    op.create_index("ix_findings_fingerprint", "findings", ["fingerprint"])


def downgrade() -> None:
    op.drop_index("ix_findings_fingerprint", table_name="findings")
    op.drop_index("ix_findings_severity", table_name="findings")
    op.drop_index("ix_findings_status", table_name="findings")
    op.drop_index("ix_findings_app_id", table_name="findings")
    op.drop_table("findings")
    _FINDING_STATUS.drop(op.get_bind(), checkfirst=True)
    _FINDING_SEVERITY.drop(op.get_bind(), checkfirst=True)
    _FINDING_CATEGORY.drop(op.get_bind(), checkfirst=True)

    op.drop_column("monitored_apps", "health_score")
    op.drop_column("monitored_apps", "last_scanned_at")
    op.drop_column("monitored_apps", "auto_fix_high_severity")
    op.drop_column("monitored_apps", "repo_url")

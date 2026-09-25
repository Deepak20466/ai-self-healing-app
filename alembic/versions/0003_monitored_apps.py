"""monitored_apps: multi-app support (config table backing per-app YAML).

Adds a nullable `app_id` FK to `errors`, `contract_violations` and
`heal_jobs` -- nullable (not backfilled) because pre-existing rows predate
multi-app support and have no app to attribute to; every new row going
forward always sets it (enforced in application code, not the DB, so a
future single-column NOT NULL migration stays possible without touching
this one -- see CLAUDE.md's "never edit a landed migration" convention).

Revision ID: 0003_monitored_apps
Revises: 0002_target_app_demo
Create Date: 2026-09-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003_monitored_apps"
down_revision: str | None = "0002_target_app_demo"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "monitored_apps",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("language", sa.String(50), nullable=False),
        sa.Column("local_repo_path", sa.String(500), nullable=False),
        sa.Column("github_repo", sa.String(255), nullable=False),
        sa.Column("allowed_write_paths", postgresql.JSONB(), nullable=False),
        sa.Column("test_command", sa.Text(), nullable=False),
        sa.Column("lint_command", sa.Text(), nullable=True),
        sa.Column("health_url", sa.String(500), nullable=True),
        sa.Column("ingest_token", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )

    for table in ("errors", "contract_violations", "heal_jobs"):
        op.add_column(
            table,
            sa.Column(
                "app_id",
                sa.BigInteger(),
                sa.ForeignKey("monitored_apps.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
        op.create_index(f"ix_{table}_app_id", table, ["app_id"])


def downgrade() -> None:
    for table in ("errors", "contract_violations", "heal_jobs"):
        op.drop_index(f"ix_{table}_app_id", table_name=table)
        op.drop_column(table, "app_id")
    op.drop_table("monitored_apps")

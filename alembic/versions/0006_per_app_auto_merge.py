"""Per-app auto-merge (terminal-only v1.0 Step 3).

`monitored_apps.auto_merge`: default OFF for every app (connected repos
always start OFF; the seeded demo app can be turned ON via
`config/monitored_apps.yaml`/`selfheal apps set --auto-merge on`, since it's
the one repo this system is allowed to be reckless with). `heal_jobs.
auto_merge_override`: nullable -- NULL means "use the app's own setting",
True/False is a per-fix override (`selfheal fix --auto-merge`), set once at
fix-request time and never changed afterward.

Revision ID: 0006_per_app_auto_merge
Revises: 0005_pr_opened_at
Create Date: 2026-09-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_per_app_auto_merge"
down_revision: str | None = "0005_pr_opened_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "monitored_apps",
        sa.Column("auto_merge", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "heal_jobs",
        sa.Column("auto_merge_override", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("heal_jobs", "auto_merge_override")
    op.drop_column("monitored_apps", "auto_merge")

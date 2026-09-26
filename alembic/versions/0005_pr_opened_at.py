"""heal_jobs.pr_opened_at: when the fix PR was opened.

Lets MTTR be measured as detection -> fix PR (there is no production deploy
step in this setup). Backfills jobs still sitting in `pr_opened`, where
`updated_at` is exactly the moment the PR opened.

Revision ID: 0005_pr_opened_at
Revises: 0004_connect_a_repo
Create Date: 2026-09-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_pr_opened_at"
down_revision: str | None = "0004_connect_a_repo"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("heal_jobs", sa.Column("pr_opened_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE heal_jobs SET pr_opened_at = updated_at WHERE status = 'pr_opened'")


def downgrade() -> None:
    op.drop_column("heal_jobs", "pr_opened_at")

"""heal_jobs.produced_by_backend (terminal-only v1.0 Step 4).

Records which AI backend actually produced a heal_job's fix, so PR/comment
attribution can name it and `healer/automerge.py` can refuse to auto-merge
a fix a FALLBACK backend produced (only the chain's first choice is ever
eligible for auto-merge). NULL for jobs from before this column existed, or
that never got far enough to run a backend.

Revision ID: 0007_backend_attribution
Revises: 0006_per_app_auto_merge
Create Date: 2026-09-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_backend_attribution"
down_revision: str | None = "0006_per_app_auto_merge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "heal_jobs",
        sa.Column("produced_by_backend", sa.String(length=50), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("heal_jobs", "produced_by_backend")

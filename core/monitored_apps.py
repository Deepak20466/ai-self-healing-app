"""Load `config/monitored_apps.yaml` and upsert it into the `monitored_apps`
table (multi-app support -- see CLAUDE.md's "Multi-app support" log entry).

The YAML file is the human-editable source of truth; the DB row is what
every server-side lookup actually reads (propose_patch's write scope,
run_tests' default test target, the healer's worktree/PR target repo,
sentinel's per-app ingest-token auth) -- looked up from the heal_job's/
error's `app_id`, never trusted from a caller-supplied app name. Upserting
by `name` (not `id`) keeps this idempotent and safe to re-run: editing an
existing app's config in the YAML updates its row in place rather than
creating a duplicate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import MonitoredApp

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "monitored_apps.yaml"


def load_app_configs(path: Path = DEFAULT_CONFIG_PATH) -> list[dict[str, Any]]:
    """Parse the YAML file into a list of app config dicts."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    apps = raw.get("apps", []) if raw else []
    if not isinstance(apps, list):
        raise ValueError(f"{path}: 'apps' must be a list")
    return apps


async def sync_monitored_apps(
    session: AsyncSession, path: Path = DEFAULT_CONFIG_PATH
) -> list[MonitoredApp]:
    """Upsert every app in `path` into `monitored_apps`, keyed by `name`.

    Returns the resulting rows. Caller commits (matches this project's other
    seed/sync scripts, e.g. scripts/seed_demo.py).
    """
    configs = load_app_configs(path)
    rows: list[MonitoredApp] = []
    for cfg in configs:
        values = {
            "name": cfg["name"],
            "language": cfg["language"],
            "local_repo_path": cfg["local_repo_path"],
            "github_repo": cfg["github_repo"],
            "allowed_write_paths": cfg["allowed_write_paths"],
            "test_command": cfg["test_command"],
            "lint_command": cfg.get("lint_command"),
            "health_url": cfg.get("health_url"),
            "ingest_token": cfg["ingest_token"],
        }
        stmt = (
            pg_insert(MonitoredApp)
            .values(**values)
            .on_conflict_do_update(index_elements=[MonitoredApp.name], set_=values)
            .returning(MonitoredApp)
        )
        row = (await session.execute(stmt)).scalar_one()
        rows.append(row)
    return rows

"""Upsert `config/monitored_apps.yaml` into the `monitored_apps` table.

Idempotent (upserts by `name`), same pattern as `scripts/seed_demo.py`.
Run this after editing the YAML file, or once when setting up a new
environment: `python scripts/sync_monitored_apps.py`.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.db import dispose_engine, session_scope
from core.monitored_apps import sync_monitored_apps


async def main() -> None:
    async with session_scope() as session:
        apps = await sync_monitored_apps(session)
    for app in apps:
        print(f"  {app.name} ({app.language}) -> {app.local_repo_path}")
    print(f"Synced {len(apps)} monitored app(s).")
    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())

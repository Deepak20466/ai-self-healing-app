"""One-off: delete test/seed rows that leaked into the dev database.

Only touches `errors` rows whose exception_type is a known test-only marker
(and heal_jobs/other rows that reference them by fingerprint). Real demo
errors (ZeroDivisionError, KeyError, ...) are left alone. Dry-run by default;
pass --apply to delete. Refuses to run against a *_test database.
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import text

from core.config import settings
from core.db import session_scope

TEST_TYPES = ("OpenOne", "ResolvedOne", "McpToolsErrorsTestType", "test-runner:test")


async def main(apply: bool) -> None:
    if settings.database_url.rstrip("/").endswith("_test"):
        raise SystemExit("Refusing: DATABASE_URL points at the test database.")
    async with session_scope() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT id, exception_type, fingerprint FROM errors "
                    "WHERE exception_type = ANY(:t) OR exception_type LIKE 'TestType%'"
                ),
                {"t": list(TEST_TYPES)},
            )
        ).all()
        print(f"{len(rows)} test error rows: {sorted({r[1] for r in rows})}")
        if not apply or not rows:
            return
        fps = [r[2] for r in rows]
        ids = [r[0] for r in rows]
        await s.execute(text("DELETE FROM heal_jobs WHERE fingerprint = ANY(:f)"), {"f": fps})
        await s.execute(text("DELETE FROM errors WHERE id = ANY(:i)"), {"i": ids})
        print("deleted")


if __name__ == "__main__":
    asyncio.run(main("--apply" in sys.argv))

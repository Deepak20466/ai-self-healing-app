"""Write a `metrics.json` snapshot from real data (SPEC.md METRICS DASHBOARD:
"scripts/export_metrics.py writes a metrics.json snapshot used in the
README"). Queries `core/metrics.py` directly against the real dev database
(same precedent as `scripts/seed_demo.py`/`scripts/local_deploy.py` — never
run by pytest, never touches `selfheal_test`).

Usage: python scripts/export_metrics.py [--out metrics.json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _json_default(value: object) -> object:
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise TypeError(f"not JSON serializable: {value!r}")


async def _collect() -> dict[str, object]:
    from core.config import settings
    from core.db import dispose_engine, session_scope
    from core.metrics import get_metrics_summary

    async with session_scope() as session:
        summary = await get_metrics_summary(
            session, daily_budget_usd=float(settings.daily_budget_usd)
        )
    await dispose_engine()
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        **json.loads(summary.model_dump_json()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(REPO_ROOT / "metrics.json"))
    args = parser.parse_args()

    data = asyncio.run(_collect())
    out_path = Path(args.out)
    out_path.write_text(json.dumps(data, indent=2, default=_json_default))
    print(json.dumps(data, indent=2, default=_json_default))
    print(f"\nWrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

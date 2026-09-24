"""Contract checks: expected outputs for known inputs against the seeded dataset.

Each `ContractCase` names a real HTTP call and the exact response we know is
correct for the deterministic data in seed_data.py. The sentinel prober
(sentinel/prober.py) replays these every `CONTRACT_PROBE_INTERVAL_SECONDS`
and flags any mismatch as a `contract_violation` — this is how the two
"silent" bugs (off-by-one, timezone) get caught even though they never raise
an exception.

`source_file`/`source_line` are resolved via `inspect` against the actual
bugs.py function so they can never drift out of sync with the code.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from apps.target_app import bugs, seed_data

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _source_location(func: Callable[..., Any]) -> tuple[str, int]:
    file_path = Path(inspect.getsourcefile(func) or "").resolve()
    _, line_number = inspect.getsourcelines(func)
    try:
        relative = file_path.relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        relative = file_path.as_posix()
    return relative, line_number


class ContractCase(BaseModel):
    """One known-good (endpoint, input) -> expected-output assertion."""

    name: str
    description: str
    method: str = "GET"
    path: str
    params: dict[str, Any] = {}
    expected: dict[str, Any]
    source_file: str
    source_line: int


def _case(
    *,
    name: str,
    description: str,
    method: str,
    path: str,
    params: dict[str, Any] | None,
    expected: dict[str, Any],
    responsible_func: Callable[..., Any],
) -> ContractCase:
    file_path, line_number = _source_location(responsible_func)
    return ContractCase(
        name=name,
        description=description,
        method=method,
        path=path,
        params=params or {},
        expected=expected,
        source_file=file_path,
        source_line=line_number,
    )


CONTRACT_CASES: list[ContractCase] = [
    _case(
        name="average_rating_healthy",
        description="A rated item's average rating is computed correctly.",
        method="GET",
        path=f"/items/{seed_data.RATED_ITEM_ID}/average-rating",
        params=None,
        expected={"item_id": seed_data.RATED_ITEM_ID, "average_rating": 4.5},
        responsible_func=bugs.average_rating,
    ),
    _case(
        name="order_status_label_healthy",
        description="A normal order status resolves to its human-readable label.",
        method="GET",
        path=f"/orders/{seed_data.HEALTHY_ORDER_ID}/status-label",
        params=None,
        expected={"order_id": seed_data.HEALTHY_ORDER_ID, "status": "shipped", "label": "Shipped"},
        responsible_func=bugs.order_status_label,
    ),
    _case(
        name="top_items_by_rating",
        description=(
            "Top 3 items by average rating must be USB-C Hub (5.0), Wireless Mouse (4.5), "
            "Webcam 1080p (4.0) in that order."
        ),
        method="GET",
        path="/items/top",
        params={"n": 3},
        expected={
            "items": [
                {"id": 3, "name": "USB-C Hub", "price_cents": 3499, "average_rating": 5.0},
                {"id": 1, "name": "Wireless Mouse", "price_cents": 1999, "average_rating": 4.5},
                {"id": 5, "name": "Webcam 1080p", "price_cents": 4999, "average_rating": 4.0},
            ]
        },
        responsible_func=bugs.top_items_by_rating,
    ),
    _case(
        name="delivery_estimate_timezone",
        description=(
            "Order placed 2026-01-02 01:15 IST (2026-01-01 19:45 UTC) should deliver "
            "3 storefront-local-calendar-days later, on 2026-01-05."
        ),
        method="GET",
        path=f"/orders/{seed_data.TIMEZONE_ORDER_ID}/delivery-estimate",
        params=None,
        expected={
            "order_id": seed_data.TIMEZONE_ORDER_ID,
            "estimated_delivery_date": "2026-01-05",
        },
        responsible_func=bugs.estimate_delivery_date,
    ),
]


def find_violations(actual: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    """Return the subset of `expected` keys whose value in `actual` differs (or is missing)."""
    mismatches: dict[str, Any] = {}
    for key, expected_value in expected.items():
        if key not in actual or actual[key] != expected_value:
            mismatches[key] = {"expected": expected_value, "actual": actual.get(key, "<missing>")}
    return mismatches

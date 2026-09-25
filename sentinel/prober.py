"""Synthetic prober: replays apps/target_app/contracts.py against a live app.

`probe_once` is pure I/O-in, data-out (given an `httpx.AsyncClient`, which
tests point at target_app's in-process ASGI app via `ASGITransport` instead
of a real socket) so it's fully testable without two real running
processes; `run_prober_loop` is the thin scheduling wrapper used by the real
sentinel-pod process.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from apps.target_app.contracts import CONTRACT_CASES, ContractCase, find_violations
from core.config import settings
from core.db import async_session_factory
from core.monitored_apps import get_app_by_name
from sentinel import storage


@dataclass
class ProbeResult:
    case: ContractCase
    actual: dict[str, Any]
    mismatches: dict[str, Any]

    @property
    def is_violation(self) -> bool:
        return bool(self.mismatches)


async def probe_once(client: httpx.AsyncClient) -> list[ProbeResult]:
    """Run every contract case once against `client` and compare to expectations."""
    results: list[ProbeResult] = []
    for case in CONTRACT_CASES:
        try:
            response = await client.request(case.method, case.path, params=case.params)
            response.raise_for_status()
            actual = response.json()
        except httpx.HTTPError as exc:
            actual = {"__probe_error__": str(exc)}
        mismatches = find_violations(actual, case.expected)
        results.append(ProbeResult(case=case, actual=actual, mismatches=mismatches))
    return results


async def persist_results(session: AsyncSession, results: list[ProbeResult]) -> None:
    """Record every violation in `results`. Caller owns the session's lifecycle."""
    if not any(result.is_violation for result in results):
        return
    target_app = await get_app_by_name(session, "target_app")
    app_id = target_app.id if target_app else None
    for result in results:
        if not result.is_violation:
            continue
        await storage.record_contract_violation(
            session,
            endpoint=result.case.path,
            case_name=result.case.name,
            expected=result.case.expected,
            actual=result.actual,
            file_path=result.case.source_file,
            line_number=result.case.source_line,
            app_id=app_id,
        )


async def run_prober_loop(base_url: str, *, interval_seconds: int | None = None) -> None:
    """Forever: probe target_app, persist violations, sleep, repeat."""
    interval = interval_seconds or settings.contract_probe_interval_seconds
    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        while True:
            results = await probe_once(client)
            async with async_session_factory() as session:
                await persist_results(session, results)
            await asyncio.sleep(interval)

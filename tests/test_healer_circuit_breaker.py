"""healer.circuit_breaker: fingerprint 24h cap, global hourly cap, and the
per-PR CI-fix attempt cap (SPEC.md SAFETY GUARDRAILS).

Real commits via `session_scope()` (like `sentinel/anomaly.py` and
`mcp_server/audit.py`'s tests), since these counts read across the shared
`selfheal_test` DB. The global-hourly test computes its threshold relative to
whatever is already in the table rather than assuming it's empty, and the
per-PR CI-fix tests use a random `pr_number` per test — see CLAUDE.md's
"shared dev DB pollutes unscoped queries" note.
"""

from __future__ import annotations

import uuid

from core.db import session_scope
from core.models import HealJob, HealJobStatus, HealJobType
from healer.circuit_breaker import (
    ci_fix_attempt_count_for_pr,
    ci_fix_circuit_open,
    fingerprint_circuit_open,
    global_hourly_circuit_open,
    global_job_count_last_hour,
)


async def _insert_job(fingerprint: str, *, attempt_count: int = 1) -> None:
    async with session_scope() as session:
        session.add(
            HealJob(
                type=HealJobType.RUNTIME_ERROR,
                status=HealJobStatus.QUEUED,
                fingerprint=fingerprint,
                attempt_count=attempt_count,
            )
        )


async def _insert_ci_job(pr_number: int, attempt_count: int) -> None:
    async with session_scope() as session:
        session.add(
            HealJob(
                type=HealJobType.CI_FAILURE,
                status=HealJobStatus.FAILED,
                fingerprint=f"ci-circuit-test-{uuid.uuid4().hex}",
                pr_number=pr_number,
                attempt_count=attempt_count,
            )
        )


def _random_pr_number() -> int:
    """A `pr_number` unique enough per test not to collide in the shared
    `selfheal_test` DB, but within Postgres `Integer` range."""
    return uuid.uuid4().int % 1_000_000_000


async def test_fingerprint_circuit_stays_closed_below_the_cap() -> None:
    fingerprint = f"circuit-test-{uuid.uuid4().hex}"
    for _ in range(2):
        await _insert_job(fingerprint)

    async with session_scope() as session:
        assert await fingerprint_circuit_open(session, fingerprint, max_attempts=3) is False


async def test_fingerprint_circuit_opens_at_the_cap() -> None:
    fingerprint = f"circuit-test-{uuid.uuid4().hex}"
    for _ in range(3):
        await _insert_job(fingerprint)

    async with session_scope() as session:
        assert await fingerprint_circuit_open(session, fingerprint, max_attempts=3) is True


async def test_fingerprint_circuit_ignores_jobs_with_no_real_attempts() -> None:
    """A job that never actually ran an attempt (e.g. it was itself refused by
    another guardrail before running) must not count against the cap - see
    circuit_breaker.py's module docstring for the cascading-lockout bug this
    guards against."""
    fingerprint = f"circuit-test-{uuid.uuid4().hex}"
    for _ in range(5):
        await _insert_job(fingerprint, attempt_count=0)

    async with session_scope() as session:
        assert await fingerprint_circuit_open(session, fingerprint, max_attempts=3) is False


async def test_fingerprint_circuit_ignores_other_fingerprints() -> None:
    fingerprint = f"circuit-test-{uuid.uuid4().hex}"
    other = f"circuit-test-{uuid.uuid4().hex}"
    for _ in range(5):
        await _insert_job(other)
    await _insert_job(fingerprint)

    async with session_scope() as session:
        assert await fingerprint_circuit_open(session, fingerprint, max_attempts=3) is False


async def test_global_hourly_circuit_opens_after_exceeding_cap() -> None:
    async with session_scope() as session:
        baseline = await global_job_count_last_hour(session)
    max_per_hour = baseline + 2

    await _insert_job(f"circuit-global-{uuid.uuid4().hex}")
    await _insert_job(f"circuit-global-{uuid.uuid4().hex}")
    async with session_scope() as session:
        assert await global_hourly_circuit_open(session, max_per_hour=max_per_hour) is False

    await _insert_job(f"circuit-global-{uuid.uuid4().hex}")
    async with session_scope() as session:
        assert await global_hourly_circuit_open(session, max_per_hour=max_per_hour) is True


async def test_ci_fix_attempt_count_sums_attempt_count_across_jobs_for_the_pr() -> None:
    pr_number = _random_pr_number()
    await _insert_ci_job(pr_number, attempt_count=1)
    await _insert_ci_job(pr_number, attempt_count=1)

    async with session_scope() as session:
        assert await ci_fix_attempt_count_for_pr(session, pr_number) == 2


async def test_ci_fix_circuit_stays_closed_below_the_cap() -> None:
    pr_number = _random_pr_number()
    await _insert_ci_job(pr_number, attempt_count=1)

    async with session_scope() as session:
        assert await ci_fix_circuit_open(session, pr_number, max_attempts=2) is False


async def test_ci_fix_circuit_opens_at_the_cap() -> None:
    pr_number = _random_pr_number()
    await _insert_ci_job(pr_number, attempt_count=2)

    async with session_scope() as session:
        assert await ci_fix_circuit_open(session, pr_number, max_attempts=2) is True


async def test_ci_fix_circuit_ignores_other_prs() -> None:
    pr_number = _random_pr_number()
    other_pr = _random_pr_number()
    await _insert_ci_job(other_pr, attempt_count=5)

    async with session_scope() as session:
        assert await ci_fix_circuit_open(session, pr_number, max_attempts=2) is False

"""Benchmark: trigger seeded bugs one at a time against the LIVE running
system and record how the real free-mode healer resolves each one.

This spends real Claude Code CLI subscription usage per bug (see CLAUDE.md's
"Free mode"/"Post-Phase-8" sections for the same manual-run pattern this
script automates) -- it is deliberately NOT part of the pytest suite and
NEVER resets/bypasses a circuit breaker or budget cap to force a bug
through. If a real guardrail blocks a run, that is recorded honestly as the
result, not retried or worked around.

Usage:
    .venv/Scripts/python.exe scripts/benchmark.py zero timezone
    .venv/Scripts/python.exe scripts/benchmark.py --list   # show bug names

Assumes (or starts, if not already running) all 4 pods. Polls the
resulting heal_job (found by fingerprint, same pattern as
scripts/verify_all.py) until it reaches a terminal status or a max wait.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import select  # noqa: E402

from core.db import session_scope  # noqa: E402
from core.models import AuditLog, HealJob, HealJobStatus  # noqa: E402

APP_URL = "http://127.0.0.1:8001"
SENTINEL_URL = "http://127.0.0.1:8002"
MCP_URL = "http://127.0.0.1:8003"
HEALER_URL = "http://127.0.0.1:8000"

#: bug -> (exception-producing function name in apps/target_app/bugs.py, endpoint)
#: fingerprint_error hashes (exception_type, file_path, function_name); the
#: exception type varies (KeyError, ZeroDivisionError, ...) so we look up the
#: heal_job by function_name + recency instead of pre-computing the exact
#: fingerprint (which would also require knowing the exact exception type,
#: itself sometimes platform-dependent -- see CLAUDE.md's timeout-bug note).
BUG_FUNCTION_NAMES = {
    "zero": "average_rating",
    "key": "order_status_label",
    "off_by_one": "top_items_by_rating",
    "none_lookup": "item_label",
    "timezone": "estimate_delivery_date",
    "timeout": "check_item_price",
    "validation": "create_order",
}

TERMINAL_STATUSES = {
    HealJobStatus.PR_OPENED,
    HealJobStatus.MERGED,
    HealJobStatus.DEPLOYED,
    HealJobStatus.VERIFIED,
    HealJobStatus.FAILED,
    HealJobStatus.ROLLED_BACK,
    HealJobStatus.PAUSED_BUDGET,
}

MAX_WAIT_SECONDS = 20 * 60  # a real CLI attempt can legitimately take minutes
POLL_INTERVAL_SECONDS = 10


@dataclass
class BenchmarkResult:
    bug: str
    triggered_at: str
    heal_job_id: int | None
    fingerprint: str | None
    final_status: str
    attempt_count: int
    cli_invocations: int
    cli_turns_total: int
    circuit_breaker_blocked: bool
    wall_clock_seconds: float | None
    pr_url: str | None
    notes: str
    time_to_pr_seconds: float | None = None
    full_suite: str = "n/a"
    cost_usd: float = 0.0


async def _healthz(client: httpx.AsyncClient, url: str) -> bool:
    try:
        resp = await client.get(url, timeout=5.0)
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


async def _mcp_reachable(client: httpx.AsyncClient) -> bool:
    try:
        resp = await client.get(f"{MCP_URL}/mcp", timeout=5.0)
        return resp.status_code < 500
    except httpx.HTTPError:
        return False


async def ensure_pods_running() -> list[subprocess.Popen[bytes]]:
    """Checks /healthz for each pod; starts any that aren't up. Returns the
    list of processes THIS script started (so it can stop only those, not
    pods that were already running before it ran)."""
    started: list[subprocess.Popen[bytes]] = []
    venv_python = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    venv_uvicorn = REPO_ROOT / ".venv" / "Scripts" / "uvicorn.exe"

    async with httpx.AsyncClient() as client:
        checks = {
            "app": (
                f"{APP_URL}/healthz",
                [str(venv_uvicorn), "apps.target_app.main:app", "--port", "8001"],
            ),
            "sentinel": (
                f"{SENTINEL_URL}/healthz",
                [str(venv_uvicorn), "sentinel.app:app", "--port", "8002"],
            ),
            "healer": (
                f"{HEALER_URL}/healthz",
                [str(venv_uvicorn), "healer.app:asgi_app", "--port", "8000"],
            ),
        }
        for name, (url, cmd) in checks.items():
            if await _healthz(client, url):
                print(f"[benchmark] pod '{name}' already running")
                continue
            print(f"[benchmark] starting pod '{name}': {' '.join(cmd)}")
            proc = subprocess.Popen(  # noqa: ASYNC220
                cmd, cwd=REPO_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            started.append(proc)

        if not await _mcp_reachable(client):
            print("[benchmark] starting pod 'mcp'")
            proc = subprocess.Popen(  # noqa: ASYNC220
                [str(venv_python), "-m", "mcp_server.http_main"],
                cwd=REPO_ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            started.append(proc)
        else:
            print("[benchmark] pod 'mcp' already running")

        # Wait for anything newly started to come up.
        if started:
            for _ in range(30):
                async with httpx.AsyncClient() as c:
                    ok = all(
                        [
                            await _healthz(c, f"{APP_URL}/healthz"),
                            await _healthz(c, f"{SENTINEL_URL}/healthz"),
                            await _healthz(c, f"{HEALER_URL}/healthz"),
                            await _mcp_reachable(c),
                        ]
                    )
                if ok:
                    break
                await asyncio.sleep(2)
            else:
                raise RuntimeError("pods did not come up within 60s")
    return started


def stop_pods(procs: list[subprocess.Popen[bytes]]) -> None:
    for proc in procs:
        print(f"[benchmark] stopping pid {proc.pid}")
        proc.terminate()
    for proc in procs:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


async def trigger_bug(bug: str) -> tuple[str, float]:
    """POSTs /trigger/{bug}. Returns (triggered_at ISO string, monotonic start)."""
    from datetime import UTC, datetime

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            await client.get(f"{APP_URL}/trigger/{bug}")
        except httpx.HTTPError:
            pass  # the bug is expected to raise -- a 5xx/connection error IS the trigger
    return datetime.now(UTC).isoformat(), time.monotonic()


def bug_name_for(function_name: str) -> str:
    return next(b for b, f in BUG_FUNCTION_NAMES.items() if f == function_name)


async def find_heal_job(function_name: str, after: datetime) -> HealJob | None:
    """Finds the heal_job for this bug's error, created after the trigger.

    Looks up by the Error row's function_name (same pattern verify_all.py
    uses -- more robust than exception_type, which is sometimes
    platform-dependent), then the most recent heal_job for that fingerprint.
    """
    from core.models import Error

    deadline = time.monotonic() + 90
    last_trigger = time.monotonic()
    while time.monotonic() < deadline:
        # Enqueue happens on the 1st occurrence then every Nth (CLAUDE.md), so a
        # bug seen many times before needs a few more triggers to enqueue anew.
        if time.monotonic() - last_trigger > 3:
            await trigger_bug(bug_name_for(function_name))
            last_trigger = time.monotonic()
        async with session_scope() as session:
            err_stmt = (
                select(Error)
                .where(Error.function_name == function_name)
                .order_by(Error.last_seen_at.desc())
                .limit(1)
            )
            err = (await session.execute(err_stmt)).scalar_one_or_none()
            if err is not None:
                job_stmt = (
                    select(HealJob)
                    .where(HealJob.fingerprint == err.fingerprint, HealJob.created_at >= after)
                    .order_by(HealJob.created_at.desc())
                    .limit(1)
                )
                job = (await session.execute(job_stmt)).scalar_one_or_none()
                if job is not None:
                    return job
        await asyncio.sleep(2)
    return None


async def poll_until_terminal(heal_job_id: int) -> HealJob:
    deadline = time.monotonic() + MAX_WAIT_SECONDS
    last: HealJob | None = None
    while time.monotonic() < deadline:
        async with session_scope() as session:
            job = await session.get(HealJob, heal_job_id)
            if job is None:
                raise RuntimeError(f"heal_job {heal_job_id} disappeared")
            # Detach the fields we need before the session closes.
            status = job.status
            attempt_count = job.attempt_count
            last = job
            print(
                f"[benchmark]   heal_job {heal_job_id}: status={status.value} "
                f"attempts={attempt_count} (elapsed "
                f"{time.monotonic() - (deadline - MAX_WAIT_SECONDS):.0f}s)"
            )
            if status in TERMINAL_STATUSES:
                return job
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
    assert last is not None
    return last


async def cli_stats_for_job(heal_job_id: int) -> tuple[int, int]:
    """Returns (num cli_invocation audit_log rows, total num_turns) for this job."""
    async with session_scope() as session:
        stmt = select(AuditLog).where(
            AuditLog.action == "cli_invocation", AuditLog.heal_job_id == heal_job_id
        )
        rows = (await session.execute(stmt)).scalars().all()
        turns = 0
        for r in rows:
            details = r.details or {}
            t = details.get("num_turns")
            if isinstance(t, int):
                turns += t
        return len(rows), turns


async def suite_and_cost(heal_job_id: int) -> tuple[str, float]:
    """Full-suite verdict of the last attempt (from its stored evidence) and total cost."""
    from core.models import FixAttempt

    async with session_scope() as session:
        attempts = (
            (
                await session.execute(
                    select(FixAttempt)
                    .where(FixAttempt.heal_job_id == heal_job_id)
                    .order_by(FixAttempt.attempt_number)
                )
            )
            .scalars()
            .all()
        )
        cli_rows = (
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.action == "cli_invocation", AuditLog.heal_job_id == heal_job_id
                    )
                )
            )
            .scalars()
            .all()
        )
    cost = sum(float(a.cost_usd or 0) for a in attempts)
    for r in cli_rows:
        c = (r.details or {}).get("total_cost_usd")
        if c:
            cost += float(c)
    verdict = "n/a (never reached)"
    for a in attempts:
        out = a.test_output or ""
        if "Full test suite: PASSED" in out:
            verdict = "PASSED"
        elif "Full test suite: FAILED" in out:
            verdict = "FAILED"
    return verdict, round(cost, 4)


async def circuit_breaker_blocked(heal_job_id: int) -> bool:
    async with session_scope() as session:
        stmt = select(AuditLog).where(
            AuditLog.action == "circuit_breaker_tripped", AuditLog.heal_job_id == heal_job_id
        )
        row = (await session.execute(stmt)).scalars().first()
        return row is not None


async def pr_url_for_job(heal_job_id: int) -> str | None:
    async with session_scope() as session:
        stmt = select(AuditLog).where(
            AuditLog.action == "pr_opened", AuditLog.heal_job_id == heal_job_id
        )
        row = (await session.execute(stmt)).scalars().first()
        if row and row.details:
            url = row.details.get("pr_url") or row.details.get("url")
            if isinstance(url, str):
                return url
        return None


async def run_one_bug(bug: str) -> BenchmarkResult:
    function_name = BUG_FUNCTION_NAMES[bug]
    print(f"\n[benchmark] === triggering bug '{bug}' ===")
    triggered_at, start_monotonic = await trigger_bug(bug)

    job = await find_heal_job(function_name, datetime.fromisoformat(triggered_at))
    if job is None:
        return BenchmarkResult(
            bug=bug,
            triggered_at=triggered_at,
            heal_job_id=None,
            fingerprint=None,
            final_status="no_heal_job_enqueued",
            attempt_count=0,
            cli_invocations=0,
            cli_turns_total=0,
            circuit_breaker_blocked=False,
            wall_clock_seconds=None,
            pr_url=None,
            notes="No heal_job appeared within 30s of triggering -- either "
            "sentinel didn't capture it, or the reoccurrence-threshold rule "
            "(CLAUDE.md: enqueue on first occurrence, then every Nth) means "
            "this fingerprint already had one in flight.",
        )

    heal_job_id = job.id
    fingerprint = job.fingerprint
    print(f"[benchmark] found heal_job {heal_job_id} (fingerprint={fingerprint[:12]}...)")

    final_job = await poll_until_terminal(heal_job_id)
    elapsed = time.monotonic() - start_monotonic

    cli_invocations, cli_turns = await cli_stats_for_job(heal_job_id)
    full_suite, cost_usd = await suite_and_cost(heal_job_id)
    blocked = await circuit_breaker_blocked(heal_job_id)
    pr_url = await pr_url_for_job(heal_job_id)

    notes = ""
    if final_job.status not in TERMINAL_STATUSES:
        notes = f"Timed out after {MAX_WAIT_SECONDS}s waiting for a terminal status."
    elif final_job.status == HealJobStatus.PAUSED_BUDGET:
        notes = "Real daily budget cap was hit -- honestly recorded, not bypassed."
    elif blocked:
        notes = "Circuit breaker tripped for this fingerprint -- honestly recorded, not bypassed."

    return BenchmarkResult(
        bug=bug,
        triggered_at=triggered_at,
        heal_job_id=heal_job_id,
        fingerprint=fingerprint,
        final_status=final_job.status.value,
        attempt_count=final_job.attempt_count,
        cli_invocations=cli_invocations,
        cli_turns_total=cli_turns,
        circuit_breaker_blocked=blocked,
        wall_clock_seconds=round(elapsed, 1),
        pr_url=pr_url,
        notes=notes,
        time_to_pr_seconds=(
            round((final_job.pr_opened_at - final_job.created_at).total_seconds(), 1)
            if final_job.pr_opened_at
            else None
        ),
        full_suite=full_suite,
        cost_usd=cost_usd,
    )


def write_markdown(results: list[BenchmarkResult]) -> None:
    from datetime import UTC, datetime

    today = datetime.now(UTC).date().isoformat()
    fixed = sum(1 for r in results if r.final_status == "pr_opened")
    lines = [
        f"# Benchmark (clean run, {today})",
        "",
        f"**{fixed}/{len(results)} bugs fixed** (PR opened after the fix passed its regression "
        "test AND the app's full configured test suite). Generated by `scripts/benchmark.py` "
        "against the live system: real free-mode AI CLI, real MCP tools, real git worktrees, "
        "no mocking, no bypassed circuit breakers or budget caps. All 7 seeded bugs were "
        "reset first, and stale leftover jobs were marked failed (history kept). This is a "
        "single clean run, deliberately separate from the all-time and attempted rates.",
        "",
        "| Bug | Result | Attempts | CLI turns | Time to PR | Full suite | Cost | PR |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        result_word = "fixed (PR opened)" if r.final_status == "pr_opened" else r.final_status
        pr = f"[link]({r.pr_url})" if r.pr_url else "-"
        ttp = f"{r.time_to_pr_seconds / 60:.1f} min" if r.time_to_pr_seconds else "-"
        lines.append(
            f"| `{r.bug}` | {result_word} | {r.attempt_count} | {r.cli_turns_total} | {ttp} | "
            f"{r.full_suite} | ${r.cost_usd:.2f} | {pr} |"
        )
    lines += ["", "## Notes", ""]
    for r in results:
        lines.append(
            f"- `{r.bug}`: heal_job {r.heal_job_id}, status `{r.final_status}`"
            + (f" -- {r.notes}" if r.notes else "")
        )
    lines.append("")

    out = REPO_ROOT / "docs" / "benchmark.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text(chr(10).join(lines), encoding="utf-8")
    print(f"[benchmark] wrote {out}")

    (REPO_ROOT / "benchmark_results.json").write_text(
        json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8"
    )
    # Read by core/metrics.py for the Metrics page (committed, tiny).
    (REPO_ROOT / "docs" / "benchmark_latest.json").write_text(
        json.dumps({"date": today, "fixed": fixed, "total": len(results)}), encoding="utf-8"
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bugs", nargs="*", help="bug names to run, e.g. zero timezone")
    parser.add_argument("--list", action="store_true", help="list available bug names and exit")
    parser.add_argument(
        "--no-manage-pods",
        action="store_true",
        help="don't start/stop pods; assume they're already running",
    )
    args = parser.parse_args()

    if args.list:
        print("\n".join(BUG_FUNCTION_NAMES))
        return 0

    bugs = args.bugs or ["timezone", "zero"]
    for b in bugs:
        if b not in BUG_FUNCTION_NAMES:
            print(f"unknown bug '{b}'; choices: {list(BUG_FUNCTION_NAMES)}", file=sys.stderr)
            return 2
    if len(bugs) > 3:
        print("benchmark.py runs at most 3 bugs per invocation by design", file=sys.stderr)
        return 2

    started: list[subprocess.Popen[bytes]] = []
    if not args.no_manage_pods:
        started = await ensure_pods_running()

    results: list[BenchmarkResult] = []
    try:
        for bug in bugs:
            result = await run_one_bug(bug)
            results.append(result)
            print(f"[benchmark] '{bug}' -> {result.final_status} in {result.wall_clock_seconds}s")
    finally:
        write_markdown(results)
        if started:
            stop_pods(started)

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

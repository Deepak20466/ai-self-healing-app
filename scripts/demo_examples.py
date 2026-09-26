"""Live proof for "any language": run the Node and Go example apps for real,
trigger each seeded bug, and verify sentinel captured it over OpenTelemetry
with the correct file + line; then scan each example.

Steps: sync `config/monitored_apps.yaml` -> start sentinel-pod (unless already
up) -> start each example with its ingest token -> hit its buggy endpoint ->
read the stored `errors` row -> run the scanner on a copy under
`connected_apps/` (the scanner refuses anything else) -> clean up.

Never invokes an AI backend. The heal_jobs sentinel enqueues for the demo
errors are marked `failed` at the end so a healer started later cannot pick
them up and spend AI budget on them.

Usage: python scripts/demo_examples.py            (exit 1 if any check fails)
"""

# ruff: noqa: ASYNC210, ASYNC220, ASYNC221
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import delete, select, update  # noqa: E402

from core.db import dispose_engine, session_scope  # noqa: E402
from core.models import Error, Finding, HealJob, HealJobStatus, MonitoredApp  # noqa: E402
from core.monitored_apps import sync_monitored_apps  # noqa: E402
from core.scanner import run_scan  # noqa: E402

SENTINEL = "http://127.0.0.1:8002"


def _line_of(path: Path, needle: str) -> int:
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if needle in line:
            return i
    raise AssertionError(f"{needle!r} not in {path}")


EXAMPLES = [
    {
        "name": "node_app",
        "port": 8101,
        "cmd": ["node", "-r", "./src/telemetry.js", "src/server.js"],
        "url": "http://127.0.0.1:8101/users/999/display-name",
        "file": "examples/node_app/src/users.js",
        "line": _line_of(ROOT / "examples/node_app/src/users.js", "user.name.toUpperCase"),
        "exception": "TypeError",
    },
    {
        "name": "go_app",
        "port": 8102,
        "cmd": ["go", "run", "."],
        "url": "http://127.0.0.1:8102/stats/average?values=",
        "file": "examples/go_app/calc.go",
        "line": _line_of(ROOT / "examples/go_app/calc.go", "return sum / len(values)"),
        "exception": "runtime.boundsError",  # replaced below; Go reports runtime.Error types
    },
]


def _wait_http(url: str, timeout: float = 90.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=2).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise TimeoutError(f"{url} never came up")


async def main() -> int:  # noqa: C901
    results: list[tuple[str, bool, str]] = []
    procs: list[subprocess.Popen[bytes]] = []
    copies: list[Path] = []
    scan_names: list[str] = []
    try:
        async with session_scope() as session:
            await sync_monitored_apps(session)

        try:
            httpx.get(f"{SENTINEL}/healthz", timeout=2)
        except httpx.HTTPError:
            procs.append(
                subprocess.Popen(  # noqa: S603
                    [sys.executable, "-m", "uvicorn", "sentinel.app:app", "--port", "8002"],
                    cwd=ROOT,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
            _wait_http(f"{SENTINEL}/healthz")

        for ex in EXAMPLES:
            async with session_scope() as session:
                app = (
                    await session.execute(
                        select(MonitoredApp).where(MonitoredApp.name == ex["name"])
                    )
                ).scalar_one()
                token, app_id = app.ingest_token, app.id
                await session.execute(delete(Error).where(Error.app_id == app_id))

            env = {
                **os.environ,
                "PORT": str(ex["port"]),
                "SELFHEAL_INGEST_TOKEN": token,
                "SELFHEAL_OTLP_ENDPOINT": SENTINEL,
            }
            proc = subprocess.Popen(  # noqa: S603
                ex["cmd"],
                cwd=ROOT / "examples" / ex["name"],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=sys.platform == "win32",
            )
            procs.append(proc)
            _wait_http(f"http://127.0.0.1:{ex['port']}/healthz")
            status = httpx.get(ex["url"], timeout=10).status_code
            results.append(
                (f"{ex['name']}: buggy endpoint returns 500", status == 500, str(status))
            )

            error = None
            for _ in range(20):
                await asyncio.sleep(1)
                async with session_scope() as session:
                    error = (
                        (await session.execute(select(Error).where(Error.app_id == app_id)))
                        .scalars()
                        .first()
                    )
                if error:
                    break
            ok = bool(error) and error.file_path == ex["file"] and error.line_number == ex["line"]
            got = (
                f"{error.file_path}:{error.line_number} ({error.exception_type})"
                if error
                else "no error captured"
            )
            results.append(
                (f"{ex['name']}: captured via OTLP at {ex['file']}:{ex['line']}", ok, got)
            )

        # --- scan each example (copied under connected_apps/, the only place the scanner allows)
        for ex in EXAMPLES:
            scan_name = f"scan-{ex['name'].replace('_', '-')}"
            dest = ROOT / "connected_apps" / scan_name
            shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(
                ROOT / "examples" / ex["name"],
                dest,
                ignore=shutil.ignore_patterns("node_modules"),
            )
            copies.append(dest)
            scan_names.append(scan_name)
            async with session_scope() as session:
                await session.execute(delete(MonitoredApp).where(MonitoredApp.name == scan_name))
                base = (
                    await session.execute(
                        select(MonitoredApp).where(MonitoredApp.name == ex["name"])
                    )
                ).scalar_one()
                scan_app = MonitoredApp(
                    name=scan_name,
                    language=base.language,
                    local_repo_path=f"connected_apps/{scan_name}",
                    github_repo=base.github_repo,
                    allowed_write_paths=[f"connected_apps/{scan_name}/"],
                    test_command="npm test" if base.language == "javascript" else "go test ./...",
                    ingest_token=f"scan-{scan_name}-{time.time_ns()}",
                    repo_url="https://example.invalid/scan-only.git",
                )
                session.add(scan_app)
                await session.flush()
                summary = await run_scan(session, scan_app)
                findings = (
                    (await session.execute(select(Finding).where(Finding.app_id == scan_app.id)))
                    .scalars()
                    .all()
                )
                cats = sorted({f.category.value for f in findings})
            results.append(
                (
                    f"{ex['name']}: scan ran, seeded failing test detected",
                    summary.tests_passed is False and "test" in cats,
                    f"tests_passed={summary.tests_passed} findings={cats} "
                    f"health={summary.health_score} skipped={list(summary.skipped_checks)}",
                )
            )
    finally:
        for proc in procs:
            subprocess.run(  # noqa: S603, S607
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)]
                if sys.platform == "win32"
                else ["kill", str(proc.pid)],
                capture_output=True,
            )
        for path in copies:
            shutil.rmtree(path, ignore_errors=True)
        async with session_scope() as session:
            if scan_names:
                await session.execute(delete(MonitoredApp).where(MonitoredApp.name.in_(scan_names)))
            ids = (
                (
                    await session.execute(
                        select(MonitoredApp.id).where(
                            MonitoredApp.name.in_([e["name"] for e in EXAMPLES])
                        )
                    )
                )
                .scalars()
                .all()
            )
            await session.execute(
                update(HealJob)
                .where(HealJob.app_id.in_(ids), HealJob.status == HealJobStatus.QUEUED)
                .values(status=HealJobStatus.FAILED)
            )
        await dispose_engine()

    width = max(len(r[0]) for r in results)
    for label, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {label.ljust(width)}  {detail}")
    return 0 if results and all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

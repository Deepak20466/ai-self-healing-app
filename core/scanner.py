"""Instant scan: install deps, run tests/lint/type-check/dependency-audit for
a connected app, store every finding in `findings` (core/models.py).

**Isolation**: every subprocess this module spawns has its `cwd` pinned to
the app's own directory under `connected_apps/<name>/` (validated with
`_app_dir` below, the same "resolve and check it's really inside the
expected root" pattern `mcp_server/sandbox.py` uses) and a timeout + capped
captured output, so one connected app's scan can never touch another app's
files, this repo's own files, or run forever. A Python app additionally gets
its own real virtualenv (`<app_dir>/.selfheal_venv/`, see `_ensure_app_venv`)
-- its dependencies (and ruff/mypy/pip-audit) install there, never into the
healer process's own environment or onto a bare `pytest`/`ruff` looked up on
PATH, so two connected apps' dependency versions (or this project's own)
can never collide. A JavaScript app gets the equivalent isolation for free
from `npm install`'s own per-directory `node_modules/`. This installs and
executes the connected repo's own code exactly as its own CI would -- that
is real code execution from a repo the operator chose to connect, the same
trust model as cloning and running any third-party project locally; it is
not sandboxed beyond directory/time/output/dependency scoping (no
container, to keep this free of new infrastructure). Only connect repos you
trust.

Every tool run here is free/open-source (ruff, mypy, pytest, pip-audit,
npm audit) -- no paid scanning service.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import Finding, FindingCategory, FindingSeverity, MonitoredApp
from core.repo_connect import CONNECTED_APPS_ROOT, app_fingerprint
from mcp_server.sandbox import REPO_ROOT

logger = structlog.get_logger(__name__)

INSTALL_TIMEOUT_SECONDS = 300.0
COMMAND_TIMEOUT_SECONDS = 180.0
MAX_CAPTURED_OUTPUT_BYTES = 100_000

ProgressCallback = Callable[[str, int], Awaitable[None]]

_SEVERITY_PENALTY = {
    FindingSeverity.LOW: 2,
    FindingSeverity.MEDIUM: 5,
    FindingSeverity.HIGH: 10,
    FindingSeverity.CRITICAL: 20,
}


class ScanError(Exception):
    """Raised when a scan can't even start (e.g. the app's directory is gone)."""


@dataclass(frozen=True)
class CommandOutcome:
    ok: bool
    timed_out: bool
    output: str


@dataclass(frozen=True)
class FindingDraft:
    category: FindingCategory
    severity: FindingSeverity
    tool: str
    message: str
    file_path: str | None = None
    line_number: int | None = None


@dataclass(frozen=True)
class ScanSummary:
    app_id: int
    tests_passed: bool | None
    findings_count: int
    health_score: int


VENV_DIR_NAME = ".selfheal_venv"
_SCANNER_TOOLS = ("pytest", "ruff", "mypy", "pip-audit")


def _venv_python(app_dir: Path) -> Path:
    """Path to the per-app venv's own python executable -- every Python
    install/test/lint/type-check/audit command for that app runs through
    this interpreter, never the healer process's own or a bare `pytest`/
    `ruff`/... found on PATH, so one connected app's dependencies (and their
    versions) can never collide with another's or with this project's own."""
    venv_dir = app_dir / VENV_DIR_NAME
    if sys.platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


async def _ensure_app_venv(app_dir: Path) -> Path:
    """Create `<app_dir>/.selfheal_venv/` if it doesn't exist yet, with
    ruff/mypy/pip-audit pre-installed (the scanner's own tools -- not the
    app's dependencies, so every Python app gets them regardless of what's
    in its own requirements.txt)."""
    python = _venv_python(app_dir)
    if not python.exists():
        await _run(f'"{sys.executable}" -m venv "{app_dir / VENV_DIR_NAME}"', cwd=app_dir)
        await _run(
            f'"{python}" -m pip install --quiet {" ".join(_SCANNER_TOOLS)}',
            cwd=app_dir,
            timeout=INSTALL_TIMEOUT_SECONDS,
        )
    return python


def _app_dir(app: MonitoredApp) -> Path:
    """Resolve and validate the app's own directory, refusing to scan
    anywhere outside `connected_apps/` even if `local_repo_path` were ever
    corrupted to something like `../../etc`."""
    resolved = (REPO_ROOT / app.local_repo_path).resolve()
    try:
        resolved.relative_to(CONNECTED_APPS_ROOT)
    except ValueError as exc:
        raise ScanError(
            f"App {app.name!r}'s local_repo_path {app.local_repo_path!r} is "
            "outside connected_apps/ -- refusing to scan"
        ) from exc
    if not resolved.is_dir():
        raise ScanError(f"App {app.name!r}'s directory {resolved} does not exist")
    return resolved


async def _run(
    command: str, *, cwd: Path, timeout: float = COMMAND_TIMEOUT_SECONDS
) -> CommandOutcome:
    process = await asyncio.create_subprocess_shell(
        command,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        return CommandOutcome(ok=False, timed_out=True, output=f"Timed out after {timeout}s")
    output = stdout.decode(errors="replace")[-MAX_CAPTURED_OUTPUT_BYTES:]
    return CommandOutcome(ok=process.returncode == 0, timed_out=False, output=output)


def parse_ruff_json(output: str) -> list[FindingDraft]:
    try:
        items = json.loads(output)
    except json.JSONDecodeError:
        return []
    drafts = []
    for item in items if isinstance(items, list) else []:
        location = item.get("location", {})
        drafts.append(
            FindingDraft(
                category=FindingCategory.LINT,
                severity=FindingSeverity.LOW,
                tool="ruff",
                file_path=item.get("filename"),
                line_number=location.get("row"),
                message=f"{item.get('code', '')}: {item.get('message', '')}".strip(": "),
            )
        )
    return drafts


def parse_mypy_json(output: str) -> list[FindingDraft]:
    drafts = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        severity = (
            FindingSeverity.MEDIUM if item.get("severity") == "error" else FindingSeverity.LOW
        )
        drafts.append(
            FindingDraft(
                category=FindingCategory.TYPE_CHECK,
                severity=severity,
                tool="mypy",
                file_path=item.get("file"),
                line_number=item.get("line"),
                message=str(item.get("message", "")),
            )
        )
    return drafts


def parse_pip_audit_json(output: str) -> list[FindingDraft]:
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return []
    drafts = []
    dependencies = data.get("dependencies", []) if isinstance(data, dict) else []
    for dep in dependencies:
        name = dep.get("name", "unknown")
        version = dep.get("version", "")
        for vuln in dep.get("vulns", []) or []:
            fix_versions = vuln.get("fix_versions") or []
            message = (
                f"{name} {version}: {vuln.get('id', 'vulnerability')} {vuln.get('description', '')}"
            ).strip()
            if fix_versions:
                message += f" (fix available: {', '.join(fix_versions)})"
            drafts.append(
                FindingDraft(
                    category=FindingCategory.DEPENDENCY,
                    severity=FindingSeverity.HIGH,
                    tool="pip-audit",
                    file_path=None,
                    line_number=None,
                    message=message,
                )
            )
    return drafts


_NPM_SEVERITY_MAP = {
    "info": FindingSeverity.LOW,
    "low": FindingSeverity.LOW,
    "moderate": FindingSeverity.MEDIUM,
    "high": FindingSeverity.HIGH,
    "critical": FindingSeverity.CRITICAL,
}


def parse_npm_audit_json(output: str) -> list[FindingDraft]:
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return []
    drafts = []
    vulnerabilities = data.get("vulnerabilities", {}) if isinstance(data, dict) else {}
    for pkg_name, info in vulnerabilities.items():
        severity = _NPM_SEVERITY_MAP.get(str(info.get("severity", "")), FindingSeverity.MEDIUM)
        via = info.get("via", [])
        titles = [v.get("title", str(v)) for v in via if isinstance(v, dict)]
        message = f"{pkg_name}: " + ("; ".join(titles) if titles else "known vulnerability")
        drafts.append(
            FindingDraft(
                category=FindingCategory.DEPENDENCY,
                severity=severity,
                tool="npm-audit",
                file_path=None,
                line_number=None,
                message=message,
            )
        )
    return drafts


def compute_health_score(drafts: list[FindingDraft], *, tests_passed: bool | None) -> int:
    """0-100, a simple deterministic heuristic (not a "correctness" score):
    start at 100, subtract a per-finding penalty by severity, subtract 20
    more if the test suite doesn't pass. Clamped to [0, 100]."""
    score = 100
    for draft in drafts:
        score -= _SEVERITY_PENALTY[draft.severity]
    if tests_passed is False:
        score -= 20
    return max(0, min(100, score))


async def _noop_progress(_stage: str, _percent: int) -> None:
    return None


async def run_scan(
    session: AsyncSession, app: MonitoredApp, *, progress: ProgressCallback | None = None
) -> ScanSummary:
    """Run the full instant scan for `app` and upsert every finding.

    Caller commits (matches this project's other session_scope()-using
    write paths). `progress(stage, percent)` is awaited at each step so a
    caller can forward it to Socket.io for a live progress bar.
    """
    notify = progress or _noop_progress
    app_dir = _app_dir(app)
    drafts: list[FindingDraft] = []
    tests_passed: bool | None = None

    has_pyproject = (app_dir / "pyproject.toml").exists()
    has_requirements = (app_dir / "requirements.txt").exists()
    venv_python: Path | None = None

    await notify("installing dependencies", 10)
    if app.language == "python":
        venv_python = await _ensure_app_venv(app_dir)
        if has_pyproject or has_requirements:
            install_cmd = "-e ." if has_pyproject else "-r requirements.txt"
            await _run(
                f'"{venv_python}" -m pip install --quiet {install_cmd}',
                cwd=app_dir,
                timeout=INSTALL_TIMEOUT_SECONDS,
            )
    elif app.language == "javascript" and (app_dir / "package.json").exists():
        await _run("npm install", cwd=app_dir, timeout=INSTALL_TIMEOUT_SECONDS)

    await notify("running tests", 35)
    test_cmd = f'"{venv_python}" -m pytest -q' if venv_python else app.test_command
    test_outcome = await _run(test_cmd, cwd=app_dir)
    tests_passed = test_outcome.ok and not test_outcome.timed_out
    if not tests_passed:
        drafts.append(
            FindingDraft(
                category=FindingCategory.TEST,
                severity=FindingSeverity.HIGH,
                tool="test-runner",
                message=(
                    f"Test suite failed (`{app.test_command}`). Tail of output:\n"
                    f"{test_outcome.output[-2000:]}"
                ),
            )
        )

    if app.lint_command:
        await notify("running linter", 55)
        if venv_python:
            lint_outcome = await _run(
                f'"{venv_python}" -m ruff check --output-format=json .', cwd=app_dir
            )
            drafts.extend(parse_ruff_json(lint_outcome.output))
        else:
            await _run(app.lint_command, cwd=app_dir)

    if venv_python:
        await notify("running type checker", 70)
        type_outcome = await _run(
            f'"{venv_python}" -m mypy . --ignore-missing-imports --output json', cwd=app_dir
        )
        drafts.extend(parse_mypy_json(type_outcome.output))

    await notify("checking dependencies for vulnerabilities", 85)
    if venv_python and (has_requirements or has_pyproject):
        audit_target = "-r requirements.txt" if has_requirements else ""
        audit_outcome = await _run(
            f'"{venv_python}" -m pip_audit {audit_target} --format json',
            cwd=app_dir,
            timeout=INSTALL_TIMEOUT_SECONDS,
        )
        drafts.extend(parse_pip_audit_json(audit_outcome.output))
    elif app.language == "javascript" and (app_dir / "package.json").exists():
        audit_outcome = await _run("npm audit --json", cwd=app_dir, timeout=INSTALL_TIMEOUT_SECONDS)
        drafts.extend(parse_npm_audit_json(audit_outcome.output))

    await notify("saving results", 95)
    for draft in drafts:
        fingerprint = app_fingerprint(
            app.id, draft.tool, draft.file_path, draft.line_number, draft.message
        )
        values: dict[str, Any] = {
            "app_id": app.id,
            "fingerprint": fingerprint,
            "category": draft.category,
            "severity": draft.severity,
            "tool": draft.tool,
            "file_path": draft.file_path,
            "line_number": draft.line_number,
            "message": draft.message,
        }
        stmt = (
            pg_insert(Finding)
            .values(**values, occurrence_count=1)
            .on_conflict_do_update(
                index_elements=[Finding.app_id, Finding.fingerprint],
                set_={
                    **{k: v for k, v in values.items() if k not in ("app_id", "fingerprint")},
                    "occurrence_count": Finding.occurrence_count + 1,
                    "last_seen_at": func.now(),
                },
            )
        )
        await session.execute(stmt)

    health_score = compute_health_score(drafts, tests_passed=tests_passed)
    app.health_score = health_score
    app.last_scanned_at = datetime.now(UTC)

    findings_count_stmt = select(Finding).where(Finding.app_id == app.id)
    findings_count = len((await session.execute(findings_count_stmt)).scalars().all())

    await notify("done", 100)
    logger.info(
        "scanner.scan_complete",
        app_id=app.id,
        app_name=app.name,
        findings_count=len(drafts),
        health_score=health_score,
        tests_passed=tests_passed,
    )
    return ScanSummary(
        app_id=app.id,
        tests_passed=tests_passed,
        findings_count=findings_count,
        health_score=health_score,
    )

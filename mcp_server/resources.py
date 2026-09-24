"""MCP resources: errors://open, repo://tree, pipeline://runs/recent."""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import select

from core.db import session_scope
from core.models import Error, OpenResolvedStatus, PipelineRun
from mcp_server.instance import mcp
from mcp_server.sandbox import REPO_ROOT


@mcp.resource("errors://open")
async def errors_open() -> list[dict[str, Any]]:
    """Every currently-open captured error, most recently seen first."""
    async with session_scope() as session:
        stmt = (
            select(Error)
            .where(Error.status == OpenResolvedStatus.OPEN)
            .order_by(Error.last_seen_at.desc())
        )
        errors = (await session.execute(stmt)).scalars().all()
        return [
            {
                "id": e.id,
                "fingerprint": e.fingerprint,
                "exception_type": e.exception_type,
                "file_path": e.file_path,
                "line_number": e.line_number,
                "occurrence_count": e.occurrence_count,
                "last_seen_at": e.last_seen_at.isoformat(),
            }
            for e in errors
        ]


@mcp.resource("repo://tree")
async def repo_tree() -> list[str]:
    """Every git-tracked file, repo-relative (`git ls-files` — respects .gitignore)."""
    process = await asyncio.create_subprocess_exec(
        "git",
        "ls-files",
        cwd=str(REPO_ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await process.communicate()
    files = [line for line in stdout.decode(errors="replace").splitlines() if line]
    return sorted(files)


@mcp.resource("pipeline://runs/recent")
async def pipeline_runs_recent() -> list[dict[str, Any]]:
    """The most recent pipeline runs mirrored locally (from CI webhook events)."""
    async with session_scope() as session:
        stmt = select(PipelineRun).order_by(PipelineRun.created_at.desc()).limit(20)
        runs = (await session.execute(stmt)).scalars().all()
        return [
            {
                "run_id": r.run_id,
                "workflow": r.workflow,
                "branch": r.branch,
                "pr_number": r.pr_number,
                "sha": r.sha,
                "status": r.status,
                "conclusion": r.conclusion,
                "created_at": r.created_at.isoformat(),
            }
            for r in runs
        ]

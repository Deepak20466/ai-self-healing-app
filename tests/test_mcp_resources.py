"""mcp_server.resources: errors://open, repo://tree, pipeline://runs/recent."""

from __future__ import annotations

from core.db import session_scope
from core.models import PipelineRun
from mcp_server.resources import errors_open, pipeline_runs_recent, repo_tree


async def test_errors_open_returns_a_list() -> None:
    result = await errors_open()
    assert isinstance(result, list)


async def test_repo_tree_includes_known_tracked_files() -> None:
    tree = await repo_tree()
    assert "SPEC.md" in tree
    assert "apps/target_app/bugs.py" in tree
    assert ".env" not in tree  # gitignored, never tracked


async def test_pipeline_runs_recent_returns_seeded_run() -> None:
    async with session_scope() as session:
        session.add(
            PipelineRun(
                run_id=9_999_999,
                workflow="ci.yml",
                branch="main",
                sha="c" * 40,
                status="completed",
                conclusion="success",
            )
        )

    runs = await pipeline_runs_recent()
    run_ids = {r["run_id"] for r in runs}
    assert 9_999_999 in run_ids

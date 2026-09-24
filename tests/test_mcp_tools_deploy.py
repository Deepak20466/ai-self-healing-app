"""mcp_server.tools.deploy: get_deployment_status, get_health, get_metrics."""

from __future__ import annotations

from datetime import UTC, datetime

from core.db import session_scope
from core.models import Deployment
from mcp_server.tools.deploy import get_deployment_status, get_health, get_metrics


async def test_get_deployment_status_with_no_deployments() -> None:
    result = await get_deployment_status(env="an-env-that-has-never-deployed")
    assert result["deployment"] is None


async def test_get_deployment_status_returns_the_latest() -> None:
    async with session_scope() as session:
        session.add(
            Deployment(
                sha="a" * 40,
                env="test-env-deploy-status",
                status="healthy",
                started_at=datetime.now(UTC),
            )
        )

    result = await get_deployment_status(env="test-env-deploy-status")
    assert result["deployment"] is not None
    assert result["deployment"]["status"] == "healthy"
    assert result["deployment"]["sha"] == "a" * 40


async def test_get_health_reports_unreachable_pods_when_nothing_is_running() -> None:
    # In the test environment none of the real pod ports are listening.
    result = await get_health()
    assert result["healthy"] is False
    assert len(result["pods"]) == 4
    assert all(not p["reachable"] for p in result["pods"])


async def test_get_metrics_returns_a_full_summary_shape() -> None:
    result = await get_metrics()
    for key in (
        "mttr_minutes",
        "fix_success_rate",
        "ci_auto_fix_rate",
        "contract_violation_catches",
        "rollback_count",
        "cost_per_fix_usd",
        "daily_spend_usd",
        "daily_budget_usd",
        "open_anomalies",
        "errors_by_type",
    ):
        assert key in result

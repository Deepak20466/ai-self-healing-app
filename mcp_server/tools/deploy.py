"""Deploy tools: get_deployment_status, get_health, get_metrics."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from sqlalchemy import select

from core.config import settings
from core.db import session_scope
from core.metrics import get_metrics_summary
from core.models import Deployment
from mcp_server.audit import audited_tool
from mcp_server.instance import mcp

_HEALTHZ_TIMEOUT_SECONDS = 5.0


@audited_tool(mcp, "get_deployment_status")
async def get_deployment_status(env: str | None = None) -> dict[str, Any]:
    """Most recent deployment (optionally filtered to `env`), plus its status."""
    async with session_scope() as session:
        stmt = select(Deployment).order_by(Deployment.started_at.desc()).limit(1)
        if env:
            stmt = stmt.where(Deployment.env == env)
        deployment = (await session.execute(stmt)).scalar_one_or_none()

        if deployment is None:
            return {"deployment": None}

        return {
            "deployment": {
                "id": deployment.id,
                "sha": deployment.sha,
                "env": deployment.env,
                "status": deployment.status,
                "started_at": deployment.started_at.isoformat(),
                "finished_at": (
                    deployment.finished_at.isoformat() if deployment.finished_at else None
                ),
            }
        }


async def _probe_healthz(name: str, port: int) -> dict[str, Any]:
    url = f"http://localhost:{port}/healthz"
    try:
        async with httpx.AsyncClient(timeout=_HEALTHZ_TIMEOUT_SECONDS) as client:
            response = await client.get(url)
        return {"pod": name, "reachable": True, "status_code": response.status_code, "url": url}
    except httpx.HTTPError as exc:
        return {"pod": name, "reachable": False, "error": str(exc), "url": url}


@audited_tool(mcp, "get_health")
async def get_health() -> dict[str, Any]:
    """Live /healthz check against every pod that exposes one, probed concurrently."""
    checks = await asyncio.gather(
        _probe_healthz("app", settings.app_port),
        _probe_healthz("sentinel", settings.sentinel_port),
        _probe_healthz("mcp", settings.mcp_port),
        _probe_healthz("healer", settings.healer_port),
    )
    all_healthy = all(c["reachable"] and c.get("status_code") == 200 for c in checks)
    return {"healthy": all_healthy, "pods": checks}


@audited_tool(mcp, "get_metrics")
async def get_metrics() -> dict[str, Any]:
    """MTTR, success rate, cost per fix, and the rest of the metrics dashboard."""
    async with session_scope() as session:
        summary = await get_metrics_summary(
            session, daily_budget_usd=float(settings.daily_budget_usd)
        )
        return summary.model_dump(mode="json")

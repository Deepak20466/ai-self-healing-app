"""healer-pod FastAPI + Socket.io app (Phase 6): the autonomous fix worker
(background task, same as sentinel-pod's prober/anomaly loops) plus the
authenticated chat/dashboard/metrics UI's backend.

Run with: `uvicorn healer.app:asgi_app --port $HEALER_PORT`. `asgi_app` wraps
`app` (FastAPI) with `socketio.ASGIApp` so both HTTP routes and the
`/socket.io/` real-time chat/notification channel are served from one port —
matching SPEC.md's "4 pods" (not 5), since Phase 4/5 already put the worker
loop and this phase's chat server in the same `healer/` pod.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import socketio
import structlog
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.db import dispose_engine, get_db, session_scope
from core.logging import configure_logging
from core.models import ChatMessage, ChatSession, Finding, FindingStatus, MonitoredApp
from core.ratelimit import check_and_consume
from core.repo_connect import RepoConnectError, connect_repo
from core.scanner import run_scan
from healer import notifier
from healer.auth import (
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE_SECONDS,
    AuthError,
    authenticate,
    client_ip,
    require_auth,
    verify_socketio_token,
)
from healer.chat_agent import as_tool_list, handle_chat_message
from healer.findings_actions import maybe_auto_fix_high_severity, request_fix_for_finding
from healer.job_progress import recent_job_progress, run_progress_broadcaster
from healer.mcp_client import MCPToolClient, ReconnectingMCPToolClient, connect_http
from healer.onboarding import open_onboarding_pull_request
from healer.worker import run_worker
from mcp_server.github_client import GitHubClientError
from mcp_server.sandbox import REPO_ROOT

configure_logging(settings.log_level)
logger = structlog.get_logger(__name__)

WEB_DIR = REPO_ROOT / "web"

sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins=[])

# Populated at lifespan startup with a connected MCPToolClient shared by the
# chat handler and dashboard REST endpoints (one long-lived streamable-HTTP
# connection to mcp-pod, same pattern as healer/worker.py's own client).
_mcp_client: MCPToolClient | None = None


def get_mcp_client() -> MCPToolClient:
    if _mcp_client is None:
        raise HTTPException(status_code=503, detail="mcp-pod not connected yet")
    return _mcp_client


async def _socket_broadcast(event: str, payload: dict[str, Any]) -> None:
    await sio.emit(event, payload)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _mcp_client
    notifier.set_socket_broadcaster(_socket_broadcast)
    mcp_url = f"http://127.0.0.1:{settings.mcp_port}/mcp"
    worker_task: asyncio.Task[None] | None = None
    mcp = ReconnectingMCPToolClient(lambda: connect_http(mcp_url))
    try:
        await mcp.start()
    except Exception:
        logger.warning("healer_app.mcp_initial_connect_failed", mcp_url=mcp_url)
    _mcp_client = mcp
    worker_task = asyncio.create_task(run_worker())
    progress_task = asyncio.create_task(run_progress_broadcaster(_socket_broadcast))
    logger.info("healer_pod_started", mcp_url=mcp_url)
    try:
        yield
    finally:
        progress_task.cancel()
        if worker_task is not None:
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("healer_app.worker_task_crashed")
        notifier.set_socket_broadcaster(None)
        _mcp_client = None
        await mcp.aclose()
        await dispose_engine()


app = FastAPI(title="healer-pod", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


# --- Auth --------------------------------------------------------------------


class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/api/auth/login")
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    ip = client_ip(request)
    limited = await check_and_consume(
        session, key=ip, bucket="login", capacity=10, refill_per_second=10 / 60
    )
    await session.commit()
    if not limited.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many attempts"
        )
    try:
        token = await authenticate(
            session, ip_address=ip, username=body.username, password=body.password
        )
    except AuthError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        secure=settings.environment != "development",
        samesite="strict",
    )
    return {"status": "ok"}


@app.post("/api/auth/logout")
async def logout(response: Response) -> dict[str, str]:
    response.delete_cookie(SESSION_COOKIE_NAME)
    return {"status": "ok"}


@app.get("/api/auth/session")
async def session_info(username: str = Depends(require_auth)) -> dict[str, str]:
    return {"username": username}


# --- Dashboard REST API (read-only, auth required) ---------------------------
# Queries the DB directly rather than round-tripping through MCP for every
# dashboard poll — the MCP tools exist for the *AI agent's* sandboxed access;
# the dashboard is server-side code that already has a normal DB session, the
# same trust boundary target_app/sentinel's own routes use.


@app.get("/api/errors")
async def api_errors(
    username: str = Depends(require_auth), mcp: MCPToolClient = Depends(get_mcp_client)
) -> list[dict[str, Any]]:
    result = await mcp.call_tool("list_open_errors", {"limit": 50})
    return as_tool_list(result)


@app.get("/api/jobs")
async def api_jobs(username: str = Depends(require_auth)) -> list[dict[str, Any]]:
    return await recent_job_progress()


@app.get("/api/metrics")
async def api_metrics(
    username: str = Depends(require_auth), mcp: MCPToolClient = Depends(get_mcp_client)
) -> dict[str, Any]:
    result = await mcp.call_tool("get_metrics", {})
    return dict(result)


@app.get("/api/health")
async def api_health(
    username: str = Depends(require_auth), mcp: MCPToolClient = Depends(get_mcp_client)
) -> dict[str, Any]:
    result = await mcp.call_tool("get_health", {})
    return dict(result)


@app.get("/api/deployments")
async def api_deployments(
    username: str = Depends(require_auth), mcp: MCPToolClient = Depends(get_mcp_client)
) -> dict[str, Any]:
    result = await mcp.call_tool("get_deployment_status", {})
    return dict(result)


@app.get("/api/pipeline")
async def api_pipeline(
    username: str = Depends(require_auth), mcp: MCPToolClient = Depends(get_mcp_client)
) -> list[dict[str, Any]]:
    result = await mcp.call_tool("list_workflow_runs", {})
    return as_tool_list(result)


# --- Connect a repo / health reports (Depends(get_db) for the write paths;
# scans run as a background task so the request returns immediately and
# progress streams over Socket.io) --------------------------------------------


def _app_summary(app_row: MonitoredApp, *, open_findings: int) -> dict[str, Any]:
    return {
        "id": app_row.id,
        "name": app_row.name,
        "language": app_row.language,
        "github_repo": app_row.github_repo,
        "connected": app_row.repo_url is not None,
        "auto_fix_high_severity": app_row.auto_fix_high_severity,
        "health_score": app_row.health_score,
        "last_scanned_at": app_row.last_scanned_at.isoformat() if app_row.last_scanned_at else None,
        "open_findings": open_findings,
    }


def _finding_summary(finding: Finding) -> dict[str, Any]:
    return {
        "id": finding.id,
        "category": finding.category.value,
        "severity": finding.severity.value,
        "tool": finding.tool,
        "file_path": finding.file_path,
        "line_number": finding.line_number,
        "message": finding.message,
        "status": finding.status.value,
        "heal_job_id": finding.heal_job_id,
        "occurrence_count": finding.occurrence_count,
    }


async def _open_findings_count(db: AsyncSession, app_id: int) -> int:
    stmt = select(Finding).where(Finding.app_id == app_id, Finding.status == FindingStatus.OPEN)
    return len((await db.execute(stmt)).scalars().all())


async def _run_scan_and_notify(app_id: int) -> None:
    """Background task: run the scan, push progress over Socket.io, apply
    the auto-fix-high-severity toggle, and notify on completion/failure.
    Never lets an exception here take down the healer process (same
    "one job's failure must not kill the whole pod" principle as
    `healer/worker.py`'s job-level try/except)."""

    async def _progress(stage: str, percent: int) -> None:
        await sio.emit("scan_progress", {"app_id": app_id, "stage": stage, "percent": percent})

    try:
        async with session_scope() as session:
            app_row = await session.get(MonitoredApp, app_id)
            if app_row is None:
                return
            await run_scan(session, app_row, progress=_progress)
            findings = (
                (await session.execute(select(Finding).where(Finding.app_id == app_id)))
                .scalars()
                .all()
            )
            await maybe_auto_fix_high_severity(session, app_row, list(findings))
        await notifier.notify("scan_complete", f"Scan finished for app #{app_id}")
    except Exception:
        logger.exception("healer_app.scan_failed", app_id=app_id)
        await sio.emit("scan_progress", {"app_id": app_id, "stage": "failed", "percent": 100})


class ConnectAppRequest(BaseModel):
    repo_url: str
    name: str | None = None


@app.post("/api/apps")
async def connect_app(
    body: ConnectAppRequest,
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    if not settings.github_token:
        raise HTTPException(status_code=503, detail="GITHUB_TOKEN is not configured")
    try:
        app_row = await connect_repo(
            db, repo_url=body.repo_url, name=body.name, github_token=settings.github_token
        )
        await db.commit()
    except RepoConnectError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    app_id = app_row.id
    asyncio.create_task(_run_scan_and_notify(app_id))
    return _app_summary(app_row, open_findings=0) | {"status": "scanning"}


@app.get("/api/apps")
async def list_apps(
    username: str = Depends(require_auth), db: AsyncSession = Depends(get_db)
) -> list[dict[str, Any]]:
    rows = (await db.execute(select(MonitoredApp))).scalars().all()
    return [_app_summary(row, open_findings=await _open_findings_count(db, row.id)) for row in rows]


@app.get("/api/apps/{app_id}")
async def get_app_detail(
    app_id: int, username: str = Depends(require_auth), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    app_row = await db.get(MonitoredApp, app_id)
    if app_row is None:
        raise HTTPException(status_code=404, detail="No such app")
    severity_rank = case(
        (Finding.severity == "critical", 3),
        (Finding.severity == "high", 2),
        (Finding.severity == "medium", 1),
        else_=0,
    )
    findings = (
        (
            await db.execute(
                select(Finding).where(Finding.app_id == app_id).order_by(severity_rank.desc())
            )
        )
        .scalars()
        .all()
    )
    open_count = sum(1 for f in findings if f.status.value == "open")
    summary = _app_summary(app_row, open_findings=open_count)
    summary["findings"] = [_finding_summary(f) for f in findings]
    return summary


@app.post("/api/apps/{app_id}/scan")
async def rescan_app(
    app_id: int, username: str = Depends(require_auth), db: AsyncSession = Depends(get_db)
) -> dict[str, str]:
    app_row = await db.get(MonitoredApp, app_id)
    if app_row is None:
        raise HTTPException(status_code=404, detail="No such app")
    asyncio.create_task(_run_scan_and_notify(app_id))
    return {"status": "scanning"}


class UpdateAppRequest(BaseModel):
    auto_fix_high_severity: bool | None = None


@app.patch("/api/apps/{app_id}")
async def update_app(
    app_id: int,
    body: UpdateAppRequest,
    username: str = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    app_row = await db.get(MonitoredApp, app_id)
    if app_row is None:
        raise HTTPException(status_code=404, detail="No such app")
    if body.auto_fix_high_severity is not None:
        app_row.auto_fix_high_severity = body.auto_fix_high_severity
    await db.commit()
    return _app_summary(app_row, open_findings=await _open_findings_count(db, app_id))


@app.post("/api/findings/{finding_id}/fix")
async def fix_finding(
    finding_id: int, username: str = Depends(require_auth), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    finding = await db.get(Finding, finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="No such finding")
    app_row = await db.get(MonitoredApp, finding.app_id)
    if app_row is None:
        raise HTTPException(status_code=404, detail="No such app")
    job = await request_fix_for_finding(db, finding, app_row)
    await db.commit()
    await notifier.notify(
        "fix_requested", f"Fix requested for finding #{finding_id} (heal_job #{job.id})"
    )
    return {"heal_job_id": job.id, "status": "queued"}


@app.post("/api/apps/{app_id}/onboard-pr")
async def onboard_app(
    app_id: int, username: str = Depends(require_auth), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    app_row = await db.get(MonitoredApp, app_id)
    if app_row is None:
        raise HTTPException(status_code=404, detail="No such app")
    try:
        pr = await open_onboarding_pull_request(app_row)
    except GitHubClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"pr_number": pr["number"], "pr_url": pr.get("html_url")}


@app.get("/api/chat/history")
async def chat_history(
    session_id: int, username: str = Depends(require_auth), db: AsyncSession = Depends(get_db)
) -> list[dict[str, Any]]:
    stmt = (
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at.asc())
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {"role": r.role, "content": r.content, "created_at": r.created_at.isoformat()} for r in rows
    ]


@app.post("/api/chat/session")
async def new_chat_session(
    username: str = Depends(require_auth), db: AsyncSession = Depends(get_db)
) -> dict[str, int]:
    chat_session = ChatSession(user_label=username)
    db.add(chat_session)
    await db.flush()
    session_id = chat_session.id
    await db.commit()
    return {"session_id": session_id}


# --- Static UI (no build step: React 18 + htm from CDN) -----------------------

if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(str(WEB_DIR / "index.html"))


# --- Socket.io: authenticated real-time chat + notifications ------------------


def _cookie_from_environ(environ: dict[str, Any]) -> str | None:
    raw = str(environ.get("HTTP_COOKIE", ""))
    for part in raw.split(";"):
        part = part.strip()
        if part.startswith(f"{SESSION_COOKIE_NAME}="):
            return part.split("=", 1)[1]
    return None


@sio.event  # type: ignore[untyped-decorator]
async def connect(sid: str, environ: dict[str, Any], auth: dict[str, Any] | None) -> bool:
    token = (auth or {}).get("token") or _cookie_from_environ(environ)
    try:
        username = verify_socketio_token(token)
    except AuthError:
        return False
    await sio.save_session(sid, {"username": username})
    return True


@sio.event  # type: ignore[untyped-decorator]
async def chat_message(sid: str, data: dict[str, Any]) -> None:
    socket_session = await sio.get_session(sid)
    if not socket_session:
        return
    chat_session_id = int(data.get("session_id", 0))
    text = str(data.get("text", ""))
    if not text or _mcp_client is None:
        return

    from core.db import session_scope

    async with session_scope() as db:
        db.add(ChatMessage(session_id=chat_session_id, role="user", content=text))

    reply = await handle_chat_message(_mcp_client, chat_session_id=chat_session_id, text=text)

    async with session_scope() as db:
        db.add(ChatMessage(session_id=chat_session_id, role="assistant", content=reply.text))

    await sio.emit(
        "chat_reply",
        {"session_id": chat_session_id, "text": reply.text, "tool_calls": reply.tool_calls},
        to=sid,
    )


asgi_app = socketio.ASGIApp(sio, other_asgi_app=app)

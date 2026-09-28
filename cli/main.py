"""`selfheal`: the terminal-only CLI over healer-pod's JSON/Socket.io API.

Every command is a thin async wrapper: resolve a saved session (or fail with
a friendly "run selfheal login"/"run selfheal up"), call the API, print a
Rich table or `--json`. Destructive actions (fix, rollback via chat) always
confirm first.
"""

from __future__ import annotations

import asyncio
import getpass
import json as jsonlib
from collections.abc import Callable, Coroutine
from typing import Annotated, Any, TypeVar

import typer
from rich.console import Console
from rich.table import Table

from cli.client import CliError, HealerClient
from cli.config import clear_session, load_session, save_session

app = typer.Typer(
    name="selfheal",
    help="Terminal client for the AI-powered self-healing system.",
    no_args_is_help=True,
)
apps_app = typer.Typer(help="Manage connected apps.", invoke_without_command=True)
app.add_typer(apps_app, name="apps")


@apps_app.callback()
def apps_callback(ctx: typer.Context) -> None:
    """`selfheal apps` alone lists connected apps; see subcommands for more."""
    if ctx.invoked_subcommand is None:
        apps_list()


console = Console()
error_console = Console(stderr=True)

T = TypeVar("T")


def _run(coro: Coroutine[Any, Any, T]) -> T:
    try:
        return asyncio.run(coro)
    except CliError as exc:
        error_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    except KeyboardInterrupt:
        raise typer.Exit(code=130) from None


def _default_base_url() -> str:
    session = load_session()
    if session is not None:
        return session.base_url
    from core.config import settings

    return f"http://127.0.0.1:{settings.healer_port}"


@app.command()
def up(
    public: Annotated[
        bool, typer.Option("--public", help="Also tunnel the sentinel webhook publicly.")
    ] = False,
) -> None:
    """Start all 4 pods (each bound to 127.0.0.1 only)."""

    async def _up() -> None:
        from cli import pods

        console.print("[cyan]Starting pods...[/cyan]")
        results = await pods.start_pods()
        for name, state in results.items():
            console.print(f"  {name}: {state}")
        if not all([await pods.pod_is_up(n) for n in pods.PODS]):
            error_console.print("[red]Some pods did not become healthy within 60s.[/red]")
            raise typer.Exit(code=1)
        console.print("[green]All pods up.[/green]")
        if public:
            console.print("[cyan]Starting Cloudflare tunnel for the sentinel webhook...[/cyan]")
            url = pods.start_webhook_tunnel()
            if url is None:
                error_console.print(
                    "[yellow]Could not start the tunnel (is `cloudflared` installed?). "
                    "Pods are still up locally.[/yellow]"
                )
            else:
                console.print(f"[green]Sentinel webhook is public at:[/green] {url}/webhooks/ci")
                console.print(
                    "[dim]Set this as the HEALER_WEBHOOK_URL GitHub repo variable "
                    "yourself, e.g.: gh variable set HEALER_WEBHOOK_URL "
                    f'--body "{url}/webhooks/ci"[/dim]'
                )

    _run(_up())


@app.command()
def down() -> None:
    """Stop every pod (and tunnel) this CLI started."""
    from cli import pods

    stopped = pods.stop_pods()
    if stopped:
        console.print(f"[green]Stopped:[/green] {', '.join(stopped)}")
    else:
        console.print("Nothing was running (that this CLI started).")


@app.command()
def status(json_out: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Show pod health and login state."""

    async def _status() -> dict[str, Any]:
        from cli import pods

        pod_status = {}
        for name in pods.PODS:
            pod_status[name] = "up" if await pods.pod_is_up(name) else "down"
        session = load_session()
        logged_in = False
        backends: dict[str, Any] = {}
        if session is not None:
            try:
                async with HealerClient(session) as client:
                    await client.get("/api/auth/session")
                    logged_in = True
                    backends = await client.get("/api/backends")
            except CliError:
                logged_in = False
        return {"pods": pod_status, "logged_in": logged_in, "backends": backends}

    result = _run(_status())
    if json_out:
        console.print_json(data=result)
        return
    table = Table(title="selfheal status")
    table.add_column("Pod")
    table.add_column("State")
    for name, state in result["pods"].items():
        color = "green" if state == "up" else "red"
        table.add_row(name, f"[{color}]{state}[/{color}]")
    console.print(table)
    login_color = "green" if result["logged_in"] else "yellow"
    login_text = "logged in" if result["logged_in"] else "not logged in (run selfheal login)"
    console.print(f"Auth: [{login_color}]{login_text}[/{login_color}]")

    def _render_chain(title: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        chain_table = Table(title=title)
        chain_table.add_column("Backend")
        chain_table.add_column("State")
        for row in rows:
            if not row.get("has_key"):
                state, color = "no key", "dim"
            elif row.get("cooling_down_until"):
                state, color = f"cooling down until {row['cooling_down_until']}", "yellow"
            else:
                state, color = "active", "green"
            chain_table.add_row(row["name"], f"[{color}]{state}[/{color}]")
        console.print(chain_table)

    if result.get("backends"):
        _render_chain("AI fix chain", result["backends"].get("fix_chain", []))
        _render_chain("AI chat chain", result["backends"].get("chat_chain", []))


@app.command()
def login(
    url: Annotated[str | None, typer.Option(help="Base URL of the healer API.")] = None,
    username: str = "admin",
) -> None:
    """Log in with the admin password (hidden prompt). Stores a session
    token in your user config dir -- never in this repo."""
    base_url = url or _default_base_url()
    password = getpass.getpass("Password: ")

    async def _login() -> None:
        import httpx

        try:
            async with httpx.AsyncClient(base_url=base_url, timeout=15.0) as client:
                resp = await client.post(
                    "/api/auth/login", json={"username": username, "password": password}
                )
        except httpx.ConnectError as exc:
            raise CliError("Can't reach the healer API. Run `selfheal up` first.") from exc
        if resp.status_code != 200:
            detail = resp.text
            try:
                detail = resp.json().get("detail", detail)
            except Exception:
                pass
            raise CliError(f"Login failed: {detail}")
        from healer.auth import SESSION_COOKIE_NAME

        token = resp.cookies.get(SESSION_COOKIE_NAME)
        if not token:
            raise CliError("Login succeeded but no session cookie was returned.")
        save_session(base_url=base_url, token=token)
        console.print("[green]Logged in.[/green]")

    _run(_login())


@app.command()
def logout() -> None:
    """Forget the stored session token."""
    session = load_session()
    if session is not None:

        async def _logout() -> None:
            try:
                async with HealerClient(session) as client:
                    await client.post("/api/auth/logout")
            except CliError:
                pass

        _run(_logout())
    clear_session()
    console.print("Logged out.")


def _print_or_json(data: Any, json_out: bool, render: Callable[[Any], None]) -> None:
    if json_out:
        console.print(jsonlib.dumps(data, default=str, indent=2))
    else:
        render(data)


@app.command()
def connect(
    github_url: str,
    name: Annotated[str | None, typer.Option(help="Override the auto-detected app name.")] = None,
) -> None:
    """Connect a GitHub repo for scanning + AI fixes."""

    async def _connect() -> dict[str, Any]:
        async with HealerClient.from_saved_session() as client:
            body: dict[str, Any] = {"repo_url": github_url}
            if name:
                body["name"] = name
            result: dict[str, Any] = await client.post("/api/apps", json=body)
            return result

    result = _run(_connect())
    console.print(f"[green]Connected:[/green] {result.get('name')} (id={result.get('id')})")
    console.print("Scanning in the background -- `selfheal scan <app>` to see the report.")


@apps_app.command("list")
def apps_list(json_out: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List connected apps."""

    async def _list() -> list[dict[str, Any]]:
        async with HealerClient.from_saved_session() as client:
            result: list[dict[str, Any]] = await client.get("/api/apps")
            return result

    rows = _run(_list())

    def render(data: list[dict[str, Any]]) -> None:
        table = Table(title="Connected apps")
        table.add_column("ID")
        table.add_column("Name")
        table.add_column("Language")
        table.add_column("Health")
        table.add_column("Open findings")
        for row in data:
            table.add_row(
                str(row.get("id")),
                str(row.get("name")),
                str(row.get("language")),
                str(row.get("health_score")),
                str(row.get("open_findings")),
            )
        console.print(table)

    _print_or_json(rows, json_out, render)


@apps_app.command("set")
def apps_set(
    app_name: str,
    auto_merge: Annotated[
        str | None,
        typer.Option("--auto-merge", help="on|off -- merge this app's PRs once CI+tests pass."),
    ] = None,
) -> None:
    """Change a connected app's settings, e.g. `selfheal apps set node_app --auto-merge on`."""
    if auto_merge is None:
        error_console.print("[red]Nothing to set. Pass --auto-merge on|off.[/red]")
        raise typer.Exit(code=1)
    if auto_merge not in ("on", "off"):
        error_console.print("[red]--auto-merge must be 'on' or 'off'.[/red]")
        raise typer.Exit(code=1)

    async def _set() -> dict[str, Any]:
        async with HealerClient.from_saved_session() as client:
            app_id = await _resolve_app_id(client, app_name)
            result: dict[str, Any] = await client.patch(
                f"/api/apps/{app_id}", json={"auto_merge": auto_merge == "on"}
            )
            return result

    result = _run(_set())
    console.print(f"auto_merge for '{app_name}': [green]{result.get('auto_merge')}[/green]")


async def _resolve_app_id(client: HealerClient, name_or_id: str) -> int:
    if name_or_id.isdigit():
        return int(name_or_id)
    result: list[dict[str, Any]] = await client.get("/api/apps")
    for row in result:
        if row.get("name") == name_or_id:
            app_id = row.get("id")
            assert isinstance(app_id, int)
            return app_id
    raise CliError(f"No connected app named '{name_or_id}'. Run `selfheal apps` to list them.")


@app.command()
def scan(app_name: str, json_out: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Trigger a scan for an app and print its health report."""

    async def _scan() -> dict[str, Any]:
        async with HealerClient.from_saved_session() as client:
            app_id = await _resolve_app_id(client, app_name)
            await client.post(f"/api/apps/{app_id}/scan")
            console.print("Scanning...")
            for _ in range(120):
                detail: dict[str, Any] = await client.get(f"/api/apps/{app_id}")
                if detail.get("last_scanned_at"):
                    return detail
                await asyncio.sleep(2)
            raise CliError("Scan did not finish within 4 minutes.")

    detail = _run(_scan())

    def render(data: dict[str, Any]) -> None:
        console.print(f"[bold]{data.get('name')}[/bold] — health score: {data.get('health_score')}")
        table = Table(title="Findings")
        table.add_column("ID")
        table.add_column("Severity")
        table.add_column("File:Line")
        table.add_column("Message")
        for f in data.get("findings", []):
            table.add_row(
                str(f.get("id")),
                str(f.get("severity")),
                f"{f.get('file_path')}:{f.get('line_number')}",
                str(f.get("message"))[:80],
            )
        console.print(table)

    _print_or_json(detail, json_out, render)


@app.command()
def fix(
    app_name: str,
    finding_id: Annotated[int | None, typer.Argument()] = None,
    high: Annotated[bool, typer.Option("--high", help="Fix every high-severity finding.")] = False,
    auto_merge: Annotated[
        bool, typer.Option("--auto-merge", help="Auto-merge this fix only, if CI+tests pass.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation prompt.")] = False,
) -> None:
    """Fix one finding, or every high-severity finding with --high. Spends AI budget."""
    if finding_id is None and not high:
        error_console.print("[red]Give a finding id, or pass --high.[/red]")
        raise typer.Exit(code=1)

    async def _run_fix() -> list[dict[str, Any]]:
        async with HealerClient.from_saved_session() as client:
            app_id = await _resolve_app_id(client, app_name)
            detail: dict[str, Any] = await client.get(f"/api/apps/{app_id}")
            findings = detail.get("findings", [])
            if finding_id is not None:
                findings = [f for f in findings if f.get("id") == finding_id]
                if not findings:
                    raise CliError(f"No finding {finding_id} on app '{app_name}'.")
            else:
                findings = [f for f in findings if f.get("severity") in ("high", "critical")]
            if not findings:
                console.print("No matching open findings.")
                return []
            if not yes:
                names = ", ".join(str(f.get("id")) for f in findings)
                if not typer.confirm(
                    f"This will spend AI budget fixing finding(s) {names}"
                    + (" with auto-merge enabled" if auto_merge else "")
                    + ". Continue?"
                ):
                    raise typer.Exit(code=0)
            results = []
            for f in findings:
                body = {"auto_merge": True} if auto_merge else None
                result: dict[str, Any] = await client.post(
                    f"/api/findings/{f['id']}/fix", json=body
                )
                results.append(result)
            return results

    results = _run(_run_fix())
    for r in results:
        console.print(f"Enqueued heal_job {r.get('heal_job_id')} for finding {r.get('finding_id')}")


@app.command()
def watch() -> None:
    """Live heal-job progress (Detected -> Analyzing -> Patch -> Tests -> PR)."""

    async def _watch() -> None:
        import socketio

        session = load_session()
        if session is None:
            raise CliError("Not logged in (or your session expired). Run `selfheal login`.")
        sio = socketio.AsyncClient()

        @sio.event  # type: ignore[untyped-decorator]
        async def job_progress(data: dict[str, Any]) -> None:
            console.print(
                f"[cyan]job {data.get('id')}[/cyan] "
                f"[bold]{data.get('stage')}[/bold] ({data.get('percent', 0)}%)"
            )

        @sio.event  # type: ignore[untyped-decorator]
        async def notification(data: dict[str, Any]) -> None:
            console.print(f"[yellow]notice:[/yellow] {data.get('message', data)}")

        try:
            await sio.connect(
                session.base_url,
                auth={"token": session.token},
                transports=["websocket"],
            )
        except Exception as exc:
            raise CliError(f"Could not connect to {session.base_url}: {exc}") from exc
        console.print("Watching for heal-job progress and notifications. Ctrl+C to stop.")
        try:
            await sio.wait()
        except KeyboardInterrupt:
            pass
        finally:
            await sio.disconnect()

    _run(_watch())


@app.command()
def errors(json_out: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List open errors."""

    async def _errors() -> list[dict[str, Any]]:
        async with HealerClient.from_saved_session() as client:
            result: list[dict[str, Any]] = await client.get("/api/errors")
            return result

    rows = _run(_errors())

    def render(data: list[dict[str, Any]]) -> None:
        table = Table(title="Open errors")
        table.add_column("ID")
        table.add_column("Type")
        table.add_column("File:Line")
        table.add_column("Occurrences")
        for row in data:
            table.add_row(
                str(row.get("id")),
                str(row.get("exception_type")),
                f"{row.get('file_path')}:{row.get('line_number')}",
                str(row.get("occurrence_count")),
            )
        console.print(table)

    _print_or_json(rows, json_out, render)


@app.command()
def prs(json_out: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List recent heal-job PRs."""

    async def _prs() -> list[dict[str, Any]]:
        async with HealerClient.from_saved_session() as client:
            result: list[dict[str, Any]] = await client.get("/api/prs")
            return result

    rows = _run(_prs())

    def render(data: list[dict[str, Any]]) -> None:
        table = Table(title="Heal-job PRs")
        table.add_column("Heal job")
        table.add_column("PR #")
        table.add_column("Status")
        table.add_column("Opened at")
        for row in data:
            table.add_row(
                str(row.get("heal_job_id")),
                str(row.get("pr_number")),
                str(row.get("status")),
                str(row.get("pr_opened_at")),
            )
        console.print(table)

    _print_or_json(rows, json_out, render)


@app.command()
def metrics(json_out: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Show MTTR, fix success rate, cost per fix, and other headline numbers."""

    async def _metrics() -> dict[str, Any]:
        async with HealerClient.from_saved_session() as client:
            result: dict[str, Any] = await client.get("/api/metrics")
            return result

    data = _run(_metrics())

    def render(d: dict[str, Any]) -> None:
        table = Table(title="Metrics")
        table.add_column("Metric")
        table.add_column("Value")
        for key, value in d.items():
            table.add_row(str(key), str(value))
        console.print(table)

    _print_or_json(data, json_out, render)


@app.command()
def capture(app_name: str) -> None:
    """Send a synthetic test error through the capture pipeline for `app_name`."""

    async def _capture() -> dict[str, Any]:
        async with HealerClient.from_saved_session() as client:
            app_id = await _resolve_app_id(client, app_name)
            result: dict[str, Any] = await client.post(f"/api/apps/{app_id}/capture-test")
            return result

    result = _run(_capture())
    console.print(
        f"[green]Captured.[/green] error_id={result.get('error_id')} "
        f"fingerprint={result.get('fingerprint')}"
    )


@app.command()
def chat() -> None:
    """Interactive AI chat. Destructive actions require typing "yes"."""

    async def _chat() -> None:
        async with HealerClient.from_saved_session() as client:
            session_resp: dict[str, Any] = await client.post("/api/chat/session")
            session_id = session_resp["session_id"]

        import socketio

        session = load_session()
        assert session is not None
        sio = socketio.AsyncClient()
        got_reply: asyncio.Event = asyncio.Event()
        last_reply: dict[str, Any] = {}

        @sio.event  # type: ignore[untyped-decorator]
        async def chat_reply(data: dict[str, Any]) -> None:
            nonlocal last_reply
            last_reply = data
            got_reply.set()

        try:
            await sio.connect(
                session.base_url, auth={"token": session.token}, transports=["websocket"]
            )
        except Exception as exc:
            raise CliError(f"Could not connect to {session.base_url}: {exc}") from exc

        console.print("[cyan]selfheal chat[/cyan] — type a message, or 'exit' to quit.")
        try:
            while True:
                try:
                    text = console.input("[bold]> [/bold]")
                except EOFError:
                    break
                if text.strip().lower() in ("exit", "quit"):
                    break
                if not text.strip():
                    continue
                got_reply.clear()
                await sio.emit("chat_message", {"session_id": session_id, "text": text})
                try:
                    await asyncio.wait_for(got_reply.wait(), timeout=120)
                except TimeoutError:
                    console.print("[yellow](no reply within 120s)[/yellow]")
                    continue
                console.print(last_reply.get("text", ""))
        finally:
            await sio.disconnect()

    _run(_chat())


@app.command()
def deploy() -> None:
    """Deploy this project (local_deploy.py). Connected repos: not supported yet."""
    from mcp_server.sandbox import REPO_ROOT

    script = REPO_ROOT / "scripts" / "local_deploy.py"
    if not script.exists():
        console.print("not supported yet (Roadmap)")
        raise typer.Exit(code=1)
    import subprocess
    import sys as _sys

    venv_py = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    result = subprocess.run(  # noqa: S603
        [str(venv_py) if venv_py.exists() else _sys.executable, str(script)],
        cwd=REPO_ROOT,
        check=False,
    )
    raise typer.Exit(code=result.returncode)


def main() -> None:
    app()


if __name__ == "__main__":
    main()

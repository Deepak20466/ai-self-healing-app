"""One-click "auto-onboarding PR": adds a small, dependency-free error-
reporting snippet to a connected app's own repo, wired to this system's
`/ingest/error` endpoint with that app's `ingest_token`.

Deliberately not an AI-agent task (no LLM call, no worktree fix-loop): the
integration snippet is a fixed template picked by the app's detected
language, so this is fast, free, and 100% deterministic -- reliably wiring
a *specific* framework's exception hook for an unknown, arbitrary repo is
not something a template can guarantee, so the PR body says exactly where
to call `report_error()` instead of guessing at auto-wiring.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.config import settings
from core.models import MonitoredApp
from mcp_server.github_client import GitHubClient

ONBOARDING_BRANCH_PREFIX = "selfheal-onboarding"


def _ingest_url(app: MonitoredApp) -> str:
    base = settings.public_url or "http://localhost:8002"
    return f"{base.rstrip('/')}/ingest/error"


_PYTHON_SNIPPET = '''"""Error reporting for the AI self-healing system.

Call `report_error(exc)` from an except block (or a framework-level error
handler) to send an unhandled exception here for automatic detection and
AI-assisted fixing. Never raises itself -- a reporting failure must never
break the app it's monitoring.
"""

from __future__ import annotations

import json
import traceback
import urllib.request

INGEST_URL = "{ingest_url}"
INGEST_TOKEN = "{ingest_token}"


def report_error(exc: BaseException) -> None:
    try:
        payload = {{
            "exception_type": type(exc).__name__,
            "message": str(exc),
            "traceback": "".join(traceback.format_exception(exc)),
        }}
        request = urllib.request.Request(
            INGEST_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={{
                "Content-Type": "application/json",
                "Authorization": f"Bearer {{INGEST_TOKEN}}",
            }},
            method="POST",
        )
        urllib.request.urlopen(request, timeout=2)
    except Exception:
        pass
'''

_NODE_SNIPPET = """// Error reporting for the AI self-healing system.
//
// Call reportError(err) from a catch block (or a framework-level error
// handler) to send an unhandled exception here for automatic detection and
// AI-assisted fixing. Never throws itself -- a reporting failure must never
// break the app it's monitoring.

const INGEST_URL = "{ingest_url}";
const INGEST_TOKEN = "{ingest_token}";

async function reportError(err) {{
  try {{
    await fetch(INGEST_URL, {{
      method: "POST",
      headers: {{
        "Content-Type": "application/json",
        Authorization: `Bearer ${{INGEST_TOKEN}}`,
      }},
      body: JSON.stringify({{
        exception_type: err && err.name ? err.name : "Error",
        message: err && err.message ? err.message : String(err),
        traceback: err && err.stack ? err.stack : "",
      }}),
    }});
  }} catch (_) {{
    // never let monitoring break the app it's monitoring
  }}
}}

module.exports = {{ reportError }};
"""


@dataclass(frozen=True)
class OnboardingFile:
    path: str
    content: str


def build_onboarding_file(app: MonitoredApp) -> OnboardingFile:
    ingest_url = _ingest_url(app)
    if app.language == "javascript":
        content = _NODE_SNIPPET.format(ingest_url=ingest_url, ingest_token=app.ingest_token)
        return OnboardingFile(path="selfheal_error_reporter.js", content=content)
    # Default to the Python template for "python" and "unknown" -- a
    # dependency-free urllib snippet works wherever Python runs.
    content = _PYTHON_SNIPPET.format(ingest_url=ingest_url, ingest_token=app.ingest_token)
    return OnboardingFile(path="selfheal_error_reporter.py", content=content)


def build_onboarding_pr_body(app: MonitoredApp, onboarding_file: OnboardingFile) -> str:
    call_example = "report_error(exc)" if app.language != "javascript" else "reportError(err)"
    return (
        f"Adds `{onboarding_file.path}`, a small dependency-free helper that reports an "
        "unhandled exception to the AI self-healing system for automatic detection and "
        "AI-assisted fixing.\n\n"
        "## Next step (not done automatically)\n"
        f"Call `{call_example}` from this app's top-level exception handler / error "
        "middleware (e.g. a `try`/`except` around the request handler, or your "
        "framework's error hook) so runtime errors reach it.\n\n"
        '_Opened automatically by the AI self-healing system\'s "connect a repo" flow._'
    )


async def open_onboarding_pull_request(app: MonitoredApp) -> dict[str, object]:
    """Commit `build_onboarding_file(app)` to a new branch and open a PR.

    Uses the GitHub Contents API directly (no worktree/clone needed for a
    single-file commit), against `app.github_repo`.
    """
    onboarding_file = build_onboarding_file(app)
    branch = f"{ONBOARDING_BRANCH_PREFIX}/{app.name}"

    async with GitHubClient(repo=app.github_repo) as client:
        repo_info = await client.get_repo()
        base_branch = str(repo_info.get("default_branch") or "main")
        base_ref = await client.get_branch_sha(base_branch)
        await client.create_branch(branch, from_sha=base_ref)
        await client.create_or_update_file(
            branch=branch,
            path=onboarding_file.path,
            content=onboarding_file.content,
            message=f"Add self-healing error reporting ({onboarding_file.path})",
        )
        pr = await client.create_pull_request(
            title="Add self-healing error reporting",
            body=build_onboarding_pr_body(app, onboarding_file),
            head=branch,
            base=base_branch,
        )
        return pr

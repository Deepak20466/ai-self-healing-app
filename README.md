# AI-Powered Self-Healing Application

A self-healing application with an MCP server: it detects runtime errors,
silent wrong-output bugs and CI/CD failures, roots-causes them with Claude,
ships a fix through CI/CD, and verifies it — with a real-time AI chat UI and
a metrics dashboard. See `SPEC.md` for the full specification and
`CLAUDE.md` for build status and conventions.

This is a working skeleton, not the finished Phase 8 write-up — the full
architecture diagram, cloud deploy guide, measured RAM numbers and demo
metrics land at the end of the build (see CLAUDE.md's phase log). This file
covers what's needed to run what exists today.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
copy .env.example .env
# Edit .env, then create the app + test databases and grant the app role
# CREATEDB (see CLAUDE.md "Environment on this machine" for why this can't
# be automated further):
.\scripts\bootstrap.ps1 -SuperuserPassword <your-postgres-superuser-password>
.venv\Scripts\alembic upgrade head
```

## AI backend: free mode vs. API mode

The healer (`healer/`) drives every automated fix through one of two
interchangeable backends, selected by `USE_CLAUDE_CODE` in `.env`:

- **Free mode (default, `USE_CLAUDE_CODE=true`).** Uses the local Claude
  Code CLI on your own Claude subscription login — no API key, no per-token
  billing. One-time setup: run `claude` once in this repo and log in
  interactively. After that, `python -m healer.main` just works.
- **API mode (`USE_CLAUDE_CODE=false`).** Uses the official `anthropic` SDK
  and requires `ANTHROPIC_API_KEY`. Install the optional extra first:
  `pip install .[api]`.

Both backends share the same queue, git-worktree lifecycle, PR/issue
creation, and guardrails (patch-scope sandboxing, anti-cheat checks, circuit
breakers, budget caps) — see `healer/agent_free.py`'s module docstring for
exactly how free mode's verification differs from API mode's, and why that
difference doesn't weaken any guardrail.

## Running the pods

Each pod is a native process (no Docker — see SPEC.md's hard constraints):

```powershell
.venv\Scripts\uvicorn apps.target_app.main:app --port 8001
.venv\Scripts\uvicorn sentinel.app:app --port 8002
.venv\Scripts\python -m mcp_server.http_main
.venv\Scripts\python -m healer.main
```

(`honcho start` via the `Procfile` is the intended Phase 7 way to bring up
all four at once; until then, run each in its own terminal.)

## Tests

```powershell
.venv\Scripts\pytest
.venv\Scripts\ruff check .
.venv\Scripts\ruff format --check .
.venv\Scripts\mypy core sentinel mcp_server healer
```

pytest always points at a throwaway `selfheal_test` database and mocks the
Anthropic and GitHub APIs — see CLAUDE.md for details.

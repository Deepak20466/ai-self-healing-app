#!/usr/bin/env bash
# Render start command: this project's 4 pods (app/sentinel/mcp/healer) are
# native processes designed to run on one host, not independent
# microservices -- healer/app.py and .mcp.json both reach the MCP server at
# a hardcoded 127.0.0.1 URL (see CLAUDE.md Phase 3/5+). Render gives us one
# service with one externally-routed $PORT, so we start app/sentinel/mcp on
# their normal fixed localhost ports (never exposed outside the dyno) and
# run healer in the foreground bound to 0.0.0.0:$PORT, since it's the only
# pod with a public UI (dashboard + chat, per deploy/Caddyfile's routing).
#
# GitHub Actions' CI webhook (sentinel-pod's /webhooks/ci, see
# deploy/Caddyfile) is NOT publicly reachable in this single-service layout
# -- it stays internal. Exposing it too would need a second Render service
# or a reverse proxy in front of healer; out of scope for "deploy the demo
# to Render", noted here rather than silently broken.
set -euo pipefail

# Render's managed Postgres gives postgres:// (or postgresql://); the app
# needs the asyncpg driver scheme.
export DATABASE_URL="$(python -c "
import os, re
print(re.sub(r'^postgres(ql)?://', 'postgresql+asyncpg://', os.environ['DATABASE_URL'], count=1))
")"

alembic upgrade head
python scripts/seed_demo.py

uvicorn apps.target_app.main:app --host 127.0.0.1 --port "${APP_PORT:-8001}" &
uvicorn sentinel.app:app --host 127.0.0.1 --port "${SENTINEL_PORT:-8002}" &
python -m mcp_server.http_main &

trap 'kill 0' EXIT

# healer-pod is the only public-facing process: bind it to Render's $PORT.
exec uvicorn healer.app:asgi_app --host 0.0.0.0 --port "${PORT}"

app: uvicorn apps.target_app.main:app --host 127.0.0.1 --port ${APP_PORT:-8001}
sentinel: uvicorn sentinel.app:app --host 127.0.0.1 --port ${SENTINEL_PORT:-8002}
mcp: python -m mcp_server.http_main
healer: uvicorn healer.app:asgi_app --host 127.0.0.1 --port ${HEALER_PORT:-8000}

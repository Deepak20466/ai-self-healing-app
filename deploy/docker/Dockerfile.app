# app-pod: the monitored demo FastAPI app (apps/target_app).
#
# Multi-stage: the "builder" stage has a C toolchain (asyncpg/argon2-cffi
# both ship wheels for common platforms, but building on an uncommon one
# still needs headers) and installs this project's own base dependencies
# into a venv; the runtime stage copies only that venv + the source tree
# into a slim, non-root image. See CLAUDE.md's "Multi-app support" /
# "Terminal-only v1.0" phase log for why there is no [api]/[dev] extra here
# -- app-pod never talks to an AI backend, it only gets monitored by one.
FROM python:3.11-slim AS builder

WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml README.md LICENSE ./
COPY core ./core
COPY apps ./apps
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY sentinel ./sentinel
COPY mcp_server ./mcp_server
COPY healer ./healer
COPY cli ./cli
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir .

FROM python:3.11-slim AS runtime

RUN groupadd --system app && useradd --system --gid app --create-home --home-dir /home/app app
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY --from=builder /build /app
COPY deploy/docker/http_healthcheck.py /app/http_healthcheck.py
RUN chown -R app:app /app /home/app
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

USER app
EXPOSE 8001
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD python /app/http_healthcheck.py APP_PORT 8001

CMD ["sh", "-c", "uvicorn apps.target_app.main:app --host 0.0.0.0 --port ${APP_PORT:-8001}"]

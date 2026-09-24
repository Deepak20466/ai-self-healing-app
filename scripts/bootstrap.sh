#!/usr/bin/env bash
# Phase 1 bootstrap for Linux/macOS: create a venv, install dependencies, and
# create the PostgreSQL role/database used by every pod.
#
# Usage:
#   ./scripts/bootstrap.sh
#   PGSUPERPASSWORD=... SELFHEAL_DB_PASSWORD=... ./scripts/bootstrap.sh
#
# If PGSUPERPASSWORD is unset, database creation is skipped; create it
# yourself, set DATABASE_URL in .env, then run `alembic upgrade head`.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PG_HOST="${PGHOST:-localhost}"
PG_PORT="${PGPORT:-5432}"
PG_SUPERUSER="${PGSUPERUSER:-postgres}"
PG_SUPERPASSWORD="${PGSUPERPASSWORD:-}"
APP_DB_NAME="${SELFHEAL_DB_NAME:-selfheal}"
APP_DB_USER="${SELFHEAL_DB_USER:-selfheal}"
APP_DB_PASSWORD="${SELFHEAL_DB_PASSWORD:-}"

echo "== Step 1/4: Python virtual environment =="
PYTHON_BIN="$(command -v python3.11 || command -v python3 || command -v python)"
if [ -z "$PYTHON_BIN" ]; then
    echo "Python 3.11+ was not found on PATH." >&2
    exit 1
fi

if [ ! -d ".venv" ]; then
    "$PYTHON_BIN" -m venv .venv
else
    echo "venv already exists at .venv, reusing it"
fi

VENV_PYTHON="$REPO_ROOT/.venv/bin/python"
"$VENV_PYTHON" -m pip install --upgrade pip
"$VENV_PYTHON" -m pip install -e ".[dev]"

echo
echo "== Step 2/4: .env file =="
if [ ! -f ".env" ]; then
    cp ".env.example" ".env"
    echo "Created .env from .env.example - fill in secrets before running the app."
else
    echo ".env already exists, leaving it untouched"
fi

echo
echo "== Step 3/4: PostgreSQL role and database =="
if [ -z "$PG_SUPERPASSWORD" ]; then
    echo "PGSUPERPASSWORD not set - skipping DB creation."
    echo "Create it manually, e.g.:"
    echo "  createuser -h $PG_HOST -p $PG_PORT -U $PG_SUPERUSER -P $APP_DB_USER"
    echo "  createdb   -h $PG_HOST -p $PG_PORT -U $PG_SUPERUSER -O $APP_DB_USER $APP_DB_NAME"
    echo "Then set DATABASE_URL in .env and run: $VENV_PYTHON -m alembic upgrade head"
else
    if ! command -v psql >/dev/null 2>&1; then
        echo "psql not found. Install the postgresql-client package and retry." >&2
        exit 1
    fi

    if [ -z "$APP_DB_PASSWORD" ]; then
        APP_DB_PASSWORD="$("$VENV_PYTHON" -c 'import secrets,string; a=string.ascii_letters+string.digits; print("".join(secrets.choice(a) for _ in range(24)))')"
    fi

    PGPASSWORD="$PG_SUPERPASSWORD" psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_SUPERUSER" -d postgres \
        -v ON_ERROR_STOP=1 -q <<SQL
DO \$\$
BEGIN
   IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = '$APP_DB_USER') THEN
      CREATE ROLE $APP_DB_USER LOGIN PASSWORD '$APP_DB_PASSWORD';
   ELSE
      ALTER ROLE $APP_DB_USER WITH PASSWORD '$APP_DB_PASSWORD';
   END IF;
END
\$\$;
SQL

    DB_EXISTS="$(PGPASSWORD="$PG_SUPERPASSWORD" psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_SUPERUSER" -d postgres \
        -tAc "SELECT 1 FROM pg_database WHERE datname = '$APP_DB_NAME'")"
    if [ "$DB_EXISTS" != "1" ]; then
        PGPASSWORD="$PG_SUPERPASSWORD" psql -h "$PG_HOST" -p "$PG_PORT" -U "$PG_SUPERUSER" -d postgres \
            -v ON_ERROR_STOP=1 -q -c "CREATE DATABASE $APP_DB_NAME OWNER $APP_DB_USER"
    fi
    echo "Role '$APP_DB_USER' and database '$APP_DB_NAME' are ready."

    DATABASE_URL="postgresql+asyncpg://${APP_DB_USER}:${APP_DB_PASSWORD}@${PG_HOST}:${PG_PORT}/${APP_DB_NAME}"
    if grep -q '^DATABASE_URL=' .env; then
        sed -i.bak "s#^DATABASE_URL=.*#DATABASE_URL=${DATABASE_URL}#" .env && rm -f .env.bak
    else
        echo "DATABASE_URL=${DATABASE_URL}" >> .env
    fi
    echo "Wrote DATABASE_URL into .env"
fi

echo
echo "== Step 4/4: Alembic migrations =="
"$VENV_PYTHON" -m alembic upgrade head
echo "Migrations applied."

echo
echo "Bootstrap complete."

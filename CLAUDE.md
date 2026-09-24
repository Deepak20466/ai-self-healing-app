# CLAUDE.md — project conventions and phase log

Source of truth for scope is `SPEC.md`. This file tracks what's actually
built, decisions made for ambiguous points, and conventions so a fresh
session can resume without re-deriving context.

## How to resume

1. Read `SPEC.md` fully.
2. Read the "Phase log" below to see what's done.
3. Run `ruff check . && ruff format --check . && mypy core sentinel mcp_server healer`
   (only check dirs that exist so far) and `pytest` before starting new work,
   to confirm the last phase is still green.
4. Continue with the next unchecked phase in SPEC.md's "BUILD ORDER".

## Environment on this machine

- Windows, PowerShell primary shell. Bash (git-bash) also available.
- PostgreSQL 17 already installed as a Windows service (`postgresql-x64-17`),
  auth is `scram-sha-256` (password required) even for localhost — no trust/
  peer shortcut. The project's own Claude Code sandbox refuses to weaken this
  (edits to `pg_hba.conf`, single-user-mode bypasses, etc. are blocked as
  "Security Weaken" / "TLS/Auth Weaken"). **Do not attempt to route around
  that guardrail** (e.g. spinning up a second trust-auth cluster) — ask the
  user to run `scripts/bootstrap.ps1` themselves (they pass their own
  superuser password as a parameter; it never needs to reach the assistant),
  or to paste a ready `DATABASE_URL`.
- venv lives at `.venv/` (created with `python -m venv .venv`, Python 3.11.9).
- App database: role + db both named `selfheal` by default, created by
  `scripts/bootstrap.ps1` / `scripts/bootstrap.sh`.

## Conventions

- Python 3.11+ everywhere except the exceptions listed in SPEC.md.
- All ORM models live in `core/models.py`, all inherit `core.db.Base`.
- Enums use `enum.StrEnum` (not `class X(str, Enum)` — ruff UP042 flags that).
- Secrets (`SESSION_SECRET`, `HEALER_WEBHOOK_SECRET`, `ADMIN_PASSWORD_HASH`,
  `ANTHROPIC_API_KEY`, `GITHUB_TOKEN`) default to `None` in `core/config.py`,
  never to a placeholder string — ruff's `S105` flags hardcoded-looking
  secrets, and a real hardcoded default would violate SPEC.md's "never
  hardcode secrets" constraint anyway. Code that needs them at runtime must
  check for `None` and fail loudly, not silently use a weak default.
- Money columns are `Numeric`, not `Float` (avoid float rounding on cost
  tracking that feeds budget enforcement).
- Table/column names follow SPEC.md's "DATABASE SCHEMA" section exactly,
  except where noted in "Ambiguities resolved" below.
- Every module gets a short module-level docstring explaining *why* it
  exists / non-obvious design choices, not what it does line-by-line.
- Lint/type/test gate for every phase:
  `ruff check`, `ruff format --check`, `mypy` (strict on `core/`, `sentinel/`,
  `mcp_server/`, `healer/` per SPEC.md), `pytest`.

## Ambiguities resolved (SPEC.md said "pick the simplest robust option")

- **Primary keys**: `BigInteger` auto-increment identity columns everywhere,
  not UUIDs. Nothing in the spec requires globally-unique IDs generated
  outside the DB.
- **`errors.status` / `contract_violations.status`**: SPEC.md's schema list
  doesn't spell out a status enum for these tables, but the MCP tool
  `list_open_errors()` needs something to filter on. Added
  `status: open|resolved` (Postgres enum `error_status`) to both tables.
- **HMAC signature format**: Stripe-style `t=<unix_ts>,v1=<hex_hmac>` over
  `f"{ts}.{body}"`. Chosen because it cleanly separates the replay-window
  check (compare `t`) from the integrity check (compare `v1`), and is a
  well-understood scheme to point to in the README.
- **Rate limiting**: a `(key, bucket)` token bucket in the `rate_limits`
  table (`core/ratelimit.py`), refilled lazily on each check (no background
  refill process — keeps RAM/process count down per SPEC.md constraint #3).
  Login lockout is a *separate* mechanism (`core/ratelimit.py:
  is_login_locked_out`) that reads `login_attempts` directly: locked out iff
  the most recent `login_max_attempts` attempts for that
  (ip_address, username) are *all* failures — one success resets it, matching
  SPEC.md's "lock out login for 15 minutes after 5 failed attempts" wording.
- **Job queue notify payload**: `enqueue_heal_job` calls
  `SELECT pg_notify($1, $2)` (parameterized) rather than a raw `NOTIFY
  channel, 'payload'` statement, to avoid manually escaping the JSON payload
  for SQL string-literal syntax.
- **`core/hmac_utils.py` filename** (not `core/hmac.py`): avoids any
  ambiguity with the stdlib `hmac` module even though Python 3's absolute
  imports would technically resolve it correctly either way.

## Phase log

### Phase 1 — Foundation: DONE (code), DB verification pending on user
Built: `pyproject.toml` (deps, ruff, mypy strict-on-core, pytest config),
`core/` package (`config.py`, `db.py`, `models.py` — all 14 SPEC.md tables,
`logging.py` structlog JSON setup, `hmac_utils.py`, `untrusted.py`,
`ratelimit.py`, `queue.py`), Alembic setup (`alembic.ini`, `alembic/env.py`
async, `alembic/versions/0001_initial.py` — full schema + 4 Postgres enums),
`.env.example`, `.gitignore`, `scripts/bootstrap.sh` + `scripts/bootstrap.ps1`,
DB-independent unit tests (`tests/test_config.py`, `test_hmac_utils.py`,
`test_untrusted.py` — 11 tests, all passing).

Verified: `pip install -e ".[dev]"` succeeds, `ruff check`/`ruff format
--check` clean, `mypy core` (strict) clean, `pytest` 11/11 passing.

**Not yet verified**: `alembic upgrade head` against a real Postgres
instance — blocked on getting DB credentials from the user (see
"Environment on this machine" above). Once `.env` has a working
`DATABASE_URL`, run `.venv\Scripts\python.exe -m alembic upgrade head` and
confirm all 14 tables + 4 enum types exist, then update this section.

### Phase 2 — Detection: NOT STARTED
### Phase 3 — MCP: NOT STARTED
### Phase 4 — Runtime healing: NOT STARTED
### Phase 5 — CI healing: NOT STARTED
### Phase 6 — UI: NOT STARTED
### Phase 7 — Ship: NOT STARTED
### Phase 8 — Prove: NOT STARTED

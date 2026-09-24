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
- **SQLAlchemy `Enum(PythonEnum)` pitfall**: by default it sends the member
  *name* ("OPEN") on the wire, not `.value` ("open") — but the Alembic
  migrations create the Postgres enum types using lowercase `.value`
  strings. Fixed with a `core/models.py:_pg_enum()` helper that always
  passes `values_callable=lambda cls: [m.value for m in cls]`. If you ever
  add a new enum column, use `_pg_enum(...)`, not `SAEnum(...)` directly, or
  every insert will fail with "invalid input value for enum ...".
- **target_app/sentinel talk over HTTP, not a shared DB session**: SPEC.md
  calls sentinel-pod's capture path an "ingest API" and describes the
  middleware/logging-handler as "drop-in" — so `apps/target_app`'s
  `SentinelMiddleware`/`SentinelLogHandler` POST to sentinel-pod's
  `/ingest/error` and `/ingest/metric` (via `sentinel/client.py`) rather than
  writing to the DB directly. This keeps app-pod's only coupling to
  sentinel-pod being HTTP, matching the 4-separate-native-processes
  architecture. Error reporting is *awaited* (so "the request returned"
  reliably means "sentinel has it", which tests depend on); metric reporting
  is fire-and-forget (never adds latency to a normal response). Both swallow
  transport errors — monitoring must never be able to break the app it
  monitors.
- **`/trigger/{bug}` IS the real endpoint, not a separate demo wrapper**: all
  7 seeded bugs are deterministic against the fixed dataset in
  `apps/target_app/seed_data.py`, so `/trigger/zero` etc. just call the same
  `apps/target_app/bugs.py` functions with the specific seeded id known to
  reproduce that bug. The 2 silent bugs (off-by-one, timezone) are also
  registered as `apps/target_app/contracts.py` `ContractCase`s so the
  sentinel prober catches them continuously, not just when manually
  triggered.
- **Bug #5 (timezone) redesign**: the first version stored a datetime with
  an IST offset and read `.date()` directly, assuming the offset would
  survive a DB round-trip — but Postgres `timestamptz` always normalizes to
  UTC on read, silently erasing the original offset, so the bug never
  actually reproduced. Fixed by introducing a fixed `STOREFRONT_TZ` (IST)
  business-timezone constant in `bugs.py`: the bug is forgetting to
  `.astimezone(STOREFRONT_TZ)` before taking `.date()`, which reproduces
  correctly regardless of what offset the row was inserted with. If you add
  another timezone-sensitive bug, remember: **`timestamptz` never round-trips
  the original UTC offset** — design the bug around a business-timezone
  conversion, not the insert-time offset.
- **heal_job re-enqueue rule**: SPEC.md says "enqueue when new or above a
  threshold" without defining "threshold" precisely. Implemented in
  `sentinel/storage.py` as: enqueue on the *first* occurrence, then again
  every `ERROR_REOCCURRENCE_THRESHOLD` (default 5) occurrences thereafter —
  but only if no heal_job for that fingerprint is currently in flight
  (status in queued/running/pr_opened/ci_fixing). This prevents duplicate
  jobs while a fix attempt is already underway; the circuit breaker (max 3
  attempts/24h) is a separate guardrail to build in Phase 4/5.
- **Test DB isolation**: `tests/conftest.py`'s `db_session` fixture wraps
  each test in an outer transaction (`conn.begin()` + `join_transaction_mode
  ="create_savepoint"`) that's rolled back afterward, so storage.py's
  internal `session.commit()` calls never actually persist past the test.
  HTTP-level tests (`target_app_client`, `sentinel_http_client`) override
  FastAPI's `get_db` dependency to the *same* session, so a single test can
  hit target_app over ASGI, have sentinel process it over ASGI, and assert
  the result — all inside one rolled-back transaction, no real sockets.
  `pytest_sessionstart` runs `scripts/seed_demo.py` once for the whole run.
- **pytest-asyncio loop scope must be `session`, not the default
  `function`**: `core.db.engine` is a module-level singleton; its asyncpg
  pool binds to whichever event loop first touched it. Per-test
  (function-scoped) loops tear down and recreate a loop for every test,
  which orphans pooled connections from previous tests and crashes on
  cleanup (`AttributeError` / `Event loop is closed`, especially bad under
  Windows' ProactorEventLoop). Set in `pyproject.toml`:
  `asyncio_default_fixture_loop_scope = "session"` and
  `asyncio_default_test_loop_scope = "session"`.
- **Fire-and-forget `asyncio.create_task` + a rollback-based test fixture
  don't mix**: `SentinelLogHandler.emit()` schedules a background task
  rather than awaiting it (it's called from sync logging code). In tests,
  don't `await asyncio.sleep(n)` and hope it finished — the fixture's
  transaction can roll back mid-task. Instead await the handler's own
  `_background_tasks` set directly (see `tests/test_sentinel_logging_handler.py`).
- **mcp SDK: `MCPServer`, not `FastMCP`.** SPEC.md says "FastMCP" throughout,
  but the installed `mcp` package (pinned `>=2.0,<3.0`) renamed that class to
  `MCPServer` in its 2.x line — `from mcp.server.mcpserver import MCPServer`.
  The decorator API (`@mcp.tool()`, `@mcp.resource(uri)`, `mcp.run(transport=
  "stdio"|"streamable-http")`) is essentially unchanged, so this satisfies
  the spec's intent (current official SDK, decorator-based tool/resource
  registration) rather than pinning an EOL'd `mcp<2` just to get a class
  named literally "FastMCP". `@mcp.tool()`/`@mcp.resource()` return the
  original function unchanged (registration is a side effect), so every tool
  is directly `await`-able in tests without going through the MCP protocol.
  Raise `mcp.server.mcpserver.exceptions.ToolError` (re-exported from
  `mcp_server/tools/_exceptions.py`) for anticipated failures — the client
  gets a clean message and no traceback is logged.
- **`propose_patch`'s write scope comes from the heal_job's `type` in the
  DB, never from a tool parameter.** A `runtime_error`/`contract_violation`
  job is restricted to `apps/target_app/`; a `ci_failure` job may touch
  anything outside the universal forbidden list (`.env`, `.git/`,
  `.github/workflows/`, `alembic/versions/`). Looking this up server-side
  (rather than trusting a caller-supplied "scope" argument) is what makes
  SPEC.md's "runtime auto-fixes may only modify apps/target_app/" a real
  guardrail instead of a suggestion an injected prompt could talk around.
- **`run_tests`/`propose_patch` take an explicit `worktree` name**, not
  implicit per-MCP-session state. SPEC.md's wording ("runs pytest in the fix
  worktree") suggests session-scoped state, but MCP's `Context.request_state`
  would make this harder to test and reason about for no real benefit — the
  calling agent already knows which worktree it created. Worktree creation
  itself isn't an MCP tool (not in SPEC.md's tool list); it's `git worktree
  add`, done by whoever starts a fix attempt (healer, Phase 4/5; test
  fixture `git_worktree` in the meantime).
- **SECURITY section widens the confirmation-token requirement beyond the
  tool list**: SPEC.md's mcp-pod bullet only says `trigger_rollback` needs a
  confirmation token, but the SECURITY section says "destructive actions
  (rollback, cancel, merge) require ... an explicit 'yes' confirmation."
  Extended `cancel_workflow` to also require `confirmation_token`
  (`mcp_server/confirmation.py`, itsdangerous-signed, 5-minute default
  expiry) for consistency — the chat layer (Phase 6) is what actually gates
  on "yes" and calls `issue_confirmation_token`.
- **Shared dev DB pollutes unscoped test queries — scope by fingerprint,
  not by type/exception_type alone.** Hit this for real: a Phase 3 test used
  `exception_type="KeyError"` for unrelated test data, which collided with
  Phase 2's `Error.exception_type == "KeyError"` query (the *real* bug's
  type) and caused `MultipleResultsFound` once both existed in the same DB.
  Every test that asserts against real committed rows (not the rollback-
  wrapped `db_session`) must filter by the exact fingerprint/id it expects,
  never by a broad type/category that other tests might also produce.
  `sentinel.fingerprint.fingerprint_error(...)` is deterministic — compute
  the expected value directly rather than assuming uniqueness.
- **MCP tools have no dependency injection — they call `core.db.
  session_scope()` themselves.** Unlike FastAPI routes (`Depends(get_db)`,
  overridable in tests), a tool function's DB access can't be redirected to
  the rollback-wrapped `db_session` fixture. Tests for session_scope()-based
  tools (`tools/errors.py`, `tools/deploy.py`, `tools/code.py`'s heal_job
  lookup) create fixtures via `session_scope()` too (real commits, random
  fingerprint/id suffixes) rather than `db_session` — same tradeoff already
  accepted for `sentinel/anomaly.py` and `mcp_server/audit.py`. `core/
  metrics.py` is the exception: it takes `session` as a parameter, so its
  tests use the clean `db_session` fixture directly.

## Phase log

### Phase 1 — Foundation: DONE
Built: `pyproject.toml` (deps, ruff, mypy strict-on-core, pytest config),
`core/` package (`config.py`, `db.py`, `models.py` — all 14 SPEC.md tables,
`logging.py` structlog JSON setup, `hmac_utils.py`, `untrusted.py`,
`ratelimit.py`, `queue.py`), Alembic setup (`alembic.ini`, `alembic/env.py`
async, `alembic/versions/0001_initial.py` — full schema + 4 Postgres enums),
`.env.example`, `.gitignore`, `scripts/bootstrap.sh` + `scripts/bootstrap.ps1`.

Verified: `pip install -e ".[dev]"` succeeds, `ruff check`/`ruff format
--check` clean, `mypy core` (strict) clean, `alembic upgrade head` runs
clean against the real `selfheal` Postgres DB (confirmed via `\dt`/`\dT`:
all 15 tables incl. `alembic_version`, all 4 enum types present).

### Phase 2 — Detection: DONE
Built:
- `apps/target_app/`: FastAPI demo app (`main.py` — app factory, so tests
  can inject a test `SentinelClient`), `models.py` + `repository.py`
  (`demo_items`/`demo_orders`, migrated by
  `alembic/versions/0002_target_app_demo_tables.py`), `seed_data.py`
  (deterministic dataset, single source of truth for seeding/bugs/contracts),
  `bugs.py` (the 7 seeded bugs — see SPEC.md list), `schemas.py`,
  `routes.py` (resource routes + `/trigger/{zero,key,off_by_one,none_lookup,
  timezone,timeout,validation}`), `contracts.py` (4 `ContractCase`s: 2
  healthy baselines, 2 that catch the silent bugs).
- `sentinel/`: `capture.py` (build a `CapturedError` from a live exception,
  preferring the deepest in-app frame), `middleware.py` +
  `logging_handler.py` (the two "drop-in" capture paths) + `client.py`
  (shared HTTP client, used by both), `scrubber.py` (regex + sensitive-key
  redaction), `fingerprint.py` (dedup hashing), `storage.py` (the actual
  DB-writing/dedup/enqueue/audit logic, shared by ingest endpoints + prober +
  anomaly loop), `prober.py` (replays `contracts.py` against a live app),
  `anomaly.py` (pure `AnomalyDetector` + async scheduling wrapper),
  `app.py` (sentinel-pod FastAPI app: `/healthz`, `/ingest/error`,
  `/ingest/metric`, `POST /webhooks/ci`, lifespan starts prober + anomaly
  background loops).
- `scripts/seed_demo.py` (idempotent upsert of the demo dataset).
- `tests/conftest.py`: rollback-per-test DB isolation + in-process
  ASGI wiring between target_app and sentinel (see "Ambiguities resolved").
- 77 tests total (up from 11 in Phase 1), covering: all 7 bugs directly,
  HTTP-level `/trigger/*` behavior, end-to-end capture (error stored with
  correct file/line, occurrence counting, scrubbing), the prober catching
  both silent bugs as contract violations, webhook HMAC verification
  (valid/unsigned/wrong-secret/replayed/unconfigured), the anomaly detector
  (pure, synthetic timestamps), the logging handler, and `core/ratelimit.py`
  (untested in Phase 1 since no DB fixture existed yet).

Verified (explicit Phase 2 checklist from SPEC.md):
- `/trigger/zero` stores the error with the correct file and line:
  `tests/test_sentinel_capture_integration.py::test_trigger_zero_stores_error_with_correct_file_and_line`
- Off-by-one and timezone bugs are caught as contract violations:
  `tests/test_sentinel_prober.py::test_probe_flags_off_by_one_and_timezone_as_violations`
- A signed webhook is stored, unsigned/replayed ones are rejected:
  `tests/test_sentinel_webhook.py` (7 tests)

`ruff check .`/`ruff format --check .` clean repo-wide. `mypy core sentinel`
(strict) clean. `pytest` 77/77 passing. Coverage on `core/`+`sentinel/`: 89%
(`core/queue.py` at 62% is expected — `dequeue_heal_job`/`HealJobListener`
aren't exercised until Phase 4's healer worker consumes jobs).

**Known limitation, deferred**: the real `sentinel/app.py` lifespan starts
`run_prober_loop`/anomaly loop as background tasks hitting target_app's
*real* HTTP port — this only actually works once both pods are run together
(e.g. via the `Procfile`, not built yet). Tests exercise the same logic
directly (`probe_once`/`persist_results`) against an in-process ASGI app
instead, which is deliberate (see conftest.py note) but means the live
end-to-end loop hasn't been run for real yet. Do that as part of Phase 6/7
when honcho/Procfile ties all pods together, or sooner if useful for a demo.

### Phase 3 — MCP: DONE
Built (`mcp_server/`, using the `mcp` SDK's `MCPServer`, not `FastMCP` — see
"Ambiguities resolved"):
- `instance.py` (shared `mcp = MCPServer(...)`), `server.py` (imports every
  tool/resource module for registration, exposes `run_stdio`/`run_http`),
  `http_main.py` (Procfile entrypoint), `.mcp.json` (stdio entrypoint for
  Claude Code in VS Code, `.venv/Scripts/python.exe -m mcp_server.server`).
- `sandbox.py`: the path-safety boundary. `check_readable`/`check_writable`
  (blocks `.env*` except `.env.example`, and — writes only — `.git/`,
  `.github/workflows/`, `alembic/versions/`), `resolve_worktree_dir`
  (sandboxed to `worktrees/`), `extract_diff_paths`/
  `check_diff_paths_writable` (validates every path in a unified diff before
  any of it is applied).
- `git_utils.py` (async subprocess wrappers: blame, log, apply/reverse-apply
  a diff, `run_pytest` with a timeout), `github_client.py` (GitHub REST:
  workflow runs, jobs/logs, rerun/cancel, PR status/reviews/checks, workflow
  dispatch), `log_trim.py` (trims a raw CI log to the failing
  `##[group]`/`##[error]` step, capped at 20KB), `confirmation.py`
  (itsdangerous-signed tokens gating `cancel_workflow`/`trigger_rollback`),
  `audit.py` (`audited_tool()` — a drop-in `@mcp.tool()` replacement that
  logs every call, scrubbed and truncated, to `audit_log`).
- `tools/errors.py`, `tools/code.py`, `tools/deploy.py`, `tools/cicd.py` —
  all 20 SPEC.md tools. `tools/deploy.py:get_health` probes all 4 pods
  concurrently (`asyncio.gather`, not sequential awaits — the first version
  was ~4x slower for no reason). `resources.py` — all 3 SPEC.md resources.
- `core/metrics.py` (new, shared): MTTR, fix success rate, CI auto-fix rate,
  cost per fix, daily spend vs budget, errors-by-type — used by
  `get_metrics` now and by the Phase 6/8 dashboard later, so the numbers
  agree everywhere they're shown.
- `tests/conftest.py` gained a `git_worktree` fixture (a real, disposable
  `git worktree` under `worktrees/`, cleaned up after each test) used by
  `git_utils`/`propose_patch`/`run_tests` tests.
- 107 new tests (184 total). Full coverage of `sandbox.py`'s allow/deny
  rules, `propose_patch`'s scope enforcement (runtime-fix jobs confined to
  `apps/target_app/`, CI-fix jobs allowed wider but never past the universal
  forbidden list, confirmed against a real git worktree with real diffs),
  confirmation-token issuance/verification/expiry, GitHub tools mocked via
  `respx`, and a registration smoke test asserting all 20 tools + 3
  resources are present with valid schemas.

Verified (explicit Phase 3 checklist from SPEC.md):
- Tools work / registration is complete: `tests/test_mcp_server_registration.py`
  (programmatic equivalent of "works via `mcp dev`" — see that file's
  docstring for why an interactive-only check couldn't be automated here).
- Writes outside `apps/target_app/` are rejected for runtime auto-fixes:
  `tests/test_mcp_tools_code.py::test_propose_patch_runtime_job_cannot_modify_outside_target_app`
  and `test_propose_patch_contract_violation_job_is_also_target_app_only`
  (and the forbidden-path list is enforced regardless of job type:
  `test_propose_patch_never_allows_forbidden_paths_regardless_of_job_type`).

`ruff check .`/`ruff format --check .` clean repo-wide. `mypy core sentinel
mcp_server` (strict) clean. `pytest` 184/184 passing, twice in a row (see
"shared dev DB pollutes unscoped queries" in Ambiguities resolved — that was
a real bug this session found and fixed, not a hypothetical). Coverage on
`core/`+`sentinel/`+`mcp_server/`: 91%.

**Known limitation, deferred**: `trigger_rollback` dispatches
`rollback.yml`, which doesn't exist until Phase 7 — the tool code is
correct and tested (mocked), but calling it for real against a real repo
will 404 until then. `mcp_server/http_main.py` (the Procfile entrypoint) has
0% test coverage — it's a one-line `if __name__ == "__main__"` wrapper
around `run_http()`, not meaningfully unit-testable; same pattern already
accepted for other pods' entrypoints.

### Phase 4 — Runtime healing: NOT STARTED
### Phase 5 — CI healing: NOT STARTED
### Phase 6 — UI: NOT STARTED
### Phase 7 — Ship: NOT STARTED
### Phase 8 — Prove: NOT STARTED

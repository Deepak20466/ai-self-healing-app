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

## Test database (never the dev DB)

- **pytest never opens the dev `selfheal` database.** `tests/conftest.py`
  forces `DATABASE_URL` to a throwaway `selfheal_test` database (an explicit
  `TEST_DATABASE_URL` in `.env` wins if present; otherwise it derives
  `<dbname>_test`) **before the first import** of `core.config`/`core.db`
  anywhere in the process — both are module-level singletons bound at import
  time, so this has to happen at the very top of `conftest.py`, ahead of
  every other project import (see the big docstring there).
- `pytest_sessionstart` then creates the test database if missing, runs
  `alembic upgrade head` against it, and seeds the deterministic demo
  dataset — automatically, every run, no manual step.
- Creating the database requires the `selfheal` role to have `CREATEDB`.
  `scripts/bootstrap.ps1` / `.sh` grant this (a normal SQL `ALTER ROLE ...
  CREATEDB`, not a Postgres-auth weakening — still scram-sha-256, no
  `pg_hba.conf` changes) and also pre-create `selfheal_test` and write
  `TEST_DATABASE_URL` into `.env`, mirroring how they already handle
  `DATABASE_URL`. If the role lacks the privilege, `pytest_sessionstart`
  raises a clear `RuntimeError` telling the user to re-run bootstrap with
  their superuser password — same pattern as the dev DB (see "Environment on
  this machine" above), never routed around.
- Per-test isolation is unchanged from Phase 2: the `db_session` fixture
  (rollback-per-test via a savepoint) for most tests, and `core.db.
  session_scope()` with randomized fingerprint/id suffixes for tests that
  need a real cross-connection commit (queue `SKIP LOCKED`, MCP tools with no
  DI). Both patterns now simply point at `selfheal_test` instead of `selfheal`.

## Anthropic and GitHub are always mocked in tests

- **No test may make a real Anthropic or GitHub API call.** Real calls only
  happen when running the actual pods (`honcho start` / a single pod
  entrypoint), never under `pytest`.
- Belt: `tests/conftest.py` unconditionally overwrites `ANTHROPIC_API_KEY`,
  `GITHUB_TOKEN` and `GITHUB_REPO` in `os.environ` with obviously-fake
  values for the whole pytest process (even if the real ones are exported in
  the ambient shell, e.g. to run the app in another terminal). If any code
  path ever forgets to mock and reaches the real API, it fails closed with a
  401 instead of silently spending money or opening a real PR.
- Suspenders: every test that exercises `mcp_server/github_client.py` (and,
  from Phase 4 on, `healer`'s Anthropic client) uses the `respx_mock`
  fixture and registers explicit routes — respx patches the httpx transport
  layer itself, so it's agnostic to which client library sits on top
  (confirmed this works for the `anthropic` SDK too, since it's httpx-based
  internally, the same way it already works for `mcp_server/github_client.py`
  — see `tests/test_mcp_github_client.py`). respx's default
  `assert_all_mocked=True` means an unmocked request raises instead of
  passing through.
- Never add a fixture or helper that constructs a real `anthropic.
  AsyncAnthropic(...)` or a real, unmocked `GitHubClient()` call in a test.
  Inject a fake/duck-typed client, or mock with `respx_mock`.

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

### Phase 4 — Runtime healing: DONE (code + tests; real end-to-end PR run still pending)
Built (`healer/`):
- `costs.py` (per-model USD pricing for input/output/cache-write/cache-read
  tokens, `Decimal`-based, falls back to Sonnet-5 rates with a warning for an
  unrecognized model), `budget.py` (`daily_spend` pause-at-cap, latches once
  tripped so a job never un-pauses mid-day even if cost momentarily reads
  under budget again), `circuit_breaker.py` (per-fingerprint 24h attempt cap
  + a separate global hourly job cap), `worktree.py` (git worktree
  create/reset/commit-and-push/remove lifecycle, async subprocess wrappers,
  30s timeout), `github_ops.py` (opens the auto-fix PR or, on failure/low
  confidence, a `needs-human-review` issue), `anthropic_client.py`
  (`AnthropicClientLike` Protocol + `build_anthropic_client()` factory - lets
  `runtime_agent.py` depend on a narrow structural type instead of the real
  SDK class, so tests inject a hand-built fake), `mcp_client.py`
  (`MCPToolClient` wrapping `ClientSession`; `connect_in_memory()` for tests
  via MCP's real in-memory transport, `connect_http()` for production),
  `prompts.py` (system prompt + `<untrusted_data>`-wrapped initial messages,
  the prompt-injection defense), `runtime_agent.py` (`run_heal_job()` - the
  main tool-call loop: checks circuit breaker + budget before each of up to
  3 attempts, proves a regression test fails-before-fix and passes-after,
  caps 20 tool calls/attempt and 8000 tokens/response, tracks token usage
  and cost per attempt, commits+pushes+opens a PR on success or opens an
  issue on exhausted attempts), `worker.py` (`run_worker()` - dequeues
  `runtime_error`/`contract_violation` jobs via `core.queue`'s
  LISTEN/NOTIFY-backed listener, falls back to a 30s poll, checks the global
  hourly circuit before claiming work).
- `tests/conftest.py` gained `fake_git_remote` (a throwaway local bare repo
  added as a git remote, so `commit_and_push` tests never touch the real
  `origin`) and the shared `isolated_budget_date` fixture (see "Ambiguities
  resolved" below for why every `daily_spend`-touching test needs it).
- 21 new tests across `test_healer_costs.py`, `test_healer_budget.py`,
  `test_healer_circuit_breaker.py`, `test_healer_worktree.py`,
  `test_healer_mcp_client.py`, and `test_healer_runtime_agent.py` (4
  end-to-end scenarios: fixes a real `ZeroDivisionError`, fixes a real
  off-by-one contract violation, an injection payload in the error message
  causes no test deletion, exceeding the daily budget pauses the job before
  any Anthropic call). Anthropic is a hand-built fake scripting the exact
  tool-call sequence (real wire-format scripting via `respx` isn't worth it
  for these deterministic scenarios); everything else in the e2e tests is
  real - a real MCP client<->server round trip over `connect_in_memory`,
  real git worktrees, real `git apply`, real `pytest` subprocess runs, and a
  real `HealJob`/`Error`/`ContractViolation` row. GitHub is mocked via
  `respx`; `git push` goes to `fake_git_remote`.

Verified: `pytest` 226/226 passing (twice in a row, including the isolated
end-to-end tests), `ruff check .`/`ruff format --check .` clean, `mypy core
sentinel mcp_server healer` (strict) clean.

**Ambiguities resolved this phase**:
- **`daily_spend` rows are real, cross-connection commits (via
  `session_scope()`), never rolled back like `db_session`-backed tests** -
  so any test that calls `is_budget_paused`/`record_spend` against "today"
  pollutes the *real* shared `selfheal_test` row for that category, which
  then corrupts every other test (in the same run or a later one) reading
  "today" for that same category. Hit this for real: a hardcoded synthetic
  future date (`2099-01-01`) reused across several manual re-runs during
  development accumulated `spend_usd`/`paused=True` on that fixed row just
  as surely as "today" would have. Fixed with a shared, opt-in
  `isolated_budget_date` fixture (`tests/conftest.py`) that monkeypatches
  `healer.budget.datetime` to a synthetic date derived fresh from
  `uuid.uuid5(uuid.NAMESPACE_DNS, uuid.uuid4().hex)` every time the fixture
  runs (year 2200-2249) - a *random* date, not a fixed one, so repeated runs
  (including CI runs on the same calendar day) can never collide with each
  other or with production data. Every test that calls `run_heal_job` (not
  just the budget-specific tests) takes this fixture, since `run_heal_job`
  itself calls `is_budget_paused`/`record_spend` internally.

**Real end-to-end run attempted this session — infra confirmed working, LLM
availability is the actual blocker.** Manually started all 4 pods (`uvicorn
apps.target_app.main:app`, `uvicorn sentinel.app:app`, `python -m
mcp_server.http_main`, `python -m healer.worker`; the first two need
`uvicorn`, not `python -m`, since their entrypoint modules only expose a
factory-built `app`, no `if __name__ == "__main__"` block — Phase 7's
Procfile should account for this), triggered `/trigger/zero`, and let the
healer work a real `ZeroDivisionError` `heal_job` against several different
LLM backends through OpenRouter (`ANTHROPIC_BASE_URL=https://openrouter.ai/api`):

- `meta-llama/llama-3.3-70b-instruct:free` → 404, free slug no longer served
- `openai/gpt-4o` (paid) → 402, OpenRouter account had insufficient credit
  for the 8000-token response cap (`CLAUDE_MAX_TOKENS` in
  `runtime_agent.py` — separate from `settings.max_tokens_per_job`, which
  caps *total* tokens across a whole job's attempts, not the per-request
  size)
- `poolside/laguna-s-2.1:free` → 429, upstream-shared rate limit
- `qwen/qwen-2.5-coder-32b-instruct:free` → 404, free slug no longer served
- `google/gemini-2.0-flash-exp:free` → 404, no endpoints (guessed from
  training data rather than queried live — don't do this; see below)
- `cohere/north-mini-code:free` → got furthest: real tool-call round trips
  against a real MCP server and a real git worktree, but hit OpenRouter's
  shared 15 req/min cap partway through a single attempt's ~15-20 rapid
  sequential tool calls, and failed the job

**Root cause, not per-model bad luck**: the healer's tool-call loop issues
many rapid sequential LLM requests within a single attempt (up to 20 tool
calls, SPEC.md's cap). Every OpenRouter free-tier slug shares capacity
across all their users and caps it around 15-20 req/min — structurally
incompatible with this loop shape regardless of which free model is
selected. A paid model (dedicated, non-shared rate limits) or the real
Anthropic API is the actual fix, not trying more free slugs.

**Don't guess OpenRouter model slugs from training data** — they drift
constantly (renamed, deprecated, moved from free to paid-only). Query
`https://openrouter.ai/api/v1/models` live and filter for
`id.endswith(":free")` plus `"tools" in supported_parameters` (tool-use
support is required; not all free models have it) before picking one.

**Real bug found and fixed**: `healer/worker.py`'s `_process_next_job` had
no exception handling around `run_heal_job` — an unhandled error thrown
deep in a single job's processing (e.g. `GitHubClientError` from
`open_low_confidence_issue` when the GitHub PAT lacked `Issues: write`)
crashed the *entire* long-running worker process, not just that job. For a
daemon meant to run continuously via LISTEN/NOTIFY, one bad job taking down
the whole process is a real production bug. Fixed: `_process_next_job` now
catches `Exception` around the `run_heal_job` call, logs
`worker.job_failed_unexpectedly`, marks that job `HealJobStatus.FAILED`,
and lets the worker loop continue to the next job. Verified by reproducing
the original crash (missing GitHub issue-creation permission) and
confirming the worker survived and the job was marked failed instead of the
process dying — no test added since this needs real subprocess-level
crash/survive verification, not a mock; recreate manually if regressing
this. Also fixed while touching this code: `healer/anthropic_client.py`'s
`build_anthropic_client()` used `**kwargs: dict[str, str]` to conditionally
add `base_url`, which mypy strict couldn't verify against `AsyncAnthropic`'s
real keyword signature — now passes `base_url=settings.anthropic_base_url`
directly (`str | None`, which the SDK already accepts as "no override").

**Still not completed**: a real PR was never opened end-to-end (every run
either errored out before or during the LLM call, or exhausted attempts
without a working fix and fell back to the low-confidence-issue path).
Given a paid model or real Anthropic credits, the next attempt should
reach a real PR — the code path through `github_ops.py`'s PR-opening branch
is exercised by the mocked e2e tests
(`test_run_heal_job_fixes_zero_division_error_end_to_end`) but still
unverified against the real GitHub API. Do this once billing/API-key
access allows a model with dedicated rate limits.

### Phase 5 — CI healing: DONE
Built (`healer/`, `sentinel/`, `core/`, `mcp_server/` — no new MCP tools
needed, Phase 3 already built every CI/CD tool this phase uses):
- `healer/ci_prompts.py` (a separate system prompt from Phase 4's
  `prompts.py`: classify flaky-vs-real using `get_workflow_run`/
  `get_job_logs`, then either `rerun_workflow` once or fix forward on the
  PR's own branch — no new-branch/new-PR step, no fail-before-pass proof
  requirement since a CI failure's own log is already the reproduction).
- `healer/ci_agent.py` (`run_ci_heal_job()` — one attempt per call, not
  Phase 4's up-to-3-internal-attempts loop; `_prepare_job()` validates +
  advances the job's state in one `session_scope()` block and returns a
  `_JobContext`/`_GiveUp`/`None` verdict, `_run_ci_attempt()` runs the
  tool-call loop and always overrides the model's `run_id`/`failed_only`
  arguments to `rerun_workflow` server-side, same principle as Phase 4's
  `heal_job_id`/`worktree` override on `propose_patch`).
- `healer/github_ops.py` gained `CIFixOutcome`/`post_ci_fix_comment` (the
  per-attempt PR comment SPEC.md requires) and `open_ci_needs_human_issue`
  (the circuit-breaker/exhausted-attempt fallback).
- `healer/worktree.py` gained `create_worktree_for_branch()` (fetch + `git
  worktree add` on an *existing* branch, for fixing forward on a PR branch
  instead of Phase 4's always-new `autofix/*` branch) and hardened
  `remove_worktree()` — see "real bug found" below.
- `healer/circuit_breaker.py` gained `ci_fix_attempt_count_for_pr`/
  `ci_fix_circuit_open` (SPEC.md's "max 2 CI-fix attempts per PR",
  `settings.max_ci_fix_attempts_per_pr` — this setting already existed in
  `core/config.py`, unused until now).
- `core/queue.py`'s `enqueue_heal_job` gained a `pr_number` param (set at
  insert time for `ci_failure` jobs) and a new public `notify_heal_job()`
  (factored out of `enqueue_heal_job`, also used by the requeue path below).
- `sentinel/storage.py`'s `record_pipeline_event` now requeues an existing
  in-flight `ci_failure` heal_job (bumps `source_pipeline_run_id`, resets to
  `queued`) instead of always inserting a new one — see "Ambiguities
  resolved this phase".
- `healer/worker.py`: dequeues all three `HealJobType`s now (was
  runtime_error/contract_violation only) and dispatches by type to
  `run_heal_job` or `run_ci_heal_job`.
- 11 new tests: `tests/test_healer_ci_agent.py` (4 end-to-end scenarios —
  fixes a real failure and pushes to the PR branch, classifies a failure as
  flaky and reruns it, rejects a diff that deletes a test, the per-PR
  circuit breaker refuses a new attempt with zero Anthropic calls),
  `tests/test_healer_circuit_breaker.py` (+4, the new CI-fix breaker),
  `tests/test_healer_worktree.py` (+1, `create_worktree_for_branch` against
  a branch that exists only on the remote), `tests/test_sentinel_webhook.py`
  (+2, the requeue-vs-enqueue-vs-circuit-broken paths). Same Phase 4 pattern
  throughout: Anthropic is a hand-built fake scripting the exact tool-call
  sequence; everything else is real (MCP client<->server round trip, git
  worktrees/`git apply`/`git push` to `fake_git_remote`, real `pytest`
  subprocess runs). GitHub mocked via `respx`.

Verified (explicit Phase 5 checklist from SPEC.md): with mocked Anthropic +
GitHub, a failing CI run gets a valid fix pushed to the PR branch
(`test_run_ci_heal_job_pushes_a_fix_and_leaves_the_job_in_flight`), and a
diff that deletes a test is rejected (`test_diff_deleting_a_test_is_rejected`
— the same `mcp_server/patch_guard.py` check Phase 4 already built; Phase 5
adds no new anti-cheat logic, just confirms it applies to `ci_failure` jobs
too, which `propose_patch`'s job-type-scoped write rules already covered).

`ruff check .`/`ruff format --check .` clean repo-wide. `mypy core sentinel
mcp_server healer` (strict) clean. `pytest` 237/237 passing, 3 times in a
row (after fixing the two hang-causing bugs and two pre-existing flaky
tests documented below — see "real bugs found").

**Ambiguities resolved this phase**:
- **A CI-fix "attempt" spans multiple heal_jobs' worth of real time, so one
  heal_job is reused across repeat failures on the same PR, not one row per
  failure.** SPEC.md's runtime-fix loop can prove success itself (run
  pytest locally, retry up to 3x, all within one call) — but a CI-fix
  attempt's real verdict is GitHub Actions re-running the workflow on the
  pushed commit, which can take minutes and arrives as a *separate* webhook
  call, not something this process can await inline. Resolved by having
  `sentinel.storage.record_pipeline_event` requeue the *same* in-flight
  `ci_failure` heal_job (new `source_pipeline_run_id`, status back to
  `queued`) when a new failure arrives for a fingerprint that already has
  one in flight, instead of enqueueing a second one. `healer.ci_agent.
  run_ci_heal_job` therefore only ever runs *one* attempt per call — no
  internal retry loop like Phase 4's — and increments `HealJob.attempt_count`
  once per real attempt. The per-PR circuit breaker
  (`ci_fix_attempt_count_for_pr`) sums `attempt_count` across every
  `ci_failure` row for a `pr_number` (normally just the one, reused, row)
  rather than counting rows, since row-counting would undercount attempts
  under this reuse scheme.
- **A CI-fix attempt that produces no working patch ends the job
  immediately, win or lose on `max_ci_fix_attempts_per_pr`.** If nothing was
  pushed, no future CI run will ever arrive to requeue the job — so
  "attempts remaining" is moot; leaving it non-terminal would just dangle
  forever. `run_ci_heal_job` marks the job `failed` and opens a
  needs-human-review issue on ANY unsuccessful attempt (not only once the
  cap is reached), while a *pushed* fix or a triggered rerun both leave the
  job `ci_fixing` (in-flight) since a real future CI event will legitimately
  arrive to move it forward.
- **`propose_patch`/`patch_guard.py` needed zero changes for CI jobs.**
  Phase 3 already derived write-scope from `heal_job.type` (`ci_failure` →
  no `apps/target_app/` restriction, still blocked from the universal
  forbidden paths) and Phase 4's anti-cheat checks (`check_not_cheating`)
  are diff-text-level and job-type-agnostic. Confirmed rather than assumed:
  `test_diff_deleting_a_test_is_rejected` exercises this for a `ci_failure`
  job specifically.
- **`create_worktree_for_branch` vs. Phase 4's `create_worktree`**: a
  runtime fix always starts a brand-new `autofix/*` branch off `main`
  (`create_worktree`, `-b <branch>`); a CI fix has to check out the PR's
  *existing* branch instead. `git worktree add <path> <branch>` (no `-b`)
  after a `git fetch <remote> <branch>` relies on git's own DWIM behavior
  (same as `git checkout <branch>` for an unambiguous remote branch) to
  auto-create a local branch tracking it — verified this actually works
  (not just documented) against a real local bare-repo remote before relying
  on it in `create_worktree_for_branch`/its test.

**Two real, related bugs found and fixed this session — both caused actual
process hangs, not just wrong output, and both are worth understanding if
anything in `healer/` starts hanging again:**

1. **`healer/worktree.py`'s `remove_worktree` had no timeout**, unlike every
   other git call in that module (`_run_git` wraps every call in
   `asyncio.wait_for(..., GIT_TIMEOUT_SECONDS)`). On Windows, a just-exited
   child process (the nested `pytest` subprocess `run_tests` spawns inside a
   worktree) can leave a file handle inside that worktree directory open for
   a moment after `communicate()` returns, and `git worktree remove --force`
   run immediately after can then block on that file lock — with no
   timeout at all, that stalled the *entire* worker/test process
   indefinitely, not just that one cleanup call. Fixed with a
   `_run_git_best_effort()` helper (timeout + `process.kill()`, but never
   raises — a leftover worktree directory is still just a minor annoyance,
   per that function's existing docstring).
2. **A nested `session_scope()` deadlock in `healer/ci_agent.py`, found by
   diagnosing a real hang via `pg_stat_activity`/`pg_locks` (not
   guesswork)**: an earlier version's `_give_up()` helper did a GitHub call
   *and* opened its own `session_scope()` to write the job's terminal
   status, and was itself called from *inside* the caller's own already-open
   `session_scope()` block (which had already flushed — but not yet
   committed — its own update to the exact same `heal_jobs` row). That's a
   genuine deadlock, not just slowness: the outer transaction was blocked in
   Python waiting for `_give_up()`'s coroutine to return, while `_give_up()`'s
   own inner transaction was blocked in Postgres (`pg_locks` showed a
   `transactionid` wait) on the outer transaction's still-open row lock —
   two connections, neither able to proceed, and no query-level timeout on
   either side to break it. Diagnosed by connecting directly with `psql` and
   reading `pg_stat_activity`/`pg_locks` while the hung process was still
   alive (`state = 'idle in transaction'` on one backend, waiting to hear
   back from application code stuck awaiting the other). Fixed by
   restructuring: `_prepare_job()` now does **all** of a job's DB writes for
   the "stop here" cases in one single `session_scope()` block and returns
   a plain-data verdict (`_JobContext` to proceed, `_GiveUp(pr_number,
   reason)` to open a fallback issue, or `None` to just stop) — never a
   GitHub call. `run_ci_heal_job` only calls the (now DB-free)
   `_open_needs_human_issue()` *after* that block has already committed and
   closed. If you add another "stop and maybe open an issue" branch to this
   function, keep that split — a DB write and an awaited external call must
   never share one open transaction.
   **Process hygiene note for future sessions**: an interrupted/backgrounded
   test run on Windows can leave its process tree (and, more importantly,
   its Postgres backend connections) alive well after the tool reports it
   "interrupted" — `TaskStop` on the specific task id reliably kills the
   tree (confirmed via `Get-CimInstance Win32_Process`), but a bare Ctrl-C
   from a user's own terminal earlier in this session did not. A hang that
   won't resolve is worth checking `pg_stat_activity`
   (`state = 'idle in transaction'` + another session's `wait_event =
   transactionid` waiting on it) before assuming it's just slow — `psql` is
   at `C:\Program Files\PostgreSQL\17\bin\psql.exe`, not on PATH in this
   environment's bash.

**A third issue, test-only but from the same "shared DB, global counters"
family**: `tests/test_healer_ci_agent.py` originally used hardcoded
`pr_number`s (301-304). `ci_fix_circuit_open` sums `attempt_count` globally
by `pr_number` across the whole shared `selfheal_test` DB (by design — see
above), so re-running this file repeatedly (as happened a lot while
diagnosing the two bugs above) left real committed attempts behind under
those same numbers, and a later run's circuit-breaker check tripped
immediately — not because of any code bug, but because the *test itself*
had effectively already used up its own budget in earlier runs. Same root
cause as the pre-existing "shared dev DB pollutes unscoped queries" note,
now hit for a `pr_number`-keyed counter specifically. Fixed by giving this
file its own `_random_pr_number()` (same pattern as `tests/
test_healer_circuit_breaker.py`'s). **Any new test that exercises a
globally-scoped counter (by fingerprint, pr_number, or job type) must
randomize the key it counts by, every time** — a hardcoded key is a ticking
time bomb the moment the test is run more than once against the same
`selfheal_test` database, including by a human re-running it manually while
debugging.

**A fourth, pre-existing (not Phase 5, not this session's new code) flaky
test found while re-running the suite for confirmation**:
`tests/test_mcp_confirmation.py::test_tampered_token_is_rejected` tampered
the token's *last* character specifically. Base64's final character in a
run whose length isn't a multiple of 3 bytes can encode fewer than 6 real
bits — the rest are padding bits `itsdangerous` ignores on decode — so some
single-character substitutions there are silent no-ops: the tampered string
differs, but decodes to the exact same bytes, and verification legitimately
still passes. Whether `'a'`/`'b'` land in the same decode-equivalence class
depends on that test run's random signature bytes, so this passed most runs
and failed rarely. Fixed by tampering a character in the middle of the
token's *payload* segment (before the first `.`) instead — no padding
ambiguity there, and itsdangerous signs across payload+timestamp before
attempting to decode either, so any single-byte change there deterministically
trips `BadSignature`. (Separately, `tests/test_healer_budget.py::
test_categories_are_independent` also failed once during this session's
unusually high number of consecutive full-suite reruns while diagnosing the
bugs above, then passed clean on every other run; `healer/budget.py`'s
`_get_or_create_row` genuinely filters by both `day` *and* `category`
— confirmed by reading it — so this was almost certainly `isolated_budget_
date`'s random-date space (16,800 slots) coincidentally colliding across two
of the many dozens of budget-touching tests run back-to-back today, not a
real category-isolation bug. Not fixed — the randomization is already the
correct mitigation for normal usage; today's collision odds were inflated by
this session's rerun count specifically. Worth knowing about if it ever
recurs, so it isn't mistaken for a regression.)

### Phase 5+ — Free mode (Claude Code CLI backend): DONE
A parallel implementation of the healer backend using Claude Code CLI instead
of the Anthropic SDK — same queue, same MCP tools, same guardrails, no API
key, no per-token billing.

Built (`healer/`, `core/`, `mcp_server/`):
- `healer/agent_free.py` (~990 lines): `run_claude_cli()` wraps the local
  `claude` CLI via `asyncio.create_subprocess_exec`/`shell`, passes prompt via
  stdin, parses JSON output (`-p --output-format json`), detects and raises
  exceptions for not-logged-in/usage-limit/timeout/malformed-JSON scenarios.
  `run_heal_job_free()` and `run_ci_heal_job_free()` parallel Phase 4/5's
  runtime/CI healing respectively, calling the CLI and using the *same* MCP
  `propose_patch`/`run_tests` tools for verification (verification is
  code-enforced, not CLI-claimed). Budget tracking via `audit_log` rows
  (action="cli_invocation"), not USD cost. System prompts unchanged from
  Phase 4/5 — the difference is invocation method, not prompting.
- `healer/worker.py` (modified): `_select_backend()` checks `USE_CLAUDE_CODE`
  setting at startup, imports and returns the appropriate `run_heal_job`/
  `run_ci_heal_job` from `agent_free` (free mode) or `runtime_agent`/
  `ci_agent` (API mode).
- `healer/anthropic_client.py` (modified): anthropic import moved inside
  `TYPE_CHECKING`, only imported when API mode's `build_anthropic_client()`
  is called — lazy, raises helpful "pip install .[api]" if missing.
- `core/config.py` (modified): added 10 settings: `use_claude_code` (default
  true), `claude_cli_path`, `claude_cli_timeout_s` (600), `claude_cli_max_turns`
  (30), `max_cli_calls_per_day` (50). Anthropic settings made optional (default
  `None`), only required in API mode. `.env` path made absolute (computes at
  module load time) to survive cwd changes in worktrees.
- `.mcp.json` (modified): server name changed to "selfheal", Python path made
  absolute (full venv path), allowing CLI spawned with cwd=worktree to still
  resolve the MCP server.
- `pyproject.toml` (modified): anthropic moved from base deps to [api] optional
  extra; still in [dev] for mypy TYPE_CHECKING verification.
- `.env.example` and `README.md` (modified): documented both modes, one-time
  setup (run `claude` once to log in), added new settings.

Key technical challenges and solutions:
- **Windows .cmd shim launch**: npm-installed `claude` resolves to `claude.cmd`,
  which `create_subprocess_exec` can't launch (WinError 193, no shell, no
  PATHEXT). Discovered and verified fix: use `create_subprocess_shell` with
  properly quoted path: `f'"{resolved}" {subprocess.list2cmdline(rest_args)}'`
  (path quoted, args handled by list2cmdline) — no outer wrapping, just
  straightforward quoting.
- **Environment variable isolation**: stripped ANTHROPIC_* env vars before
  spawning CLI so testing/dev vars don't leak and force API auth instead of
  subscription login.
- **Worktree absolute path handling**: core/config.py now computes `.env` path
  as absolute (`Path(__file__).resolve().parent.parent / ".env"`) to survive
  cwd changes when CLI spawned in worktree directories.
- **Regression test detection**: added `_touched_paths()` and
  `_regression_test_path()` to auto-detect which test file the diff touches,
  so `run_tests` only runs that file instead of entire suite (reduced test
  time from 3+ min to 4.5s per attempt).
- **Test DB isolation for CLI counters**: added `isolated_cli_call_date` fixture
  (random synthetic year 2200-2249 per test run) to prevent collision on shared
  `selfheal_test` DB when counting `audit_log` CLI invocations by date.

Real end-to-end validation run (job 316, `/trigger/validation` bug):
- All 3 attempts executed successfully (CLI ran, MCP tools called, git diffs
  applied, tests ran).
- No fix passed the regression test (expected for a validation bug with no
  obvious single-edit fix), so job marked FAILED and fallback issue opened
  (correct behavior).
- Proves entire flow works: CLI invocation → MCP round trip → verification →
  job state update → GitHub issue creation.

Test changes: added 750+ lines to `tests/test_healer_agent_free.py` covering
all CLI subprocess scenarios (Windows .cmd vs .exe, timeout, malformed JSON,
not-logged-in detection, environment filtering), plus 4 end-to-end scenarios
(all use real MCP client<->server, real git worktrees, real `git apply`, real
pytest). Anthropic/GitHub mocked as usual. All existing Phase 4/5 tests still
passing (no changes to those code paths).

Verified: `pytest` 251/251 passing, `ruff check` clean, `ruff format --check`
clean, `mypy core sentinel mcp_server healer` (strict) clean. Real end-to-end
job execution confirmed working.

### Phase 6 — UI: DONE
Built:
- `healer/auth.py`: single-admin auth. `ADMIN_PASSWORD_HASH` (argon2)
  verified via `argon2-cffi`, signed itsdangerous session cookies
  (`selfheal_session`, httpOnly/Secure-outside-dev/SameSite=Strict, 12h),
  `require_auth` FastAPI dependency (401 on missing/invalid/expired cookie),
  `authenticate()` wired to Phase 1's already-built `core/ratelimit.py`
  (`is_login_locked_out`/`record_login_attempt` — unused until now since
  nothing needed login before this phase's UI existed).
- `healer/notifier.py`: Slack webhook / SMTP notifications per SPEC.md
  NOTIFICATIONS (skips silently if unconfigured), plus a
  `set_socket_broadcaster`/`notify()` pair that also pushes a Socket.io
  "notification" event to every connected client — this is what lets
  Phase 7's `local_deploy.py` prove a rollback "notifies the chat" without a
  human watching a terminal.
- `healer/chat_agent.py`: chat message routing. **Design choice**: a
  regex-matched set of the exact questions/commands SPEC.md lists ("show
  stats", "what's the pipeline status", "why did CI fail on PR #N", "roll
  back <env>", "cancel workflow #N", "rerun workflow #N", "show me error
  #N") are answered directly from live MCP tool data — fast, deterministic,
  testable without a real LLM. Anything else falls back to the active AI
  backend (Claude Code CLI in free mode via `run_claude_cli`, restricted to
  a read-only `--allowedTools` subset — `trigger_rollback`/`cancel_workflow`
  are never in that list, so an injected instruction has no destructive tool
  to even attempt). Destructive actions (`trigger_rollback`, `cancel_workflow`)
  go through an in-memory per-chat-session `PendingConfirmation`: the server
  itself issues the confirmation token via `mcp_server/confirmation.py` only
  after the user replies "yes" — the LLM is never trusted to supply or
  fabricate a token. `as_tool_list()` unwraps the `mcp` SDK's `{"result":
  [...]}` structured-content wrapping for list-returning tools (discovered
  while testing `list_open_errors`/`list_workflow_runs` — a bare list return
  type gets wrapped in an object since MCP structured content must be a
  JSON object, not a bare array).
- `healer/agent_free.py`: added an `allowed_tools` override parameter to
  `run_claude_cli` (defaults to the existing `mcp__selfheal__*` used by heal
  jobs) so chat can pass its narrower read-only list — the only change to
  this already-tested Phase 5+ module.
- `healer/app.py`: the healer-pod FastAPI + Socket.io app. Runs
  `healer.worker.run_worker()` as a background `asyncio.Task` in its
  lifespan (same pattern sentinel-pod already uses for its prober/anomaly
  loops), so the worker and the chat/dashboard UI share one process/port —
  SPEC.md's "4 pods", not 5. Routes: `/healthz` (public), `/api/auth/*`
  (login applies the chat/login rate-limit bucket from `core/ratelimit.py`
  before checking the password), `/api/errors`, `/api/metrics`,
  `/api/health`, `/api/deployments`, `/api/pipeline` (all `require_auth` +
  read through a long-lived `MCPToolClient` connected once at startup),
  `/api/chat/session` + `/api/chat/history` (persist to `chat_sessions`/
  `chat_messages`), and a Socket.io server (`connect` validates the session
  cookie or an explicit `auth.token`, `chat_message`/`chat_reply` events).
  Static UI served from `/` + `/static/*` via `StaticFiles`.
- `web/`: React 18 + htm + Socket.io client, all from CDN, zero build step
  (`index.html`, `app.js`, `style.css`). Login page, dashboard (health/
  errors/pipeline panels, polling every 8-10s — see "Ambiguities resolved"
  below for why polling instead of full Socket.io push for these), AI chat
  (Socket.io, persisted history, confirmation hint), metrics page. Dark/light
  theme via CSS variables + `prefers-color-scheme` + a manual toggle
  persisted to `localStorage`. Responsive down to mobile width. Toasts driven
  by the Socket.io `notification` event.
- `scripts/hash_password.py`: argon2 hash generator for `ADMIN_PASSWORD_HASH`
  (SPEC.md SECURITY explicitly names this script).
- 25 new tests: `tests/test_healer_auth.py` (session cookie round-trip,
  401 without a cookie, successful/failed login, 5-failed-attempts lockout —
  reusing Phase 1's `core/ratelimit.py` for real against `db_session`),
  `tests/test_healer_app.py` (httpx `AsyncClient` over ASGI with `get_db`/
  `get_mcp_client` dependency-overridden — same pattern `tests/conftest.py`
  already uses for sentinel-pod/target_app; unauthenticated 401s, full
  login → dashboard → logout → 401-again flow, chat session/history),
  `tests/test_healer_chat_agent.py` (a real MCP client<->server round trip
  via `connect_in_memory`, GitHub mocked via `respx`: "show stats" against
  the real `get_metrics` tool, pipeline status, "why did CI fail on PR #12"
  with real failing-check data, showing a real seeded error, the full
  rollback confirm/cancel flow proving GitHub's dispatch endpoint is called
  exactly once only after "yes" and zero times after "no", and an injection
  test proving `--allowedTools` never includes `trigger_rollback` for the
  LLM-fallback path regardless of what the user's text asks for).

Verified (explicit Phase 6 checklist from SPEC.md):
- Unauthenticated access returns 401:
  `tests/test_healer_app.py::test_unauthenticated_metrics_returns_401` /
  `test_unauthenticated_errors_returns_401`.
- The chat answers "show stats" and "why did CI fail on PR #N" from real
  tool data, in free mode (the deterministic-intent path runs regardless of
  `USE_CLAUDE_CODE`, but is exactly the path SPEC.md's free-mode-chat
  checklist exercises): `tests/test_healer_chat_agent.py::
  test_show_stats_uses_real_metrics_tool` /
  `test_why_did_ci_fail_on_pr_reports_failing_checks`.
- A rollback only runs after "yes":
  `tests/test_healer_chat_agent.py::
  test_rollback_requires_yes_confirmation_before_running` /
  `test_rollback_cancelled_with_no_never_calls_github`.
- Mobile width + dark mode: `web/style.css` uses CSS variables redefined
  under both `prefers-color-scheme: dark` and an explicit `data-theme`
  toggle, a single responsive breakpoint at 480px, and no fixed widths wider
  than the viewport (manually reviewed; no headless-browser test harness
  exists in this repo to automate a visual check).

`ruff check .`/`ruff format --check .` clean repo-wide. `mypy core sentinel
mcp_server healer` (strict) clean. `pytest` 270/271 (one pre-existing,
environment-only failure: `test_get_health_reports_unreachable_pods_when_
nothing_is_running` expects nothing listening on ports 8001-8003, which
fails only when pods happen to already be running locally in another
terminal — not something Phase 6 introduced or can fix in-repo).

**Ambiguities resolved this phase**:
- **Dashboard live updates are short-interval polling (8-10s), not
  Socket.io push, for errors/pipeline/health panels.** SPEC.md says "live
  updates via Socket.io: progress timeline per heal job" — but wiring
  Phase 4/5's `runtime_agent.py`/`ci_agent.py`/`worker.py` to emit
  Socket.io events mid-attempt would mean touching already-tested,
  contract-critical Phase 4/5 code for a UI-only concern, which CLAUDE.md's
  "don't modify completed phases without strict need" convention weighs
  against. Socket.io is used for genuinely real-time things instead: chat
  (`chat_message`/`chat_reply`) and `healer/notifier.py`'s cross-cutting
  `notify()` broadcast (budget pauses, rollback results, anomaly alerts —
  events that already funnel through one shared function, so wiring them to
  Socket.io was a single, safe, additive change). If per-attempt job
  progress push is wanted later, add a `notifier.notify()`-style call at
  each stage inside `runtime_agent.py`/`ci_agent.py` deliberately, as its
  own reviewed change.
- **`healer/app.py` queries MCP tools for dashboard data, not the DB
  directly**, even though the healer-pod process already has a normal DB
  session available (`core.db.get_db`). This keeps exactly one code path
  (mcp_server's tools) responsible for shaping "what an error/pipeline
  run/deployment looks like as JSON", so the dashboard, the chat, and the AI
  agent can never disagree about a field name — already caught one such
  mismatch during testing (`list_workflow_runs` returns `workflow_name`,
  not `workflow`).
- **`mcp`'s structured content wraps a bare-list tool return in `{"result":
  [...]}`** (JSON-RPC structured content must be a JSON object, never a bare
  array) — discovered by testing `list_open_errors`/`list_workflow_runs`
  through a real `connect_in_memory` round trip, not assumed. Added
  `healer/chat_agent.py:as_tool_list()` to unwrap this consistently in both
  `chat_agent.py` and `app.py` — `get_error`/`get_metrics`/single-object
  tools are unaffected since they already return a plain dict.
- **Chat's pending destructive-action confirmation is an in-memory,
  per-process dict** (`healer/chat_agent.py:_pending`), not a DB table or a
  new `chat_sessions` column. A lost "waiting for yes" on a healer-pod
  restart is safe to lose (the user just re-issues the rollback/cancel
  command) and this avoids a schema migration for Phase 6 UI-only state.
- **Login rate limiting reuses the existing token-bucket `check_and_consume`
  as a per-IP soft cap (10 attempts / 60s) in addition to**, not instead of,
  the hard 5-failures/15-minute lockout in `core/ratelimit.py`'s
  `is_login_locked_out` — the two mechanisms serve different purposes
  (softly slow down a scripted brute force vs. hard-lock an account after
  genuine repeated failures) and SPEC.md's SECURITY section calls for both
  ("rate limiting ... on the chat, login and ingest endpoints" plus
  separately "lock out login for 15 minutes after 5 failed attempts").

### Phase 7 — Ship: DONE
Built:
- `.github/workflows/ci.yml`, `ci-failure.yml`, `deploy.yml`, `rollback.yml`,
  `health-check.yml` per SPEC.md's CI/CD PIPELINE section, plus
  `scripts/ci_webhook_notify.py` — a dependency-free (stdlib-only) HMAC
  signer/poster shared by every workflow to notify `HEALER_WEBHOOK_URL`,
  verified byte-for-byte compatible with `core/hmac_utils.py`'s `t=<ts>,
  v1=<hex_hmac>` scheme (see "Ambiguities resolved").
- `deploy/systemd/selfheal-{app,sentinel,mcp,healer}.service`: `Restart=
  on-failure`, `MemoryMax=` (200M for app/sentinel/mcp, 300M for healer
  since it also spawns the Claude Code CLI as a child process),
  `ProtectSystem=strict` + `ReadWritePaths=/opt/selfheal`.
- `deploy/Caddyfile`: reverse-proxies only sentinel-pod's `/webhooks/ci`
  (+its own `/healthz`) and healer-pod (chat/dashboard UI + `/socket.io/*`)
  publicly; app-pod and mcp-pod stay localhost-only, reached only by other
  pods.
- `scripts/provision_vm.sh`: idempotent Ubuntu provisioning (Python 3.11,
  tuned Postgres, Caddy, Claude Code CLI, `deploy` user, `/opt/selfheal`
  layout, systemd units, ufw, unattended-upgrades). Every step guards on
  current state before acting, per its own docstring.
- `scripts/smoke_test.py`: healthz for app/sentinel/healer, MCP-reachability
  for mcp-pod (see "Ambiguities resolved" — mcp-pod has no `/healthz`), plus
  one real contract endpoint (`/items/top`). `--force-fail` exists solely so
  `local_deploy.py`'s rollback path can be exercised on demand without
  needing a genuinely broken pod.
- `scripts/local_deploy.py`: the same atomic-release/migrate/smoke-test/
  rollback flow as `deploy.yml`, run against localhost. Builds a release via
  `git archive` into `local_deploy_root/releases/<sha>/`, copies `.env` from
  `shared/`, runs `alembic upgrade head` from *inside* that release
  directory (so `python -m alembic` resolves `core`/`alembic` from that
  exact snapshot — real atomic-release semantics, not just a symlink prop),
  flips a `current.txt`/`previous.txt` marker pair (see "Ambiguities
  resolved" for why markers instead of a real symlink), runs
  `smoke_test.py`, and on failure flips `current.txt` back to
  `previous.txt` and writes a `deployments` row + calls `healer.notifier.
  notify()`.
- `scripts/break_ci_demo.py`: creates a branch with one legitimately broken
  assertion (not a deletion/xfail — SPEC.md's `break_ci_demo.py`), for
  demoing the CI-fix loop end to end.
- `scripts/tunnel_note.md`: SSH reverse-tunnel option for exposing the
  webhook without a cloud VM.
- `Procfile` (app/sentinel/mcp/healer, `honcho start`).

**Verified for real, not just written** (SPEC.md Phase 7 checklist: "a
forced failing smoke test triggers a rollback, and the chat reports it"):
ran `scripts/local_deploy.py` three times against the real dev DB with
healer-pod (and leftover app/sentinel/mcp pods already running locally)
live:
1. First deploy of HEAD (`9230a5a`): smoke test passed for real (`OK` on
   all 4 healthz/reachability checks + `/items/top`), `deployments` row
   `status=deployed` written (id 23).
2. A second commit deployed with `--force-fail-smoke-test`: smoke test
   correctly failed, `current.txt` flipped back to the first release's sha,
   `deployments` row `status=rolled_back` written (id 24), `healer.notifier.
   notify()` called without raising (Slack/SMTP unconfigured in this dev
   environment so both skip silently by design — confirmed by reading
   `notifier.py`, not assumed).
3. Confirmed via a direct DB query (`SELECT * FROM deployments ORDER BY id
   DESC LIMIT 3`) that both rows exist with the expected sha/status/
   timestamps, and that `local_deploy_root/current.txt` ends up pointing at
   the *first* (good) sha after the forced failure, not the broken one.
The demo commit used for step 2 was reset via `git reset --hard HEAD~1`
immediately after (never pushed, never left in history).

`ruff check .`/`ruff format --check .` clean repo-wide. `mypy core sentinel
mcp_server healer` (strict) clean — `scripts/` is intentionally outside the
mypy-strict scope (see CLAUDE.md Conventions; same as Phase 1-6 scripts).
`pytest`: 270/271 (same one pre-existing environment-only failure as Phase
6, unrelated to Phase 7's changes — nothing in this phase touches Python
application code, only new scripts/workflows/systemd/Caddy files, so no new
tests were needed for it beyond the real, manual local_deploy.py runs above).

**Ambiguities resolved this phase**:
- **mcp-pod has no `/healthz`** (a pre-existing Phase 3 gap: it speaks MCP's
  streamable-HTTP protocol at `/mcp`, not a plain FastAPI app with its own
  routes, and `mcp.run(transport="streamable-http", ...)` doesn't expose a
  way to bolt on an extra REST route without reaching into the `mcp` SDK's
  internals). Rather than modify Phase 3's `mcp_server/server.py` under
  Phase 7 time pressure, `scripts/smoke_test.py` checks mcp-pod for bare
  reachability (`GET /mcp` returns *any* HTTP response, even 4xx) instead of
  requiring a 200 from `/healthz` — proves the process is up and listening,
  which is all a smoke test needs. `mcp_server/tools/deploy.py:get_health`'s
  existing (Phase 3) `_probe_healthz` still hits `/healthz` for mcp-pod too
  and will always report it unreachable; fixing that for real is a Phase 3
  change out of scope here — noted for a future session.
- **Windows dev boxes can't reliably create real symlinks** (needs Developer
  Mode or elevated privileges), so `scripts/local_deploy.py`'s "current"/
  "previous" pointers are plain text marker files, not the real `ln -sfn`
  symlink `deploy.yml`'s actual SSH-to-Ubuntu flow uses. Functionally
  equivalent for the demo (atomic swap = one file write), documented here so
  nobody mistakes the marker-file approach for what runs in production.
- **`local_deploy.py` reuses the existing dev `.venv`** rather than creating
  a fresh venv per release (unlike `deploy.yml`'s real flow, which does
  `python3.11 -m venv` per release on the VM) — this script is for
  demonstrating/testing the release-swap-and-rollback *mechanics* locally,
  not dependency isolation, and creating a full new venv per local demo run
  would be slow for no benefit here.
- **`scripts/ci_webhook_notify.py` is deliberately dependency-free (stdlib
  `urllib`/`hmac`/`hashlib`/`json` only)**, not a thin wrapper around
  `core/hmac_utils.py`, so `ci.yml`'s very first step (reporting "CI
  started") can run before `pip install -e ".[dev]"` has happened in that
  job, and so no workflow needs a Python environment with the full project
  installed just to send one signed HTTP POST.

### Phase 8 — Prove: DONE
Built:
- `scripts/measure_ram.py`: finds each pod by the port it's listening on via
  `psutil.net_connections` (cross-platform, no `netstat`/`ss` shelling out)
  and reports real RSS.
- `scripts/export_metrics.py`: dumps `core/metrics.py:get_metrics_summary`'s
  real output (the same function backing the `get_metrics` MCP tool and
  chat's "show stats") to `metrics.json`.
- `README.md` rewritten: Mermaid architecture diagram (4 pods + Postgres +
  GitHub, mirroring SPEC.md's ARCHITECTURE section), setup, free-vs-API mode
  (extended, not duplicated, from the Phase 5+ version), running the pods,
  a 5-step demo walkthrough (runtime bug, silent/contract bug, broken CI,
  chat, forced-rollback), the cloud deploy guide with an explicit GitHub
  secrets/variables table, measured RAM, real metrics, and a recruiter
  highlights section.

**Real measurements, not estimates**:
- Started all 4 pods for real (`uvicorn`/`python -m mcp_server.http_main`),
  confirmed each `/healthz` (mcp-pod: `/mcp` reachability, see Phase 7 log),
  let them idle ~10s, then ran `scripts/measure_ram.py`: **app 107.1MB,
  sentinel 107.7MB, mcp 119.8MB, healer 144.9MB, total 479.5MB** — real
  `psutil` RSS, written to `ram_measurement.json` (gitignored).
- **This exceeds SPEC.md's 300MB target, measured honestly rather than
  hidden or fudged.** Root cause, documented in the README rather than
  glossed over: this measurement is on **Windows**, not the Ubuntu VM
  SPEC.md's constraint actually targets. Each pod is a fully separate
  Python process; Windows doesn't give separate processes copy-on-write
  shared pages for the same loaded libraries the way Linux's `fork()` model
  does, and each pod independently loads a full FastAPI/SQLAlchemy/Pydantic/
  structlog stack. This is a real, known Windows-vs-Linux Python RSS gap,
  not a code defect in this repo — but it's not verified on Linux either,
  since no Ubuntu VM was available this session. **Flagged as a concrete
  follow-up**: re-run `scripts/measure_ram.py` on a real (or even a
  throwaway) Ubuntu VM after `provision_vm.sh`, and update the README's
  numbers — the script and the acceptance criterion are both real and
  ready, only the Linux measurement itself is outstanding.
- Triggered `/trigger/zero`, `/trigger/key`, `/trigger/none_lookup` against
  the live app-pod/sentinel-pod pair, then ran `scripts/export_metrics.py`
  against the real dev DB: `metrics.json` (gitignored, regenerable) with
  real `contract_violation_catches: 8`, `rollback_count: 2` (from Phase 7's
  two real `local_deploy.py` runs), `total_cost_usd: 6.8151`, and a real
  `errors_by_type` breakdown — copied into the README's Metrics section.

Verified (explicit Phase 8 checklist from SPEC.md): architecture diagram
present (Mermaid, GitHub-native rendering), setup instructions, free-vs-API
mode explained, cloud deploy guide referencing `provision_vm.sh` +
`local_deploy.py`, measured RAM (real, both the number and its Windows
caveat), `metrics.json` from a real demo run, demo walkthrough, recruiter
highlights grounded in what's actually built (no marketing claims beyond
what CLAUDE.md's phase logs can back up).

`ruff check .`/`ruff format --check .` clean repo-wide. `mypy core sentinel
mcp_server healer` (strict) clean. `pytest`: **271/271 passing** (the one
environment-only flake from Phase 6/7's logs was a leftover process on a
pod's port from an earlier manual session — gone once those processes were
stopped for the RAM measurement above, confirming it really was
environmental and not a latent bug).

**This closes SPEC.md's BUILD ORDER.** All 8 phases are done. One
honestly-reported open item for a future session (doesn't block the
acceptance criteria that *can* be verified without paid infrastructure): a
real Ubuntu VM RAM re-measurement (this session only had a Windows dev
machine).

### Post-Phase-8 — real end-to-end free-mode PR: DONE

**Real PR opened by the healer, in free mode, no paid API**:
[PR #10](https://github.com/Deepak20466/ai-self-healing-app/pull/10) —
`healer/agent_free.py`'s `run_heal_job_free` correctly diagnosed and fixed
Phase 2's seeded timezone contract violation (`/orders/102/delivery-
estimate`): `bugs.py:estimate_delivery_date` read `order.created_at.date()`
directly, and Postgres normalizes `timestamptz` to UTC on read regardless of
insert offset — the fix converts to the storefront timezone first
(`.astimezone(STOREFRONT_TZ).date()`), with a new self-contained regression
test (`apps/target_app/test_delivery_estimate_timezone.py`) proving it
failed before and passes after. One attempt, 28 CLI turns, ~$2.15 of Claude
subscription usage, zero Anthropic API cost. (The PR's diff looks huge on
GitHub only because its branch base predates this session's Phase 6-8
merges — GitHub is diffing against a stale base; the healer's actual change
is `bugs.py` (8 lines) + the new test file (44 lines).)

**Two more real, previously-undiscovered bugs found and fixed this
session** (on top of Phase 5+'s already-fixed circuit-breaker and
job-attempt-counting bugs), both blocking every real free-mode heal
attempt:

1. **The `cmd.exe` shim launch fix from Phase 5+ was itself broken.**
   `create_subprocess_exec` flattens any argv list into one command-line
   string via `list2cmdline` before calling `CreateProcess` — Windows has no
   real argv. The Phase 5+ fix pre-built a fully-quoted `cmd.exe /d /s /c
   "<cmd>"` string and passed it as a single argv *element*, which
   `list2cmdline` then re-escaped (corrupting the embedded quotes) when
   flattening the whole list a second time — reproduced live as "the
   network path was not found." Fixed in `healer/agent_free.py` by
   switching to `asyncio.create_subprocess_shell` with that same pre-built
   string: shell mode passes it straight to `CreateProcess` with no second
   quoting pass. Verified directly against a real worktree + real `claude`
   CLI call, not just the mocked unit test (which never caught this, since
   it mocks `create_subprocess_exec` entirely and so never exercises
   `list2cmdline`'s real behavior).
2. **`.mcp.json`'s stdio server broke inside a worktree.** The CLI
   subprocess always runs with `cwd` set to the fix-attempt's git worktree.
   `.mcp.json` spawned `python -m mcp_server.server` as a stdio child of
   that same CLI process, inheriting the same `cwd` — and `-m` prepends the
   *current directory* to `sys.path`, so that subprocess imported the
   worktree's own checked-out copy of `mcp_server/sandbox.py` (a worktree is
   a full checkout) instead of the real repo's copy. `sandbox.py`'s
   `REPO_ROOT = Path(__file__).resolve().parents[1]` then resolved to the
   *worktree* root, so every `resolve_worktree_dir` call computed
   `WORKTREES_ROOT` as `<worktree>/worktrees` — which never exists — and
   every `propose_patch`/`run_tests` call failed with "Worktree does not
   exist", for the entire duration of every attempt. Reproduced directly by
   importing `mcp_server.sandbox` with `cwd` set to a real worktree.
   Fixed by pointing `.mcp.json` at mcp-pod's already-running HTTP endpoint
   (`http://127.0.0.1:8003/mcp`) instead of spawning a fresh stdio server
   per CLI call — this also matches the real 4-pod production architecture
   (mcp-pod is already a long-running process with the correct `REPO_ROOT`
   baked in at its own startup, decoupled from any CLI's `cwd`). Tradeoff:
   using `.mcp.json` for interactive Claude Code work in VS Code now also
   requires mcp-pod running on port 8003, not just a bare `python -m
   mcp_server.server`; worth it to eliminate a cwd-dependent footgun that
   silently broke 100% of real free-mode heal attempts.

Both bugs were invisible to the existing mocked test suite (mocking
`create_subprocess_exec`/`run_claude_cli` entirely means neither
`list2cmdline`'s real flattening behavior nor `.mcp.json`'s real subprocess
resolution is ever exercised) — they only surfaced by actually running a
live job against the real `claude` CLI. If free-mode heal attempts start
silently failing again, re-verify both of these directly against a real
worktree before assuming the bug is elsewhere.

### Post-Phase-8 follow-up — stale worktree base + PR #10 cleanup: DONE

**Real bug**: `healer/worktree.py:create_worktree` based every new autofix
branch on the local `main` ref, never fetching first. This process's own
local `main` was 9 commits behind `origin/main` (Phase 5+ through Phase 8
were committed locally but never pushed until this follow-up), so PR #10's
branch forked from a stale point and its diff included every one of those
unrelated commits on top of the real 2-file fix. Fixed: `create_worktree`
now always `git fetch <remote>` first and bases on `<remote>/main` (default
`origin/main`), never the local ref. (`create_worktree_for_branch`, used by
CI-fix jobs, already fetched before checkout — no change needed there.)
Regression test: `tests/test_healer_worktree.py::
test_create_worktree_fetches_and_bases_off_the_remotes_latest_main` advances
a fake remote's `main` to a commit the local repo has never seen and asserts
the new worktree lands on it, proving the fetch is real and not just a
local-ref assumption.

**Cleanup, one-time**: pushed local `main` to `origin` (a clean fast-forward,
`origin/main` was a strict ancestor — never a force-push to `main`), then
rebased PR #10's branch (`autofix/0a9375e7f2e4-325`) onto the now-current
`origin/main` and force-pushed *that branch only*. Confirmed via the GitHub
API: PR #10 now shows exactly 2 changed files (`apps/target_app/bugs.py`,
`apps/target_app/test_delivery_estimate_timezone.py`, +52/-8), matching the
healer's actual fix.

### Post-Phase-8 — public demo via Cloudflare Tunnel: DONE

No cloud VM available, so `scripts/start_public_demo.ps1`/
`stop_public_demo.ps1` expose the local pods publicly using Cloudflare quick
tunnels (`cloudflared tunnel --url ...` — no account, no Docker, no DNS).
**Only healer-pod (8000, UI/chat) and sentinel-pod's `/webhooks/ci` (8002)
are tunneled**; the MCP server (8003) is never exposed since it has no auth
of its own (it trusts being reachable only from the same host as the
healer), and neither is the target app (8001) or Postgres (5432).
`start_public_demo.ps1` refuses to start if `ADMIN_PASSWORD_HASH` isn't a
real argon2 hash, `SESSION_SECRET` is unset, or `AUTO_MERGE=true` — see that
script's `Test-EnvLooksReal`. `HEALER_WEBHOOK_URL`/`PUBLIC_URL` GitHub repo
variables are updated to that run's tunnel URLs automatically (quick tunnel
URLs are ephemeral, a new one every restart); `DEPLOY_HOST` is deliberately
left alone/unset so `deploy.yml` keeps skipping cleanly, matching Phase 7's
existing "skip cleanly when unconfigured" guardrail.

**Real bug found and fixed**: `.env`'s `ENVIRONMENT=development` default
suppresses the session cookie's `Secure` flag
(`healer/app.py:140: secure=settings.environment != "development"`) — fine
for `localhost`, but wrong the moment the app is reachable over the
tunnel's real HTTPS. Set `ENVIRONMENT=production` before running a public
demo (the start script doesn't do this for you, since it can't tell dev
intent from demo intent — do it once in `.env` and leave it).

**Verified for real through the live tunnel** (not just locally): a wrong
login password returns 401 with no cookie set; unauthenticated GETs to
`/api/metrics`, `/api/errors`, `/api/health`, `/api/deployments`,
`/api/chat/history` all return 401; a correctly-HMAC-signed `/webhooks/ci`
POST (built with `core.hmac_utils.sign_payload`, header `X-Signature`, exact
byte-for-byte body — a heredoc's trailing newline silently broke the
signature during testing, worth remembering if a manual webhook test ever
mysteriously 401s) returns 200 and is recorded; an unsigned or
tampered-signature request returns 401. `GITHUB_REPO`'s `.env` value is
`Deepak20466/ai-self-healing-app`; `sentinel/schemas.py:CIWebhookPayload`
requires `run_id`/`workflow`/`branch`/`sha`/`status` (not a raw GitHub
Actions webhook shape) — use that shape for any future manual webhook test
against this endpoint.

### Post-Phase-8 — live acceptance verifier: DONE

`scripts/verify_all.py` tests the live running system (all 4 pods, the
public Cloudflare tunnel, real GitHub) against SPEC.md's ACCEPTANCE
CRITERIA and prints a PASS/FAIL/SKIPPED table — see `VERIFICATION.md` for
the full result (63 PASS, 1 FAIL, 6 SKIPPED, stable across repeat runs).
**Never invokes the Claude Code CLI** — real AI usage costs the operator's
subscription, so this script is meant to be re-run freely (CI, after any
change) without ever spending that budget. Anywhere the acceptance criteria
need a real AI-produced fix, it cites *existing* evidence (PR #10, the
mocked e2e test suite) rather than generating new evidence itself.

**The one real FAIL is RAM over budget on Windows (~500MB vs. 300MB)** —
already documented as a platform gap in Phase 8's log, not a new bug;
unresolved because no Linux VM was available this session either.

**Real bugs found and fixed while building/running the verifier**:
1. `HealJob` already has a `fingerprint` column directly on the row — no
   need to look up the source `Error`/`ContractViolation` to get it (an
   earlier draft of the verifier tried to, incorrectly, via a nonexistent
   `error_id` attribute).
2. `healer/budget.py`'s `record_spend`/`is_budget_paused` have no date
   override parameter (today's date is implicit) — the same
   `isolated_budget_date` monkeypatch pattern tests/conftest.py already uses
   is required for *any* caller (including a one-off script, not just
   pytest) that wants to probe the budget cap without touching the real
   production `daily_spend` row for today.
3. `python-socketio`'s `AsyncClient` silently requires `aiohttp` to be
   installed for its default HTTP transport — with it missing, `sio.connect()`
   fails with a generic `ConnectionError: Unexpected connection error` that
   gives no hint the actual cause is a missing dependency, not a network or
   auth problem. Added `aiohttp>=3.10` to `[dev]` (only needed for this kind
   of out-of-process client testing — the server side, python-socketio's
   ASGI app in `healer/app.py`, has never needed it).
4. `gh run list --limit 3` on `main` can miss the actual `CI` run if other
   workflows (`Deploy`, `CI failure -> AI fix`) also ran recently on the
   same branch and sort ahead of it — fixed by filtering with
   `--workflow ci.yml` directly instead of over-fetching and filtering
   client-side.
5. MCP tool parameter names don't always match the obvious guess:
   `list_files` takes `glob` (not `directory`), `search_code` takes
   `pattern` (not `query`), `get_recent_commits` takes `n` (not `limit`).
   Worth checking `mcp_server/tools/*.py`'s actual signatures directly
   rather than assuming from the tool name.

**Design choice**: bug attribution (which `Error` row belongs to which
seeded `/trigger/*` bug) matches on `Error.function_name`, not
`exception_type` — the timeout bug's exception class varies by platform
(`ConnectTimeout` here), while `function_name` (`check_item_price`, etc.) is
stable and directly names the exact function in `bugs.py` that raised it.

### Post-Phase-8 — live CI self-healing, run for real end to end: DONE

**[PR #14](https://github.com/Deepak20466/ai-self-healing-app/pull/14)** —
`scripts/break_ci_demo.py` broke a real test assertion, pushed to a new
branch/PR, `ci.yml` failed for real, `ci-failure.yml` notified the healer
over the public Cloudflare tunnel from the earlier "public demo" session,
and the healer's free-mode CI-fix agent (`run_ci_heal_job_free`) correctly
classified the failure as real (not flaky), pushed a
[fix commit](https://github.com/Deepak20466/ai-self-healing-app/commit/5142307175294aa32cc0cd8bbce157b4fd188dd8)
restoring the broken assertion, and posted a PR comment with its root-cause
analysis and test evidence. CI went green automatically on the fix commit.
One real Claude Code CLI attempt, no mocking anywhere in the loop.

**Three previously-latent infrastructure bugs found and fixed this run —
all invisible until a real webhook/queue/worker cycle actually ran, exactly
like the two Windows-specific `agent_free.py`/`.mcp.json` bugs the original
PR #10 run uncovered:**

1. **GitHub repo had zero secrets configured.** `HEALER_WEBHOOK_SECRET` was
   never set as a GitHub Actions secret (only ever in local `.env`), so
   `ci-failure.yml`'s webhook step silently no-op'd (`ci_webhook_notify.py`
   skips cleanly when `WEBHOOK_SECRET` is empty — by design, so a workflow
   never fails over a missing notification config — but that also means a
   *forgotten* secret produces zero visible symptoms). Fixed by `gh secret
   set HEALER_WEBHOOK_SECRET` from the local `.env` value.
2. **Dev machine's system clock was ~13 minutes behind real UTC.** The
   webhook's HMAC scheme (`core/hmac_utils.py`) has a 5-minute replay
   window; GitHub Actions signs with the real time, so every otherwise-valid
   signed request was rejected as "outside the replay window" — visible as
   401s in sentinel's access log even after the secret was correctly set.
   `w32tm /resync` failed (no NTP path in this sandboxed environment) and
   directly setting the clock was correctly blocked by Claude Code's own
   "Security Weaken" guardrail — the fix was simply waiting for Windows'
   own background time sync to catch up, then re-running the failed CI job
   so a fresh signature was generated inside the (now correct) replay
   window. **If a webhook mysteriously 401s despite a correct secret, check
   clock drift before assuming the secret is wrong.**
3. **`ci-failure.yml` sent the wrong branch/run_id to the webhook — a real,
   previously-undiscovered bug, not an environment issue.** GitHub Actions
   does not allow a step's `env:` block to override its own reserved
   `GITHUB_*` names (`GITHUB_RUN_ID`, `GITHUB_REF_NAME`, `GITHUB_SHA`,
   `GITHUB_WORKFLOW`) — the runner injects its own values for whichever
   workflow is *currently executing* (`ci-failure.yml` itself, since it's
   `workflow_run`-triggered) after step env is applied, silently discarding
   the override. Every field except `PR_NUMBER` (a genuinely custom name)
   described `ci-failure.yml`'s own run instead of the CI run that actually
   failed — branch came through as `"main"` (ci-failure.yml's own checkout
   ref) instead of the PR's real branch, which made
   `healer/worktree.py:create_worktree_for_branch` try to check out `main`
   (already in use by the main checkout) and crash before a single Claude
   CLI call. Fixed with non-reserved `SOURCE_*` env var names in
   `ci-failure.yml`, with `ci_webhook_notify.py:build_payload()` preferring
   them and falling back to the ambient `GITHUB_*` vars (correct for
   `ci.yml`'s own start/finish calls, which never set `SOURCE_*`). Added
   `tests/test_ci_webhook_notify.py`. **Never trust a step's `env:` block to
   override a reserved `GITHUB_*` variable — use a custom name instead.**
4. **Worker busy-spin/starvation when the global hourly heal-job cap is
   open.** `healer/worker.py:_process_next_job` requeued the capped job but
   returned `True` ("a job was claimed, retry immediately") instead of
   `False` — since `dequeue_heal_job` is FIFO, the *same* just-requeued job
   was immediately dequeued again, hit the cap again, forever: 100% CPU,
   thousands of identical `worker.global_hourly_cap_hit` log lines, and
   every other queued job (including this run's own PR #14 fix job)
   starved for as long as the cap stayed open. This session's own heavy
   `verify_all.py` testing (15+ runtime-error jobs in under an hour) is what
   actually tripped the real cap (`max_heal_jobs_per_hour_global=10`) and
   exposed the bug — a legitimate guardrail working as designed, just with
   a broken backoff path. Fixed to return `False` so the main loop's
   existing notify/fallback wait applies instead of spinning. Added
   `tests/test_healer_worker.py`.

**Process note, not a code bug**: restarting healer-pod mid-flight (to pick
up fix #3/#4 above) killed an in-progress attempt for a *different* job
without that job's exception handler ever running, leaving it orphaned in a
non-terminal status (`ci_fixing`) that `dequeue_heal_job` can never
re-claim (it only selects `QUEUED` rows) — manually reset that job back to
`QUEUED` to recover. Separately, this session also manually reset a
*different*, genuinely in-flight job's bookkeeping by mistake, based on a
misread of `started_at` — the running attempt's own in-memory state was
unaffected (it doesn't re-read `attempt_count` mid-flight) and it correctly
overwrote the premature reset with the real outcome, but it's a good
reminder: **before resetting an in-flight heal_job's status, correlate
`started_at` against the current healer-pod process's actual start time
and check for live `claude.exe` processes** — a recent `started_at` from
*before* your own restart is a strong sign the current process is still
legitimately working it, not an orphan from a killed one.

### Multi-app support (Step 1 of the multi-app/multi-language extension) — IN PROGRESS

Extending beyond the single hardcoded `apps/target_app/` to support any
number of registered apps. Not in SPEC.md's original scope (SPEC.md
predates this extension) — driven by a follow-up request. Sub-steps done
so far, each committed and pushed separately with full lint/mypy/pytest
green:

1. **`monitored_apps` table + YAML config** — `alembic/versions/
   0003_monitored_apps.py` creates the table and a nullable `app_id` FK on
   `errors`/`contract_violations`/`heal_jobs` (nullable because pre-existing
   rows predate multi-app support; every new row going forward sets it in
   application code). `config/monitored_apps.yaml` is the human-editable
   source of truth, synced into the DB by `core/monitored_apps.py:
   sync_monitored_apps()` (upsert by `name`, idempotent) — run automatically
   by `pytest_sessionstart` and by `scripts/sync_monitored_apps.py` for a
   real environment. `apps/target_app` is registered as the first row with
   behavior unchanged.
2. **Errors/violations/heal_jobs attributed to an app** — `core/queue.py:
   enqueue_heal_job` and `sentinel/storage.py`'s `record_error`/
   `record_contract_violation` take an `app_id`. `sentinel/app.py` resolves
   the reporting app from an *optional* `Authorization: Bearer <token>`
   header (`core/monitored_apps.py:get_app_by_ingest_token`) — deliberately
   not enforced with a 401, so an app that hasn't set a token still gets
   captured, just unattributed (`app_id=None`), identical to pre-multi-app
   behavior. `SentinelClient`/`SyncSentinelClient` send this header from the
   new `SENTINEL_INGEST_TOKEN` setting. The prober attributes contract
   violations to `target_app`'s own row by a one-time name lookup (it only
   ever probes target_app's contracts.py currently).
3. **Flask and Django middleware** — `sentinel/sync_client.py` (blocking
   httpx.Client counterpart to `SentinelClient`, since Flask/Django request
   handling is itself synchronous), `sentinel/flask_middleware.py` (hooks
   Flask's `got_request_exception` signal — **must pass `connect(...,
   weak=False)`**, a real bug found while testing: the default weak
   reference lets the local closure receiver get garbage-collected right
   after `init_sentinel_flask()` returns, silently disconnecting it),
   `sentinel/django_middleware.py` (implements `process_exception`, Django's
   dedicated unhandled-view-exception hook). Both are optional deps
   (`flask`, `django` — added to `[dev]` so their tests run; a monitored app
   using one of these only needs that one framework installed, not both).
   `sentinel/capture.py:build_captured_error` gained an optional
   `in_app_markers` param so a non-target_app integration prefers its own
   in-app frames.
4. **`propose_patch`/`run_tests` scoped by the job's own app** —
   `mcp_server/sandbox.py`'s `check_writable`/`check_diff_paths_writable`
   take `allowed_prefixes` (a list, not a single string) now.
   `propose_patch` derives it from the heal_job's `app_id` ->
   `MonitoredApp.allowed_write_paths` for `runtime_error`/
   `contract_violation` jobs (falling back to the legacy
   `apps/target_app/` default when `app_id` is `None`, i.e. a job from
   before multi-app support) — still resolved server-side from the DB only,
   never from a caller parameter, same principle as the original
   single-app restriction. `run_tests` gained an optional `heal_job_id`: a
   non-Python app's own `test_command` is run verbatim via the new
   `mcp_server/git_utils.run_test_command` (a generic shell-command runner,
   for the upcoming Node/Go example apps) instead of `python -m pytest`;
   omitting `heal_job_id`, or a Python app, keeps the exact pre-existing
   pytest code path unchanged. `tests/test_mcp_tools_code.py::
   test_propose_patch_scopes_a_multi_app_job_to_its_own_app` is the
   isolation test proving app A's write scope never leaks to app B, even
   when both live under `apps/`.

**Ambiguity resolved**: healer worktree creation and PR target repo need
**no change** for multi-app support as implemented so far. Every app
registered via `config/monitored_apps.yaml` so far (`target_app`, and the
planned `examples/node_app`/`examples/go_app`) lives in a subdirectory of
this *same* repo (`MonitoredApp.local_repo_path` documents this
convention directly on the model) — so there is exactly one
`github_repo`/`GITHUB_TOKEN` in practice, and the existing single-repo
`mcp_server/github_client.py` + `healer/worktree.py` already work
unmodified once write-scope is correctly restricted to the app's own
subdirectory (done in sub-step 4 above). `MonitoredApp.github_repo` exists
per-row for forward compatibility/clarity, not because it's exercised
differently today.

**Update (see "Connect-a-repo" below): a genuinely separate repo IS now
supported.** `healer/worktree.py:create_worktree_for_connected_app` +
`healer/agent_free.py`'s `is_connected_app` branch and `mcp_server/
github_client.py`'s per-instance `repo` override are exactly the
worktree-remote/PR-target wiring this paragraph originally said wasn't
built — built as part of the connect-a-repo feature, not this multi-app
step, but it supersedes this note.

**Not yet started** (next session should pick up here):
- **Step 2: any language via OpenTelemetry** — sentinel OTLP/HTTP ingest
  endpoint (JSON + protobuf), stack-trace parsing for Python/JS/Java/Go/
  C#/PHP/Ruby, per-language patch-guard anti-cheat detection (pytest
  skip/xfail, JS `it.skip`/`xit`/`test.only`, Java `@Disabled`/`@Ignore`,
  Go `t.Skip`, C# `[Ignore]`/`Skip=`, PHP `markTestSkipped`, Ruby
  `skip`/`pending`), and `examples/` Node.js (Express) + Go apps each with
  one seeded bug, OpenTelemetry configured, tests, and registered in
  `config/monitored_apps.yaml`. Node is already confirmed installed on
  this dev machine (`node v24.13.1`); Go was not checked yet.
- **Step 3: docs + verify** — `docs/onboarding.md`, README/SPEC.md/
  VERIFICATION.md updates, and extending `scripts/verify_all.py` with
  multi-app + OTLP checks.
- The live CI self-healing proof (PR #14) predates this multi-app work and
  was correctly *not* repeated.

### Connect-a-repo: "paste a URL → health report → AI fix PRs" — DONE

A follow-up feature request, not in SPEC.md and independent of the
multi-app/OpenTelemetry work above (that work's apps live inside this repo;
this feature's apps are genuinely external repos). Still free, no new paid
services.

Built:
- `alembic/versions/0004_connect_a_repo.py`: new `findings` table (dedup by
  `fingerprint`, same shape as `errors`/`contract_violations`) plus
  `monitored_apps.repo_url`/`auto_fix_high_severity`/`last_scanned_at`/
  `health_score`.
- `core/repo_connect.py`: `connect_repo()` — verifies `GITHUB_TOKEN` access
  (`check_repo_access`, a clear message naming the fix on 403/404), clones
  into `connected_apps/<name>/` (git-ignored, its own independent git repo),
  auto-detects language/test/lint command from the manifest
  (`pyproject.toml`/`requirements.txt` → python, `package.json` → js reading
  its own `scripts.test`/`scripts.lint`, `go.mod` → go), and inserts the
  `MonitoredApp` row directly (no YAML involved for these apps).
- `core/scanner.py`: `run_scan()` — installs deps, runs tests/lint/
  type-check/dependency-audit, all scoped to the app's own directory
  (`_app_dir` rejects anything outside `connected_apps/`) with a timeout and
  capped output per command. **Python apps get a real per-app virtualenv**
  (`<app_dir>/.selfheal_venv/`, `_ensure_app_venv`) with pytest/ruff/mypy/
  pip-audit installed into it — every command runs through that venv's own
  `python -m`, never a bare `pytest`/`ruff` resolved from whatever's on
  PATH. JS apps get the equivalent isolation for free from `npm install`'s
  own `node_modules/`. Parsers: `parse_ruff_json`/`parse_mypy_json`/
  `parse_pip_audit_json`/`parse_npm_audit_json`. `compute_health_score` is a
  simple documented heuristic (100 minus a per-finding severity penalty,
  minus 20 if tests fail), not a claim of code quality.
- `healer/findings_actions.py`: `request_fix_for_finding` turns a `Finding`
  into an ordinary `runtime_error` heal_job (a synthetic `Error` row sharing
  the finding's own fingerprint) — zero changes needed to the existing
  runtime heal loop, guardrails, or verification logic.
  `maybe_auto_fix_high_severity` wires in the "auto-fix high-severity
  findings" toggle (default off) after each scan.
- `healer/onboarding.py`: a **deterministic, no-LLM** one-file PR (via new
  `GitHubClient.get_branch_sha`/`create_branch`/`create_or_update_file` —
  the Git Data/Contents API, no local clone needed) adding a dependency-free
  error-reporting snippet (`selfheal_error_reporter.py`/`.js`) to the
  connected repo, wired to that app's own `ingest_token`.
- `healer/app.py`: `POST /api/apps` (connect + kick off a background scan),
  `GET /api/apps`/`GET /api/apps/{id}`, `POST /api/apps/{id}/scan`,
  `PATCH /api/apps/{id}` (the auto-fix toggle), `POST /api/findings/{id}/fix`,
  `POST /api/apps/{id}/onboard-pr`. Scan progress streams over the existing
  Socket.io connection (`scan_progress` event: `{app_id, stage, percent}`).
- `web/app.js` + `web/style.css`: a new **Apps** tab — "Add app" form,
  connected-apps list (health score, open findings, live progress bar), and
  a per-app detail page (findings table with a **Fix** button per finding,
  the auto-fix toggle, rescan/onboard-PR buttons).
- `scripts/verify_all.py` gained `check_connect_a_repo` (schema reachability
  always; `/api/apps` auth + content checks when a public URL/password are
  given) — deliberately never clones/scans a real repo itself (too slow/
  networked for a verifier meant to be re-run freely); the real flow is
  covered by the tests below plus a real manual smoke run (see "Real
  end-to-end verification" below).

**Real, previously-external-repo-only case now supported**: unlike the
multi-app work above (every app lives inside this repo, so `git worktree
add` against this repo's own `.git` always worked), a connected app is a
genuinely separate git repository. `healer/worktree.py:
create_worktree_for_connected_app` clones from the app's own
`connected_apps/<name>/` checkout (fast, local, no network) into a fresh
worktree instead of using `git worktree add`; `remove_plain_clone` cleans it
up. `healer/agent_free.py:run_heal_job_free` branches on `app.repo_url is
not None` to pick this path, pushes to `https://x-access-token:<token>@
github.com/<app.github_repo>.git` instead of `origin`, and opens the PR
against that repo's own real default branch (fetched via `GitHubClient.
get_repo()`). `healer/worker.py` now builds `GitHubClient(repo=app.
github_repo)` per job from the job's own app, not the global `GITHUB_REPO`
setting. CI-fix jobs for a connected app are out of scope (no GitHub Actions
running under this project's control there) — only the runtime-fix ("Fix"
button) path was extended.

**Real bug found and fixed via live smoke-testing, not just unit tests**:
the first version of `core/scanner.py` ran bare `pytest`/`ruff`/`mypy`/
`pip-audit` subject to whatever happened to be resolvable on PATH for the
shell subprocess — which, depending on how the healer-pod process was
launched, was often *nothing* (a real cloned repo's scan failed with
"'pytest' is not recognized...") or, worse, *this project's own* installed
tools. Reproduced by actually cloning a real public GitHub repo
(`octocat/Hello-World`) through the live API and watching it fail. Fixed
with the per-app venv described above — and even after adding the venv,
`_ensure_app_venv`'s first version still forgot to list `pytest` itself
among the tools it installs into that venv (`_SCANNER_TOOLS` only had
`ruff`, `mypy`, `pip-audit`), so every Python app's test step still failed,
just with a different, easy-to-miss error ("No module named pytest") —
caught by then testing against a real fixture Python project with a
genuinely passing test and noticing `tests_passed` came back `False`
instead of `True`. `tests/test_scanner.py::
test_run_scan_isolates_a_real_python_app_in_its_own_venv` is a real
(unmocked) regression test for exactly this — creates a real venv, installs
the real tools, and asserts a trivially-passing test actually reports as
passing. It's slow (~60-120s: a fresh venv + 4 package installs) but
deliberate: a mocked version of this test would never have caught either
bug, since both were about what actually happens when the real subprocess
commands run.

**Ambiguities resolved**:
- **A `Finding`'s "Fix" reuses the runtime-error heal loop via a synthetic
  `Error` row**, rather than teaching `healer/agent_free.py` a third source
  kind. `get_error` (the first MCP tool the fix agent calls) then just
  works unchanged. The tradeoff: a dependency-vulnerability finding (no
  file/line) gets `file_path=app.local_repo_path, line_number=0` on its
  synthetic `Error` row — a placeholder the fix agent's prompt can still
  reason about via the finding's own `message`, not a precise location.
- **`allowed_write_paths=["<local_repo_path>/"]` for a connected app is
  just `["connected_apps/<name>/"]`** — `mcp_server/sandbox.py`'s existing
  `check_writable`/`check_diff_paths_writable` needed *zero* changes,
  because they only ever validate the relative-path *string* from a diff
  against a prefix list; they never assume that string resolves under
  `REPO_ROOT` for real (the actual file lives under `WORKTREES_ROOT`
  instead, once `create_worktree_for_connected_app` clones the app in) —
  confirmed by reading `check_writable` closely before assuming a sandbox
  change was needed.
- **The scanner's health score is a simple, explicitly-documented
  heuristic**, not a real code-quality metric: SPEC-extension terms like
  "score" and "test pass rate" don't define a formula, so
  `compute_health_score` picks the simplest defensible one (100 minus a
  per-finding severity penalty, minus 20 for a failing test suite, clamped
  to [0, 100]) and says so in its docstring — avoids the score being
  mistaken for something more rigorous than it is.
- **Onboarding is deterministic (no LLM call) on purpose.** Reliably having
  an AI agent auto-wire error-reporting middleware into an *arbitrary,
  unknown* framework/entrypoint is a much harder and less reliable problem
  than fixing a known bug with a regression test to prove it — a fixed
  template plus an explicit "call this from your error handler" PR body is
  simpler and actually reliable, in keeping with SPEC.md's general "pick
  the simplest robust option" guidance extended to this feature.

Verified (tests + a real manual run, no mocking for the manual part):
- `tests/test_repo_connect.py` (16 tests: URL parsing, name slugging, stack
  detection from a real `tmp_path` manifest, the access-check error message
  via `respx`), `tests/test_scanner.py` (13 tests: parser unit tests, the
  `_app_dir` containment guard, the health-score heuristic, and the real
  unmocked venv-isolation integration test described above),
  `tests/test_healer_worktree.py` (+1: `create_worktree_for_connected_app`
  against a real local git repo), `tests/test_healer_worker.py` (+1: a
  multi-app job's `GitHubClient` is built with that app's own
  `github_repo`), `tests/test_healer_connect_repo_api.py` (7 tests: auth
  gating, connect success/failure, list/detail, the auto-fix toggle, the
  fix-a-finding-enqueues-a-real-heal_job flow, the onboarding-PR endpoint).
- **Real, live manual run** (no pods' worth of mocking): started all 4 pods
  for real, connected the real public repo `octocat/Hello-World` through
  the live `/api/apps` endpoint (real GitHub API call, real `git clone`,
  real scan), confirmed `GET /api/apps`/`GET /api/apps/{id}` return the
  right shape, clicked "Fix" on the resulting finding through the real API
  and confirmed a real `heal_job` row was enqueued with the finding's own
  fingerprint — then created a second, local-only fixture Python project
  (a `requirements.txt` + one trivially-passing test) and ran a real scan
  against it directly, which is what surfaced and let me fix the two
  isolation bugs above. All test-only rows/directories from this manual run
  were cleaned up afterward (not left in the dev/test databases or
  `connected_apps/`).

`ruff check .`/`ruff format --check .` clean repo-wide. `mypy core sentinel
mcp_server healer` (strict) clean. `pytest`: 325/326 (one pre-existing,
environment-only Windows ProactorEventLoop flake in
`test_healer_agent_free.py`, documented extensively earlier in this file —
unrelated to this feature, reproduces identically on a clean stash of these
changes).

**Not done / deferred**: CI-fix (not just runtime-fix) for a connected
external repo — would need GitHub Actions running in that repo and a
webhook pointed back at this system, out of scope for this pass. The
onboarding snippet's "next step" (actually wiring `report_error()`/
`reportError()` into the app's own error handler) is intentionally left to
the operator, not automated — see "ambiguities resolved" above.

### Connect-a-repo follow-up — push-access safety gate: DONE

Connect-a-repo's scanner and "Fix" flow both execute the connected repo's
own code (install deps, run its test/lint commands, push a fix commit) —
so `core/repo_connect.py:check_repo_access` now rejects any repo the
configured `GITHUB_TOKEN` can't actually *push* to, not just read. GitHub's
`GET /repos/{owner}/{repo}` already returns a `permissions.push` boolean for
an authenticated caller (`mcp_server/github_client.py:get_repo` was
unchanged — it already returned the full JSON body); a missing
`permissions` object (e.g. some unauthenticated-shaped response) fails
closed rather than being treated as implicit access. Added an optional
`ALLOWED_REPO_OWNERS` setting (`core/config.py`, comma-separated,
`settings.allowed_repo_owners_list` does the split/lowercase/trim) as a
second, independent gate: when set, only repos whose owner is on that list
can be connected at all, checked *before* the GitHub call so a
disallowed-owner attempt costs zero API calls. Both checks raise
`RepoConnectError` with a message naming the exact fix (add push access, or
add the owner to `ALLOWED_REPO_OWNERS`), surfaced to the UI as today via
`healer/app.py`'s existing `POST /api/apps` -> 400 handling — no changes
needed there. Tests: `tests/test_repo_connect.py` gained 5 new cases (push
access granted+owner allowed -> success, push denied -> rejected, no
`permissions` field at all -> rejected, owner not in
`ALLOWED_REPO_OWNERS` -> rejected without even calling GitHub, owner in
`ALLOWED_REPO_OWNERS` -> success); the pre-existing "succeeds when
reachable" test was updated to include `permissions.push: true` since a
push-less response now correctly fails it.

`ruff check .`/`ruff format --check .` clean. `mypy core sentinel
mcp_server healer` (strict) clean. `pytest`: 328/329 (the one failure is the
pre-existing, already-documented Windows ProactorEventLoop flake in
`test_healer_agent_free.py`, unrelated to this change).

### 2026-09-26 — Pluggable AI backend (Codex CLI, Gemini CLI)

Follow-up request, not in SPEC.md: make the healer's AI backend pluggable
beyond the existing free-mode-Claude-Code-CLI / API-mode-Anthropic-SDK pair,
adding OpenAI's Codex CLI and Google's Gemini CLI as two more selectable
backends.

Built:
- `core/config.py`: new `ai_backend: str` setting (`"claude_cli"` (default)
  | `"codex_cli"` | `"gemini_cli"` | `"api"`), plus per-backend
  `codex_cli_path`/`codex_cli_timeout_s`/`codex_cli_max_turns` and
  `gemini_cli_path`/`gemini_cli_timeout_s`/`gemini_cli_max_turns` (mirroring
  the existing `claude_cli_*` settings). The old `use_claude_code: bool`
  setting is kept, working as a backwards-compatible alias via a new
  `@model_validator(mode="before")` classmethod
  (`_derive_ai_backend_from_legacy_flag`): if `USE_CLAUDE_CODE` is present
  in the merged env/`.env`/init-kwargs data and `AI_BACKEND` is not, it
  derives `ai_backend` (`true` -> `"claude_cli"`, `false` -> `"api"`); if
  both are present, `AI_BACKEND` wins outright (an explicit new-style
  setting is never silently overridden by the legacy one); if neither is
  present, `ai_backend` just keeps its own `"claude_cli"` default. Verified
  directly (not just by reading pydantic-settings' docs) that a "before"
  model validator on `BaseSettings` receives the fully-merged dict *before*
  defaults are applied, so "was this key explicitly set anywhere" is
  reliably distinguishable from "is this field's default value" — a
  standalone script exercising all three cases (legacy-only, new-only,
  both) confirmed the expected `ai_backend` in each case before this was
  wired into the real `Settings` class.
- `healer/cli_common.py` (new, shared): factors out the parts of
  `healer/agent_free.py`'s CLI-subprocess plumbing that have nothing to do
  with which specific CLI is being launched — the Windows `.cmd`/`.bat`-shim
  workaround (`kill_process_tree`/`is_windows_shim`/`windows_shim_command`,
  copied logic, not imported, so `agent_codex.py`/`agent_gemini.py` have no
  dependency on `agent_free.py`'s internals — see that module for why the
  shim handling is necessary at all), a generic `CLIError` exception
  hierarchy (`CLINotFoundError`/`CLINotLoggedInError`/`CLIUsageLimitError`/
  `CLITimeoutError`/`CLIMalformedOutputError`), and `strip_env_vars`.
  **`agent_free.py` itself was deliberately left untouched** — it's a
  load-bearing, already-tested Phase 5+ module with its own tested
  `ClaudeCLI*` exception names and `CLIResult` dataclass; only the two new
  backend modules import from `cli_common.py`.
- `healer/agent_codex.py` (new): OpenAI Codex CLI backend. Exposes
  `run_heal_job_codex`/`run_ci_heal_job_codex` (same `(job_id, *, mcp,
  github, remote)` signature as `agent_free.py`'s equivalents) and
  `run_codex_cli` (the low-level subprocess wrapper, mirroring
  `run_claude_cli`). Reuses `agent_free.CLIResult` (not a separate
  structurally-identical dataclass — needed so the shared, imported-as-is
  `agent_free._verify_and_summarize`/`_runtime_prompt`/`_ci_prompt`
  functions type-check against it) and `agent_free._cli_calls_today`/
  `_is_cli_budget_paused` (reused directly rather than duplicated, so
  `tests/conftest.py`'s existing `isolated_cli_call_date` fixture, which
  monkeypatches `healer.agent_free.datetime`, isolates this backend's tests
  too with no new fixture needed — this was a real bug caught by running
  the new tests: an early version defined its own `_cli_calls_today` using
  its own `datetime` import, which the shared fixture never touched, so
  every e2e test saw a spurious `PAUSED_BUDGET` from stale
  `cli_invocation` rows left by earlier runs against the real current UTC
  date). `codex exec -` (stdin prompt) `--json` is Codex's non-interactive
  mode; unlike Claude Code's `--output-format json` (one JSON object),
  `--json` emits **newline-delimited JSON events**, so `run_codex_cli`
  parses every line and keeps the last `task_complete`/`agent_message`/
  `error`-typed event as the result.
- `healer/agent_gemini.py` (new): Google Gemini CLI backend, same shape
  (`run_heal_job_gemini`/`run_ci_heal_job_gemini`/`run_gemini_cli`). Prompt
  via stdin (Gemini CLI runs non-interactively whenever stdin is piped, per
  its docs), `--output-format json` (a single JSON object, like Claude
  Code's — `response`/`stats.models.<model>.tokens.{prompt,candidates,
  cached}` is the documented shape this module parses, falling back to a
  flat `usage` shape if that's what a given version actually returns).
- `healer/worker.py`: `_select_backend()` now dispatches on
  `settings.ai_backend` across all four values (was a two-way `if
  settings.use_claude_code` check), each branch importing its backend
  lazily — selecting one backend never requires another backend's module
  (or CLI) to even be importable/installed. An unrecognized `AI_BACKEND`
  value raises `ValueError` at worker startup rather than silently falling
  through to a default, so a typo in `.env` fails loudly instead of quietly
  running the wrong backend.
- Tests: `tests/test_healer_agent_codex.py`/`tests/test_healer_agent_gemini.py`
  (32 new tests total), mirroring `tests/test_healer_agent_free.py`'s
  structure exactly — `run_codex_cli`/`run_gemini_cli` unit tests against a
  mocked subprocess (success/timeout/not-found/not-logged-in/usage-limit/
  malformed-output, plus one test per backend confirming the MCP-restriction
  config file it writes has the right shape/content), and end-to-end
  `run_heal_job_codex`/`run_heal_job_gemini` tests against a real MCP
  server, real git worktrees, and a throwaway git remote, with only the
  low-level CLI call itself monkeypatched (same "the fake performs its
  edits via real `git apply`/`mcp.call_tool` before returning a scripted
  result, so the code's own post-hoc verification is what's actually being
  tested" principle `test_healer_agent_free.py` established). Also added
  `tests/test_healer_worker.py::
  test_select_backend_dispatches_free_mode_backends_by_ai_backend_setting`
  (parametrized over all three CLI backends), a matching `api`-mode test,
  and an unknown-`AI_BACKEND`-value test.

**What's live-verified vs. documented-only, stated plainly:**
- `claude_cli` (unchanged, pre-existing): live-verified — real PRs (#10,
  #14) opened by the real CLI against real bugs, see earlier phase-log
  entries.
- `codex_cli`/`gemini_cli` (this session's work): **documented-only,
  never run against a real install.** `codex --help` and `gemini --help`
  were both checked directly on this machine before writing either module
  and both failed with "command not found" (no npm/pip install of either
  present) — confirmed via Bash, not assumed. Everything about each CLI's
  actual flags, JSON/JSONL output shape, and MCP-restriction mechanism is
  built from that CLI's own public documentation (cited in each module's
  docstring) as of this session, via live web search rather than from
  training-data recall specifically because CLAUDE.md's Phase 4 log already
  has a documented incident of a guessed-from-training-data model/tool slug
  turning out to be wrong. A real, non-hypothetical limitation was found
  during that research and is called out explicitly in
  `agent_codex.py`'s docstring rather than glossed over: Codex CLI's
  non-interactive `exec` mode has no documented way to allow MCP tool calls
  without either (a) an interactive approval prompt that can't be answered
  with stdin already closed, or (b) `--dangerously-bypass-approvals-and-
  sandbox`, which disables *all* sandboxing, not just MCP approval — citing
  `openai/codex` issue #24135. This codebase's answer is `sandbox_mode=
  "read-only"` + `approval_policy="never"` in a per-invocation `CODEX_HOME`
  (Codex's own built-in write/shell tools become no-ops under a read-only
  sandbox, so the *only* way it can make a lasting change is through
  `propose_patch` via MCP) — a real, narrower mitigation than the documented
  bypass flag, but explicitly **not** the same tool-level guarantee
  `--disallowedTools` gives Claude Code, and not confirmed against a live
  `codex` binary. Before trusting either new backend beyond a sandboxed
  local test: install the real CLI, re-run this session's own
  live-verification pattern (the same "trigger a seeded bug, let the healer
  work it, confirm a real PR" check documented under "Post-Phase-8 — real
  end-to-end free-mode PR" above), and fix whatever's wrong in the relevant
  module + its docstring. Do not assume either module is correct just
  because its mocked tests pass — the mocked tests, by construction, can
  only confirm this module's *own* logic handles a given wire shape
  correctly, never that the real CLI actually produces that shape (the
  Phase 5+ `claude_cli` backend itself had two bugs — the `cmd.exe`
  double-quoting bug and the `.mcp.json` cwd-resolution bug — that were
  invisible to its mocked test suite and only surfaced by running a real
  job against the real CLI; see that phase's log).
- `api` mode: unchanged by this session, still only tested with a mocked
  Anthropic client (no real end-to-end PR via API mode yet — see Phase 4's
  "still not completed" note, which remains accurate).

**Ambiguities resolved this session**:
- **`ai_backend` derivation happens in a `model_validator(mode="before")`,
  not a `@property`** — a computed property can't change what
  `settings.ai_backend` *is* (other code reads the field directly), and a
  `model_validator(mode="after")` would run too late to let a plain string
  default (`"claude_cli"`) coexist with "was this explicitly set" detection
  the way `mode="before"`'s access to the raw pre-validation dict does.
- **Both new CLI backends reuse `agent_free.CLIResult` as their own
  `CLIResult`** (`CLIResult = agent_free.CLIResult`, not a separate
  dataclass with identical fields) rather than each defining their own.
  Discovered via mypy strict, not by design upfront: `_verify_and_summarize`
  (imported unchanged from `agent_free.py`, since its logic — inspect the
  worktree's diff, call the shared `run_tests` MCP tool — has zero
  backend-specific behavior) is typed against `agent_free.CLIResult`
  specifically; a structurally-identical-but-distinct dataclass in each new
  module failed mypy's nominal typing (`incompatible type
  "agent_codex.CLIResult"; expected "agent_free.CLIResult"`). Reusing the
  same class is both the mypy fix and, in hindsight, the more honest
  design — there's nothing backend-specific about what fields a parsed CLI
  result needs to carry.
- **`MAX_CLI_CALLS_PER_DAY` is a single cap shared across whichever CLI
  backend is active, not a separate per-backend counter** — all three CLI
  backends write the same `cli_invocation` `audit_log` action name (with a
  `details.backend` field distinguishing which one, for observability), and
  `_is_cli_budget_paused` counts that action name since UTC midnight
  regardless of backend. This is intentional, not an oversight: exactly one
  backend is active per deployment (`AI_BACKEND` is a single startup-time
  setting), so a per-backend counter would never actually differ from the
  shared one in practice, and sharing avoids adding three more settings for
  a distinction that can't currently occur.
- **`healer/cli_common.py`'s Windows-shim helpers are copied from
  `agent_free.py`, not imported from it** (`agent_codex.py`/
  `agent_gemini.py` depend only on `cli_common.py`, never on
  `agent_free.py`'s internals for this part) — a deliberate tradeoff of a
  small amount of duplication for keeping the three CLI backends
  independent of each other; `agent_free.py`'s own copy is unchanged and
  untouched by this session.

`ruff check .`/`ruff format --check .` clean repo-wide. `mypy core sentinel
mcp_server healer` (strict) clean. `pytest`: 358 tests collected, 357-358
passing across repeat runs — the one occasional failure is the same
pre-existing Windows ProactorEventLoop flake every prior session's log
documents (`RuntimeError: Event loop is closed` during a test's own DB
connection teardown), landing on a *different* test each run depending on
timing (once on `test_healer_agent_codex.py`'s end-to-end test, once
nowhere at all) — confirmed as the pre-existing issue, not something this
session's changes introduced, by reproducing it standalone against the
unmodified `test_healer_agent_free.py::
test_run_heal_job_free_fixes_zero_division_error_end_to_end` (same
`AttributeError`/`RuntimeError` signature, same "isolated run reproduces it
every time, full-suite run mostly doesn't" pattern).

### Post-Phase-8 — real README screenshots: DONE

Started all 4 pods for real locally (`uvicorn apps.target_app.main:app
--port 8001`, `uvicorn sentinel.app:app --port 8002`, `python -m
mcp_server.http_main`, `uvicorn healer.app:asgi_app --port 8000`), confirmed
each healthy (`/healthz` 200 for app/sentinel/healer, `/mcp` reachable-but-
400 for mcp-pod per the Phase 7 "no `/healthz`" note above), then used
headless Playwright (`playwright`, installed into `.venv` + `chromium`) to
capture `docs/images/login.png` — the real healer-pod login page rendered
by a real running process, not a mock.

**Dashboard/chat/metrics/app-health screenshots were skipped, deliberately,
not just left undone.** `ADMIN_PASSWORD_HASH` is a one-way argon2 hash (see
`scripts/hash_password.py`) with no recorded plaintext anywhere in this
repo or `.env`'s comments — there is no way to log in and capture the
gated pages without either (a) guessing a password, which would just fail
against argon2, or (b) writing a new hash into `.env` to set a known
password for the screenshot, which changes the real admin credential this
environment (including the earlier "public demo via Cloudflare Tunnel"
session) may still rely on. Both are exactly the kind of auth-weakening
CLAUDE.md's "Environment on this machine" section already refuses for the
unrelated Postgres-auth case, so the same principle was applied here:
skipped rather than routed around. README.md's new Screenshots section
explains this in one sentence next to the image, and links PR #10 and
PR #14 as the real proof of AI-driven fixes instead of staged UI captures
of pages that need a password nobody currently has in plaintext.

All 4 pods were stopped cleanly afterward (no `honcho`/uvicorn/`python -m`
processes left running). No application code was touched — `ruff check`/
`ruff format --check`/`mypy core sentinel mcp_server healer` (strict) all
clean, `pytest` 357/358 (the one failure was
`test_healer_agent_codex.py::
test_run_heal_job_codex_fixes_zero_division_error_end_to_end`, with the
same `RuntimeError: Event loop is closed` / asyncpg-teardown-on-a-closed-
ProactorEventLoop signature as the already-documented Windows flake above —
a different test hitting the same known, environment-only issue, not a new
regression).

### 2026-09-26 — scripts/benchmark.py: a real, live benchmark run

Built `scripts/benchmark.py`: triggers one seeded bug at a time against the
live running system (starts the 4 pods if not already up, same pattern as
`scripts/verify_all.py`), finds the resulting `heal_job` by looking up the
`Error` row's `function_name` then joining on `fingerprint` (same lookup
pattern `verify_all.py` uses), polls until a terminal `HealJobStatus` or a
20-minute timeout, and records attempt_count, CLI invocation count + total
turns (from `audit_log` rows with `action="cli_invocation"`, per Phase 5+'s
"budget tracking via audit_log" design), whether a circuit breaker fired,
wall-clock time, and PR url. Caps itself at 2 bugs per invocation. Never
resets or bypasses a circuit breaker/budget cap — a real block is recorded
as the outcome, not routed around. Writes `docs/benchmark.md` +
`benchmark_results.json`, and only stops the pods it itself started.

**Real run**: `timezone` + `zero`, chosen per the task's guidance (one bug
with prior real evidence of a working fix — PR #10 — plus one different,
independently-tractable bug already exercised by Phase 4's own e2e tests).
Neither reached a fresh Claude CLI attempt — both were blocked by genuine,
pre-existing guardrail state, confirmed by querying the DB directly after
the run (not assumed):

- **`timezone`**: no new `heal_job` was enqueued. Root cause: this
  fingerprint's `heal_job` #325 is already sitting at `pr_opened` — that job
  *is* PR #10, still open/unmerged three sessions later.
  `sentinel/storage.py`'s dedup rule treats `pr_opened` as "in flight" and
  correctly refused to open a duplicate PR for a bug that already has one.
  Working as designed, not a bug in the benchmark or the healer — but it
  means this benchmark did not exercise a fresh CLI run for this bug.
- **`zero`**: `heal_job` #334 was created, then immediately marked `failed`
  by `healer/circuit_breaker.py`'s per-fingerprint 24h attempt cap — this
  fingerprint already had 14 prior heal_job rows (ids 317-333) from this
  project's own earlier heavy `verify_all.py`/manual testing sessions
  earlier the same day. `attempt_count=0`, 0 CLI calls, 0s wall-clock: the
  breaker fires before any Claude CLI invocation. Recorded as-is, per the
  task's explicit "don't bypass real limits" instruction.

**Takeaway, and why this is still useful signal despite neither bug getting
a fresh attempt**: both outcomes are exactly the guardrails working as
designed — a dedup rule preventing a duplicate PR, and a circuit breaker
preventing runaway retries against the same fingerprint — caught in the act
by a real live run rather than only unit-tested in isolation. See
`docs/benchmark.md` for the full root-cause writeup and a note on which
bugs (`key`, `none_lookup`, `off_by_one`, `validation` — little/no recent
`heal_jobs` history at the time of this run) would be better candidates for
a follow-up run that actually reaches a fresh CLI attempt.

`ruff check .`/`ruff format --check .` clean repo-wide. `mypy core sentinel
mcp_server healer` (strict) clean (`scripts/` stays outside strict scope,
same as every other script). `pytest` green modulo the two already-
documented pre-existing flakes (Windows ProactorEventLoop teardown,
`isolated_budget_date` rare collision).

### 2026-09-26 — real-Chrome UI test pass + screenshots
Drove the UI with Playwright (channel="chrome", headed) on localhost and a Cloudflare tunnel; all login/dashboard/chat/rollback-confirmation/metrics/apps/sign-out checks passed. Fixed: `start_public_demo.ps1`'s argon2 check failed on the quoted hash in `.env` (double-quoted PS regex swallowed `$argon2id`); index.html had no favicon (console 404). Screenshots in `docs/images/`. Open items: healer-pod never reconnects to mcp-pod after an mcp-pod restart (500s until healer restart); on quick tunnels use `cloudflared --protocol http2` if QUIC fails; headed Chrome may crash renderer tabs on Windows (use a fresh browser per section). verify_all: 69 PASS / 1 FAIL (RAM) / 9 SKIPPED.

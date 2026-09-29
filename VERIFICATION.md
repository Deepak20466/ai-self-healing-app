# Verification

Run by `scripts/verify_all.py` against the live local system (4 pods running,
real Postgres, real GitHub), the full pytest suite, and CI on `main` (green).
No real AI heal runs were made: the healer was started with
`AI_BACKEND=api ANTHROPIC_API_KEY=invalid ANTHROPIC_BASE_URL=http://127.0.0.1:9`
so any seeded-bug job that got claimed would fail closed instead of spending
real AI budget or opening an unwanted PR.

**Terminal-only v1.0 (Steps 1-7) pass — 2026-09-29**: no browser, no web UI
anywhere in this repo (removed in Step 1). Every check below either talks to
the JSON/Socket.io API directly (`httpx`), calls MCP tools over the real
`mcp` protocol, or shells out to the actual `selfheal` CLI entry point
(`python -m cli.main ...`) — nothing here drives a browser.

Last run: 2026-09-29, local pods only (no public tunnel / admin password, so
the tunnel-and-login-gated checks below are SKIPPED, not FAILED) —
**68 PASS, 1 FAIL (RAM budget, known, see below), 18 SKIPPED**.

## ✅ Works (verified live)

| What | Evidence |
|---|---|
| All 4 pods start and answer health checks | app, sentinel, healer `/healthz` 200; MCP pod reachable |
| `selfheal` CLI: entry point runs, `status --json` sees all 4 pods up | `python -m cli.main status --json` |
| 7 seeded bugs trigger; errors stored with the right file and line | e.g. `zero` -> `bugs.py:32` |
| Silent bugs (off-by-one, timezone) caught by the contract prober | 1 violation flagged this run |
| Heal jobs get queued for triggered bugs; circuit breaker state readable | 20 recent jobs |
| 13 read-only MCP tools respond against real data; `run_tests` runs in a real worktree | verify_all |
| Write outside an app's allowed scope rejected by `propose_patch` | verify_all |
| `AI_BACKEND`/`AI_CHAIN` backend selection: `claude_cli`, `codex_cli`, `gemini_cli`, `api`, `gemini_api`, `groq_api`, `openrouter_api` all pick the right runner; a bad value fails at startup | fresh process per value (selection only, no CLI/HTTP call made) |
| `groq_api` backend: real HTTP tool-calling round trip | `scripts/check_ai_backends.py` — `PASS`, real response `'pong'` |
| `openrouter_api` backend: real HTTP tool-calling round trip (`openrouter/free`) | `scripts/check_ai_backends.py` — `PASS`, real response `'pong'`; a separate direct call with a tool definition returned a genuine `tool_calls` response |
| Daily budget cap pauses the healer | $999 spend vs $2 cap -> paused |
| Local deploy, then forced-failure rollback (marker flips back) | `local_deploy.py` |
| CI green on `main` | latest `ci.yml` run: success |
| One real AI-produced fix PR (runtime/silent bug) | PR #10 (free mode, Claude Code CLI) |
| One real AI CI-fix on a PR, end to end | PR #14 |
| Remote-verification mode (`run_heal_job_remote_verify`), run live for the first time, against a real connected heavy-dependency repo | Ran twice (job 373, job 374) against `multi-agent-workspace`: routing, the real Claude CLI call, and the real GitHub call all worked; the AI itself didn't converge on a working diff within its turn budget, and surfaced a real crash bug in `healer/worker.py` (fixed — see CLAUDE.md 2026-09-29) confirmed fixed by the second live run failing the job cleanly instead of killing the worker |
| Real-world bug fix (not planted) on a connected external repo | [multi-agent-workspace PR #1](https://github.com/Deepak20466/multi-agent-workspace/pull/1) — `cryptography>=49.0.0`, 3 real CVEs; target repo's own CI failed at an unrelated pre-existing step (`ModuleNotFoundError: No module named 'psycopg'`), not this diff — see README's "Proof: a real-world bug fix" section |
| No Docker files in the repo | scan |
| Packaging: `python -m build` produces a valid wheel + sdist (v1.0.0) | `ai_self_healing-1.0.0-{py3-none-any.whl,tar.gz}`; wheel installs and runs standalone in a throwaway venv (see CLAUDE.md Step 6) |
| OTLP ingest: token required (401 without), JSON and gzipped protobuf accepted, error stored against the right app | verify_all |
| **Node (Express) example**: seeded `TypeError` captured over OpenTelemetry at `examples/node_app/src/users.js:15`; scan flags the failing test | `scripts/demo_examples.py` (run by verify_all) |
| **Go example**: seeded divide-by-zero panic captured over OpenTelemetry at `examples/go_app/calc.go:12`; scan flags the failing test, `govulncheck` reported as skipped (not installed) | same |
| Per-language stack-trace parsers, scanner detection, patch-guard test-skip rejection, onboarding file per language | verify_all + pytest |
| Privacy guard: MCP tool RESULTS are scrubbed before reaching any AI backend, not just logged args | Real (unmocked) test against the live `audited_tool` wrapper: `tests/test_mcp_audit.py::test_audited_tool_scrubs_the_returned_result_not_just_logged_args`; not re-proven by verify_all since no live tool call in this system naturally echoes a secret back to contrive a fresh live case (see CLAUDE.md Step 5) |
| GitHub Copilot Chat (Agent mode, VS Code) against mcp-pod | Confirmed against a real Copilot Chat session: Agent mode discovered `.vscode/mcp.json` and successfully listed/called `selfheal`'s MCP tools |

## ⚠️ Built but not verified live

| What | Why |
|---|---|
| Codex CLI and Gemini CLI backends: running a fix | Neither CLI is installed here; built from public docs, mocked tests only. Only backend *selection* was verified |
| `api` (Anthropic SDK) backend: real PR | Mocked Anthropic client only; no API key/billing used |
| `gemini_api` backend: running a fix, or even the tiny live probe — **now BLOCKED, not just unverified, and removed from this project's own default AI_CHAIN/CHAT_CHAIN** | First key tried was `AQ.`-format; `scripts/check_ai_backends.py` got `401 ACCESS_TOKEN_TYPE_UNSUPPORTED` even via the correct `x-goog-api-key` header — matches several reports on Google's own AI Developer Forum of the same failure for `AQ.`-format keys specifically. A follow-up `AIzaSy`-format key instead got `400 API_KEY_INVALID` on the plainest possible call (`GET /v1beta/models`) — a key-specific problem (wrong project / API not enabled / bad copy), not the `AQ.` bug. Neither key has worked yet; not a code bug in either case — see CLAUDE.md Step 5 and its 2026-09-29 follow-ups |
| Login, `/api/prs`, `/api/backends`, per-app `auto_merge` PATCH, chat, metrics, `/api/apps` through the real API | Needs the admin password (only a hash exists) and a public tunnel; both absent this run. Covered by pytest, and by an earlier session's real Cloudflare-tunnel pass (see below) |
| Signed CI webhook over the public internet | Tunnel not running this pass. Verified earlier through a Cloudflare tunnel; covered by pytest now |
| Fresh CI-fix run | Costs AI usage; PR #14 is the earlier real run |
| Rollback/cancel/rerun MCP tools against real GitHub | Destructive; mocked tests only |
| Prompt-injection resistance | Mocked test only, not a live model |
| Cloud VM deploy (`provision_vm.sh`, systemd, Caddy, `deploy.yml`) | No VM available; only the local equivalent ran |
| Connected-repo "Fix" PR and onboarding PR | Only the enqueue step was run live in an earlier session; no real fix PR on an external repo |
| Scheduled health-check workflow | Needs a public URL; skips cleanly without one |
| `selfheal prepare`/`--onboard`/`--suggest`/`selfheal audit` (monorepo support, "make repo fixable") | Unit- and integration-tested (mocked AI/GitHub, real git worktrees/`git apply`/`run_tests` round trip) -- see the pending live demo run against a real connected repo for real-world confirmation |
| Remote-verify PR CI polling + retry (`healer/remote_ci_poll.py`) | Mocked-tests only (respx for GitHub, a scripted fake CLI response applying a real `git apply`+push to a throwaway local bare repo, same pattern as `test_healer_remote_verify.py`) -- never run against a real connected repo's real GitHub Actions failure; needs a real connected repo with heavy dependencies and a real CI failure on its PR to verify live |

## ❌ Not built

| What | Note |
|---|---|
| Live capture/scan for Java, C#, PHP, Ruby | Parsers, scanner detection and anti-cheat are unit-tested only; no example app or toolchain run for these (see the README support table) |
| CI-fix for connected external repos | Runtime-fix only |
| Idle RAM under 300 MB | Not met: 535.8 MB measured on Windows this run; documented Windows-vs-Linux gap (see README), not measured on Linux |
| Real PyPI publish | `release.yml` builds + attaches a GitHub Release on every `v*` tag; the actual `pypi-publish` job is gated on a `PYPI_API_TOKEN` repo secret nobody has configured yet, so it has never run for real |

## Notes from earlier sessions (still accurate)

- Headed Chrome via Playwright sometimes crashed the renderer tab during the old web-UI era's testing — moot now that the web UI is gone entirely (terminal-only v1.0, Step 1).
- Known gap (not fixed): healer-pod opens its MCP connection once at startup and never reconnects, so restarting mcp-pod makes the CLI/chat return errors until healer-pod is restarted.
- This machine has a broken 32-bit Git (`C:\Program Files (x86)\Git`, "BUG (fork bomb)") first on PowerShell's PATH; pods started from PowerShell inherit it and the git-based MCP tools fail until a working Git is first on PATH.
- Earlier, with a real Cloudflare tunnel and a real admin password: 69 PASS, 1 FAIL, 9 SKIPPED, and every login/dashboard/chat/rollback-confirmation/metrics/apps/sign-out check passed through the browser API (pre-dates the web UI's removal; the underlying `/api/*` routes are unchanged).

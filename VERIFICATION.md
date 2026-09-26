# Verification

Run by `scripts/verify_all.py` against the live local system (4 pods running,
real Postgres, real GitHub) plus the pytest suite (359 tests) and CI on `main` (green).
No real AI heal runs were made: the healer was started with its CLI disabled so
the seeded-bug jobs could not invoke Claude.

Last run: 2026-09-26 (with a real Chrome UI pass on localhost and a Cloudflare tunnel) — 69 PASS, 1 FAIL (RAM budget, known, listed below), 9 skipped for the reasons below. Live UI checks: login/wrong password, dashboard, chat (incl. rollback needs "yes"), metrics, apps, sign-out — all passed on both URLs.

## ✅ Works (verified live)

| What | Evidence |
|---|---|
| All 4 pods start and answer health checks | app, sentinel, healer `/healthz` 200; MCP pod reachable |
| 7 seeded bugs trigger; errors stored with the right file and line | e.g. `zero` -> `bugs.py:32` |
| Silent bugs (off-by-one, timezone) caught by the contract prober | 2 violations flagged |
| Heal jobs get queued for triggered bugs; circuit breaker state readable | 20 recent jobs |
| 13 read-only MCP tools respond against real data; `run_tests` runs in a real worktree | verify_all |
| Write outside `apps/target_app/` rejected by `propose_patch` | verify_all |
| Daily budget cap pauses the healer | $999 spend vs $2 cap -> paused |
| Local deploy, then forced-failure rollback (marker flips back) | `local_deploy.py` |
| Connect-a-repo on a real repo I own (`Deepak20466/ShopCart`): access check, clone, stack detection, npm scan | detected JavaScript, tests passed, 32 findings, health 0 (cleaned up afterwards) |
| `AI_BACKEND` switching: `claude_cli`, `codex_cli`, `gemini_cli`, `api` pick the right runner; a bad value fails at startup | fresh process per value (selection only, no CLI called) |
| CI green on `main` | run 36232570682 |
| One real AI-produced fix PR (runtime/silent bug) | PR #10 (free mode, Claude Code CLI) |
| One real AI CI-fix on a PR, end to end | PR #14 |
| No Docker files in the repo | scan |

## ⚠️ Built but not verified live

| What | Why |
|---|---|
| Codex CLI and Gemini CLI backends: running a fix | Neither CLI is installed here; built from public docs, mocked tests only. Only backend *selection* was verified |
| `api` (Anthropic SDK) backend: real PR | Mocked Anthropic client only; no API key/billing used |
| Login, chat, dashboard, metrics page, `/api/apps` through the browser API | Needs the admin password (only a hash exists) and a public tunnel; both absent this run. Covered by pytest |
| Signed CI webhook over the public internet | Tunnel not running. Verified earlier through a Cloudflare tunnel; covered by pytest now |
| Fresh CI-fix run | Costs AI usage; PR #14 is the earlier real run |
| Rollback/cancel/rerun MCP tools against real GitHub | Destructive; mocked tests only |
| Prompt-injection resistance | Mocked test only, not a live model |
| Cloud VM deploy (`provision_vm.sh`, systemd, Caddy, `deploy.yml`) | No VM available; only the local equivalent ran |
| Connected-repo "Fix" PR and onboarding PR | Only the enqueue step was run live earlier; no real fix PR on an external repo |
| Scheduled health-check workflow | Fixed the permission that made it fail, but with no public URL it now just skips |

## ❌ Not built

| What | Note |
|---|---|
| OpenTelemetry / any-language support (OTLP ingest, JS/Java/Go/C#/PHP/Ruby trace parsing, per-language anti-cheat, Node and Go example apps) | Roadmap. Only Python, Flask, Django middleware exist |
| CI-fix for connected external repos | Runtime-fix only |
| Idle RAM under 300 MB | Not met: ~360 MB measured on Windows this run (up to ~480 MB earlier); not measured on Linux |
| Dashboard/chat/metrics screenshots | Only the login page is captured (see README) |

## Notes from the 2026-09-26 UI pass

- Headed Chrome via Playwright on this Windows box sometimes crashes the renderer tab (seen on the Dashboard after ~10s idle). Not reproducible with mocked data, not in headless mode, and not app-specific; tests were run with a fresh browser per section.
- Known gap (not fixed): healer-pod opens its MCP connection once at startup and never reconnects, so restarting mcp-pod makes chat and the dashboard return 500 until healer-pod is restarted.
- This machine has a broken 32-bit Git (`C:\Program Files (x86)\Git`, "BUG (fork bomb)") first on PowerShell's PATH; pods started from PowerShell inherit it and the git-based MCP tools fail until a working Git is first on PATH.

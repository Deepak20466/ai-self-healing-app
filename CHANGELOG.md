# Changelog

All notable changes to this project are summarized here. This is a
factual summary, not marketing copy — `CLAUDE.md`'s phase log is the
source of truth for how each item was built and verified; see it for
detail, caveats, and what's live-verified vs. mocked-tests-only.

## v1.1.0 — 2026-09-29

### Added
- **Monorepo support** (`core/repo_connect.py:find_subprojects`/
  `select_subproject`): detects nested sub-projects up to 2 levels below a
  connected repo's root and scopes a connect/scan/fix to one of them
  (`selfheal scan <app> --path <subdir>`), with no schema or sandbox
  change needed.
- **Remote-verification fix mode** (`healer/remote_verify.py`): for a
  connected app whose manifest names a heavy/ML dependency (torch/
  tensorflow/transformers/chromadb/CUDA-class — never installed locally,
  by standing project rule), opens a fix PR verified by the connected
  repo's own GitHub Actions CI instead of a local test run. Clearly
  labeled, never auto-merged.
- **Remote-verify PR CI polling + retry** (`healer/remote_ci_poll.py`,
  new): a background poller (same shape as the auto-merge poller) reads a
  remote-verify PR's real GitHub check-run status via the API — no
  webhook required — and pushes one retry fix to the same branch on a
  real CI failure, capped at 2 total attempts per PR. Mocked-tests-only;
  never run against a real connected repo's real CI failure.
- **`selfheal prepare`** (`core/repo_health_check.py`): a static, read-only
  readiness checklist for a connected repo (tests, CI, test config, heavy
  deps) that never executes the app's own code.
- **Onboarding via `--onboard`** (`healer/onboarding_prepare.py`): spends
  AI budget on one PR adding only what `prepare` found missing, verified
  locally when possible and never auto-merged.
- **Suggest mode via `--suggest`** (`healer/suggest_mode.py`): the
  system's weakest-confidence tier — an unverified suggestion PR for a
  repo where no other fix path applies, clearly labeled.
- **`selfheal audit`** (`core/repo_audit.py`): a health verdict across
  every repo the configured `gh`/GitHub token can see, using the
  tree/contents API rather than cloning, so it scales cheaply.
- **GitHub Copilot Chat MCP interop** (`.vscode/mcp.json`): exposes this
  project's MCP tools to GitHub Copilot Chat in VS Code alongside the
  existing Claude Code wiring.
- **AI-chain dedup fix**: `AI_CHAIN`/`CHAT_CHAIN` now dedupe, keeping
  first-occurrence order, instead of letting a repeated backend name skew
  chain-exhaustion/cooldown logic.
- **Global credential scrubbing fix**: logs are scrubbed of credentials
  consistently across code paths that previously missed it; stuck
  onboarding jobs are also fixed as part of the same change.
- **Docker images + Kubernetes-tested CI** (`deploy/docker/
  Dockerfile.{app,sentinel,mcp,healer}`, `deploy/helm/selfheal/`,
  `.github/workflows/k8s-ci.yml`): the 4 pods are containerized and
  deployed to a real `kind` cluster via Helm on every push, in GitHub
  Actions only — Docker/kind/Helm/kubectl are never installed or run on a
  developer's own machine, per this project's own standing safety
  convention. Proves real infra-level self-healing (a killed pod process
  gets recreated by Kubernetes) in addition to this project's own
  application-level AI healing.

### Fixed
- Kubernetes liveness/restart test: `kill -9 1` doesn't work inside a
  container (PID 1 handling); the test now kills the actual `uvicorn`
  process and tolerates the resulting expected exit code from `kubectl
  exec`.
- The demo migration Job now seeds the demo dataset itself — root cause of
  an earlier k8s-ci smoke-test failure.
- `scripts/verify_all.py`'s Docker check was stale (asserted "no Docker
  files anywhere in repo", left over from before Docker/Kubernetes support
  existed) — replaced with a check that the expected Docker/Helm/k8s-ci
  files are present and the latest `k8s-ci.yml` GitHub Actions run is
  green.

## v1.0.0 — 2026-09-29

Initial terminal-only v1.0 release: the full 8-phase SPEC.md build (self-
healing detection, MCP tool server, runtime + CI healing, a terminal
`selfheal` CLI, pluggable AI backends including free Claude Code/Codex/
Gemini CLIs and paid API backends with fallback chains, per-app auto-merge,
a privacy/secret-scrubbing guard on every tool result reaching an AI
backend, and PyPI packaging) — see `CLAUDE.md`'s phase log for the full
history and `VERIFICATION.md` for what was verified live vs. mocked-tests-
only at that point.

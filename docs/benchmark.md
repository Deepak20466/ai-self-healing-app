# Benchmark: real live self-healing runs

Generated 2026-09-26T00:18:45.383358+00:00 by `scripts/benchmark.py` against
the live running system (all 4 pods, real free-mode Claude Code CLI would
have been invoked had either job actually run, real MCP tool calls, real git
worktrees). No mocking, and — per the task's explicit constraint — no
bypassed circuit breakers or budget caps. Both real outcomes below turned
out to be "blocked by a genuine existing guardrail," not "the healer tried
and failed" — see the root-cause section, added after querying the database
directly post-run. See CLAUDE.md's dated benchmark entry for the same
numbers.

| Bug | Result | Attempts | CLI calls | CLI turns | Time to terminal | PR |
|---|---|---|---|---|---|---|
| `timezone` | no new heal_job (existing one already in flight — see below) | 0 | 0 | 0 | - | - |
| `zero` | failed (circuit breaker blocked, 0 attempts) | 0 | 0 | 0 | 0s | - |

## Narrative

### `timezone`

- heal_job id: none created by this run
- final status: `no_heal_job_enqueued`
- circuit breaker blocked: false (a different guardrail was actually the cause — see root cause below)
- **Root cause (confirmed by querying the DB directly after the run)**:
  this fingerprint (`0a9375e7f2e405b175c23c4c736806a1`) already has
  `heal_job` #325 sitting at `pr_opened` — that job **is PR #10**
  (`https://github.com/Deepak20466/ai-self-healing-app/pull/10`), the real
  free-mode fix from the Post-Phase-8 session, still open/unmerged.
  `sentinel/storage.py`'s dedup rule (documented in CLAUDE.md's "Ambiguities
  resolved") only enqueues a new heal_job for a fingerprint when no job for
  it is currently `queued/running/pr_opened/ci_fixing` — `pr_opened` counts
  as in-flight on purpose, to avoid opening a second PR for a bug that
  already has one open. Triggering `/trigger/timezone` again correctly did
  *not* spawn a duplicate job. This is the dedup guardrail working exactly
  as designed, not a benchmark failure — but it does mean the `timezone` bug
  wasn't a fresh, clean re-run of the healer for this benchmark, since its
  "fix" already exists as an untouched real PR from three sessions ago.

### `zero`

- heal_job id: 334
- fingerprint: `a2ecdbb48d22a335be69fcd55eb6e7e1`
- final status: `failed`
- attempt_count: 0 (blocked before a single Claude CLI call)
- circuit breaker blocked: **true**
- **Root cause (confirmed by querying the DB directly after the run)**: this
  fingerprint has 14 prior `heal_job` rows within the lookback window,
  accumulated from this project's own earlier heavy `verify_all.py`/manual
  testing sessions (job ids 317-333, mostly `failed`, one with
  `attempt_count=3`). `healer/circuit_breaker.py`'s per-fingerprint 24h
  attempt cap (`max_heal_attempts_per_fingerprint_24h`) correctly refused a
  15th attempt and the job was marked `failed` immediately
  (`circuit_breaker_tripped` audit row at essentially the same timestamp as
  `detection`) — zero wall-clock time, zero CLI calls, because the breaker
  fires before any Claude CLI invocation. Per the task's explicit
  instruction, this was recorded as-is and the breaker was **not** reset or
  bypassed to force a "clean" run.

## What this benchmark actually demonstrates

Neither bug reached a fresh healer attempt, but for two different, entirely
legitimate reasons — both real production guardrails firing correctly, not
bugs in the healer or in this benchmark script:

1. `timezone`'s bug is *already fixed* by a real, previously-produced PR
   (#10) that's just never been merged — the in-flight dedup rule is
   working as intended.
2. `zero`'s fingerprint had already exhausted its 24h attempt budget from
   this project's own repeated testing earlier in the day — the
   per-fingerprint circuit breaker is working as intended.

Both are exactly the kind of "honestly report the real guardrail state,
don't force a green result" outcome the task asked for. A follow-up
benchmark run picking bugs with no recent heal_job history (e.g. `key`,
`none_lookup`, `off_by_one`, or `validation`, which have little/no history
in the current `heal_jobs` table) or run after the 24h window/PR #10 clears
would be needed to see a fresh end-to-end CLI attempt play out to
`pr_opened`/`failed`-for-real.

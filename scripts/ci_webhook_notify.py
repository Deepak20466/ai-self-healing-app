"""Sign and POST a CI event to sentinel-pod's `POST /webhooks/ci` from inside
a GitHub Actions job (SPEC.md CI/CD PIPELINE: "sends a start event ... and a
finish event", and `ci-failure.yml`'s workflow_run notification).

Reads GitHub's own `GITHUB_*`/`GITHUB_EVENT_*` env vars (always present in
Actions) for run_id/workflow/branch/sha/pr_number, and `HEALER_WEBHOOK_URL`/
`WEBHOOK_SECRET` for where/how to sign — see `core/hmac_utils.py` for the
exact `t=<ts>,v1=<hex_hmac>` scheme this must match on the receiving end.
This has no dependency on the rest of the package (deliberately: it runs
under whatever bare Python the Actions runner has, via `pip install httpx`
in the workflow step, before `pip install -e .` even happens for `ci.yml`'s
own start-of-job ping).
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.request


def sign(secret: str, body: bytes, timestamp: int) -> str:
    message = f"{timestamp}.".encode() + body
    digest = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def build_payload(env: dict[str, str], *, status: str, conclusion: str | None) -> dict[str, object]:
    """Build the webhook JSON body from `env`.

    SOURCE_* (set only by ci-failure.yml) always wins over the ambient
    GITHUB_* vars: GitHub Actions does NOT let a step's `env:` block override
    its own reserved GITHUB_* names (GITHUB_RUN_ID, GITHUB_REF_NAME,
    GITHUB_SHA, GITHUB_WORKFLOW) -- the runner injects its own values for the
    CURRENTLY EXECUTING workflow after step env is applied, silently
    discarding any override. ci-failure.yml is workflow_run-triggered, so
    without this, every field here would describe ci-failure.yml's own run
    (branch "main", its own run_id/sha/workflow name) instead of the CI run
    that actually failed -- reproduced for real running the live CI
    self-healing demo, see CLAUDE.md. ci.yml's own start/finish calls never
    set SOURCE_*, so they correctly fall through to the ambient GITHUB_*
    vars, which describe ci.yml's own run as intended there.
    """
    run_id = int(env.get("SOURCE_RUN_ID") or env.get("GITHUB_RUN_ID", "0"))
    workflow = env.get("SOURCE_WORKFLOW") or env.get("GITHUB_WORKFLOW", "ci.yml")
    branch = (
        env.get("SOURCE_BRANCH") or env.get("GITHUB_HEAD_REF") or env.get("GITHUB_REF_NAME", "main")
    )
    sha = env.get("SOURCE_SHA") or env.get("GITHUB_SHA", "0" * 40)
    pr_number_env = env.get("PR_NUMBER")

    payload: dict[str, object] = {
        "run_id": run_id,
        "workflow": workflow,
        "branch": branch,
        "sha": sha,
        "status": "in_progress" if status == "started" else "completed",
    }
    if pr_number_env:
        payload["pr_number"] = int(pr_number_env)
    if conclusion:
        payload["conclusion"] = conclusion
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", required=True, choices=["started", "finished"])
    parser.add_argument("--conclusion", default=None)
    args = parser.parse_args()

    webhook_url = os.environ.get("HEALER_WEBHOOK_URL", "")
    secret = os.environ.get("WEBHOOK_SECRET", "")
    if not webhook_url or not secret:
        print("HEALER_WEBHOOK_URL/WEBHOOK_SECRET not set; skipping webhook notification.")
        return 0

    repo = os.environ.get("GITHUB_REPOSITORY", "")
    payload = build_payload(dict(os.environ), status=args.status, conclusion=args.conclusion)
    run_id = payload["run_id"]

    body = json.dumps(payload).encode("utf-8")
    timestamp = int(time.time())
    signature = sign(secret, body, timestamp)

    request = urllib.request.Request(
        webhook_url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "X-Signature": signature},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            print(f"webhook notified ({repo} run {run_id}): HTTP {response.status}")
    except Exception as exc:  # noqa: BLE001 - never fail the CI job over this
        print(f"webhook notification failed (non-fatal): {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

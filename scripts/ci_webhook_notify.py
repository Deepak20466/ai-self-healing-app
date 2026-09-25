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
    run_id = int(os.environ.get("GITHUB_RUN_ID", "0"))
    workflow = os.environ.get("GITHUB_WORKFLOW", "ci.yml")
    branch = os.environ.get("GITHUB_HEAD_REF") or os.environ.get("GITHUB_REF_NAME", "main")
    sha = os.environ.get("GITHUB_SHA", "0" * 40)
    pr_number_env = os.environ.get("PR_NUMBER")

    payload: dict[str, object] = {
        "run_id": run_id,
        "workflow": workflow,
        "branch": branch,
        "sha": sha,
        "status": "in_progress" if args.status == "started" else "completed",
    }
    if pr_number_env:
        payload["pr_number"] = int(pr_number_env)
    if args.conclusion:
        payload["conclusion"] = args.conclusion

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

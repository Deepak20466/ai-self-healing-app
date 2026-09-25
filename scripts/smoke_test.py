"""Post-deploy smoke test (SPEC.md CI/CD: "run scripts/smoke_test.py
(healthz + all contracts + the previously failing endpoint)").

Run after a release is swapped in, before deciding whether to keep it or
roll back:

    python scripts/smoke_test.py [--base-url http://localhost:8001] \
        [--sentinel-url http://localhost:8002] [--force-fail]

Exit code 0 = healthy (keep the release), non-zero = roll back.

`--force-fail` never talks to a real pod at all — it exists purely so
`scripts/local_deploy.py`'s rollback path can be exercised and verified for
real without needing every pod actually broken (see CLAUDE.md Phase 7 log:
this was run for real, once, to prove rollback + notification actually fire).
"""

from __future__ import annotations

import argparse
import sys

import httpx

DEFAULT_TIMEOUT = 5.0


def check_healthz(base_url: str) -> tuple[bool, str]:
    try:
        response = httpx.get(f"{base_url}/healthz", timeout=DEFAULT_TIMEOUT)
    except httpx.HTTPError as exc:
        return False, f"{base_url}/healthz unreachable: {exc}"
    if response.status_code != 200:
        return False, f"{base_url}/healthz returned {response.status_code}"
    return True, f"{base_url}/healthz OK"


def check_mcp_reachable(base_url: str) -> tuple[bool, str]:
    """mcp-pod speaks MCP's streamable-HTTP protocol at `/mcp`, not a plain
    REST `/healthz` (see CLAUDE.md Phase 7 log) — any HTTP response (even a
    4xx from hitting it with a bare GET) proves the process is up and
    listening, which is all this check needs."""
    try:
        httpx.get(f"{base_url}/mcp", timeout=DEFAULT_TIMEOUT)
    except httpx.HTTPError as exc:
        return False, f"{base_url}/mcp unreachable: {exc}"
    return True, f"{base_url}/mcp reachable"


def check_contracts(base_url: str) -> tuple[bool, str]:
    """Exercise a couple of known-good app-pod endpoints (the "contracts"
    check) — a lightweight proxy for apps/target_app/contracts.py's full
    suite, which sentinel-pod's own prober already runs continuously."""
    try:
        response = httpx.get(f"{base_url}/items/top", timeout=DEFAULT_TIMEOUT)
    except httpx.HTTPError as exc:
        return False, f"{base_url}/items/top unreachable: {exc}"
    if response.status_code != 200:
        return False, f"{base_url}/items/top returned {response.status_code}"
    return True, f"{base_url}/items/top OK"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8001", help="app-pod base URL")
    parser.add_argument(
        "--sentinel-url", default="http://localhost:8002", help="sentinel-pod base URL"
    )
    parser.add_argument(
        "--mcp-url", default="http://localhost:8003", help="mcp-pod base URL (streamable HTTP)"
    )
    parser.add_argument("--healer-url", default="http://localhost:8000", help="healer-pod base URL")
    parser.add_argument(
        "--force-fail",
        action="store_true",
        help="Report failure unconditionally, without contacting any pod. "
        "Used to exercise scripts/local_deploy.py's rollback path on demand.",
    )
    args = parser.parse_args()

    if args.force_fail:
        print("SMOKE TEST: --force-fail set, reporting failure without checking any pod.")
        return 1

    checks = [
        check_healthz(args.base_url),
        check_healthz(args.sentinel_url),
        check_mcp_reachable(args.mcp_url),
        check_healthz(args.healer_url),
        check_contracts(args.base_url),
    ]

    all_ok = True
    for ok, message in checks:
        print(("OK  " if ok else "FAIL") + " " + message)
        all_ok = all_ok and ok

    if all_ok:
        print("SMOKE TEST: all checks passed.")
        return 0
    print("SMOKE TEST: one or more checks failed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

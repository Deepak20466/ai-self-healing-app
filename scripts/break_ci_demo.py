"""Demo helper for SPEC.md's "CI self-healing" acceptance criterion:
deliberately break a test on a new branch/PR so `ci-failure.yml` +
`healer/ci_agent.py` (or `agent_free.run_ci_heal_job_free` in free mode) have
something real to detect, explain, and fix.

Usage:
    python scripts/break_ci_demo.py [--branch autofix-demo/ci-break]

Creates a new git branch off the current HEAD, weakens one assertion in an
existing target_app test to something guaranteed to fail (an off-by-one on a
known-good value, not a deleted/xfail'd test — this script exists to *break*
CI legitimately, the same way a real regression would, not to fake it around
the anti-cheat guardrails `mcp_server/patch_guard.py` enforces on the fix
side), commits it, and prints the `git push` command to open the PR from.
Never pushes on its own — that's a deliberate manual/CI step, not something
a local script should do unattended.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TARGET_TEST = REPO_ROOT / "tests" / "test_target_app_bugs.py"


def _run(cmd: list[str]) -> None:
    print(f"$ {' '.join(cmd)}")
    subprocess.run(cmd, cwd=str(REPO_ROOT), check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--branch", default="autofix-demo/ci-break")
    args = parser.parse_args()

    if not TARGET_TEST.exists():
        print(f"expected test file not found: {TARGET_TEST}", file=sys.stderr)
        return 1

    original = TARGET_TEST.read_text(encoding="utf-8")
    if "assert" not in original:
        print(f"{TARGET_TEST} has no assertion to break", file=sys.stderr)
        return 1

    # Flip the first `== ` comparison in the file to `== 999999` — a real,
    # legitimate test failure (wrong expected value), not a deletion/xfail/
    # skip, so the demo genuinely exercises the CI-fix loop's real-failure
    # classification path, not its flaky-failure path.
    marker = "def test_"
    first_test_start = original.find(marker)
    if first_test_start == -1:
        print(f"no test function found in {TARGET_TEST}", file=sys.stderr)
        return 1

    broken = original[:first_test_start] + original[first_test_start:].replace(
        "assert ", "assert 999999 == 1 and ", 1
    )
    if broken == original:
        print("could not find an assertion to break", file=sys.stderr)
        return 1

    _run(["git", "checkout", "-b", args.branch])
    TARGET_TEST.write_text(broken, encoding="utf-8")
    _run(["git", "add", str(TARGET_TEST)])
    _run(
        [
            "git",
            "commit",
            "-m",
            "demo: intentionally break a test for the CI self-healing demo",
        ]
    )

    print()
    print("Branch created with one deliberately broken test. Next:")
    print(f"  git push -u origin {args.branch}")
    print("  gh pr create --fill   # or open the PR in the GitHub UI")
    print()
    print("CI will fail, ci-failure.yml will notify the healer webhook, and")
    print("the AI CI-fix loop should push a real fix commit to this branch.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

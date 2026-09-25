"""Local-mode deploy (SPEC.md CI/CD PIPELINE #7): the same atomic-release /
symlink-swap / rollback flow `deploy.yml` runs over SSH on the cloud VM, run
against `localhost` instead — so the full self-healing loop can be demoed
without a real server. Never requires Docker; pure filesystem + `git
archive` + Postgres.

    python scripts/local_deploy.py                 # deploy HEAD
    python scripts/local_deploy.py --sha <full-sha> # deploy a specific commit
    python scripts/local_deploy.py --force-fail-smoke-test   # exercise rollback

Layout under `LOCAL_DEPLOY_ROOT` (default `./local_deploy_root`, gitignored):
    releases/<sha>/   -- a `git archive` snapshot of that commit
    shared/.env       -- copied from the repo's real `.env` once
    current.txt       -- the sha of the release currently "live"
    previous.txt      -- the sha to roll back to if the next deploy fails

Windows note: real symlinks need elevated privileges or Developer Mode, so
"current"/"previous" are plain marker files rather than a `current` symlink
— `deploy.yml`'s real SSH flow to an Ubuntu VM uses an actual symlink per
SPEC.md; this is the portable equivalent for local/demo use (see CLAUDE.md
"Ambiguities resolved").

This script writes a real `deployments` row and calls `healer.notifier.
notify()` on both success and rollback, against the *real* dev database
(`DATABASE_URL`, not `TEST_DATABASE_URL`) — the same precedent
`scripts/seed_demo.py` already sets for non-pytest scripts. It is not run
by pytest and never touches `selfheal_test`.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    print(f"$ {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)  # type: ignore[arg-type]
    if result.stdout:
        print(result.stdout.rstrip())
    if result.stderr:
        print(result.stderr.rstrip(), file=sys.stderr)
    return result


def git_head_sha() -> str:
    result = _run(["git", "rev-parse", "HEAD"], cwd=str(REPO_ROOT))
    if result.returncode != 0:
        raise RuntimeError("git rev-parse HEAD failed")
    return result.stdout.strip()


def build_release(deploy_root: Path, sha: str) -> Path:
    """`git archive` the given commit into releases/<sha>/ (idempotent)."""
    release_dir = deploy_root / "releases" / sha
    if release_dir.exists():
        print(f"release {sha} already built at {release_dir}, reusing")
        return release_dir
    release_dir.mkdir(parents=True, exist_ok=True)

    archive_proc = subprocess.Popen(
        ["git", "archive", "--format=tar", sha], cwd=str(REPO_ROOT), stdout=subprocess.PIPE
    )
    extract_proc = subprocess.run(["tar", "-x", "-C", str(release_dir)], stdin=archive_proc.stdout)
    if archive_proc.stdout is not None:
        archive_proc.stdout.close()
    archive_proc.wait()
    if archive_proc.returncode != 0 or extract_proc.returncode != 0:
        shutil.rmtree(release_dir, ignore_errors=True)
        raise RuntimeError(f"failed to build release {sha}")
    return release_dir


def link_shared_env(deploy_root: Path, release_dir: Path) -> None:
    shared_dir = deploy_root / "shared"
    shared_dir.mkdir(parents=True, exist_ok=True)
    shared_env = shared_dir / ".env"
    if not shared_env.exists():
        real_env = REPO_ROOT / ".env"
        if real_env.exists():
            shutil.copyfile(real_env, shared_env)
        else:
            print("WARNING: no .env found to seed shared/.env; release will use defaults")
            shared_env.touch()
    shutil.copyfile(shared_env, release_dir / ".env")


def run_migrations(release_dir: Path) -> bool:
    python = sys.executable
    result = _run([python, "-m", "alembic", "upgrade", "head"], cwd=str(release_dir))
    return result.returncode == 0


def read_marker(path: Path) -> str | None:
    return path.read_text().strip() if path.exists() else None


def write_marker(path: Path, sha: str) -> None:
    path.write_text(sha)


async def _record_deployment_and_notify(
    *, sha: str, env: str, status: str, started_at: datetime
) -> None:
    """Write a real `deployments` row and fire a notification, exactly like
    `deploy.yml`'s real SSH flow would ("report every stage back to the
    healer webhook, which streams it to the chat") — see CLAUDE.md for why
    this runs against the real dev DB, same precedent as seed_demo.py."""
    from core.db import dispose_engine, session_scope
    from core.models import Deployment
    from healer.notifier import notify

    async with session_scope() as session:
        deployment = Deployment(
            sha=sha, env=env, status=status, started_at=started_at, finished_at=datetime.now(UTC)
        )
        session.add(deployment)

    message = f"Deployment {sha[:8]} to {env}: {status}"
    await notify("deployment" if status == "deployed" else "rollback", message, sha=sha, env=env)
    await dispose_engine()


def run_smoke_test(force_fail: bool) -> bool:
    python = sys.executable
    cmd = [python, str(REPO_ROOT / "scripts" / "smoke_test.py")]
    if force_fail:
        cmd.append("--force-fail")
    result = _run(cmd)
    return result.returncode == 0


def deploy(*, sha: str, env: str, force_fail_smoke_test: bool) -> bool:
    """Returns True if the deploy stuck, False if it was rolled back."""
    deploy_root = REPO_ROOT / "local_deploy_root"
    deploy_root.mkdir(parents=True, exist_ok=True)
    current_marker = deploy_root / "current.txt"
    previous_marker = deploy_root / "previous.txt"

    started_at = datetime.now(UTC)
    previous_sha = read_marker(current_marker)

    print(f"=== Deploying {sha} to env=local (previous release: {previous_sha or 'none'}) ===")
    release_dir = build_release(deploy_root, sha)
    link_shared_env(deploy_root, release_dir)

    if not run_migrations(release_dir):
        print("MIGRATION FAILED — aborting before touching the current release pointer.")
        asyncio.run(
            _record_deployment_and_notify(sha=sha, env=env, status="failed", started_at=started_at)
        )
        return False

    # Atomic release swap: flip "current" to the new release before smoke
    # testing it, keeping the old sha in "previous" so a failed smoke test
    # can flip straight back — mirrors deploy.yml's symlink swap/un-swap.
    if previous_sha:
        write_marker(previous_marker, previous_sha)
    write_marker(current_marker, sha)
    print(f"current release pointer -> {sha}")

    print("--- running smoke test ---")
    healthy = run_smoke_test(force_fail_smoke_test)

    if healthy:
        print(f"=== Deploy {sha} succeeded: smoke test passed. ===")
        asyncio.run(
            _record_deployment_and_notify(
                sha=sha, env=env, status="deployed", started_at=started_at
            )
        )
        return True

    print(f"=== Smoke test FAILED for {sha}. Rolling back... ===")
    if previous_sha:
        write_marker(current_marker, previous_sha)
        print(f"current release pointer rolled back -> {previous_sha}")
    else:
        current_marker.unlink(missing_ok=True)
        print("no previous release to roll back to; cleared current release pointer")

    asyncio.run(
        _record_deployment_and_notify(sha=sha, env=env, status="rolled_back", started_at=started_at)
    )
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", default=None, help="Commit to deploy (default: HEAD)")
    parser.add_argument("--env", default="local")
    parser.add_argument(
        "--force-fail-smoke-test",
        action="store_true",
        help="Force the smoke test to fail, to exercise/demo the rollback path.",
    )
    args = parser.parse_args()

    sha = args.sha or git_head_sha()
    ok = deploy(sha=sha, env=args.env, force_fail_smoke_test=args.force_fail_smoke_test)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

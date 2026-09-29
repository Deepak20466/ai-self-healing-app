""" "Connect a repo" flow: verify access, clone, detect stack, register.

Every connected app is cloned into `connected_apps/<name>/` (git-ignored,
each one its own independent git checkout with its own `.git/`) rather than
into `apps/`, which is reserved for apps shipped inside this repo
(`config/monitored_apps.yaml`'s `target_app`). Keeping them physically
separate means `mcp_server/sandbox.py`'s existing `REPO_ROOT`-relative path
checks need no change: a connected app's `allowed_write_paths` is just
`["connected_apps/<name>/"]`, resolved and validated exactly like
`apps/target_app/` always has been.

The clone itself briefly embeds `GITHUB_TOKEN` in the remote URL for
authentication (GitHub's supported `https://x-access-token:<token>@...`
form), then immediately rewrites the remote back to a plain, tokenless URL
--- the token never lands in `.git/config` on disk. The same GitHub PAT
this project already requires for every other GitHub call is reused; no new
service or credential is introduced.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.language_tools import detect_profile
from core.models import MonitoredApp
from mcp_server.github_client import GitHubClient, GitHubClientError
from mcp_server.sandbox import REPO_ROOT, to_repo_relative

CONNECTED_APPS_ROOT = REPO_ROOT / "connected_apps"
CLONE_TIMEOUT_SECONDS = 120.0

#: how many directory levels below the repo root to search for a nested
#: manifest (root itself is depth 0) -- "up to 2 levels deep" per the
#: monorepo-support request.
MONOREPO_MAX_DEPTH = 2
_MONOREPO_SKIP_DIRS = frozenset(
    {"node_modules", ".venv", "dist", "build", ".git", "__pycache__", ".selfheal_venv"}
)

_APP_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_GITHUB_URL_PATTERN = re.compile(
    r"^(?:https?://github\.com/|git@github\.com:)(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"
)


class RepoConnectError(Exception):
    """Raised for any step of connect-a-repo the caller should show to the user."""


class SubprojectsFound(RepoConnectError):
    """Raised by `connect_repo`/`select_subproject` when the repo root has no
    manifest of its own but nested sub-projects were found -- the caller
    (an API/CLI layer) is expected to show `subprojects` and ask the
    operator to re-run with a `sub_path`. Its own message already lists
    every detected path, so a caller that just prints the message (as
    `RepoConnectError`'s existing handling does everywhere) needs no special
    case."""

    def __init__(self, subprojects: list[SubProject]):
        self.subprojects = subprojects
        paths = ", ".join(sp.rel_path or "." for sp in subprojects)
        super().__init__(
            f"No manifest at the repo root, but found sub-project(s): {paths}. "
            "Re-run connect with --path <subdir> to pick one."
        )


@dataclass(frozen=True)
class DetectedStack:
    language: str
    test_command: str
    lint_command: str | None
    install_command: str | None
    """Shell command to install dependencies before scanning, or None if
    nothing to install (no manifest file found)."""


@dataclass(frozen=True)
class SubProject:
    """One manifest found while walking a repo for monorepo support.
    `rel_path` is `""` for the repo root itself, else a forward-slash
    relative path like `"backend"` or `"services/api"`."""

    rel_path: str
    stack: DetectedStack


def parse_github_url(repo_url: str) -> str:
    """Extract `owner/repo` from an https or ssh GitHub URL."""
    match = _GITHUB_URL_PATTERN.match(repo_url.strip())
    if not match:
        raise RepoConnectError(
            f"{repo_url!r} doesn't look like a GitHub repo URL "
            "(expected https://github.com/owner/repo or git@github.com:owner/repo)"
        )
    return f"{match.group('owner')}/{match.group('repo')}"


def slugify_app_name(owner_repo: str) -> str:
    """`owner/repo` -> a filesystem/branch-safe app name (just the repo part, lowercased)."""
    repo = owner_repo.split("/", 1)[1].lower()
    slug = re.sub(r"[^a-z0-9_-]", "-", repo).strip("-") or "app"
    return slug[:64]


async def check_repo_access(owner_repo: str) -> dict[str, object]:
    """Confirm `GITHUB_TOKEN` can see `owner_repo`, has *push* (write) access
    to it, and (if `ALLOWED_REPO_OWNERS` is configured) that its owner is on
    the allow-list. Raises a clear, user-facing RepoConnectError (naming the
    fix) on any failure, per the feature spec: "clear message telling me to
    add the repo to my token if denied".

    The push-access check is a safety gate, not just an access check:
    connect-a-repo's scanner (core/scanner.py) and its "Fix" flow both
    execute the repo's own code (installs deps, runs its test/lint commands,
    and can push a fix commit) — allowing a repo the token can only *read*
    would mean running arbitrary untrusted code with no way to ever land the
    fix, and more importantly no signal that the operator actually owns/
    trusts that repo enough to grant it push access in the first place.
    """
    owner = owner_repo.split("/", 1)[0].lower()
    allowed_owners = settings.allowed_repo_owners_list
    if allowed_owners and owner not in allowed_owners:
        raise RepoConnectError(
            f"{owner_repo}'s owner {owner!r} is not in ALLOWED_REPO_OWNERS "
            f"({', '.join(allowed_owners)}) — add it there first if this repo "
            "should be connectable."
        )

    async with GitHubClient() as client:
        try:
            data = await client.get_repo(owner_repo)
        except GitHubClientError as exc:
            raise RepoConnectError(
                f"Can't access {owner_repo} with the configured GITHUB_TOKEN "
                f"({exc}). If this is a private repo or a fine-grained token, "
                "add this repository to the token's repository access list "
                "(GitHub Settings -> Developer settings -> Personal access "
                "tokens) and try again."
            ) from exc

    permissions = data.get("permissions")
    has_push = isinstance(permissions, dict) and bool(permissions.get("push"))
    if not has_push:
        raise RepoConnectError(
            f"GITHUB_TOKEN does not have write access to {owner_repo} — scans "
            "execute the repo's code, so only repos you can push to are allowed"
        )
    return data


async def _run_git(args: list[str], *, cwd: Path) -> None:
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=CLONE_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise RepoConnectError(f"git {' '.join(args)} timed out") from exc
    if process.returncode != 0:
        raise RepoConnectError(f"git {' '.join(args)} failed: {stderr.decode(errors='replace')}")


async def clone_repo(owner_repo: str, name: str, *, github_token: str) -> Path:
    """Shallow-clone `owner_repo` into `connected_apps/<name>/`, then strip the
    embedded token from the remote URL."""
    CONNECTED_APPS_ROOT.mkdir(exist_ok=True)
    dest = CONNECTED_APPS_ROOT / name
    if dest.exists():
        raise RepoConnectError(f"An app named {name!r} is already connected")
    auth_url = f"https://x-access-token:{github_token}@github.com/{owner_repo}.git"
    plain_url = f"https://github.com/{owner_repo}.git"
    await _run_git(["clone", "--depth", "1", auth_url, str(dest)], cwd=CONNECTED_APPS_ROOT)
    await _run_git(["remote", "set-url", "origin", plain_url], cwd=dest)
    return dest


def _read_json(path: Path) -> dict[str, object]:
    try:
        return dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        return {}


def detect_stack(local_path: Path) -> DetectedStack:
    """Inspect the checked-out repo's root for a manifest and pick sensible
    defaults per SPEC-extension: "auto-detect language/framework/test
    command". Falls back to a generic python/pytest guess if nothing is
    recognized, since a scan step that finds zero manifests is still allowed
    to try (and will just report "nothing to install")."""
    package_json = local_path / "package.json"
    if package_json.exists():
        data = _read_json(package_json)
        raw_scripts = data.get("scripts")
        scripts: dict[str, object] = raw_scripts if isinstance(raw_scripts, dict) else {}
        test_cmd = "npm test" if "test" in scripts else "npm test --if-present"
        lint_cmd = "npm run lint" if "lint" in scripts else None
        return DetectedStack(
            language="javascript",
            test_command=test_cmd,
            lint_command=lint_cmd,
            install_command="npm install",
        )

    profile = detect_profile(local_path)
    if profile is not None:
        return DetectedStack(
            language=profile.language,
            test_command=profile.test.command,
            lint_command=profile.lint.command if profile.lint else None,
            install_command=profile.install.command if profile.install else None,
        )

    pyproject = local_path / "pyproject.toml"
    requirements = local_path / "requirements.txt"
    if pyproject.exists() or requirements.exists():
        install_command = (
            "pip install -e ." if pyproject.exists() else "pip install -r requirements.txt"
        )
        return DetectedStack(
            language="python",
            test_command="pytest",
            lint_command="ruff check .",
            install_command=install_command,
        )

    return DetectedStack(
        language="unknown", test_command="pytest", lint_command=None, install_command=None
    )


def find_subprojects(local_path: Path, *, max_depth: int = MONOREPO_MAX_DEPTH) -> list[SubProject]:
    """Walk `local_path` up to `max_depth` levels deep (root included, so
    depth 2 means root + 2 levels of subdirectories), skipping
    node_modules/.venv/dist/build/.git/other noise dirs and any hidden
    directory, and return every directory whose own manifest `detect_stack`
    recognizes (i.e. not `language == "unknown"`) as a `SubProject`. A
    directory is checked regardless of whether an ancestor already matched
    (a repo can legitimately have both a root manifest and nested ones), and
    is still recursed into either way."""
    found: list[SubProject] = []

    def walk(dir_path: Path, depth: int) -> None:
        stack = detect_stack(dir_path)
        if stack.language != "unknown":
            rel = "" if dir_path == local_path else dir_path.relative_to(local_path).as_posix()
            found.append(SubProject(rel_path=rel, stack=stack))
        if depth >= max_depth:
            return
        try:
            children = sorted(
                p
                for p in dir_path.iterdir()
                if p.is_dir() and p.name not in _MONOREPO_SKIP_DIRS and not p.name.startswith(".")
            )
        except OSError:
            return
        for child in children:
            walk(child, depth + 1)

    walk(local_path, 0)
    return found


def _resolve_sub_path(local_path: Path, sub_path: str) -> Path:
    """Resolve and validate `sub_path` (as passed to `--path`) is a real
    directory inside `local_path` -- refuses anything that would resolve
    outside the clone (e.g. `../..`), same containment principle as
    `mcp_server/sandbox.py`."""
    normalized = sub_path.strip("/\\")
    target = (local_path / normalized).resolve()
    try:
        target.relative_to(local_path.resolve())
    except ValueError as exc:
        raise RepoConnectError(f"--path {sub_path!r} escapes the repo") from exc
    if not target.is_dir():
        raise RepoConnectError(f"{sub_path!r} is not a directory in this repo")
    return target


async def connect_repo(
    session: AsyncSession,
    *,
    repo_url: str,
    name: str | None,
    github_token: str,
    sub_path: str | None = None,
) -> MonitoredApp:
    """The full "connect a repo" flow: verify access, pick a unique name,
    clone, detect the stack, and insert the `monitored_apps` row.

    Raises RepoConnectError at whichever step fails first, with a message
    meant to be shown directly to the user (see `check_repo_access`'s
    docstring for the access-denied case specifically).
    """
    owner_repo = parse_github_url(repo_url)
    await check_repo_access(owner_repo)

    app_name = (name or slugify_app_name(owner_repo)).lower()
    if not _APP_NAME_PATTERN.match(app_name):
        raise RepoConnectError(
            f"{app_name!r} isn't a valid app name (lowercase letters, digits, "
            "'-' and '_' only, starting with a letter or digit)"
        )
    existing = (
        await session.execute(select(MonitoredApp).where(MonitoredApp.name == app_name))
    ).scalar_one_or_none()
    if existing is not None:
        raise RepoConnectError(f"An app named {app_name!r} is already connected")

    local_path = await clone_repo(owner_repo, app_name, github_token=github_token)

    if sub_path is not None:
        target_dir = _resolve_sub_path(local_path, sub_path)
        stack = detect_stack(target_dir)
    else:
        stack = detect_stack(local_path)
        if stack.language == "unknown":
            subprojects = [sp for sp in find_subprojects(local_path) if sp.rel_path != ""]
            if subprojects:
                raise SubprojectsFound(subprojects)
        target_dir = local_path

    app = MonitoredApp(
        name=app_name,
        language=stack.language,
        local_repo_path=to_repo_relative(target_dir),
        github_repo=owner_repo,
        allowed_write_paths=[f"{to_repo_relative(target_dir)}/"],
        test_command=stack.test_command,
        lint_command=stack.lint_command,
        health_url=None,
        ingest_token=generate_ingest_token(),
        repo_url=repo_url,
    )
    session.add(app)
    await session.flush()
    return app


def select_subproject(app: MonitoredApp, sub_path: str) -> None:
    """Re-point an already-connected app at a different sub-project folder
    of its own clone (`selfheal scan <app> --path <subdir>`) -- mutates
    `local_repo_path`/`allowed_write_paths`/`test_command`/`lint_command` in
    place; caller commits. Because every write-scope/test-run check in this
    codebase (`mcp_server/sandbox.py`'s `check_writable`, `core/scanner.py`'s
    `_app_dir`) reads those fields off the `MonitoredApp` row rather than
    trusting a per-call parameter, repointing the row here is what makes
    "fixes and test runs scoped to that sub-project folder" true for every
    downstream consumer without changing any of them."""
    # local_repo_path may already be a sub-project path (e.g. "connected_apps/foo/backend");
    # re-resolve from the clone root (connected_apps/<name>) so --path is always relative
    # to the repo, not to whatever sub-project is currently selected.
    clone_root = CONNECTED_APPS_ROOT / app.name
    if not clone_root.is_dir():
        raise RepoConnectError(f"App {app.name!r}'s clone directory is missing: {clone_root}")
    target_dir = _resolve_sub_path(clone_root, sub_path)
    stack = detect_stack(target_dir)
    app.language = stack.language
    app.local_repo_path = to_repo_relative(target_dir)
    app.allowed_write_paths = [f"{to_repo_relative(target_dir)}/"]
    app.test_command = stack.test_command
    app.lint_command = stack.lint_command


def generate_ingest_token() -> str:
    return secrets.token_hex(32)


def app_fingerprint(
    app_id: int, tool: str, file_path: str | None, line_number: int | None, message: str
) -> str:
    """Dedup key for a Finding, same scheme as errors/contract_violations:
    hash the identity, not the exact wording, so re-scans upsert rather than
    duplicate. `message` is included (unlike error fingerprints) because two
    different lint rules on the same line are two different findings."""
    normalized_path = (file_path or "").replace("\\", "/")
    digest_input = f"{app_id}:{tool}:{normalized_path}:{line_number}:{message}"
    return hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:32]

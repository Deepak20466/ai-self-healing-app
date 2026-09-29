"""`selfheal audit`: a verdict per sub-project across every (non-archived)
repo `gh` can see for the authenticated user -- via GitHub's API (one file-
tree listing + at most one manifest-content fetch per candidate
sub-project), never cloning a repo. This is what lets it scale to "all my
repos" without touching `connected_apps/` or attempting any install.

Deliberately coarser than `core/repo_health_check.py`'s `selfheal prepare`
(which inspects a real, already-cloned checkout in detail): the audit's
`ci_workflow_exists` check doesn't try to confirm the workflow actually RUNS
tests (that needs reading and interpreting the workflow's own YAML content,
which `prepare` already does for one repo at a time) -- run `selfheal
prepare <app>` on any repo this audit flags for the full checklist before
deciding to onboard or fix it.

Requires the `gh` CLI on PATH and already authenticated (`gh auth status`) --
the same tool this project's own git history has used for GitHub operations
throughout development, reused here rather than adding a second GitHub API
client for one read-only listing feature.
"""

from __future__ import annotations

import asyncio
import base64
import fnmatch
import json
from dataclasses import dataclass

from core.repo_health_check import _TEST_GLOBS
from core.scanner import _HEAVY_DEPENDENCY_NAMES

#: manifest filename -> language, same set `core/repo_connect.py:detect_stack`
#: and `core/language_tools.py:detect_profile` recognize.
_MANIFEST_NAMES: dict[str, str] = {
    "requirements.txt": "python",
    "pyproject.toml": "python",
    "package.json": "javascript",
    "go.mod": "go",
    "pom.xml": "java",
    "build.gradle": "java",
    "build.gradle.kts": "java",
    "composer.json": "php",
    "Gemfile": "ruby",
}

#: only these get a manifest-content fetch to check for heavy dependencies
#: (the denylist is Python/JS-specific; other languages' heavy-dependency
#: story isn't modeled here, same scope limit `core/scanner.py` itself has).
_CONTENT_CHECKED_LANGUAGES = ("python", "javascript")

MAX_SUBPROJECT_DEPTH = 2


class RepoAuditError(Exception):
    """Raised when `gh` itself fails (not installed, not authenticated, rate-limited)."""


@dataclass(frozen=True)
class SubProjectAudit:
    repo: str
    sub_path: str  # "" for the repo root
    language: str
    verdict: str  # "fixable_locally" | "fixable_via_ci" | "needs_prepare" | "not_supported"
    reason: str


async def _run_gh(*args: str) -> str:
    process = await asyncio.create_subprocess_exec(
        "gh",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise RepoAuditError(f"gh {' '.join(args)} failed: {stderr.decode(errors='replace')}")
    return stdout.decode(errors="replace")


async def _gh_json(*args: str) -> object:
    return json.loads(await _run_gh(*args))


async def list_my_repos(*, limit: int = 50) -> list[dict[str, object]]:
    """Non-archived repos `gh` sees for the authenticated user (owned +
    collaborator, via `gh repo list` with no owner argument)."""
    data = await _gh_json(
        "repo",
        "list",
        "--json",
        "nameWithOwner,defaultBranchRef,isArchived,isFork",
        "--limit",
        str(limit),
    )
    if not isinstance(data, list):
        return []
    return [r for r in data if isinstance(r, dict) and not r.get("isArchived")]


async def _repo_tree(name_with_owner: str, branch: str) -> list[str]:
    try:
        data = await _gh_json("api", f"repos/{name_with_owner}/git/trees/{branch}?recursive=1")
    except RepoAuditError:
        return []
    if not isinstance(data, dict):
        return []
    tree = data.get("tree", [])
    if not isinstance(tree, list):
        return []
    return [
        item["path"]
        for item in tree
        if isinstance(item, dict) and item.get("type") == "blob" and "path" in item
    ]


async def _manifest_content(name_with_owner: str, path: str) -> str:
    try:
        data = await _gh_json("api", f"repos/{name_with_owner}/contents/{path}")
    except RepoAuditError:
        return ""
    if not isinstance(data, dict) or "content" not in data:
        return ""
    try:
        return base64.b64decode(str(data["content"])).decode("utf-8", errors="ignore")
    except (ValueError, TypeError):
        return ""


def _find_subproject_dirs(
    paths: list[str], *, max_depth: int = MAX_SUBPROJECT_DEPTH
) -> dict[str, str]:
    """dir path (`""` for root) -> language, for every directory up to
    `max_depth` levels deep containing a recognized manifest at its own
    level. The first manifest found for a given dir wins (matches
    `core/repo_connect.py:detect_stack`'s own root-manifest-first bias)."""
    found: dict[str, str] = {}
    for path in paths:
        parts = path.split("/")
        filename = parts[-1]
        dir_path = "/".join(parts[:-1])
        depth = len(parts) - 1
        if filename in _MANIFEST_NAMES and depth <= max_depth and dir_path not in found:
            found[dir_path] = _MANIFEST_NAMES[filename]
    return found


def _has_test_file(paths: list[str], dir_path: str, language: str) -> bool:
    patterns = _TEST_GLOBS.get(language, ())
    if not patterns:
        return False
    prefix = f"{dir_path}/" if dir_path else ""
    for path in paths:
        if dir_path and not path.startswith(prefix):
            continue
        name = path.rsplit("/", 1)[-1]
        if any(fnmatch.fnmatch(name, pattern) for pattern in patterns):
            return True
    return False


def _has_ci_workflow(paths: list[str]) -> bool:
    """`.github/workflows/` is checked at the repo root only -- the same
    real-world convention `healer/remote_verify.py:has_ci_workflow`'s own
    docstring relies on (a monorepo's sub-projects overwhelmingly share one
    root workflow directory, not one each)."""
    return any(p.startswith(".github/workflows/") and p.endswith((".yml", ".yaml")) for p in paths)


async def audit_repo(name_with_owner: str, default_branch: str) -> list[SubProjectAudit]:
    """The full verdict list for one repo -- one entry per detected
    sub-project, or a single `not_supported` entry if none was found."""
    paths = await _repo_tree(name_with_owner, default_branch)
    if not paths:
        return [
            SubProjectAudit(
                repo=name_with_owner,
                sub_path="",
                language="unknown",
                verdict="not_supported",
                reason="repo is empty, unreadable, or the default branch couldn't be listed",
            )
        ]

    subprojects = _find_subproject_dirs(paths)
    if not subprojects:
        return [
            SubProjectAudit(
                repo=name_with_owner,
                sub_path="",
                language="unknown",
                verdict="not_supported",
                reason="no recognized manifest found (requirements.txt/package.json/go.mod/...)",
            )
        ]

    has_ci = _has_ci_workflow(paths)
    results = []
    for dir_path, language in subprojects.items():
        has_tests = _has_test_file(paths, dir_path, language)

        heavy = False
        if language in _CONTENT_CHECKED_LANGUAGES:
            for name in _MANIFEST_NAMES:
                candidate = f"{dir_path}/{name}" if dir_path else name
                if candidate in paths:
                    content = await _manifest_content(name_with_owner, candidate)
                    heavy = any(h in content.lower() for h in _HEAVY_DEPENDENCY_NAMES)
                    break

        if not has_tests:
            verdict, reason = "needs_prepare", "no test files found (by filename convention)"
        elif heavy and has_ci:
            verdict, reason = (
                "fixable_via_ci",
                "heavy dependencies present; verified by this repo's own CI",
            )
        elif heavy:
            verdict, reason = (
                "needs_prepare",
                "heavy dependencies present but no CI workflow to verify against",
            )
        else:
            verdict, reason = "fixable_locally", "has tests and no heavy dependencies detected"

        results.append(
            SubProjectAudit(
                repo=name_with_owner,
                sub_path=dir_path,
                language=language,
                verdict=verdict,
                reason=reason,
            )
        )
    return results


async def audit_all_repos(*, limit: int = 50) -> list[SubProjectAudit]:
    repos = await list_my_repos(limit=limit)
    results: list[SubProjectAudit] = []
    for repo in repos:
        name = str(repo.get("nameWithOwner", ""))
        if not name:
            continue
        default_branch_ref = repo.get("defaultBranchRef")
        branch = "main"
        if isinstance(default_branch_ref, dict):
            branch = str(default_branch_ref.get("name") or "main")
        results.extend(await audit_repo(name, branch))
    return results

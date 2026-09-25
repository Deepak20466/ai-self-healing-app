"""mcp_server.sandbox: the path-safety boundary every tool goes through."""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_server.sandbox import (
    REPO_ROOT,
    RUNTIME_FIX_ALLOWED_PREFIX,
    SandboxViolation,
    check_diff_paths_writable,
    check_readable,
    check_writable,
    extract_diff_paths,
    resolve_repo_path,
    resolve_worktree_dir,
)


def test_resolve_repo_path_allows_a_normal_relative_path() -> None:
    resolved = resolve_repo_path("apps/target_app/bugs.py")
    assert resolved.name == "bugs.py"


def test_resolve_repo_path_rejects_traversal_escape() -> None:
    with pytest.raises(SandboxViolation):
        resolve_repo_path("../../etc/passwd")


def test_resolve_repo_path_rejects_absolute_escape() -> None:
    """A hardcoded `C:/Windows/...` string is only absolute on Windows —
    `Path`'s `/` operator only discards the left side (`REPO_ROOT`) when the
    right side is absolute *for the current platform*, so on Linux this
    string is just a relative path that lands harmlessly inside the repo,
    and the test silently passed without ever exercising the escape check
    (reproduced for real in CI: `DID NOT RAISE SandboxViolation` on Ubuntu).
    Build a path that's genuinely absolute on whichever OS is running by
    joining `REPO_ROOT`'s own anchor (`/` on POSIX, `C:\\` on Windows) with
    something clearly outside the repo, instead of a platform-specific
    literal.
    """
    outside_the_repo = str(Path(REPO_ROOT.anchor) / "definitely-outside-the-repo")
    with pytest.raises(SandboxViolation):
        resolve_repo_path(outside_the_repo)


def test_check_readable_allows_normal_source_file() -> None:
    check_readable("apps/target_app/bugs.py")  # does not raise


def test_check_readable_rejects_dotenv() -> None:
    with pytest.raises(SandboxViolation):
        check_readable(".env")


def test_check_readable_rejects_dotenv_variants() -> None:
    with pytest.raises(SandboxViolation):
        check_readable(".env.local")


def test_check_readable_allows_dotenv_example() -> None:
    check_readable(".env.example")  # does not raise, not a secret


def test_check_readable_rejects_git_internals() -> None:
    with pytest.raises(SandboxViolation):
        check_readable(".git/config")


def test_check_writable_rejects_dotenv() -> None:
    with pytest.raises(SandboxViolation):
        check_writable(".env")


def test_check_writable_rejects_git_dir() -> None:
    with pytest.raises(SandboxViolation):
        check_writable(".git/hooks/pre-commit")


def test_check_writable_rejects_github_workflows() -> None:
    with pytest.raises(SandboxViolation):
        check_writable(".github/workflows/ci.yml")


def test_check_writable_rejects_alembic_versions() -> None:
    with pytest.raises(SandboxViolation):
        check_writable("alembic/versions/0001_initial.py")


def test_check_writable_allows_normal_source_outside_any_scope() -> None:
    check_writable("sentinel/storage.py")  # does not raise


def test_check_writable_with_runtime_scope_allows_target_app() -> None:
    check_writable("apps/target_app/bugs.py", allowed_prefix=RUNTIME_FIX_ALLOWED_PREFIX)


def test_check_writable_with_runtime_scope_rejects_outside_target_app() -> None:
    with pytest.raises(SandboxViolation):
        check_writable("sentinel/storage.py", allowed_prefix=RUNTIME_FIX_ALLOWED_PREFIX)


def test_extract_diff_paths_reads_standard_unified_diff_headers() -> None:
    diff = (
        "diff --git a/apps/target_app/bugs.py b/apps/target_app/bugs.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/apps/target_app/bugs.py\n"
        "+++ b/apps/target_app/bugs.py\n"
        "@@ -1,3 +1,3 @@\n"
        "-old line\n"
        "+new line\n"
    )
    assert extract_diff_paths(diff) == {"apps/target_app/bugs.py"}


def test_extract_diff_paths_ignores_dev_null() -> None:
    diff = "--- /dev/null\n+++ b/apps/target_app/new_file.py\n"
    assert extract_diff_paths(diff) == {"apps/target_app/new_file.py"}


def test_check_diff_paths_writable_rejects_forbidden_path_in_diff() -> None:
    diff = "--- a/.github/workflows/ci.yml\n+++ b/.github/workflows/ci.yml\n"
    with pytest.raises(SandboxViolation):
        check_diff_paths_writable(diff)


def test_check_diff_paths_writable_rejects_out_of_scope_for_runtime_fix() -> None:
    diff = "--- a/sentinel/storage.py\n+++ b/sentinel/storage.py\n"
    with pytest.raises(SandboxViolation):
        check_diff_paths_writable(diff, allowed_prefix=RUNTIME_FIX_ALLOWED_PREFIX)


def test_check_diff_paths_writable_accepts_in_scope_diff() -> None:
    diff = "--- a/apps/target_app/bugs.py\n+++ b/apps/target_app/bugs.py\n"
    touched = check_diff_paths_writable(diff, allowed_prefix=RUNTIME_FIX_ALLOWED_PREFIX)
    assert touched == {"apps/target_app/bugs.py"}


def test_check_diff_paths_writable_rejects_empty_diff() -> None:
    with pytest.raises(SandboxViolation):
        check_diff_paths_writable("no file headers here")


def test_resolve_worktree_dir_rejects_traversal_in_name() -> None:
    with pytest.raises(SandboxViolation):
        resolve_worktree_dir("../outside")


def test_resolve_worktree_dir_rejects_path_separators() -> None:
    with pytest.raises(SandboxViolation):
        resolve_worktree_dir("sub/dir")


def test_resolve_worktree_dir_rejects_nonexistent_worktree() -> None:
    with pytest.raises(SandboxViolation):
        resolve_worktree_dir("does-not-exist-12345")


def test_resolve_worktree_dir_accepts_a_real_worktree(git_worktree: tuple[str, Path]) -> None:
    name, path = git_worktree
    resolved = resolve_worktree_dir(name)
    assert resolved == path.resolve()

"""Tests for core/repo_audit.py: the pure classification logic (given a
file-path listing, no real `gh`/network call) plus `audit_repo`'s async
flow with `_repo_tree`/`_manifest_content` monkeypatched -- no real `gh`
subprocess is ever spawned in these tests.
"""

from __future__ import annotations

import pytest

from core.repo_audit import (
    SubProjectAudit,
    _find_subproject_dirs,
    _has_ci_workflow,
    _has_test_file,
    audit_repo,
)


def test_find_subproject_dirs_finds_root_manifest() -> None:
    paths = ["package.json", "src/app.js"]
    assert _find_subproject_dirs(paths) == {"": "javascript"}


def test_find_subproject_dirs_finds_nested_manifests() -> None:
    paths = ["backend/requirements.txt", "backend/app.py", "frontend/package.json"]
    assert _find_subproject_dirs(paths) == {"backend": "python", "frontend": "javascript"}


def test_find_subproject_dirs_ignores_manifests_past_max_depth() -> None:
    paths = ["a/b/c/requirements.txt"]
    assert _find_subproject_dirs(paths, max_depth=2) == {}
    assert _find_subproject_dirs(paths, max_depth=3) == {"a/b/c": "python"}


def test_has_test_file_scoped_to_its_own_subproject() -> None:
    paths = ["backend/test_app.py", "frontend/app.test.js"]
    assert _has_test_file(paths, "backend", "python") is True
    assert _has_test_file(paths, "frontend", "python") is False
    assert _has_test_file(paths, "frontend", "javascript") is True


def test_has_ci_workflow_checks_repo_root_only() -> None:
    assert _has_ci_workflow([".github/workflows/ci.yml"]) is True
    assert _has_ci_workflow(["backend/.github/workflows/ci.yml"]) is False
    assert _has_ci_workflow(["README.md"]) is False


async def test_audit_repo_not_supported_when_no_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.repo_audit as repo_audit_module

    async def _fake_tree(name: str, branch: str) -> list[str]:
        return ["README.md"]

    monkeypatch.setattr(repo_audit_module, "_repo_tree", _fake_tree)
    results = await audit_repo("acme/empty", "main")
    assert results == [
        SubProjectAudit(
            repo="acme/empty",
            sub_path="",
            language="unknown",
            verdict="not_supported",
            reason="no recognized manifest found (requirements.txt/package.json/go.mod/...)",
        )
    ]


async def test_audit_repo_needs_prepare_when_no_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.repo_audit as repo_audit_module

    async def _fake_tree(name: str, branch: str) -> list[str]:
        return ["requirements.txt", "app.py"]

    monkeypatch.setattr(repo_audit_module, "_repo_tree", _fake_tree)
    results = await audit_repo("acme/no-tests", "main")
    assert len(results) == 1
    assert results[0].verdict == "needs_prepare"
    assert "no test files" in results[0].reason


async def test_audit_repo_fixable_locally_with_tests_and_light_deps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.repo_audit as repo_audit_module

    async def _fake_tree(name: str, branch: str) -> list[str]:
        return ["requirements.txt", "app.py", "test_app.py"]

    async def _fake_manifest(name: str, path: str) -> str:
        return "fastapi\nsqlalchemy\n"

    monkeypatch.setattr(repo_audit_module, "_repo_tree", _fake_tree)
    monkeypatch.setattr(repo_audit_module, "_manifest_content", _fake_manifest)
    results = await audit_repo("acme/good", "main")
    assert len(results) == 1
    assert results[0].verdict == "fixable_locally"


async def test_audit_repo_fixable_via_ci_when_heavy_deps_and_ci_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.repo_audit as repo_audit_module

    async def _fake_tree(name: str, branch: str) -> list[str]:
        return ["requirements.txt", "test_app.py", ".github/workflows/ci.yml"]

    async def _fake_manifest(name: str, path: str) -> str:
        return "torch>=2.0\n"

    monkeypatch.setattr(repo_audit_module, "_repo_tree", _fake_tree)
    monkeypatch.setattr(repo_audit_module, "_manifest_content", _fake_manifest)
    results = await audit_repo("acme/heavy-ci", "main")
    assert len(results) == 1
    assert results[0].verdict == "fixable_via_ci"


async def test_audit_repo_needs_prepare_when_heavy_deps_and_no_ci(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.repo_audit as repo_audit_module

    async def _fake_tree(name: str, branch: str) -> list[str]:
        return ["requirements.txt", "test_app.py"]

    async def _fake_manifest(name: str, path: str) -> str:
        return "torch>=2.0\n"

    monkeypatch.setattr(repo_audit_module, "_repo_tree", _fake_tree)
    monkeypatch.setattr(repo_audit_module, "_manifest_content", _fake_manifest)
    results = await audit_repo("acme/heavy-no-ci", "main")
    assert len(results) == 1
    assert results[0].verdict == "needs_prepare"
    assert "no CI workflow" in results[0].reason


async def test_audit_repo_handles_a_monorepo_with_mixed_verdicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.repo_audit as repo_audit_module

    async def _fake_tree(name: str, branch: str) -> list[str]:
        return [
            "backend/requirements.txt",
            "backend/test_app.py",
            "frontend/package.json",
        ]

    async def _fake_manifest(name: str, path: str) -> str:
        return "fastapi\n"

    monkeypatch.setattr(repo_audit_module, "_repo_tree", _fake_tree)
    monkeypatch.setattr(repo_audit_module, "_manifest_content", _fake_manifest)
    results = await audit_repo("acme/mono", "main")
    verdicts = {r.sub_path: r.verdict for r in results}
    assert verdicts == {"backend": "fixable_locally", "frontend": "needs_prepare"}

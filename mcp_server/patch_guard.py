"""Patch-level safety guardrails for `propose_patch` (SPEC.md SAFETY GUARDRAILS).

Two independent, diff-text-level checks, both enforced here in code — never
only in a prompt — so a prompt-injection payload embedded in an error
message, traceback, or CI log can never talk Claude into shipping an
oversized patch or weakening the test suite to "fix" something:

  - `check_patch_limits`: max files / max changed lines (from `.env`).
  - `check_not_cheating`: rejects diffs that delete or disable a test,
    weaken coverage config, or add `# noqa` / `# type: ignore` just to pass.

Deliberately conservative: these are heuristics over the unified diff text,
not a full AST diff. A false positive (a legitimate patch rejected) is a
safe failure mode here — the caller retries with a smaller/cleaner patch —
whereas a false negative (a cheat that slips through) is exactly what
SPEC.md's "never make CI pass by cheating" rule exists to prevent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from core.config import settings
from mcp_server.sandbox import SandboxViolation

_TEST_PATH_RE = re.compile(r"(^|/)tests?/.*\.py$|(^|/)test_[^/]*\.py$|_test\.py$")
_DEF_TEST_RE = re.compile(r"^(?:async\s+)?def (test_\w+)\s*\(")
_SKIP_MARK_RE = re.compile(r"@pytest\.mark\.(skip|xfail)\b")
_PYTEST_SKIP_CALL_RE = re.compile(r"\bpytest\.(skip|xfail)\s*\(")
_SUPPRESSION_RE = re.compile(r"#\s*(type:\s*ignore|noqa)\b")
_COVERAGE_CONFIG_PATHS = ("pyproject.toml", ".coveragerc", "setup.cfg", "tox.ini")
_COVERAGE_KEY_RE = re.compile(r"\b(fail_under|--cov-fail-under|--no-cov)\b")

_FILE_OLD_HEADER_RE = re.compile(r"^--- (?:a/)?(?P<path>\S+)")
_FILE_NEW_HEADER_RE = re.compile(r"^\+\+\+ (?:b/)?(?P<path>\S+)")


@dataclass(frozen=True)
class _FileSection:
    old_path: str
    new_path: str
    body: list[str]


def _split_file_sections(unified_diff: str) -> list[_FileSection]:
    """Split a unified diff into per-file (old_path, new_path, hunk lines).

    A standard `git diff`/`git apply`-compatible diff always emits a
    `--- a/X` line immediately followed by `+++ b/Y`, then that file's `@@`
    hunks until the next file's `--- ` header (or EOF).
    """
    lines = unified_diff.splitlines()
    sections: list[_FileSection] = []
    i = 0
    pending_old: str | None = None
    while i < len(lines):
        old_match = _FILE_OLD_HEADER_RE.match(lines[i])
        if old_match:
            pending_old = old_match.group("path")
            i += 1
            continue

        new_match = _FILE_NEW_HEADER_RE.match(lines[i]) if pending_old is not None else None
        if new_match:
            new_path = new_match.group("path")
            i += 1
            body: list[str] = []
            while i < len(lines) and not lines[i].startswith("--- "):
                body.append(lines[i])
                i += 1
            sections.append(_FileSection(old_path=pending_old or "", new_path=new_path, body=body))
            pending_old = None
            continue

        i += 1
    return sections


def check_patch_limits(unified_diff: str, touched_paths: set[str]) -> None:
    """Reject a diff touching more than `max_patch_files`/`max_patch_changed_lines`."""
    if len(touched_paths) > settings.max_patch_files:
        raise SandboxViolation(
            f"Patch touches {len(touched_paths)} files, over the "
            f"{settings.max_patch_files}-file limit"
        )

    changed_lines = sum(
        1
        for line in unified_diff.splitlines()
        if (line.startswith("+") or line.startswith("-"))
        and not (line.startswith("+++") or line.startswith("---"))
    )
    if changed_lines > settings.max_patch_changed_lines:
        raise SandboxViolation(
            f"Patch changes {changed_lines} lines, over the "
            f"{settings.max_patch_changed_lines}-line limit"
        )


def check_not_cheating(unified_diff: str) -> None:
    """Reject a diff that tries to make tests/CI pass by cheating instead of fixing.

    Checks (any one violation rejects the whole diff):
      - deleting a test file
      - net-removing a test function (removed without being re-added)
      - adding `@pytest.mark.skip`/`xfail` or a `pytest.skip()`/`xfail()` call
      - adding `# noqa` / `# type: ignore`
      - weakening coverage config (`fail_under`, `--cov-fail-under`, `--no-cov`)
    """
    sections = _split_file_sections(unified_diff)

    removed_tests: set[str] = set()
    added_tests: set[str] = set()

    for section in sections:
        is_test_file = bool(
            _TEST_PATH_RE.search(section.old_path) or _TEST_PATH_RE.search(section.new_path)
        )

        if is_test_file and section.new_path == "/dev/null":
            raise SandboxViolation(f"Patch deletes a test file: {section.old_path!r}")

        for line in section.body:
            if line.startswith("+++") or line.startswith("---"):
                continue

            if line.startswith("-") and is_test_file:
                match = _DEF_TEST_RE.match(line[1:].strip())
                if match:
                    removed_tests.add(match.group(1))

            if line.startswith("+"):
                content = line[1:]
                if is_test_file:
                    stripped = content.strip()
                    match = _DEF_TEST_RE.match(stripped)
                    if match:
                        added_tests.add(match.group(1))
                    if _SKIP_MARK_RE.search(content) or _PYTEST_SKIP_CALL_RE.search(content):
                        raise SandboxViolation(
                            f"Patch adds a skip/xfail marker to a test in {section.new_path!r}"
                        )

                if _SUPPRESSION_RE.search(content):
                    raise SandboxViolation(
                        f"Patch adds a '# noqa' or '# type: ignore' suppression in "
                        f"{section.new_path!r} — fix the underlying issue instead"
                    )

                basename = section.new_path.rsplit("/", 1)[-1]
                if basename in _COVERAGE_CONFIG_PATHS and _COVERAGE_KEY_RE.search(content):
                    raise SandboxViolation(
                        f"Patch weakens coverage/test configuration in {section.new_path!r}"
                    )

    silently_removed = removed_tests - added_tests
    if silently_removed:
        raise SandboxViolation(
            f"Patch removes existing test function(s) without replacing them: "
            f"{sorted(silently_removed)}"
        )

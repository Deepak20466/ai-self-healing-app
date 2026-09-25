"""scripts.reset_demo_bugs: pure file-restore logic, no real git/GitHub.

`reset_via_pr()` (git worktree + push + `gh pr create`) is deliberately not
exercised here — it needs a real remote and `gh` auth, the same reason
`scripts/local_deploy.py` isn't unit-tested either. `restore_files()` is the
part with real logic (byte-exact restore, change detection) and is fully
testable against a throwaway directory standing in for the repo root.
"""

from __future__ import annotations

from pathlib import Path

from scripts.reset_demo_bugs import ORIGINALS_DIR, RESTORE_TARGETS, restore_files


def _make_fake_repo(tmp_path: Path, *, contents: dict[str, bytes]) -> Path:
    for rel_path, data in contents.items():
        target = tmp_path / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return tmp_path


def test_restore_files_overwrites_a_modified_bug_file(tmp_path: Path) -> None:
    # Use a real snapshot's target, but seed the fake repo root with a
    # "fixed" (modified) version of it first, to prove restore overwrites it.
    rel_path = next(iter(RESTORE_TARGETS))
    original_bytes = (ORIGINALS_DIR / RESTORE_TARGETS[rel_path]).read_bytes()

    fake_repo = _make_fake_repo(tmp_path, contents={rel_path: b"not the original content"})

    changed = restore_files(fake_repo)

    assert rel_path in changed
    assert (fake_repo / rel_path).read_bytes() == original_bytes


def test_restore_files_is_a_noop_when_already_original(tmp_path: Path) -> None:
    contents = {
        rel_path: (ORIGINALS_DIR / snapshot_name).read_bytes()
        for rel_path, snapshot_name in RESTORE_TARGETS.items()
    }
    fake_repo = _make_fake_repo(tmp_path, contents=contents)

    changed = restore_files(fake_repo)

    assert changed == []


def test_restore_files_creates_missing_parent_directories(tmp_path: Path) -> None:
    fake_repo = tmp_path  # nothing pre-created under apps/target_app or tests/

    changed = restore_files(fake_repo)

    assert set(changed) == set(RESTORE_TARGETS)
    for rel_path in RESTORE_TARGETS:
        assert (fake_repo / rel_path).is_file()


def test_all_restore_targets_have_a_real_snapshot_on_disk() -> None:
    for snapshot_name in RESTORE_TARGETS.values():
        assert (ORIGINALS_DIR / snapshot_name).is_file()


def test_restored_bugs_file_matches_the_live_seeded_bug_source() -> None:
    """The stored snapshot must actually be the buggy version, not a stale or
    already-fixed copy -- assert it contains the known bug #1 body rather
    than trusting the file merely exists."""
    snapshot = (ORIGINALS_DIR / RESTORE_TARGETS["apps/target_app/bugs.py"]).read_text(
        encoding="utf-8"
    )
    assert "return item.rating_sum / item.rating_count" in snapshot
    assert "utc_date = order.created_at.date()" in snapshot
    assert "utc_date = order.created_at.astimezone(STOREFRONT_TZ).date()" not in snapshot

"""Per-language anti-cheat (patch guard) and scanner stack detection / skipped tools."""

from __future__ import annotations

import pytest

from core import scanner
from core.language_tools import Check, ToolProfile, detect_profile
from core.repo_connect import detect_stack
from mcp_server.patch_guard import check_not_cheating
from mcp_server.sandbox import SandboxViolation


def _diff(path: str, *lines: str) -> str:
    body = "\n".join(lines)
    return f"--- a/{path}\n+++ b/{path}\n@@ -1,3 +1,4 @@\n{body}\n"


# (test file path, added line that disables/skips a test)
SKIPS = [
    ("src/math.test.js", "+  it.skip('adds', () => {});"),
    ("src/math.spec.ts", "+  test.only('adds', () => {});"),
    ("src/math.test.js", "+  xit('adds', () => {});"),
    ("src/test/java/com/a/CartTest.java", "+    @Disabled"),
    ("src/test/java/com/a/CartTest.java", "+    @Ignore"),
    ("main_test.go", '+\tt.Skip("flaky")'),
    ("Tests/CartTests.cs", "+    [Ignore]"),
    ("Tests/CartTests.cs", '+    [Fact(Skip = "later")]'),
    ("tests/CartTest.php", '+        $this->markTestSkipped("later");'),
    ("spec/cart_spec.rb", "+  skip 'later'"),
    ("spec/cart_spec.rb", "+  pending 'later'"),
    ("test/cart_test.rb", "+    skip"),
]


@pytest.mark.parametrize(("path", "line"), SKIPS)
def test_added_skip_marker_is_rejected(path: str, line: str) -> None:
    with pytest.raises(SandboxViolation):
        check_not_cheating(_diff(path, " context", line))


# (test file path, removed test declaration)
REMOVALS = [
    ("src/math.test.js", "-  it('adds', () => {"),
    ("src/test/java/com/a/CartTest.java", "-    @Test"),
    ("main_test.go", "-func TestAdd(t *testing.T) {"),
    ("Tests/CartTests.cs", "-    [Fact]"),
    ("tests/CartTest.php", "-    public function testTotal() {"),
    ("spec/cart_spec.rb", '-  it "totals" do'),
]


@pytest.mark.parametrize(("path", "line"), REMOVALS)
def test_removing_a_test_without_replacement_is_rejected(path: str, line: str) -> None:
    with pytest.raises(SandboxViolation):
        check_not_cheating(_diff(path, " context", line))


@pytest.mark.parametrize(
    "path", ["src/math.test.js", "main_test.go", "src/test/java/A.java", "spec/a_spec.rb"]
)
def test_deleting_a_test_file_is_rejected(path: str) -> None:
    diff = f"--- a/{path}\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-x\n-y\n"
    with pytest.raises(SandboxViolation):
        check_not_cheating(diff)


def test_legitimate_multilang_fix_passes() -> None:
    check_not_cheating(_diff("src/math.js", " a", "-  return a / b;", "+  return b ? a / b : 0;"))
    check_not_cheating(_diff("main_test.go", " a", "+func TestNew(t *testing.T) {"))
    check_not_cheating(
        _diff("spec/a_spec.rb", " a", '-  it "old" do', '+  it "renamed" do')  # net zero
    )


@pytest.mark.parametrize(
    ("files", "language", "test_cmd"),
    [
        (["package.json"], "javascript", "npm test --if-present"),
        (["go.mod"], "go", "go test ./..."),
        (["pom.xml"], "java", "mvn -B -q test"),
        (["build.gradle"], "java", "gradle test --no-daemon"),
        (["build.gradle.kts"], "java", "gradle test --no-daemon"),
        (["App.csproj"], "csharp", "dotnet test"),
        (["composer.json"], "php", "vendor/bin/phpunit"),
        (["Gemfile"], "ruby", "bundle exec rake test"),
        (["requirements.txt"], "python", "pytest"),
    ],
)
def test_detect_stack_per_language(
    tmp_path, files: list[str], language: str, test_cmd: str
) -> None:
    for name in files:
        (tmp_path / name).write_text("{}")
    stack = detect_stack(tmp_path)
    assert stack.language == language
    assert stack.test_command == test_cmd


def test_ruby_with_spec_dir_uses_rspec(tmp_path) -> None:
    (tmp_path / "Gemfile").write_text("")
    (tmp_path / "spec").mkdir()
    assert detect_profile(tmp_path).test.command == "bundle exec rspec"  # type: ignore[union-attr]


async def test_missing_tools_are_skipped_not_failed(tmp_path) -> None:
    profile = ToolProfile(
        language="java",
        build_tool="maven",
        install=Check("nope-install", "definitely-not-a-real-binary-xyz"),
        test=Check("nope-test", "definitely-not-a-real-binary-xyz"),
        lint=Check("nope-lint", "definitely-not-a-real-binary-xyz"),
        audit=Check("nope-audit", "definitely-not-a-real-binary-xyz"),
    )

    async def noop(_s: str, _p: int) -> None:
        return None

    drafts, tests_passed, skipped = await scanner._run_profile_checks(
        profile, tmp_path, "nope-test", noop
    )
    assert drafts == []
    assert tests_passed is None
    assert len(skipped) == 4
    assert all("skipped: tool not installed" in s for s in skipped)


async def test_present_tool_failure_becomes_a_finding(tmp_path) -> None:
    import sys

    py = f'"{sys.executable}"'
    profile = ToolProfile(
        language="go",
        build_tool="go",
        install=None,
        test=Check(f'{py} -c "raise SystemExit(1)"', sys.executable),
        lint=None,
        audit=None,
    )

    async def noop(_s: str, _p: int) -> None:
        return None

    drafts, tests_passed, skipped = await scanner._run_profile_checks(
        profile, tmp_path, profile.test.command, noop
    )
    assert tests_passed is False
    assert [d.tool for d in drafts] == ["test-runner"]
    assert skipped == ["audit: skipped: no standard go audit command"]

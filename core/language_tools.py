"""Per-language build/test/lint/audit tool profiles for the scanner.

Why a table: adding a language to "connect a repo" should be data, not new
control flow. Python and JavaScript keep their bespoke scanner paths
(per-app venv, npm audit parsing); every other language runs through the
generic profile runner in `core/scanner.py`, which turns each command's
exit status into a finding and marks a check "skipped: tool not installed"
when its binary isn't on PATH (a missing toolchain is an environment fact,
not a finding about the connected repo).

Each `Check.requires` is either a bare binary name (looked up on PATH) or a
repo-relative path containing "/" (e.g. `vendor/bin/phpunit`, which only
exists after `composer install`).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Check:
    command: str
    requires: str


@dataclass(frozen=True)
class ToolProfile:
    language: str
    build_tool: str
    install: Check | None
    test: Check
    lint: Check | None
    audit: Check | None
    #: substring in a zero-exit audit's output that still means "vulnerable"
    #: (`dotnet list package --vulnerable` always exits 0).
    audit_vulnerable_marker: str | None = None


def _gradle(path: Path) -> ToolProfile:
    wrapper = "gradlew.bat" if sys.platform == "win32" else "./gradlew"
    has_wrapper = (path / "gradlew").exists()
    cmd = wrapper if has_wrapper else "gradle"
    requires = "gradlew" if has_wrapper else "gradle"
    return ToolProfile(
        language="java",
        build_tool="gradle",
        install=None,
        test=Check(f"{cmd} test --no-daemon", requires),
        lint=Check(f"{cmd} check -x test --no-daemon", requires),
        audit=None,  # no standard, dependency-free Gradle audit command
    )


_MAVEN = ToolProfile(
    language="java",
    build_tool="maven",
    install=Check("mvn -B -q dependency:resolve", "mvn"),
    test=Check("mvn -B -q test", "mvn"),
    lint=Check("mvn -B -q checkstyle:check", "mvn"),
    audit=Check("mvn -B -q org.owasp:dependency-check-maven:check", "mvn"),
)

_GO = ToolProfile(
    language="go",
    build_tool="go",
    install=Check("go mod download", "go"),
    test=Check("go test ./...", "go"),
    lint=Check("go vet ./...", "go"),
    audit=Check("govulncheck ./...", "govulncheck"),
)

_DOTNET = ToolProfile(
    language="csharp",
    build_tool="dotnet",
    install=Check("dotnet restore", "dotnet"),
    test=Check("dotnet test", "dotnet"),
    lint=Check("dotnet format --verify-no-changes", "dotnet"),
    audit=Check("dotnet list package --vulnerable --include-transitive", "dotnet"),
    audit_vulnerable_marker="has the following vulnerable packages",
)

_PHP = ToolProfile(
    language="php",
    build_tool="composer",
    install=Check("composer install --no-interaction --no-progress", "composer"),
    test=Check("vendor/bin/phpunit", "vendor/bin/phpunit"),
    lint=Check("vendor/bin/phpcs", "vendor/bin/phpcs"),
    audit=Check("composer audit", "composer"),
)


def _ruby(path: Path) -> ToolProfile:
    test = "bundle exec rspec" if (path / "spec").is_dir() else "bundle exec rake test"
    return ToolProfile(
        language="ruby",
        build_tool="bundler",
        install=Check("bundle install", "bundle"),
        test=Check(test, "bundle"),
        lint=Check("bundle exec rubocop", "bundle"),
        audit=Check("bundle exec bundle-audit check", "bundle"),
    )


def detect_profile(path: Path) -> ToolProfile | None:
    """Pick a non-Python/JS profile from the repo root's manifest, else None."""
    if (path / "go.mod").exists():
        return _GO
    if (path / "pom.xml").exists():
        return _MAVEN
    if (path / "build.gradle").exists() or (path / "build.gradle.kts").exists():
        return _gradle(path)
    if any(path.glob("*.csproj")) or any(path.glob("*.sln")):
        return _DOTNET
    if (path / "composer.json").exists():
        return _PHP
    if (path / "Gemfile").exists():
        return _ruby(path)
    return None

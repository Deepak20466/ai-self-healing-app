"""`selfheal prepare`: a static, read-only report of what's missing for a
connected app to be self-healable -- never runs the app's own code (no
install, no test execution, no AI call). This is deliberately the same
never-execute posture `core/scanner.py`'s static-only mode already uses for
heavy-dependency apps, just applied to every app up front, before deciding
whether a fix is even attemptable.

Four checks, each answerable from the files already on disk in the app's
own clone:
1. Does it have any test files at all (by filename convention, per
   language -- `test_*.py`, `*.test.js`, `*_test.go`, ...)?
2. Does it have a GitHub Actions workflow, and does that workflow actually
   run tests (not just lint/build)?
3. Does its manifest depend on a client library for an external service
   (Postgres/MySQL/Redis/MongoDB) that a CI run would need a real
   service/service-container for?
4. Does its manifest name a heavy/ML dependency (see
   `core.scanner.detect_heavy_dependencies`) that this project will never
   install, meaning any fix can only ever be CI-verified, never locally?

`missing` is the plain-English checklist `selfheal prepare` prints and
`healer/onboarding_prepare.py` uses to decide what an onboarding PR should
add.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from core.scanner import detect_heavy_dependencies

_MONOREPO_SKIP_DIRS = frozenset(
    {"node_modules", ".venv", "dist", "build", ".git", "__pycache__", ".selfheal_venv"}
)

#: filename glob patterns that count as "a test file", by language.
_TEST_GLOBS: dict[str, tuple[str, ...]] = {
    "python": ("test_*.py", "*_test.py"),
    "javascript": ("*.test.js", "*.spec.js", "*.test.ts", "*.spec.ts", "*.test.jsx", "*.test.tsx"),
    "go": ("*_test.go",),
    "java": ("*Test.java", "*Tests.java"),
    "csharp": ("*Test.cs", "*Tests.cs"),
    "php": ("*Test.php",),
    "ruby": ("*_spec.rb", "test_*.rb"),
}

#: manifest dependency name -> (service label, packages that indicate it).
_SERVICE_CLIENT_PACKAGES: dict[str, tuple[str, ...]] = {
    "postgresql": ("psycopg2", "psycopg2-binary", "asyncpg", "pg", "pg-promise"),
    "mysql": ("pymysql", "mysqlclient", "mysql2", "mysql-connector-python"),
    "redis": ("redis", "ioredis", "aioredis"),
    "mongodb": ("pymongo", "motor", "mongoose", "mongodb"),
}


@dataclass(frozen=True)
class PrepareReport:
    app_name: str
    language: str
    has_tests: bool
    test_file_count: int
    has_ci_workflow: bool
    ci_runs_tests: bool
    external_services: tuple[str, ...]
    heavy_dependencies: tuple[str, ...]
    missing: tuple[str, ...] = field(default_factory=tuple)

    @property
    def already_fixable(self) -> bool:
        """True iff nothing on the checklist is missing -- the normal fix
        path (local verification, or remote-verify for heavy deps) already
        works for this app; an onboarding PR would have nothing to add."""
        return not self.missing


def _iter_files(root: Path, *, max_files: int = 5000) -> list[Path]:
    found: list[Path] = []

    def walk(dir_path: Path) -> None:
        if len(found) >= max_files:
            return
        try:
            entries = list(dir_path.iterdir())
        except OSError:
            return
        for entry in entries:
            if len(found) >= max_files:
                return
            if entry.is_dir():
                if entry.name not in _MONOREPO_SKIP_DIRS and not entry.name.startswith("."):
                    walk(entry)
            elif entry.is_file():
                found.append(entry)

    walk(root)
    return found


def _count_test_files(app_dir: Path, language: str) -> int:
    import fnmatch

    patterns = _TEST_GLOBS.get(language, ())
    if not patterns:
        return 0
    count = 0
    for path in _iter_files(app_dir):
        if any(fnmatch.fnmatch(path.name, pattern) for pattern in patterns):
            count += 1
    return count


def _workflow_files(app_dir: Path) -> list[Path]:
    workflows_dir = app_dir / ".github" / "workflows"
    if not workflows_dir.is_dir():
        return []
    return sorted(
        p for p in workflows_dir.iterdir() if p.is_file() and p.suffix in (".yml", ".yaml")
    )


def _ci_runs_tests(app_dir: Path) -> bool:
    """Crude but honest: a workflow "runs tests" if any of its `run:` step
    text mentions a common test-runner invocation. False negatives (a
    custom test script with an unusual name) are possible -- `selfheal
    prepare` is a heuristic checklist, not a guarantee."""
    markers = (
        "pytest",
        "npm test",
        "npm run test",
        "go test",
        "mvn test",
        "gradle test",
        "dotnet test",
        "phpunit",
        "rspec",
        "bundle exec rake test",
        "yarn test",
    )
    for workflow in _workflow_files(app_dir):
        text = workflow.read_text(encoding="utf-8", errors="ignore").lower()
        if any(marker in text for marker in markers):
            return True
    return False


def _manifest_dependency_names(app_dir: Path) -> set[str]:
    names: set[str] = set()
    requirements = app_dir / "requirements.txt"
    if requirements.exists():
        for line in requirements.read_text(encoding="utf-8", errors="ignore").splitlines():
            pkg = line.split("#", 1)[0].strip()
            pkg = pkg.split("=", 1)[0].split(">", 1)[0].split("<", 1)[0].split("[", 1)[0].strip()
            if pkg:
                names.add(pkg.lower())
    package_json = app_dir / "package.json"
    if package_json.exists():
        import json

        try:
            data = json.loads(package_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
        if isinstance(data, dict):
            for section in ("dependencies", "devDependencies"):
                for name in data.get(section) or {}:
                    names.add(str(name).lower())
    return names


def _external_services(app_dir: Path) -> tuple[str, ...]:
    deps = _manifest_dependency_names(app_dir)
    found = []
    for service, packages in _SERVICE_CLIENT_PACKAGES.items():
        if any(pkg.lower() in deps for pkg in packages):
            found.append(service)
    return tuple(sorted(found))


def analyze_repo(app_dir: Path, *, app_name: str, language: str) -> PrepareReport:
    """The full static checklist for one app (or sub-project) directory."""
    test_file_count = _count_test_files(app_dir, language)
    has_tests = test_file_count > 0
    workflows = _workflow_files(app_dir)
    has_ci = bool(workflows)
    ci_tests = _ci_runs_tests(app_dir) if has_ci else False
    services = _external_services(app_dir)
    heavy = tuple(detect_heavy_dependencies(app_dir))

    missing = []
    if not has_tests:
        missing.append("No test files found (by filename convention for this language).")
    if not has_ci:
        missing.append("No GitHub Actions workflow found.")
    elif not ci_tests:
        missing.append(
            "A GitHub Actions workflow exists but doesn't appear to run tests "
            "(no pytest/npm test/go test/... invocation found)."
        )
    has_test_config = (app_dir / ".env.test").exists() or (app_dir / ".env.example").exists()
    if services and not has_test_config:
        missing.append(
            f"Uses external service(s) ({', '.join(services)}) with no visible test "
            "config (.env.test/.env.example) -- CI will need service containers and "
            "placeholder credentials."
        )
    if heavy:
        missing.append(
            f"Heavy dependencies present ({', '.join(heavy)}) -- this project never "
            "installs them locally, so any fix can only be verified by this repo's own CI."
        )

    return PrepareReport(
        app_name=app_name,
        language=language,
        has_tests=has_tests,
        test_file_count=test_file_count,
        has_ci_workflow=has_ci,
        ci_runs_tests=ci_tests,
        external_services=services,
        heavy_dependencies=heavy,
        missing=tuple(missing),
    )

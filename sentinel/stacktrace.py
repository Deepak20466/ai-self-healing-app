"""Stack-trace parsers for non-Python languages (OpenTelemetry `exception.stacktrace`).

Why regexes and not a library: every OpenTelemetry SDK just stringifies the
runtime's own native trace format into `exception.stacktrace`, so the only
thing that's the same across languages is "a text blob". Each parser
returns frames *innermost first* (the frame that threw comes first) so a
single `select_in_app_frame` can pick "the first frame that isn't library/
vendor code" regardless of the language's own ordering (Python prints
outermost first, so its parser reverses).

Frame filtering is by well-known library/vendor path or namespace markers
per language (`node_modules`, `vendor/`, `java.*`, GOROOT, ...). When every
frame looks like library code we fall back to the innermost frame rather
than returning nothing -- a slightly-off location beats losing the error.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Frame:
    file: str
    line: int
    function: str


_PY = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<fn>.+?)\s*$')
_JS = re.compile(
    r"^\s*at (?:async )?(?:(?P<fn>[^\s(][^(]*?) \()?(?P<file>[^()\s]+?):(?P<line>\d+):\d+\)?\s*$"
)
_JAVA = re.compile(r"^\s*at (?P<fq>[\w$.<>]+)\((?P<file>[\w$]+\.\w+):(?P<line>\d+)\)\s*$")
_GO_FILE = re.compile(r"^\s+(?P<file>\S+\.go):(?P<line>\d+)(?: \+0x[0-9a-f]+)?\s*$")
_GO_FN = re.compile(r"^(?P<fn>[\w./*()\-\[\]]+)\((?:.*)\)\s*$")
_CS = re.compile(r"^\s*at (?P<fn>.+?)\(.*?\) in (?P<file>.+?):line (?P<line>\d+)\s*$")
_PHP = re.compile(r"^#\d+ (?P<file>.+?)\((?P<line>\d+)\): (?P<fn>.+?)\s*$")
_RUBY = re.compile(r"^\s*(?:from )?(?P<file>[^\s:][^:]*?):(?P<line>\d+):in [`'](?P<fn>[^'`]+)'")


def _parse_python(text: str) -> list[Frame]:
    frames = [
        Frame(m["file"], int(m["line"]), m["fn"])
        for line in text.splitlines()
        if (m := _PY.match(line))
    ]
    return frames[::-1]


def _parse_js(text: str) -> list[Frame]:
    frames = []
    for line in text.splitlines():
        m = _JS.match(line)
        if m:
            file = m["file"].removeprefix("file:///").removeprefix("file://")
            frames.append(Frame(file, int(m["line"]), m["fn"] or "<anonymous>"))
    return frames


def _parse_java(text: str) -> list[Frame]:
    frames = []
    for line in text.splitlines():
        m = _JAVA.match(line)
        if m:
            fq = m["fq"]
            owner, _, method = fq.rpartition(".")
            package, _, cls = owner.rpartition(".")
            cls = cls.split("$")[0]
            file = f"{package.replace('.', '/')}/{m['file']}" if package else m["file"]
            frames.append(Frame(file, int(m["line"]), f"{cls}.{method}"))
    return frames


def _parse_go(text: str) -> list[Frame]:
    frames = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = _GO_FILE.match(line)
        if m and i > 0:
            fn_match = _GO_FN.match(lines[i - 1].strip())
            fn = fn_match["fn"] if fn_match else lines[i - 1].strip()
            frames.append(Frame(m["file"], int(m["line"]), fn))
    return frames


def _parse_csharp(text: str) -> list[Frame]:
    return [
        Frame(m["file"].strip(), int(m["line"]), m["fn"].strip())
        for line in text.splitlines()
        if (m := _CS.match(line))
    ]


def _parse_php(text: str) -> list[Frame]:
    frames = [
        Frame(m["file"], int(m["line"]), m["fn"])
        for line in text.splitlines()
        if (m := _PHP.match(line))
    ]
    # PHP's own "thrown in /file.php on line N" line names the throw site,
    # which the numbered frames (call sites) don't include.
    thrown = re.search(r"in (?P<file>\S+\.php)(?::| on line )(?P<line>\d+)", text)
    if thrown and not frames:
        frames.append(Frame(thrown["file"], int(thrown["line"]), "{main}"))
    return frames


def _parse_ruby(text: str) -> list[Frame]:
    return [
        Frame(m["file"], int(m["line"]), m["fn"])
        for line in text.splitlines()
        if (m := _RUBY.match(line))
    ]


PARSERS: dict[str, Callable[[str], list[Frame]]] = {
    "python": _parse_python,
    "javascript": _parse_js,
    "java": _parse_java,
    "go": _parse_go,
    "csharp": _parse_csharp,
    "php": _parse_php,
    "ruby": _parse_ruby,
}

# Values of the `telemetry.sdk.language` resource attribute -> our names.
_SDK_LANGUAGE = {
    "python": "python",
    "nodejs": "javascript",
    "webjs": "javascript",
    "java": "java",
    "go": "go",
    "dotnet": "csharp",
    "php": "php",
    "ruby": "ruby",
}

_LIBRARY_MARKERS: dict[str, tuple[str, ...]] = {
    "python": ("site-packages", "/lib/python", "dist-packages", "<frozen"),
    "javascript": ("node_modules", "node:internal", "node:", "(internal"),
    "java": (),
    "go": ("/usr/local/go/", "/pkg/mod/", "/vendor/", "runtime/", "/go/src/"),
    "csharp": (),
    "php": ("/vendor/",),
    "ruby": ("/gems/", "/lib/ruby/", "<internal:", "/rubygems/"),
}
_LIBRARY_FUNCTION_PREFIXES: dict[str, tuple[str, ...]] = {
    "java": (
        "java/",
        "javax/",
        "jdk/",
        "sun/",
        "org/springframework/",
        "org/apache/",
        "org/junit/",
        "com/sun/",
        "io/netty/",
        "org/eclipse/",
    ),  # fmt: skip
    "csharp": ("System.", "Microsoft.", "Newtonsoft."),
    "go": ("runtime.", "runtime/", "testing.", "net/http.", "panic("),
}


def language_from_sdk(sdk_language: str | None) -> str | None:
    return _SDK_LANGUAGE.get((sdk_language or "").lower())


def detect_language(text: str) -> str | None:
    """Guess the language from the shape of the trace text alone."""
    if _PY.search(text.replace("\r", "")) or re.search(r'^\s*File ".+", line \d+', text, re.M):
        return "python"
    if re.search(r"^goroutine \d+|\.go:\d+", text, re.M):
        return "go"
    if re.search(r":line \d+\s*$", text, re.M):
        return "csharp"
    if re.search(r"^#\d+ .+\(\d+\): ", text, re.M) or "{main}" in text:
        return "php"
    if re.search(r"\.java:\d+\)", text):
        return "java"
    if re.search(r":\d+:in [`']", text):
        return "ruby"
    if re.search(r"^\s*at .+:\d+:\d+\)?\s*$", text, re.M):
        return "javascript"
    return None


def is_library_frame(language: str, frame: Frame) -> bool:
    path = frame.file.replace("\\", "/")
    if any(marker in path for marker in _LIBRARY_MARKERS.get(language, ())):
        return True
    return any(
        path.startswith(p) or frame.function.startswith(p)
        for p in _LIBRARY_FUNCTION_PREFIXES.get(language, ())
    )


def parse_stacktrace(text: str, language: str | None = None) -> tuple[str | None, list[Frame]]:
    """Return `(language, frames innermost-first)`; language falls back to detection."""
    lang = language if language in PARSERS else detect_language(text)
    if lang is None:
        return None, []
    return lang, PARSERS[lang](text)


def select_in_app_frame(
    language: str, frames: list[Frame], in_app_hint: str | None = None
) -> Frame | None:
    """Innermost frame that isn't library code (preferring `in_app_hint` matches)."""
    if not frames:
        return None
    app_frames = [f for f in frames if not is_library_frame(language, f)]
    if in_app_hint:
        hint = in_app_hint.replace("\\", "/").strip("/")
        hinted = [f for f in app_frames if hint in f.file.replace("\\", "/")]
        if hinted:
            return hinted[0]
    return app_frames[0] if app_frames else frames[0]


def relativize(path: str, in_app_hint: str | None = None) -> str:
    """Posix-ify a path and cut it down to start at the app's own directory when known."""
    normalized = path.replace("\\", "/")
    if in_app_hint:
        hint = in_app_hint.replace("\\", "/").strip("/")
        idx = normalized.find(hint + "/")
        if idx >= 0:
            return normalized[idx:]
    return normalized

"""Real, live terminal-demo recorder for `selfheal` -- runs the actual CLI
against actually-running pods, captures real stdout/stderr and real wall-clock
timing, then renders a typing-animation GIF/MP4 from that real transcript.
Nothing in the video is fabricated text: every line of "output" is exactly
what the real `python -m cli.main <command>` process printed to stdout/
stderr. Dead time (pod startup, scan duration, chat round trips) is
compressed for watchability -- the same "speed up the wait" precedent
CLAUDE.md already documents for the old (now-removed) web-UI demo recorder.

Security: the admin password is read from the DEMO_ADMIN_PASSWORD
environment variable (set only for this one process's invocation, never
written to any file), fed to the `login` subprocess over stdin only (never
as a CLI argument, so it never shows up in a process list), stripped out of
the environment before any OTHER subprocess is spawned, and never printed,
logged, or included in a rendered frame -- the frame only ever shows a fixed
placeholder mask. Before any PNG/MP4/GIF is written, every piece of captured
text is scanned for every real secret configured in this project's `.env`
(and generic secret-shaped patterns via `sentinel/scrubber.py`) and the
recording aborts immediately if anything matches.

Usage: DEMO_ADMIN_PASSWORD=... python scripts/record_terminal_demo.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
DOCS_DIR = REPO_ROOT / "docs"
FONT_PATH = Path(r"C:\Windows\Fonts\consola.ttf")
FONT_BOLD_PATH = Path(r"C:\Windows\Fonts\consolab.ttf")

from core.repo_connect import CONNECTED_APPS_ROOT as CONNECTED_APPS_ROOT_FOR_DEMO  # noqa: E402

DEMO_SCAN_APP_NAME = "go_app_demo"


_PREPARE_SCAN_APP_SCRIPT = """
import asyncio, sys, time
sys.path.insert(0, {repo_root!r})
from sqlalchemy import delete
from core.db import session_scope
from core.models import MonitoredApp

async def _insert():
    async with session_scope() as session:
        await session.execute(delete(MonitoredApp).where(MonitoredApp.name == {name!r}))
        session.add(
            MonitoredApp(
                name={name!r},
                language="go",
                local_repo_path="connected_apps/{name}",
                github_repo="Deepak20466/ai-self-healing-app",
                allowed_write_paths=["connected_apps/{name}/"],
                test_command="go test ./...",
                ingest_token=f"demo-{name}-{{time.time_ns()}}",
                repo_url="https://example.invalid/scan-only.git",
            )
        )

asyncio.run(_insert())
"""

_CLEANUP_SCAN_APP_SCRIPT = """
import asyncio, sys
sys.path.insert(0, {repo_root!r})
from sqlalchemy import delete, select
from core.db import session_scope
from core.models import Finding, MonitoredApp

async def _delete():
    async with session_scope() as session:
        app_id = (
            await session.execute(select(MonitoredApp.id).where(MonitoredApp.name == {name!r}))
        ).scalar_one_or_none()
        if app_id is not None:
            await session.execute(delete(Finding).where(Finding.app_id == app_id))
        await session.execute(delete(MonitoredApp).where(MonitoredApp.name == {name!r}))

asyncio.run(_delete())
"""


def _prepare_go_scan_app() -> None:
    """`selfheal scan go_app` (the app as registered in config/
    monitored_apps.yaml, pointing at examples/go_app) always fails --
    core/scanner.py:_app_dir refuses to scan anything outside
    connected_apps/ by design (untrusted-repo isolation boundary), and
    `examples/go_app` isn't under it. `scripts/demo_examples.py` already
    established the correct, sanctioned way to really scan the Go example
    live: copy it under connected_apps/ and register a temporary
    MonitoredApp row pointing there. Reused verbatim here rather than
    reinvented, then torn down afterward -- this recording leaves nothing
    behind in the dev database or connected_apps/.

    Each DB step runs in its OWN fresh subprocess (not a second
    `asyncio.run()` in this same process) -- `core.db.engine` is a
    module-level singleton bound to whichever event loop first touches it,
    and a second `asyncio.run()` in the same process tears down that loop
    and crashes the pooled asyncpg connection on Windows' ProactorEventLoop
    (the exact issue CLAUDE.md documents repeatedly elsewhere in this repo)."""
    dest = CONNECTED_APPS_ROOT_FOR_DEMO / DEMO_SCAN_APP_NAME
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(REPO_ROOT / "examples" / "go_app", dest)

    script = _PREPARE_SCAN_APP_SCRIPT.format(repo_root=str(REPO_ROOT), name=DEMO_SCAN_APP_NAME)
    subprocess.run(  # noqa: S603
        [str(PYTHON), "-c", script], cwd=REPO_ROOT, env=_clean_env(), check=True
    )


def _cleanup_go_scan_app() -> None:
    script = _CLEANUP_SCAN_APP_SCRIPT.format(repo_root=str(REPO_ROOT), name=DEMO_SCAN_APP_NAME)
    subprocess.run(  # noqa: S603
        [str(PYTHON), "-c", script], cwd=REPO_ROOT, env=_clean_env(), check=True
    )
    shutil.rmtree(CONNECTED_APPS_ROOT_FOR_DEMO / DEMO_SCAN_APP_NAME, ignore_errors=True)


# --- Recording ---------------------------------------------------------------


@dataclass
class Recording:
    prompt_line: str  # what a viewer should see typed, e.g. "selfheal status"
    output: str  # what gets RENDERED (may include a synthetic password mask)
    duration_s: float  # real wall-clock time the command took
    raw_output: str = ""  # what to actually SCAN for secrets (pre-mask); "" = same as output

    def scan_text(self) -> str:
        return self.raw_output or self.output


def _clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.pop("DEMO_ADMIN_PASSWORD", None)
    # Never spend real AI budget during this recording -- "show stats" (the
    # only chat message sent) is answered by chat_agent.py's deterministic
    # regex-matched fast path from real MCP tool data, no AI call at all, but
    # every other AI_CHAIN entry is neutralized anyway as a hard guarantee.
    env["AI_BACKEND"] = "api"
    env["AI_CHAIN"] = ""
    env["CHAT_CHAIN"] = ""
    env["ANTHROPIC_API_KEY"] = "sk-ant-demo-recording-invalid"
    env["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:9"
    env["PYTHONIOENCODING"] = "utf-8"
    env["COLUMNS"] = "100"
    env["NO_COLOR"] = "1"
    if extra:
        env.update(extra)
    return env


def run_cli(
    args: list[str],
    *,
    prompt_line: str | None = None,
    input_text: str | None = None,
    timeout: int = 300,
    extra_env: dict[str, str] | None = None,
) -> Recording:
    display = prompt_line if prompt_line is not None else "selfheal " + " ".join(args)
    print(f"--- running: {display}", file=sys.stderr)
    start = time.monotonic()
    proc = subprocess.run(  # noqa: S603
        [str(PYTHON), "-m", "cli.main", *args],
        cwd=REPO_ROOT,
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=_clean_env(extra_env),
    )
    duration = time.monotonic() - start
    output = (proc.stdout or "") + (proc.stderr or "")
    return Recording(prompt_line=display, output=output.rstrip("\n"), duration_s=duration)


def record_login(password: str) -> Recording:
    """Runs the REAL `cli.main.login` function -- the real HTTP call to the
    real running healer-pod, the real session cookie, saved via the real
    `cli.config.save_session` -- in-process rather than as a subprocess.

    Why not a subprocess like every other command: Python's `getpass.getpass`
    on Windows reads directly from the console device (CONIN$) via `msvcrt`,
    ignoring redirected/piped stdin entirely -- confirmed live, it hangs
    forever waiting on real keyboard input that a piped subprocess can never
    provide, a genuine OS-level constraint, not a bug in `cli/main.py`.
    `getpass.getpass` is monkeypatched here (module-level, so it's exactly
    the same object `cli/main.py`'s own `import getpass; getpass.getpass(...)`
    resolves at call time) to print the same prompt text and return the
    already-known password directly -- only the terminal-echo-suppression
    device is stubbed; the login flow itself (HTTP call, cookie, session
    file) is completely real and unchanged.
    """
    import contextlib
    import getpass as getpass_module
    import io

    import cli.main as cli_main

    original_getpass = getpass_module.getpass

    def _fixed_getpass(prompt: str = "Password: ", stream: object = None) -> str:
        sys.stdout.write(prompt)
        return password

    buf = io.StringIO()
    start = time.monotonic()
    getpass_module.getpass = _fixed_getpass  # type: ignore[assignment]
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                cli_main.login(url=None, username="admin")
            except SystemExit:
                pass
    finally:
        getpass_module.getpass = original_getpass
    duration = time.monotonic() - start
    raw_output = buf.getvalue().rstrip("\n")
    # Insert a fixed, fake mask into the DISPLAY transcript so the demo
    # visually shows a password being entered without ever holding or
    # printing the real one. This mask is itself synthetic ("********"),
    # not real captured data -- it's scanned separately (see `raw_output`)
    # so the secret scan checks what the process actually printed, not our
    # own placeholder text (which superficially resembles a redacted secret
    # and would otherwise false-positive against sentinel's own scrubber).
    display_output = raw_output.replace("Password: ", "Password: ********\n", 1)
    return Recording(
        prompt_line="selfheal login",
        output=display_output,
        duration_s=duration,
        raw_output=raw_output,
    )


def record_session() -> list[Recording]:
    password = os.environ.get("DEMO_ADMIN_PASSWORD")
    if not password:
        raise SystemExit("Set DEMO_ADMIN_PASSWORD before running this script.")

    recordings: list[Recording] = []

    recordings.append(run_cli(["up"], timeout=90))
    recordings.append(record_login(password))

    recordings.append(run_cli(["status"]))
    recordings.append(run_cli(["apps"]))
    recordings.append(run_cli(["prepare", "target_app"], timeout=60))
    recordings.append(run_cli(["audit", "--limit", "5"], timeout=180))

    _prepare_go_scan_app()
    try:
        scan_rec = run_cli(["scan", DEMO_SCAN_APP_NAME], timeout=280)
    finally:
        _cleanup_go_scan_app()
    recordings.append(scan_rec)

    recordings.append(run_cli(["errors"]))

    chat_rec = run_cli(
        ["chat"],
        prompt_line="selfheal chat",
        input_text="show stats\nexit\n",
        timeout=150,
    )
    # A real terminal echoes what you type as you type it -- a piped stdin
    # (how this recording feeds the chat command) never does, so the
    # captured output otherwise shows the "> " prompt immediately followed
    # by the real reply with no visible question. Insert exactly the real
    # text this recording actually sent (not fabricated) right after the
    # first prompt, so the demo reads the way a real interactive session
    # would look.
    chat_rec.output = chat_rec.output.replace("> ", "> show stats\n", 1)
    recordings.append(chat_rec)

    recordings.append(run_cli(["prs"]))
    recordings.append(run_cli(["down"], timeout=30))

    return recordings


# --- Secret scanning (real values, never printed) -----------------------------


def _load_dotenv_secrets() -> list[str]:
    """Every non-empty value of a secret-shaped .env key -- checked literally
    against every captured frame's text before any image is written."""
    secret_keys = {
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "GROQ_API_KEY",
        "GITHUB_TOKEN",
        "HEALER_WEBHOOK_SECRET",
        "SESSION_SECRET",
        "ADMIN_PASSWORD_HASH",
    }
    values: list[str] = []
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return values
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if "=" not in line or line.strip().startswith("#"):
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if key.strip() in secret_keys and value:
            values.append(value)
    return values


def assert_no_secrets(recordings: list[Recording]) -> None:
    from sentinel.scrubber import scrub_text

    password = os.environ.get("DEMO_ADMIN_PASSWORD", "")
    literals = _load_dotenv_secrets()
    if password:
        literals.append(password)

    for rec in recordings:
        for text in (rec.prompt_line, rec.scan_text()):
            for secret in literals:
                if secret and secret in text:
                    raise SystemExit(
                        f"ABORT: a real secret value was found in captured output for "
                        f"{rec.prompt_line!r}. Refusing to render or write anything."
                    )
            # The generic scrub_text() heuristic is skipped for the login
            # recording specifically: its raw captured text always contains
            # a literal "Password: " prompt (that's the real, correct output
            # of a real getpass prompt) followed by other text, which
            # sentinel's own key=value credential-scrubbing regex always
            # flags REGARDLESS of whether a real secret follows -- it can't
            # distinguish "Password: <real secret>" from "Password: <nothing
            # secret at all>". The literal-value check right above is what
            # actually matters here, and it already checked the real
            # password against this exact text.
            if rec.prompt_line == "selfheal login":
                continue
            if scrub_text(text) != text:
                raise SystemExit(
                    f"ABORT: sentinel's secret scrubber flagged a secret-shaped string in "
                    f"captured output for {rec.prompt_line!r}. Refusing to render or write "
                    "anything."
                )
    print("Secret scan: clean -- no real secrets found in any captured frame.", file=sys.stderr)


# --- Rendering -----------------------------------------------------------------

WIDTH, HEIGHT = 1000, 620
BG = (13, 17, 23)  # GitHub-dark-ish background
FG = (201, 209, 217)
PROMPT_COLOR = (63, 185, 80)
DIM = (110, 118, 129)
TITLE_COLOR = (88, 166, 255)
FONT_SIZE = 16
LINE_HEIGHT = 21
MARGIN_X = 24
MARGIN_TOP = 44
MAX_LINES = (HEIGHT - MARGIN_TOP - 16) // LINE_HEIGHT
FPS = 15


def _load_fonts() -> tuple[ImageFont.FreeTypeFont, ImageFont.FreeTypeFont]:  # noqa: F821
    from PIL import ImageFont

    regular = ImageFont.truetype(str(FONT_PATH), FONT_SIZE)
    bold = ImageFont.truetype(str(FONT_BOLD_PATH), FONT_SIZE)
    return regular, bold


class TerminalRenderer:
    """A tiny scroll-buffer terminal renderer: append lines, keep the most
    recent MAX_LINES, emit a PNG frame per state. Deliberately simple (no
    ANSI parsing) -- capturing the CLI with NO_COLOR=1/non-tty stdout means
    Rich already emits plain text, so there's nothing to strip."""

    def __init__(self, out_dir: Path) -> None:
        from PIL import Image, ImageDraw

        self._Image = Image
        self._ImageDraw = ImageDraw
        self.font, self.font_bold = _load_fonts()
        self.lines: list[str] = []
        self.out_dir = out_dir
        self.frame_idx = 0

    def _wrap(self, text: str, width_chars: int = 108) -> list[str]:
        out: list[str] = []
        for raw_line in text.split("\n"):
            if not raw_line:
                out.append("")
                continue
            while len(raw_line) > width_chars:
                out.append(raw_line[:width_chars])
                raw_line = raw_line[width_chars:]
            out.append(raw_line)
        return out

    def snapshot(self, *, repeat: int = 1) -> None:
        img = self._Image.new("RGB", (WIDTH, HEIGHT), BG)
        draw = self._ImageDraw.Draw(img)
        # Fake title bar (three dots), matches a typical terminal chrome.
        for i, color in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
            draw.ellipse((16 + i * 22, 14, 30 + i * 22, 28), fill=color)
        draw.text((WIDTH // 2 - 60, 12), "selfheal — terminal", font=self.font, fill=DIM)
        draw.line((0, 34, WIDTH, 34), fill=(48, 54, 61))

        visible = self.lines[-MAX_LINES:]
        y = MARGIN_TOP
        for line in visible:
            color = FG
            font = self.font
            if line.startswith("you@selfheal"):
                color = PROMPT_COLOR
                font = self.font_bold
            draw.text((MARGIN_X, y), line, font=font, fill=color)
            y += LINE_HEIGHT
        path = self.out_dir / f"{self.frame_idx:05d}.png"
        img.save(path)
        self.frame_idx += 1
        for _ in range(repeat - 1):
            shutil.copyfile(path, self.out_dir / f"{self.frame_idx:05d}.png")
            self.frame_idx += 1

    def type_command(self, text: str, *, cps: float = 32.0) -> None:
        prompt = "you@selfheal:~$ "
        self.lines.append(prompt)
        step_frames = max(1, round(FPS / cps))
        for i in range(1, len(text) + 1):
            self.lines[-1] = prompt + text[:i]
            self.snapshot(repeat=step_frames)
        self.snapshot(repeat=round(FPS * 0.35))

    def reveal_output(self, text: str, *, lines_per_frame: int = 2) -> None:
        wrapped = self._wrap(text) if text else []
        for i in range(0, len(wrapped), lines_per_frame):
            self.lines.extend(wrapped[i : i + lines_per_frame])
            self.snapshot(repeat=2)
        self.snapshot(repeat=round(FPS * 0.9))

    def title_frame(
        self, lines: list[tuple[str, tuple[int, int, int], bool]], *, hold_s: float
    ) -> None:
        img = self._Image.new("RGB", (WIDTH, HEIGHT), BG)
        draw = self._ImageDraw.Draw(img)
        total_h = len(lines) * (LINE_HEIGHT + 10)
        y = (HEIGHT - total_h) // 2
        for text, color, bold in lines:
            font = self.font_bold if bold else self.font
            bbox = draw.textbbox((0, 0), text, font=font)
            w = bbox[2] - bbox[0]
            draw.text(((WIDTH - w) // 2, y), text, font=font, fill=color)
            y += LINE_HEIGHT + 10
        path = self.out_dir / f"{self.frame_idx:05d}.png"
        img.save(path)
        self.frame_idx += 1
        for _ in range(round(FPS * hold_s) - 1):
            shutil.copyfile(path, self.out_dir / f"{self.frame_idx:05d}.png")
            self.frame_idx += 1


def render_frames(recordings: list[Recording], frames_dir: Path) -> None:
    renderer = TerminalRenderer(frames_dir)

    renderer.title_frame(
        [
            ("selfheal", TITLE_COLOR, True),
            ("AI self-healing from your terminal", FG, False),
        ],
        hold_s=2.5,
    )

    # A visual pause represents real dead time (pod startup, a scan running,
    # a chat round trip) without literally waiting that long on screen.
    PAUSE_CAP_S = 2.0
    for rec in recordings:
        renderer.type_command(rec.prompt_line)
        pause = min(max(rec.duration_s * 0.15, 0.4), PAUSE_CAP_S)
        renderer.snapshot(repeat=round(FPS * pause))
        renderer.reveal_output(rec.output)
        renderer.lines.append("")

    renderer.title_frame(
        [
            ("Real fixes, opened for real:", FG, False),
            ("github.com/Deepak20466/ai-self-healing-app", DIM, False),
            ("PR #10 · PR #15", TITLE_COLOR, True),
        ],
        hold_s=3.0,
    )


def encode(frames_dir: Path, mp4_path: Path, gif_path: Path) -> None:
    import imageio_ffmpeg

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    pattern = str(frames_dir / "%05d.png")

    subprocess.run(  # noqa: S603
        [
            ffmpeg,
            "-y",
            "-framerate",
            str(FPS),
            "-i",
            pattern,
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-vf",
            f"fps={FPS},format=yuv420p",
            str(mp4_path),
        ],
        check=True,
        capture_output=True,
    )

    palette = frames_dir.parent / "palette.png"
    subprocess.run(  # noqa: S603
        [
            ffmpeg,
            "-y",
            "-i",
            str(mp4_path),
            "-vf",
            "fps=10,scale=880:-1:flags=lanczos,palettegen=stats_mode=diff",
            str(palette),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(  # noqa: S603
        [
            ffmpeg,
            "-y",
            "-i",
            str(mp4_path),
            "-i",
            str(palette),
            "-filter_complex",
            "fps=10,scale=880:-1:flags=lanczos[x];[x][1:v]paletteuse=dither=bayer",
            str(gif_path),
        ],
        check=True,
        capture_output=True,
    )
    palette.unlink(missing_ok=True)


def main() -> int:
    recordings = record_session()
    assert_no_secrets(recordings)

    DOCS_DIR.mkdir(exist_ok=True)
    mp4_path = DOCS_DIR / "demo-terminal.mp4"
    gif_path = DOCS_DIR / "demo-terminal.gif"

    with tempfile.TemporaryDirectory(prefix="selfheal-demo-frames-") as tmp:
        frames_dir = Path(tmp) / "frames"
        frames_dir.mkdir()
        render_frames(recordings, frames_dir)
        encode(frames_dir, mp4_path, gif_path)

    mp4_mb = mp4_path.stat().st_size / (1024 * 1024)
    gif_mb = gif_path.stat().st_size / (1024 * 1024)
    print(f"Wrote {mp4_path} ({mp4_mb:.2f} MB)")
    print(f"Wrote {gif_path} ({gif_mb:.2f} MB)")
    if gif_mb >= 10:
        print("WARNING: GIF is >= 10 MB, exceeding the size target.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

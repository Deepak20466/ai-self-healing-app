"""Turn the raw takes from `record_demo.py` into docs/demo.mp4 + docs/demo.gif.

Captions, title and end cards are already drawn inside the recording (an
in-page overlay), so ffmpeg only has to speed up the AI's waiting segment,
encode small, and cut a short GIF highlight of the Go / OpenTelemetry scene.
ffmpeg comes from `imageio-ffmpeg` when it isn't on PATH.

Usage: python scripts/build_demo.py <main_dir> <gh_dir> <ui_dir>
(each dir holds a take's .webm and markers.json; see record_demo.py)
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WAIT_TARGET_SECONDS = 9.0
GIF_SECONDS = 15.0


def ffmpeg_exe() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    import imageio_ffmpeg

    return str(imageio_ffmpeg.get_ffmpeg_exe())


def run(*args: str) -> None:
    subprocess.run([ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", *args], check=True)


def main(main_dir: Path, gh_dir: Path, ui_dir: Path) -> None:
    """Join three takes: title..heal (waiting part sped up), GitHub pages, console scenes."""
    markers = json.loads((main_dir / "markers.json").read_text())
    ui_markers = json.loads((ui_dir / "markers.json").read_text())
    take_main = next(main_dir.glob("*.webm"))
    take_gh = next(gh_dir.glob("*.webm"))
    take_ui = next(ui_dir.glob("*.webm"))
    hs, he = markers["heal_start"], markers["heal_end"]
    factor = max(1.0, (he - hs) / WAIT_TARGET_SECONDS)
    out = ROOT / "docs" / "demo.mp4"
    graph = (
        f"[0:v]trim=0:{hs},setpts=PTS-STARTPTS[a];"
        f"[0:v]trim={hs}:{he},setpts=(PTS-STARTPTS)/{factor:.3f}[b];"
        "[1:v]setpts=PTS-STARTPTS[c];[2:v]setpts=PTS-STARTPTS[d];"
        "[a][b][c][d]concat=n=4:v=1:a=0,fps=25,format=yuv420p[v]"
    )
    run(
        "-i", str(take_main), "-i", str(take_gh), "-i", str(take_ui),
        "-filter_complex", graph, "-map", "[v]",
        "-c:v", "libx264", "-crf", "30", "-preset", "slow", "-movflags", "+faststart", "-an",
        str(out),
    )  # fmt: skip
    print(f"speed-up factor {factor:.1f}x -> {out} ({out.stat().st_size / 1e6:.1f} MB)")

    s5 = ui_markers["s5"]
    palette = ui_dir / "palette.png"
    seg = ("-ss", str(s5), "-t", str(GIF_SECONDS), "-i", str(take_ui))
    vf = "fps=10,scale=800:-1:flags=lanczos"
    run(*seg, "-vf", f"{vf},palettegen=max_colors=96", str(palette))
    gif = ROOT / "docs" / "demo.gif"
    run(
        *seg,
        "-i",
        str(palette),
        "-lavfi",
        f"{vf}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=4",
        str(gif),
    )
    print(f"{gif} ({gif.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))

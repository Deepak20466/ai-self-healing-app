# ruff: noqa: ASYNC210, ASYNC220, ASYNC221, ASYNC240, E501
"""Record the raw demo video (headless Playwright, one browser, 1440x900, dark).

Drives the *live* local system: pods must already be running, including a
healer that is allowed exactly one real AI heal run. Writes the raw
recording plus `markers.json` (scene start times and the slow "waiting"
segment) to the output directory; `scripts/build_demo.py` turns that into
the captioned, sped-up docs/demo.mp4.

The admin password is read from VERIFY_ADMIN_PASSWORD only and is never
written to a file (the recording shows only the masked password field).

Usage: python scripts/record_demo.py <out_dir> all|gh|ui|patch [pr_number]

The GitHub scenes (gh) and the console scenes (ui) can be recorded as separate
takes: a long headless Chromium session sometimes crashes its renderer on
GitHub's heavy pages, and separate takes keep each browser fresh.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path

import httpx
from playwright.async_api import Page, async_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from core.db import dispose_engine, session_scope  # noqa: E402
from core.models import Error, HealJob  # noqa: E402

UI = "http://127.0.0.1:8000"
REPO = "https://github.com/Deepak20466/ai-self-healing-app"
TRIGGERS = {"none_lookup": ("item_label", "bugs.py:66")}
CAPTION_JS = """(text) => {
  let el = document.getElementById('__cap');
  if (!el) {
    el = document.createElement('div');
    el.id = '__cap';
    el.style.cssText = 'position:fixed;left:0;right:0;bottom:0;z-index:2147483647;' +
      'padding:18px 40px;background:rgba(0,0,0,.82);color:#fff;font:600 26px system-ui,sans-serif;' +
      'text-align:center;letter-spacing:.2px;';
    document.body.appendChild(el);
  }
  el.textContent = text;
}"""
HIGHLIGHT_JS = """(needle) => {
  for (const tr of document.querySelectorAll('tr')) {
    if (tr.textContent.includes(needle)) {
      tr.style.outline = '3px solid #ffb020'; tr.style.background = 'rgba(255,176,32,.15)';
      tr.scrollIntoView({block: 'center'});
      return true;
    }
  }
  return false;
}"""


def card(title: str, sub: str = "") -> str:
    return (
        '<body style="margin:0;height:100vh;display:flex;flex-direction:column;'
        "align-items:center;justify-content:center;background:#0d1117;color:#fff;"
        'font-family:system-ui,sans-serif;text-align:center;padding:0 80px">'
        f'<h1 style="font-size:56px;margin:0 0 24px">{title}</h1>'
        f'<p style="font-size:30px;color:#9fb3c8;margin:0">{sub}</p></body>'
    )


class Recorder:
    def __init__(self, page: Page) -> None:
        self.page = page
        self.t0 = time.time()
        self.markers: dict[str, float] = {}

    def mark(self, name: str) -> None:
        self.markers[name] = round(time.time() - self.t0, 2)

    async def cap(self, text: str) -> None:
        await self.page.evaluate(CAPTION_JS, text)

    async def wait(self, seconds: float) -> None:
        await self.page.wait_for_timeout(int(seconds * 1000))

    async def goto(self, url: str, caption: str) -> None:
        await self.page.goto(url, wait_until="domcontentloaded")
        await self.wait(1.5)
        await self.cap(caption)

    async def open_tab(self, label: str, caption: str) -> None:
        await self.page.get_by_role("link", name=label, exact=True).first.click()
        await self.wait(1.5)
        await self.cap(caption)


async def heal_state(fn: str) -> tuple[int | None, str | None, int | None]:
    async with session_scope() as s:
        err = (await s.execute(select(Error).where(Error.function_name == fn))).scalar_one()
        job = (
            await s.execute(
                select(HealJob)
                .where(HealJob.fingerprint == err.fingerprint)
                .order_by(HealJob.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    return (
        job.id if job else None,
        job.status.value if job else None,
        job.pr_number if job else None,
    )


async def main(out_dir: Path, bug: str, part: str, pr: int = 0) -> int:
    password = os.environ.get("VERIFY_ADMIN_PASSWORD", "")
    if part in ("all", "ui", "patch") and not password:
        raise SystemExit("VERIFY_ADMIN_PASSWORD is not set")
    fn, location = TRIGGERS[bug]
    status = None
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True)

    async with async_playwright() as pw:
        # GitHub crashes headless Chromium's JIT on this machine; --jitless avoids it
        browser = await pw.chromium.launch(headless=True, args=["--js-flags=--jitless"])
        ctx = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            record_video_dir=str(out_dir),
            record_video_size={"width": 1440, "height": 900},
            color_scheme="dark",
        )
        page = await ctx.new_page()
        r = Recorder(page)

        if part == "all":
            # --- title card
            r.mark("title")
            await page.set_content(
                card(
                    "AI Self-Healing System",
                    "detects and fixes bugs in any language",
                )
            )
            await r.wait(4)

            # --- 1. login + dashboard
            r.mark("s1")
            await r.goto(
                UI, "Sign in to the console: single admin, argon2 hash, signed session cookie"
            )
            await page.fill("#password", password)
            await r.wait(2)
            await page.click("button[type=submit]")
            await page.get_by_role("link", name="Dashboard", exact=True).wait_for(timeout=20000)
            await r.cap("Dashboard: pod health, open errors and CI pipeline runs in one place")
            await r.wait(4)
            await page.mouse.wheel(0, 500)
            await r.wait(3)
            await page.mouse.wheel(0, -500)

            # --- 2. trigger a Python bug, watch the heal
            r.mark("s2")
            await r.cap("A seeded bug is triggered over HTTP: an AttributeError on a missing item")
            async with httpx.AsyncClient(timeout=20) as c:
                await c.get(f"http://127.0.0.1:8001/trigger/{bug}")
            await r.wait(2)
            for _ in range(12):
                await page.reload(wait_until="domcontentloaded")
                await r.wait(3)
                if await page.evaluate(HIGHLIGHT_JS, location):
                    break
            await r.cap(f"Sentinel captured it with the exact file and line: {location}")
            await r.wait(4)
            r.mark("heal_start")
            await r.cap(
                "The healer works the job: Claude Code + MCP tools, isolated worktree, regression test (sped up)"
            )
            deadline = time.time() + 30 * 60
            status: str | None = None
            pr: int | None = None
            last_reload = time.time()
            while time.time() < deadline:
                _, status, pr = await heal_state(fn)
                if status in {"pr_opened", "failed", "merged"}:
                    break
                if time.time() - last_reload > 20:
                    await page.reload(wait_until="domcontentloaded")
                    await page.evaluate(
                        CAPTION_JS,
                        "The healer works the job: Claude Code + MCP tools, isolated worktree, regression test (sped up)",
                    )
                    last_reload = time.time()
                await asyncio.sleep(3)
            r.mark("heal_end")
            r.markers["pr_number"] = pr or 0
            r.markers["heal_status"] = status or ""
            print("heal status:", status, "pr:", pr, flush=True)
            if status != "pr_opened" or not pr:
                (out_dir / "markers.json").write_text(json.dumps(r.markers))
                await ctx.close()
                await browser.close()
                await dispose_engine()
                return 2

        if part == "gh":
            # --- 3. the new PR
            r.mark("s3")
            await r.goto(
                f"{REPO}/pull/{pr}", "A real pull request, opened by the AI: root cause and the fix"
            )
            await r.wait(4)
            await page.mouse.wheel(0, 500)
            await r.wait(4)
            await r.goto(
                f"{REPO}/pull/{pr}/files",
                "The diff, plus a regression test that failed before the fix and passes after",
            )
            await r.wait(4)
            await page.mouse.wheel(0, 600)
            await r.wait(4)

            # --- 4. PR #14: AI CI-fix
            r.mark("s4")
            await r.goto(
                f"{REPO}/pull/14",
                "CI self-healing: a deliberately broken test on PR #14 was fixed by the AI",
            )
            await r.wait(2)
            comments = page.locator(".timeline-comment")
            n = await comments.count()
            if n > 1:
                await comments.nth(1).scroll_into_view_if_needed()
            await r.cap("The AI's comment: root cause, what it pushed, and the test evidence")
            await r.wait(6)

        if part == "patch":
            # re-take of the dashboard + metrics scenes after the metrics/timeline fix
            await r.goto(UI, "Back in the console")
            await page.fill("#password", password)
            await page.click("button[type=submit]")
            await page.get_by_role("link", name="Dashboard", exact=True).wait_for(timeout=20000)
            await page.wait_for_timeout(2500)
            r.mark("dash")
            await page.evaluate("document.getElementById('__cap')?.remove()")
            await page.screenshot(path=str(ROOT / "docs/images/dashboard.png"))
            await r.cap(
                "Each heal job now shows live progress: detected, analyzing, patch, tests, PR opened"
            )
            await r.wait(7)
            r.mark("s7")
            await r.open_tab(
                "Metrics",
                "Metrics: fix success rate (PR opened with passing tests), detection-to-PR time, cost per fix",
            )
            await r.wait(2)
            await page.evaluate("document.getElementById('__cap')?.remove()")
            await page.screenshot(path=str(ROOT / "docs/images/metrics.png"))
            await r.cap(
                "Metrics: fix success rate (PR opened with passing tests), detection-to-PR time, cost per fix"
            )
            await r.wait(4)
            await page.mouse.wheel(0, 500)
            await r.wait(3)
            r.mark("done")

        if part == "ui":
            # --- login only (ui part)
            await r.goto(UI, "Back in the console")
            await page.fill("#password", password)
            await page.click("button[type=submit]")
            await page.get_by_role("link", name="Dashboard", exact=True).wait_for(timeout=20000)
            # --- 5. any language: Go
            r.mark("s5")
            await r.goto(UI, "Any language: the Go example app has a seeded divide-by-zero bug")
            await r.wait(2)
            async with httpx.AsyncClient(timeout=20) as c:
                await c.get("http://127.0.0.1:8102/stats/average?values=")
            await r.cap(
                "Triggered in Go. Captured over standard OpenTelemetry (OTLP), no custom agent"
            )
            for _ in range(10):
                await page.reload(wait_until="domcontentloaded")
                await r.wait(3)
                if await page.evaluate(HIGHLIGHT_JS, "calc.go"):
                    break
            await r.cap("Error stored with the right file and line: examples/go_app/calc.go")
            await r.wait(4)
            await r.open_tab("Apps", "Connect any repo: automatic health report per app")
            await r.wait(3)
            await page.get_by_role("link", name="go-example", exact=True).click()
            await r.wait(2)
            await r.cap("Go example health report: tests run, findings listed, score 70")
            await r.wait(4)
            await page.mouse.wheel(0, 500)
            await r.wait(3)

            # --- 6. chat
            r.mark("s6")
            await r.open_tab("Chat", "AI chat answers from live data through the MCP server")
            box = page.get_by_label("Chat message")
            await box.fill("show stats")
            await box.press("Enter")
            await r.wait(7)
            await box.fill("what broke?")
            await box.press("Enter")
            await r.wait(8)

            # --- 7. metrics
            r.mark("s7")
            await r.open_tab(
                "Metrics", "Metrics: fix success rate, time to repair and cost per fix"
            )
            await r.wait(4)
            await page.mouse.wheel(0, 500)
            await r.wait(3)

            # --- end card
            r.mark("end")
            await page.set_content(
                card(
                    "github.com/Deepak20466/ai-self-healing-app",
                    "Open source. Built with FastAPI, Postgres and an MCP server.",
                )
            )
            await r.wait(4)
            r.mark("done")

        await ctx.close()
        await browser.close()
    await dispose_engine()
    (out_dir / "markers.json").write_text(json.dumps(r.markers))
    print(json.dumps(r.markers), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(
        asyncio.run(
            main(
                Path(sys.argv[1]),
                "none_lookup",
                sys.argv[2],
                int(sys.argv[3]) if len(sys.argv) > 3 else 0,
            )
        )
    )

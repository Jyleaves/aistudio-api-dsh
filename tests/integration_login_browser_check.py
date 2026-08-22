"""Integration smoke test: launch the system-browser login context and load
Google's AddSession page. Run manually:

    .venv/Scripts/python.exe tests/integration_login_browser_check.py
"""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aistudio_api.infrastructure.browser.browser_engine import (
    async_launch_login_context,
    detect_system_login_browser,
    _login_window_args,
)
from urllib.parse import urlencode


LOGIN_URL = "https://accounts.google.com/AddSession?" + urlencode(
    {"continue": "https://aistudio.google.com"}
)


async def main() -> int:
    detected = detect_system_login_browser()
    print(f"detected system browser: {detected}")
    if detected is None:
        print("FAIL: no system browser found")
        return 1

    profile_dir = tempfile.mkdtemp(prefix="aistudio-login-check-")
    handle = await async_launch_login_context(headless=True, profile_dir=profile_dir)
    print(f"backend: {handle.backend}")
    try:
        page = handle.context.pages[0] if handle.context.pages else await handle.context.new_page()
        await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
        print(f"landed on: {page.url}")

        webdriver = await page.evaluate("() => navigator.webdriver")
        print(f"navigator.webdriver = {webdriver}")

        email_field = page.locator("input[name='identifier'], input[type='email']").first
        try:
            await email_field.wait_for(state="visible", timeout=15000)
            print("identifier input visible: yes")
            identifier_ok = True
        except Exception as exc:
            print(f"identifier input visible: no ({exc})")
            identifier_ok = False

        title = await page.title()
        print(f"page title: {title!r}")

        ok = (
            "accounts.google.com" in page.url
            and webdriver in (False, None)
            and identifier_ok
        )
        print("RESULT:", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        await handle.context.close()
        if handle.playwright is not None:
            await handle.playwright.stop()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

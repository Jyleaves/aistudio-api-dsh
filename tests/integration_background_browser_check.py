"""Integration check: can each browser backend actually serve AI Studio traffic?

Copies the active account's cookies into an isolated profile, loads
aistudio.google.com, and verifies the signed-in session plus the page JS
environment the request hooks depend on. Run manually:

    .venv/Scripts/python.exe tests/integration_background_browser_check.py
"""

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

from aistudio_api.infrastructure.browser import browser_engine as engine

BACKENDS = [
    ("CloakBrowser (项目内)", str(PROJECT / "cloakbrowser-chromium" / "chrome.exe"), "cloakbrowser"),
    ("系统 Chrome", r"C:\Program Files\Google\Chrome\Application\chrome.exe", "chrome"),
    ("Chromium snapshot (chrome-win)", str(PROJECT / "data" / "tmp" / "chrome-win" / "chrome.exe"), "chromium"),
]

REGISTRY = json.loads((PROJECT / "data" / "accounts" / "registry.json").read_text(encoding="utf-8"))
AUTH_FILE = PROJECT / "data" / "accounts" / REGISTRY["active_account_id"] / "auth.json"


def check_backend(label: str, executable: str, kind: str) -> bool:
    workdir = Path(tempfile.mkdtemp(prefix=f"bg-check-{kind}-"))
    profile = str(workdir / "profile")
    original_detect = engine.detect_background_browser
    engine.detect_background_browser = lambda: {"executable_path": executable, "kind": kind}
    try:
        started = time.time()
        context = engine.sync_launch_persistent_context(profile, headless=True)
        launch_secs = time.time() - started
        page = context.pages[0] if context.pages else context.new_page()
        cookies = json.loads(AUTH_FILE.read_text(encoding="utf-8")).get("cookies", [])
        context.add_cookies(cookies)
        page.goto("https://aistudio.google.com/", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(6000)

        url = page.url
        signed_in = "accounts.google.com" not in url
        has_makersuite = page.evaluate("() => !!window.default_MakerSuite")
        webdriver = page.evaluate("() => navigator.webdriver")
        total_secs = time.time() - started

        ok = signed_in and has_makersuite
        print(f"[{label}]")
        print(f"  启动 {launch_secs:.1f}s | 总耗时 {total_secs:.1f}s | webdriver={webdriver}")
        print(f"  登录态: {'有效' if signed_in else '失效(被重定向到登录页)'} | 请求hook前提(default_MakerSuite): {'就绪' if has_makersuite else '缺失'}")
        print(f"  RESULT: {'PASS' if ok else 'FAIL'}")
        return ok
    except Exception as exc:
        print(f"[{label}]")
        print(f"  RESULT: FAIL ({str(exc).splitlines()[0][:120]})")
        return False
    finally:
        engine.detect_background_browser = original_detect
        try:
            context.close()
        except Exception:
            pass
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    results = {}
    for label, executable, kind in BACKENDS:
        if not Path(executable).is_file():
            print(f"[{label}] SKIPPED (not found: {executable})")
            continue
        results[label] = check_backend(label, executable, kind)
        print()
    print("=== 汇总 ===")
    for label, ok in results.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")

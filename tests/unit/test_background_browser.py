"""Tests for background browser auto-discovery in browser_engine."""

import sys
import types

sys.modules.setdefault("dotenv", types.SimpleNamespace(load_dotenv=lambda: None))
playwright_module = sys.modules.setdefault("playwright", types.ModuleType("playwright"))
async_api_module = sys.modules.setdefault("playwright.async_api", types.ModuleType("playwright.async_api"))
sync_api_module = sys.modules.setdefault("playwright.sync_api", types.ModuleType("playwright.sync_api"))
setattr(playwright_module, "async_api", async_api_module)
setattr(playwright_module, "sync_api", sync_api_module)
setattr(async_api_module, "async_playwright", lambda: None)
setattr(sync_api_module, "sync_playwright", lambda: None)

from aistudio_api.infrastructure.browser import browser_engine as engine
from aistudio_api.infrastructure.browser.browser_engine import detect_background_browser


def test_discovery_prefers_env_executable_then_bundled_then_system(monkeypatch, tmp_path):
    env_browser = tmp_path / "custom-chrome.exe"
    env_browser.write_bytes(b"")
    bundled = tmp_path / "cloakbrowser-chromium" / "chrome.exe"
    bundled.parent.mkdir()
    bundled.write_bytes(b"")
    playwright_chromium = tmp_path / "chromium-1234" / "chrome-win" / "chrome.exe"
    playwright_chromium.parent.mkdir(parents=True)
    playwright_chromium.write_bytes(b"")
    system = tmp_path / "chrome.exe"
    system.write_bytes(b"")

    monkeypatch.setattr(engine, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(engine.settings, "browser_executable_path", str(env_browser))
    monkeypatch.setattr(engine, "_find_playwright_chromium", lambda: str(playwright_chromium))
    monkeypatch.setattr(
        engine, "_find_system_browser_executable", lambda channel: str(system)
    )

    found = detect_background_browser()
    assert found == {"executable_path": str(env_browser.resolve()), "kind": "cloakbrowser"}

    # env 未设置时：Playwright 稳定版 Chromium 最优先
    monkeypatch.setattr(engine.settings, "browser_executable_path", None)
    found = detect_background_browser()
    assert found["executable_path"] == str(playwright_chromium)
    assert found["kind"] == "chromium"

    # Playwright Chromium 缺失时：项目内 CloakBrowser
    monkeypatch.setattr(engine, "_find_playwright_chromium", lambda: None)
    found = detect_background_browser()
    assert found["executable_path"] == str(bundled.resolve())
    assert found["kind"] == "cloakbrowser"

    # 项目内也没有时：系统 Chrome
    bundled.unlink()
    found = detect_background_browser()
    assert found == {"executable_path": str(system), "kind": "chrome"}


def test_find_playwright_chromium_locates_newest_version(monkeypatch, tmp_path):
    root = tmp_path / "ms-playwright"
    for version in ("chromium-1000", "chromium-1234", "chromium-1100"):
        chrome = root / version / "chrome-win" / "chrome.exe"
        chrome.parent.mkdir(parents=True, exist_ok=True)
        chrome.write_bytes(b"")

    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(root))
    try:
        located = engine._find_playwright_chromium()
    finally:
        import os as _os
        _os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)

    assert located is not None
    assert "chromium-1234" in located


def test_find_playwright_chromium_prefers_installed_bundle(monkeypatch, tmp_path):
    bundled = tmp_path / "playwright-browsers" / "chromium-bundled" / "chrome-win64"
    bundled.mkdir(parents=True)
    executable = bundled / "chrome.exe"
    executable.write_bytes(b"")
    monkeypatch.setattr(engine, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    assert engine._find_playwright_chromium() == str(executable)


def test_discovery_returns_none_without_any_browser(monkeypatch, tmp_path):
    monkeypatch.setattr(engine, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(engine.settings, "browser_executable_path", None)
    monkeypatch.setattr(engine, "_find_playwright_chromium", lambda: None)
    monkeypatch.setattr(engine, "_find_system_browser_executable", lambda channel: None)
    assert detect_background_browser() is None


def test_sync_launch_persistent_context_reports_install_hint(monkeypatch, tmp_path):
    monkeypatch.setattr(engine.settings, "browser_engine", "chromium")
    monkeypatch.setattr(engine, "detect_background_browser", lambda: None)

    try:
        engine.sync_launch_persistent_context(str(tmp_path / "profile"))
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "未找到可用的浏览器" in str(exc)
        assert "google.com/chrome" in str(exc)
        assert "CloakBrowser" in str(exc)


def test_sync_launch_persistent_context_uses_discovered_browser(monkeypatch, tmp_path):
    class FakeContext:
        def __init__(self):
            self.closed = False
            self.init_scripts = []

        def add_init_script(self, script):
            self.init_scripts.append(script)

        def close(self):
            self.closed = True

    class FakeChromium:
        def __init__(self, context):
            self._context = context
            self.launch_options = None

        def launch_persistent_context(self, **options):
            self.launch_options = options
            return self._context

    class FakePlaywright:
        def __init__(self, chromium):
            self.chromium = chromium
            self.stopped = False

        def start(self):
            return self

        def stop(self):
            self.stopped = True

    fake_context = FakeContext()
    pw = FakePlaywright(FakeChromium(fake_context))

    class Starter:
        def start(self):
            return pw

    monkeypatch.setattr(engine.settings, "browser_engine", "chromium")
    monkeypatch.setattr(
        engine, "detect_background_browser",
        lambda: {"executable_path": r"C:\Chrome\chrome.exe", "kind": "chrome"},
    )
    monkeypatch.setattr(engine.settings, "proxy_url", None)
    monkeypatch.setattr(engine.settings, "browser_chromium_sandbox", True)
    monkeypatch.setattr(engine.settings, "browser_headless", True)
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: Starter())

    context = engine.sync_launch_persistent_context(
        str(tmp_path / "profile"), no_viewport=True
    )
    options = pw.chromium.launch_options
    assert options["executable_path"] == r"C:\Chrome\chrome.exe"
    assert options["chromium_sandbox"] is True
    assert "--enable-automation" in options["ignore_default_args"]
    # 系统浏览器（非 CloakBrowser 二进制）不传 fingerprint 参数
    assert all("--fingerprint" not in arg for arg in options["args"])
    assert options["viewport"] is None
    # navigator.webdriver 隐藏注入（普通 Chrome 二进制需要）
    assert context.init_scripts == [engine._HIDE_WEBDRIVER_SCRIPT]

    # close() 同时停掉 playwright driver
    context.close()
    assert pw.stopped is True


def test_login_candidates_skip_running_edge_and_failed_channels(monkeypatch, tmp_path):
    _, fake_edge = _install_browsers(monkeypatch, tmp_path)
    monkeypatch.setattr(engine.settings, "login_browser_channel", None)
    monkeypatch.setattr(engine, "_windows_default_browser_channel", lambda: "msedge")
    monkeypatch.setattr(engine, "_is_process_running", lambda name: name == "msedge.exe")
    monkeypatch.setattr(engine, "_failed_login_channels", set())

    # Edge 正在运行（启动加速/用户使用中）→ 直接跳过，只剩 Chrome
    candidates = engine._system_login_candidates()
    assert [c["channel"] for c in candidates] == ["chrome"]

    # Edge 没在运行时恢复默认浏览器优先
    monkeypatch.setattr(engine, "_is_process_running", lambda name: False)
    candidates = engine._system_login_candidates()
    assert [c["channel"] for c in candidates] == ["msedge", "chrome"]

    # 启动失败过的 channel 不再尝试
    monkeypatch.setattr(engine, "_failed_login_channels", {"msedge"})
    candidates = engine._system_login_candidates()
    assert [c["channel"] for c in candidates] == ["chrome"]


def _install_browsers(monkeypatch, tmp_path):
    fake_chrome = tmp_path / "chrome.exe"
    fake_edge = tmp_path / "msedge.exe"
    fake_chrome.write_bytes(b"")
    fake_edge.write_bytes(b"")
    monkeypatch.setattr(
        engine,
        "_SYSTEM_BROWSER_INSTALL_PATHS",
        {"chrome": [str(fake_chrome)], "msedge": [str(fake_edge)]},
    )
    monkeypatch.setattr(engine, "_SYSTEM_BROWSER_WHICH_NAMES", {"chrome": (), "msedge": ()})
    return fake_chrome, fake_edge

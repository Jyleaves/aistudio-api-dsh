"""Tests for the system-browser login launcher in browser_engine."""

import asyncio
import sys
import types

sys.modules.setdefault("dotenv", types.SimpleNamespace(load_dotenv=lambda: None))
playwright_module = sys.modules.setdefault("playwright", types.ModuleType("playwright"))
async_api_module = sys.modules.setdefault("playwright.async_api", types.ModuleType("playwright.async_api"))
setattr(playwright_module, "async_api", async_api_module)
setattr(async_api_module, "async_playwright", lambda: None)

from aistudio_api.infrastructure.browser import browser_engine as engine
from aistudio_api.infrastructure.browser.browser_engine import (
    async_launch_login_context,
    detect_system_login_browser,
)


def _install_fake_browsers(monkeypatch, tmp_path, *, chrome=False, edge=False):
    fake_chrome = tmp_path / "chrome.exe"
    fake_edge = tmp_path / "msedge.exe"
    if chrome:
        fake_chrome.write_bytes(b"")
    if edge:
        fake_edge.write_bytes(b"")
    monkeypatch.setattr(
        engine,
        "_SYSTEM_BROWSER_INSTALL_PATHS",
        {
            "chrome": [str(fake_chrome)],
            "msedge": [str(fake_edge)],
        },
    )
    monkeypatch.setattr(engine, "_SYSTEM_BROWSER_WHICH_NAMES", {"chrome": (), "msedge": ()})
    # 进程检测与测试环境隔离（不受本机 Edge 是否运行影响）
    monkeypatch.setattr(engine, "_is_process_running", lambda name: False)
    monkeypatch.setattr(engine, "_failed_login_channels", set())
    return fake_chrome, fake_edge


def test_detect_prefers_explicit_channel_override(monkeypatch, tmp_path):
    _, fake_edge = _install_fake_browsers(monkeypatch, tmp_path, edge=True)
    monkeypatch.setattr(engine.settings, "login_browser_channel", "msedge")
    result = detect_system_login_browser()
    assert result == {"channel": "msedge", "executable_path": str(fake_edge)}


def test_detect_prefers_chrome_then_falls_back_to_edge(monkeypatch, tmp_path):
    fake_chrome, fake_edge = _install_fake_browsers(monkeypatch, tmp_path, chrome=True, edge=True)
    monkeypatch.setattr(engine.settings, "login_browser_channel", None)
    monkeypatch.setattr(engine, "_windows_default_browser_channel", lambda: None)

    result = detect_system_login_browser()
    assert result == {"channel": "chrome", "executable_path": str(fake_chrome)}

    fake_chrome.unlink()
    result = detect_system_login_browser()
    assert result == {"channel": "msedge", "executable_path": str(fake_edge)}

    fake_edge.unlink()
    assert detect_system_login_browser() is None


def test_detect_prefers_users_default_browser(monkeypatch, tmp_path):
    fake_chrome, fake_edge = _install_fake_browsers(monkeypatch, tmp_path, chrome=True, edge=True)
    monkeypatch.setattr(engine.settings, "login_browser_channel", None)

    # 默认浏览器是 Edge 时优先 Edge（与用户日常浏览器一致）
    monkeypatch.setattr(engine, "_windows_default_browser_channel", lambda: "msedge")
    result = detect_system_login_browser()
    assert result == {"channel": "msedge", "executable_path": str(fake_edge)}

    # 默认浏览器是 Chrome 时优先 Chrome
    monkeypatch.setattr(engine, "_windows_default_browser_channel", lambda: "chrome")
    result = detect_system_login_browser()
    assert result == {"channel": "chrome", "executable_path": str(fake_chrome)}

    # 默认浏览器是非 Chromium 内核（如 Firefox）时回退常规顺序
    monkeypatch.setattr(engine, "_windows_default_browser_channel", lambda: None)
    result = detect_system_login_browser()
    assert result["channel"] == "chrome"


class FakeContext:
    def __init__(self, name="fake-context"):
        self.name = name
        self.init_scripts: list[str] = []

    async def add_init_script(self, script: str) -> None:
        self.init_scripts.append(script)


class FakeChromiumLauncher:
    def __init__(self, context=None, error=None):
        self.launch_options = None
        self._context = context if context is not None else FakeContext()
        self._error = error

    async def launch_persistent_context(self, **options):
        self.launch_options = options
        if self._error:
            raise self._error
        return self._context


class FakePlaywright:
    def __init__(self, chromium):
        self.chromium = chromium
        self.stopped = False

    async def stop(self):
        self.stopped = True


def _patch_async_playwright(monkeypatch, pw):
    class Starter:
        async def start(self):
            return pw

    monkeypatch.setattr("playwright.async_api.async_playwright", lambda: Starter())


def _block_cloakbrowser_download(monkeypatch, *, succeed: bool = False):
    """auto 模式下系统浏览器全部失败会回退 cloakbrowser 包并触发真实
    下载；测试里必须挡住这个回退路径。succeed=True 时用 fake context
    代替（用于验证回退本身发生的场景）。"""
    import cloakbrowser

    if succeed:
        async def _fake_launch(**kwargs):
            return FakeContext("cb-fallback")
    else:
        async def _fake_launch(**kwargs):
            raise AssertionError("cloakbrowser fallback must not run in unit tests")

    monkeypatch.setattr(cloakbrowser, "launch_persistent_context_async", _fake_launch)


def test_launch_login_context_uses_system_browser_with_persistent_profile(monkeypatch, tmp_path):
    pw = FakePlaywright(FakeChromiumLauncher())
    _patch_async_playwright(monkeypatch, pw)
    monkeypatch.setattr(engine.settings, "login_browser", "auto")
    monkeypatch.setattr(
        engine,
        "_system_login_candidates",
        lambda: [{"channel": "msedge", "executable_path": r"C:\Edge\msedge.exe"}],
    )
    monkeypatch.setattr(engine.settings, "proxy_url", None)

    profile = str(tmp_path / "login-profile")
    handle = asyncio.run(async_launch_login_context(headless=False, profile_dir=profile))

    assert handle.backend == "system:msedge"
    assert handle.context is pw.chromium._context
    assert handle.context.init_scripts == [engine._HIDE_WEBDRIVER_SCRIPT]
    assert handle.playwright is pw

    options = pw.chromium.launch_options
    assert options["user_data_dir"] == profile
    assert options["executable_path"] == r"C:\Edge\msedge.exe"
    assert "channel" not in options
    assert "--enable-automation" in options["ignore_default_args"]
    assert options["chromium_sandbox"] is True
    # 危险标记会触发 Chrome "不受支持的命令行标记" 警告条，不能出现
    assert "--disable-blink-features=AutomationControlled" not in options["args"]
    assert "--no-sandbox" not in options["args"]
    assert "--start-maximized" in options["args"]
    assert options["no_viewport"] is True
    assert "proxy" not in options


def test_launch_login_context_uses_bundled_playwright_chromium(monkeypatch, tmp_path):
    pw = FakePlaywright(FakeChromiumLauncher())
    _patch_async_playwright(monkeypatch, pw)
    monkeypatch.setattr(engine.settings, "login_browser", "chromium")
    monkeypatch.setattr(engine, "_find_playwright_chromium", lambda: r"C:\Asteria\playwright-browsers\chrome.exe")
    monkeypatch.setattr(engine.settings, "proxy_url", None)

    handle = asyncio.run(
        async_launch_login_context(headless=False, profile_dir=str(tmp_path / "p"))
    )

    assert handle.backend == "playwright:chromium"
    assert pw.chromium.launch_options["executable_path"] == r"C:\Asteria\playwright-browsers\chrome.exe"
    assert "channel" not in pw.chromium.launch_options


def test_launch_login_context_falls_back_to_other_system_browser(monkeypatch, tmp_path):
    # 模拟 Edge（默认浏览器）被后台进程占用启动失败，自动降级 Chrome
    attempts: list[str] = []

    class SwitchingLauncher:
        def __init__(self):
            self.launch_options = None

        async def launch_persistent_context(self, **options):
            attempts.append(options.get("executable_path") or options.get("channel"))
            if len(attempts) == 1:
                raise RuntimeError("现有的会话已打开")
            self.launch_options = options
            return FakeContext("fallback-context")

    pw = FakePlaywright(SwitchingLauncher())
    _patch_async_playwright(monkeypatch, pw)
    monkeypatch.setattr(engine.settings, "login_browser", "auto")
    monkeypatch.setattr(
        engine,
        "_system_login_candidates",
        lambda: [
            {"channel": "msedge", "executable_path": r"C:\Edge\msedge.exe"},
            {"channel": "chrome", "executable_path": r"C:\Chrome\chrome.exe"},
        ],
    )
    monkeypatch.setattr(engine.settings, "proxy_url", None)

    handle = asyncio.run(async_launch_login_context(headless=False, profile_dir=str(tmp_path / "p")))
    assert handle.backend == "system:chrome"
    assert handle.context.name == "fallback-context"
    assert attempts == [r"C:\Edge\msedge.exe", r"C:\Chrome\chrome.exe"]


def test_launch_login_context_reports_all_failures_in_system_mode(monkeypatch, tmp_path):
    pw = FakePlaywright(FakeChromiumLauncher(error=RuntimeError("boom")))
    _patch_async_playwright(monkeypatch, pw)
    monkeypatch.setattr(engine.settings, "login_browser", "system")
    monkeypatch.setattr(
        engine,
        "_system_login_candidates",
        lambda: [
            {"channel": "msedge", "executable_path": r"C:\Edge\msedge.exe"},
            {"channel": "chrome", "executable_path": r"C:\Chrome\chrome.exe"},
        ],
    )

    try:
        asyncio.run(async_launch_login_context(headless=False, profile_dir=str(tmp_path / "p")))
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "系统 Chrome/Edge 启动失败" in str(exc)
        assert "msedge" in str(exc) and "chrome" in str(exc)


def test_launch_login_context_headless_has_no_window_options(monkeypatch, tmp_path):
    pw = FakePlaywright(FakeChromiumLauncher())
    _patch_async_playwright(monkeypatch, pw)
    monkeypatch.setattr(engine.settings, "login_browser", "auto")
    monkeypatch.setattr(
        engine,
        "_system_login_candidates",
        lambda: [{"channel": "chrome", "executable_path": r"C:\Chrome\chrome.exe"}],
    )
    monkeypatch.setattr(engine.settings, "proxy_url", None)
    _block_cloakbrowser_download(monkeypatch)

    handle = asyncio.run(
        async_launch_login_context(headless=True, profile_dir=str(tmp_path / "p"))
    )
    assert handle.backend == "system:chrome"

    options = pw.chromium.launch_options
    assert "no_viewport" not in options
    assert "--start-maximized" not in options["args"]


def test_launch_login_context_stops_playwright_on_failure(monkeypatch, tmp_path):
    pw = FakePlaywright(FakeChromiumLauncher(error=RuntimeError("boom")))
    _patch_async_playwright(monkeypatch, pw)
    monkeypatch.setattr(engine.settings, "login_browser", "auto")
    monkeypatch.setattr(
        engine,
        "_system_login_candidates",
        lambda: [{"channel": "msedge", "executable_path": r"C:\Edge\msedge.exe"}],
    )
    _block_cloakbrowser_download(monkeypatch, succeed=True)

    # 唯一的系统浏览器启动失败 → auto 回退 cloakbrowser 成功；
    # 失败的 playwright 实例必须已被停掉。
    handle = asyncio.run(
        async_launch_login_context(headless=False, profile_dir=str(tmp_path / "p"))
    )
    assert handle.backend == "cloakbrowser"
    assert handle.context.name == "cb-fallback"
    assert pw.stopped is True


def test_launch_login_context_requires_system_browser_in_system_mode(monkeypatch, tmp_path):
    monkeypatch.setattr(engine.settings, "login_browser", "system")
    monkeypatch.setattr(engine, "_system_login_candidates", lambda: [])

    try:
        asyncio.run(
            async_launch_login_context(headless=False, profile_dir=str(tmp_path / "p"))
        )
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "系统 Chrome/Edge 启动失败" in str(exc)


def test_launch_login_context_falls_back_to_cloakbrowser(monkeypatch, tmp_path):
    import cloakbrowser

    monkeypatch.setattr(engine.settings, "login_browser", "auto")
    monkeypatch.setattr(engine, "detect_system_login_browser", lambda: None)
    monkeypatch.setattr(engine.settings, "proxy_url", None)

    captured: dict = {}

    async def fake_cloakbrowser_launch(**kwargs):
        captured.update(kwargs)
        return FakeContext("cb-context")

    monkeypatch.setattr(
        cloakbrowser, "launch_persistent_context_async", fake_cloakbrowser_launch
    )

    profile = tmp_path / "login-profile"
    handle = asyncio.run(async_launch_login_context(headless=False, profile_dir=str(profile)))

    assert handle.backend == "cloakbrowser"
    assert handle.context.name == "cb-context"
    assert handle.context.init_scripts == [engine._HIDE_WEBDRIVER_SCRIPT]
    assert handle.playwright is None
    assert profile.exists()
    assert captured["user_data_dir"] == str(profile)
    assert captured["viewport"] is None
    assert captured["stealth_args"] is False
    assert captured["chromium_sandbox"] is True
    assert "--start-maximized" in captured["args"]

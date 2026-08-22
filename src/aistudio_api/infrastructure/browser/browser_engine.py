"""Shared helpers for selecting and launching the browser backend."""

from __future__ import annotations

import hashlib
import logging
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aistudio_api.config import PROJECT_ROOT, build_camoufox_proxy, settings

logger = logging.getLogger("aistudio.browser_engine")


def is_camoufox_engine() -> bool:
    return settings.browser_engine == "camoufox"


def is_cloakbrowser_explicit_engine() -> bool:
    """用户显式选择 cloakbrowser 包（保留自动下载行为）。"""
    return settings.browser_engine == "cloakbrowser"


def describe_browser_backend() -> str:
    if is_camoufox_engine():
        return "camoufox"
    if is_cloakbrowser_explicit_engine():
        return "cloakbrowser:auto-download"
    found = detect_background_browser()
    if found is None:
        return "chromium:none-found"
    return f"chromium:{found['kind']}"


def _derive_stable_fingerprint_seed(key: str) -> int:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return 10000 + (int(digest[:8], 16) % 90000)


def _build_cloakbrowser_args(
    *,
    headless: bool,
    stable_fingerprint_key: str | None = None,
    extra_args: list[str] | None = None,
) -> list[str]:
    """Build cloakbrowser args without `--no-sandbox`.

    cloakbrowser's default stealth args currently inject `--no-sandbox` and a
    random fingerprint seed. Both are undesirable for our long-lived profiles:
    `--no-sandbox` is easy to spot, and random seeds make a persistent profile
    present a different browser fingerprint on each launch.
    """
    fingerprint_seed = (
        _derive_stable_fingerprint_seed(stable_fingerprint_key)
        if stable_fingerprint_key
        else None
    )
    args: list[str] = []
    if not headless:
        args.append("--start-maximized")
        args.append("--ignore-gpu-blocklist")
    if fingerprint_seed is not None:
        args.append(f"--fingerprint={fingerprint_seed}")
    args.append(
        "--fingerprint-platform=macos"
        if platform.system() == "Darwin"
        else "--fingerprint-platform=windows"
    )
    if extra_args:
        args.extend(extra_args)
    return args


def build_browser_launch_options(headless: bool | None = None) -> dict[str, Any]:
    is_headless = settings.browser_headless if headless is None else headless
    options: dict[str, Any] = {
        "headless": is_headless,
    }
    if not is_headless:
        options["args"] = ["--start-maximized"]
    proxy = build_camoufox_proxy(settings.proxy_url)
    if proxy:
        options["proxy"] = proxy
    if settings.browser_executable_path:
        options["executable_path"] = settings.browser_executable_path
    elif settings.browser_channel:
        options["channel"] = settings.browser_channel
    return options


def build_browser_context_options(headless: bool | None = None) -> dict[str, Any]:
    if is_camoufox_engine():
        return {}

    is_headless = settings.browser_headless if headless is None else headless
    if is_headless:
        return {}

    return {
        "no_viewport": True,
    }


def should_maximize_browser_window(headless: bool | None = None) -> bool:
    if is_camoufox_engine():
        return False
    return not (settings.browser_headless if headless is None else headless)


def sync_maximize_page_window(page: Any, *, headless: bool | None = None) -> None:
    if not should_maximize_browser_window(headless):
        return
    try:
        cdp = page.context.new_cdp_session(page)
        window = cdp.send("Browser.getWindowForTarget")
        cdp.send(
            "Browser.setWindowBounds",
            {
                "windowId": window["windowId"],
                "bounds": {"windowState": "maximized"},
            },
        )
        page.wait_for_timeout(200)
    except Exception:
        pass


async def async_maximize_page_window(page: Any, *, headless: bool | None = None) -> None:
    if not should_maximize_browser_window(headless):
        return
    try:
        cdp = await page.context.new_cdp_session(page)
        window = await cdp.send("Browser.getWindowForTarget")
        await cdp.send(
            "Browser.setWindowBounds",
            {
                "windowId": window["windowId"],
                "bounds": {"windowState": "maximized"},
            },
        )
        await page.wait_for_timeout(200)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Background request browser: discover an installed Chromium-family browser
# (CloakBrowser bundle, then system Chrome, then Edge) instead of requiring the
# ~500MB CloakBrowser download. Set AISTUDIO_BROWSER=cloakbrowser to force the
# old auto-downloading cloakbrowser package.
#

_BACKGROUND_BROWSER_HINT = (
    "未找到可用的浏览器。请任选其一：\n"
    " 1. 下载稳定版 Chromium（约 130MB，自动安装到 Playwright 目录）：\n"
    "    .venv\\Scripts\\python.exe -m playwright install chromium\n"
    " 2. 安装 Google Chrome：https://www.google.com/chrome/\n"
    " 3. 使用系统自带的 Microsoft Edge（Windows 10/11 默认已有）\n"
    " 4. （可选，反检测增强）下载 CloakBrowser 解压到项目目录 "
    "cloakbrowser-chromium\\chrome.exe：https://github.com/CloakHQ/cloakbrowser/releases\n"
    "也可以在 .env 中通过 AISTUDIO_BROWSER_EXECUTABLE 指定浏览器路径，"
    "或设置 AISTUDIO_BROWSER=cloakbrowser 让程序自动下载 CloakBrowser。"
)

# 对齐 cloakbrowser 包的启动参数：不带自动化痕迹标记
_CHROMIUM_IGNORE_DEFAULT_ARGS = ["--enable-automation", "--enable-unsafe-swiftshader"]


def detect_background_browser() -> dict[str, str] | None:
    """发现后台请求浏览器。

    顺序：AISTUDIO_BROWSER_EXECUTABLE → Playwright 管理的稳定版 Chromium
    （`python -m playwright install chromium`，标准安装步骤）→ 项目内
    cloakbrowser-chromium（反检测增强版，可选）→ 系统 Chrome → 系统 Edge。
    """
    candidates: list[tuple[str, str]] = []

    def _add(path: str, kind: str) -> None:
        resolved = str(Path(path).resolve())
        if all(resolved != existing for existing, _ in candidates):
            candidates.append((resolved, kind))

    if settings.browser_executable_path and Path(settings.browser_executable_path).is_file():
        _add(settings.browser_executable_path, "cloakbrowser")

    playwright_chromium = _find_playwright_chromium()
    if playwright_chromium:
        _add(playwright_chromium, "chromium")

    bundled = PROJECT_ROOT / "cloakbrowser-chromium" / "chrome.exe"
    if bundled.is_file():
        _add(str(bundled), "cloakbrowser")

    for channel in ("chrome", "msedge"):
        executable = _find_system_browser_executable(channel)
        if executable:
            _add(executable, channel)

    if not candidates:
        return None
    path, kind = candidates[0]
    return {"executable_path": path, "kind": kind}


def _find_playwright_chromium() -> str | None:
    """定位 `python -m playwright install chromium` 下载的稳定版 Chromium。"""
    roots: list[Path] = []
    override = os.getenv("PLAYWRIGHT_BROWSERS_PATH")
    if override and override != "0":
        roots.append(Path(override))
    if platform.system() == "Windows":
        local_appdata = os.getenv("LOCALAPPDATA")
        if local_appdata:
            roots.append(Path(local_appdata) / "ms-playwright")
    else:
        roots.append(Path.home() / ".cache" / "ms-playwright")

    patterns: dict[str, list[str]] = {
        # chrome-win64 是较新 playwright 的目录名，chrome-win 是旧版
        "Windows": ["chrome-win64/chrome.exe", "chrome-win/chrome.exe"],
        "Darwin": ["chrome-mac/Chromium.app/Contents/MacOS/Chromium"],
        "Linux": ["chrome-linux/chrome"],
    }
    suffixes = patterns.get(platform.system(), patterns["Linux"])
    for root in roots:
        if not root.is_dir():
            continue
        # 版本目录倒序，取最新
        for version_dir in sorted(root.glob("chromium-*"), reverse=True):
            for suffix in suffixes:
                candidate = version_dir / suffix
                if candidate.is_file():
                    return str(candidate)
    return None


def _background_launch_args(kind: str, *, headless: bool, stable_fingerprint_key: str | None = None) -> list[str]:
    if kind == "cloakbrowser":
        # fingerprint 参数只有 CloakBrowser 定制二进制认识
        return _build_cloakbrowser_args(
            headless=headless, stable_fingerprint_key=stable_fingerprint_key
        )
    args: list[str] = []
    if not headless:
        args.append("--start-maximized")
    return args


def _require_background_browser() -> dict[str, str]:
    found = detect_background_browser()
    if found is None:
        raise RuntimeError(_BACKGROUND_BROWSER_HINT)
    return found


def sync_launch_browser() -> tuple[Any, Any | None, Any | None]:
    """Launch a sync browser session.

    Returns:
        tuple of (browser, camoufox_context_manager, playwright_instance)
    """
    if is_camoufox_engine():
        from camoufox.sync_api import Camoufox

        cf = Camoufox(
            headless=settings.browser_headless,
            main_world_eval=True,
            proxy=build_camoufox_proxy(settings.proxy_url),
        )
        browser = cf.__enter__()
        return browser, cf, None

    if is_cloakbrowser_explicit_engine():
        from cloakbrowser import launch

        headless = settings.browser_headless
        browser = launch(
            headless=headless,
            proxy=build_camoufox_proxy(settings.proxy_url),
            stealth_args=False,
            args=_build_cloakbrowser_args(headless=headless),
        )
        return browser, None, None

    from playwright.sync_api import sync_playwright

    found = _require_background_browser()
    headless = settings.browser_headless
    logger.info("后台浏览器: %s (%s)", found["kind"], found["executable_path"])
    pw = sync_playwright().start()
    browser = pw.chromium.launch(
        executable_path=found["executable_path"],
        headless=headless,
        proxy=build_camoufox_proxy(settings.proxy_url),
        chromium_sandbox=settings.browser_chromium_sandbox,
        ignore_default_args=_CHROMIUM_IGNORE_DEFAULT_ARGS,
        args=_background_launch_args(found["kind"], headless=headless),
    )
    return browser, None, pw


def sync_launch_persistent_context(
    user_data_dir: str,
    *,
    headless: bool | None = None,
    **context_kwargs: Any,
) -> Any:
    """Launch a persistent Chromium BrowserContext backed by a profile dir."""
    if is_camoufox_engine():
        raise RuntimeError("sync_launch_persistent_context() only supports Chromium backend")

    if is_cloakbrowser_explicit_engine():
        from cloakbrowser import launch_persistent_context

        # cloakbrowser's persistent launcher treats `viewport=None` as "use the real
        # window size", while passing `no_viewport=True` through kwargs can leave it
        # with conflicting viewport settings. Normalize it here so non-headless UI
        # keeps the same auto-fit behavior as the old browser.new_context path.
        if context_kwargs.pop("no_viewport", False):
            context_kwargs["viewport"] = None

        headless = settings.browser_headless if headless is None else headless
        return launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=headless,
            proxy=build_camoufox_proxy(settings.proxy_url),
            stealth_args=False,
            args=_build_cloakbrowser_args(
                headless=headless,
                stable_fingerprint_key=user_data_dir,
            ),
            **context_kwargs,
        )

    from playwright.sync_api import sync_playwright

    found = _require_background_browser()
    headless = settings.browser_headless if headless is None else headless
    logger.info("后台浏览器: %s (%s)", found["kind"], found["executable_path"])

    if context_kwargs.pop("no_viewport", False):
        context_kwargs["viewport"] = None

    pw = sync_playwright().start()
    context = pw.chromium.launch_persistent_context(
        user_data_dir=user_data_dir,
        executable_path=found["executable_path"],
        headless=headless,
        proxy=build_camoufox_proxy(settings.proxy_url),
        chromium_sandbox=settings.browser_chromium_sandbox,
        ignore_default_args=_CHROMIUM_IGNORE_DEFAULT_ARGS,
        args=_background_launch_args(
            found["kind"], headless=headless, stable_fingerprint_key=user_data_dir
        ),
        **context_kwargs,
    )
    # 普通系统 Chrome/Edge 在 CDP 附加时 navigator.webdriver 为 true；
    # CloakBrowser 二进制则天然为 false。统一注入抹平差异。
    try:
        context.add_init_script(_HIDE_WEBDRIVER_SCRIPT)
    except Exception:
        pass
    _patch_close_to_stop_playwright(context, pw)
    return context


def _patch_close_to_stop_playwright(target: Any, playwright: Any) -> None:
    """让 context/browser.close() 同时停掉 playwright driver，避免泄漏。"""
    original_close = target.close

    def _close_with_cleanup(*args: Any, **kwargs: Any) -> Any:
        try:
            return original_close(*args, **kwargs)
        finally:
            try:
                playwright.stop()
            except Exception:
                pass

    target.close = _close_with_cleanup


async def async_launch_browser(*, headless: bool | None = None) -> Any:
    """Launch an async browser session."""
    if is_camoufox_engine():
        raise RuntimeError("async_launch_browser() only supports Chromium backend")

    if is_cloakbrowser_explicit_engine():
        from cloakbrowser import launch_async

        headless = settings.browser_headless if headless is None else headless
        return await launch_async(
            headless=headless,
            proxy=build_camoufox_proxy(settings.proxy_url),
            stealth_args=False,
            args=_build_cloakbrowser_args(headless=headless),
        )

    from playwright.async_api import async_playwright

    found = _require_background_browser()
    headless = settings.browser_headless if headless is None else headless
    logger.info("后台浏览器: %s (%s)", found["kind"], found["executable_path"])
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(
        executable_path=found["executable_path"],
        headless=headless,
        proxy=build_camoufox_proxy(settings.proxy_url),
        chromium_sandbox=settings.browser_chromium_sandbox,
        ignore_default_args=_CHROMIUM_IGNORE_DEFAULT_ARGS,
        args=_background_launch_args(found["kind"], headless=headless),
    )
    _patch_close_to_stop_playwright(browser, playwright)
    return browser


# ---------------------------------------------------------------------------
# Interactive login window: prefer the user's installed system browser so the
# login step never requires the ~500MB CloakBrowser download.
#

_SYSTEM_BROWSER_INSTALL_PATHS: dict[str, list[str]] = {
    "chrome": [
        r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
        r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
    ],
    "msedge": [
        r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
        r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/usr/bin/microsoft-edge",
        "/usr/bin/microsoft-edge-stable",
    ],
}

_SYSTEM_BROWSER_WHICH_NAMES: dict[str, tuple[str, ...]] = {
    "chrome": ("google-chrome", "google-chrome-stable"),
    "msedge": ("microsoft-edge", "microsoft-edge-stable"),
}


def _find_system_browser_executable(channel: str) -> str | None:
    for candidate in _SYSTEM_BROWSER_INSTALL_PATHS.get(channel, []):
        expanded = os.path.expandvars(candidate)
        if os.path.isfile(expanded):
            return expanded
    for name in _SYSTEM_BROWSER_WHICH_NAMES.get(channel, ()):
        found = shutil.which(name)
        if found:
            return found
    return None


def _windows_default_browser_channel() -> str | None:
    """Read the user's default browser from the Windows registry.

    Returns "chrome"/"msedge" when the default is a Chromium-family browser we
    can launch, so the login window matches the browser the user actually uses.
    """
    if platform.system() != "Windows":
        return None
    try:
        import winreg
    except ImportError:  # pragma: no cover - Windows-only
        return None
    subkeys = (
        r"Software\Microsoft\Windows\CurrentVersion\UrlAssociations\https\UserChoice",
        r"Software\Microsoft\Windows\CurrentVersion\UrlAssociations\http\UserChoice",
        r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.htm\UserChoice",
        r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.html\UserChoice",
    )
    for subkey in subkeys:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey) as key:
                prog_id, _ = winreg.QueryValueEx(key, "ProgId")
        except OSError:
            continue
        prog_id = (prog_id or "").lower()
        if "edge" in prog_id:
            return "msedge"
        if "chrome" in prog_id:
            return "chrome"
        return None
    return None


def _is_process_running(image_name: str) -> bool:
    """检测浏览器进程是否正在运行（用于规避 Edge 启动加速的转发问题）。"""
    try:
        if platform.system() == "Windows":
            # 不用 text=True：中文 Windows 的 tasklist 输出是 GBK 编码，
            # UTF-8 模式的 Python 解码会抛异常。直接按字节比较。
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {image_name}"],
                capture_output=True,
                timeout=5,
                creationflags=0x08000000,
            )
            return image_name.lower().encode() in (result.stdout or b"").lower()
        result = subprocess.run(
            ["pgrep", "-x", image_name], capture_output=True, timeout=5
        )
        return result.returncode == 0
    except Exception:
        return False


# 同一进程内启动失败的登录浏览器不再重试（例如 Edge 被后台进程占用），
# 避免每次"添加账号"都重复失败、重复弹出多余的 about:blank 窗口。
_failed_login_channels: set[str] = set()


def _system_login_candidates() -> list[dict[str, str]]:
    """Ordered system-browser candidates for the login window."""
    override = (settings.login_browser_channel or "").strip().lower()
    if override:
        executable = _find_system_browser_executable(override)
        return [{"channel": override, "executable_path": executable or ""}]

    preferred = _windows_default_browser_channel()
    if preferred in ("chrome", "msedge"):
        channels = (preferred, "msedge" if preferred == "chrome" else "chrome")
    else:
        channels = ("chrome", "msedge")
    candidates: list[dict[str, str]] = []
    for channel in channels:
        if channel in _failed_login_channels:
            continue
        # Edge 的"启动加速"会让后台常驻 msedge.exe；此时带自定义 profile
        # 的新实例会被转发给常驻进程后直接退出，还会在用户的 Edge 里弹出
        # 一个多余的 about:blank 窗口。检测到 Edge 正在运行就跳过它。
        if channel == "msedge" and _is_process_running("msedge.exe"):
            logger.info("检测到 Edge 正在运行（启动加速/用户使用中），登录窗口跳过 Edge")
            continue
        executable = _find_system_browser_executable(channel)
        if executable:
            candidates.append({"channel": channel, "executable_path": executable})
    return candidates


def detect_system_login_browser() -> dict[str, str] | None:
    """Find an installed Chrome/Edge for the interactive login window.

    Explicit AISTUDIO_LOGIN_BROWSER_CHANNEL wins; otherwise prefer the user's
    default browser when it is Chrome/Edge, then Chrome, then Edge
    (preinstalled on Windows 10/11). Returns a dict with ``channel`` and, when
    found on disk, ``executable_path``.
    """
    candidates = _system_login_candidates()
    return candidates[0] if candidates else None


@dataclass(slots=True)
class LoginBrowserHandle:
    """Persistent login context plus the resources the caller must release."""

    context: Any
    playwright: Any | None  # must be stopped by the caller when set
    backend: str  # "system:chrome" | "system:msedge" | "cloakbrowser"


def _login_window_args(headless: bool) -> list[str]:
    # 不要传 --disable-blink-features=AutomationControlled 这类"危险标记"：
    # Chrome 会在窗口顶部显示"不受支持的命令行标记"警告条。
    # navigator.webdriver 的伪装改用 _hide_webdriver_flag 的 init 脚本实现。
    args = []
    if not headless:
        args.append("--start-maximized")
    return args


# Playwright 通过 CDP 控制浏览器时 navigator.webdriver 会是 true，Google
# 登录页可能据此显示"此浏览器或应用不安全"。用 init 脚本覆盖成真实用户
# 浏览器的值（false），比 --disable-blink-features 标记更隐蔽且不触发
# Chrome 的"不受支持的命令行标记"警告条。
_HIDE_WEBDRIVER_SCRIPT = (
    "Object.defineProperty(navigator, 'webdriver', "
    "{get: () => false, configurable: true});"
)


async def async_launch_login_context(*, headless: bool, profile_dir: str) -> LoginBrowserHandle:
    """Launch the interactive Google login browser with a persistent profile.

    The persistent profile keeps Google sessions across logins, so adding an
    account shows Google's account chooser (one click + authorize) instead of
    the full email/password/2FA flow every time.

    Preference order honors AISTUDIO_LOGIN_BROWSER (default "auto"):
    1. system Chrome/Edge — no download required. The user's default browser
       is tried first; on launch failure (e.g. Edge's startup-boost background
       process hijacks custom-profile launches) the other browser is tried.
    2. cloakbrowser stealth Chromium — downloads on demand
    """
    if settings.login_browser in ("auto", "system"):
        failures: list[str] = []
        for candidate in _system_login_candidates():
            try:
                handle = await _launch_system_login_browser(
                    candidate, headless=headless, profile_dir=profile_dir
                )
                _failed_login_channels.discard(candidate["channel"])
                return handle
            except Exception as exc:
                failures.append(f"{candidate['channel']}: {exc}")
                _failed_login_channels.add(candidate["channel"])
                logger.warning(
                    "login browser %s failed to launch, trying next candidate: %s",
                    candidate["channel"],
                    str(exc).splitlines()[0] if str(exc) else exc,
                )
        if settings.login_browser == "system":
            detail = "; ".join(failures) if failures else "未找到可用的系统 Chrome/Edge"
            raise RuntimeError(f"系统 Chrome/Edge 启动失败：{detail}")

    from cloakbrowser import launch_persistent_context_async

    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    context_kwargs: dict[str, Any] = {}
    if not headless:
        # cloakbrowser's persistent launcher treats viewport=None as "use the
        # real window size"; no_viewport=True can conflict there.
        context_kwargs["viewport"] = None
    context = await launch_persistent_context_async(
        user_data_dir=profile_dir,
        headless=headless,
        proxy=build_camoufox_proxy(settings.proxy_url),
        stealth_args=False,
        chromium_sandbox=True,
        args=_login_window_args(headless),
        **context_kwargs,
    )
    await context.add_init_script(_HIDE_WEBDRIVER_SCRIPT)
    return LoginBrowserHandle(context=context, playwright=None, backend="cloakbrowser")


async def _launch_system_login_browser(
    candidate: dict[str, str],
    *,
    headless: bool,
    profile_dir: str,
) -> LoginBrowserHandle:
    from playwright.async_api import async_playwright

    playwright = await async_playwright().start()
    launch_options: dict[str, Any] = {
        "user_data_dir": profile_dir,
        "headless": headless,
        # Playwright 默认禁用沙箱（会加 --no-sandbox，Chrome 顶部出现
        # "不受支持的命令行标记"警告）。登录窗口加载的是 Google 官方
        # 页面且由用户亲自交互，恢复默认沙箱更安全也少一个自动化痕迹。
        "chromium_sandbox": True,
        "args": _login_window_args(headless),
        "ignore_default_args": ["--enable-automation"],
    }
    if not headless:
        launch_options["no_viewport"] = True
    proxy = build_camoufox_proxy(settings.proxy_url)
    if proxy:
        launch_options["proxy"] = proxy
    if candidate["executable_path"]:
        launch_options["executable_path"] = candidate["executable_path"]
    else:
        launch_options["channel"] = candidate["channel"]
    try:
        context = await playwright.chromium.launch_persistent_context(**launch_options)
    except Exception:
        await playwright.stop()
        raise
    await context.add_init_script(_HIDE_WEBDRIVER_SCRIPT)
    return LoginBrowserHandle(
        context=context,
        playwright=playwright,
        backend=f"system:{candidate['channel']}",
    )

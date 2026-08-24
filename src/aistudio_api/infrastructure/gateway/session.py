"""Shared browser session management for gateway operations."""

from __future__ import annotations

import asyncio
import shutil

import json
import logging
import threading
import time
import uuid
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
from typing import Any

from aistudio_api.config import settings
from aistudio_api.infrastructure.account.account_store import AccountStore
from aistudio_api.infrastructure.browser.browser_engine import (
    build_browser_context_options,
    describe_browser_backend,
    is_camoufox_engine,
    sync_launch_browser,
    sync_launch_persistent_context,
    sync_maximize_page_window,
)
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent
from aistudio_api.infrastructure.gateway.model_catalog import filter_gemini_models

log = logging.getLogger("aistudio.session")

AI_STUDIO_URL = "https://aistudio.google.com/prompts/new_chat?model=gemini-3.7-flash"
AI_STUDIO_URL_FALLBACK = "https://aistudio.google.com/app/prompts/new_chat"
GOOGLE_LOGIN_BOOTSTRAP_URL = (
    "https://accounts.google.com/ServiceLogin?continue=https://aistudio.google.com"
)
INSTALL_HOOKS_JS = r"""
mw:((() => {
    // Verify hooks are actually present on XHR prototype, not just a stale flag
    const xhrHookAlive = XMLHttpRequest.prototype.open.__api_hooked === true;
    const fetchHookAlive = window.fetch.__api_hooked === true;
    if (window.__bg_hooked && xhrHookAlive && fetchHookAlive) return 'already_hooked';
    // Reset stale flag if hooks are missing
    if (window.__bg_hooked && (!xhrHookAlive || !fetchHookAlive)) window.__bg_hooked = false;

    const dms = window.default_MakerSuite;
    if (!dms) return 'no_default_MakerSuite';

    // Auto-detect snapshot function via feature matching
    let snapKey = null;
    for (const k of Object.keys(dms)) {
        try {
            if (typeof dms[k] !== 'function') continue;
            const src = dms[k].toString();
            if (src.includes('.snapshot({') && src.includes('content') && src.includes('yield')) {
                snapKey = k;
                break;
            }
        } catch(e) {}
    }
    if (!snapKey) return 'no_snapshot_fn';

    // Hook snapshot function to capture service (only if not already hooked)
    if (!dms[snapKey].__api_hooked) {
        const origSnap = dms[snapKey];
        dms[snapKey] = function(...args) {
            window.__bg_service = args[0];
            const result = origSnap.apply(this, args);
            if (result instanceof Promise) return result.then(s => { window.__bg_snapshot = s; return s; });
            window.__bg_snapshot = result;
            return result;
        };
        dms[snapKey].__api_hooked = true;
    }

    // XHR hook for body replacement (always re-install if missing)
    const origOpen = XMLHttpRequest.prototype.open;
    const origSend = XMLHttpRequest.prototype.send;
    const hookedOpen = function(method, url, ...args) {
        this.__url = url;
        this.__is_gen = url.includes('GenerateContent') && !url.includes('CountTokens');
        window.__last_hook_url = url;
        return origOpen.call(this, method, url, ...args);
    };
    hookedOpen.__api_hooked = true;
    XMLHttpRequest.prototype.open = hookedOpen;
    XMLHttpRequest.prototype.send = function(body) {
        if (this.__is_gen && window.__pending_body) {
            const captured = window.__pending_body;
            window.__pending_body = null;
            window.__hooked = true;
            window.__last_hook_url = this.__url || '';
            return origSend.call(this, captured);
        }
        return origSend.call(this, body);
    };

    // fetch hook for body replacement (streaming uses fetch)
    const origFetch = window.fetch;
    const hookedFetch = function(input, init) {
        let url = typeof input === 'string' ? input : (input instanceof Request ? input.url : String(input));
        if (url.includes('GenerateContent') && !url.includes('CountTokens') && window.__pending_body) {
            const captured = window.__pending_body;
            window.__pending_body = null;
            window.__hooked = true;
            window.__last_hook_url = url;
            if (init) {
                init.body = captured;
            } else {
                init = { body: captured };
            }
            return origFetch.call(this, input, init);
        }
        return origFetch.call(this, input, init);
    };
    hookedFetch.__api_hooked = true;
    window.fetch = hookedFetch;

    window.__bg_hooked = true;
    window.__snap_key = snapKey;
    return 'hooked:' + snapKey;
})())
"""

DIALOG_CLEANUP_JS = """(() => {
    document.querySelectorAll('button').forEach((button) => {
        const text = (button.textContent || '').trim().toLowerCase();
        if (['dismiss', 'close', 'accept', 'ok', 'agree', 'got it'].includes(text)) {
            button.click();
        }
    });
    document.querySelectorAll('.cdk-overlay-backdrop').forEach((node) => node.remove());
    document.querySelectorAll('.cdk-overlay-container').forEach((node) => node.remove());
})()"""

BOTGUARD_BOOTSTRAP_PROMPT = "say '1'"
TEMPLATE_CAPTURE_PROMPT = "say 't'"

# Google only sets these after a real sign-in. NID/OTZ are handed out to
# anonymous visitors, so they prove nothing about authentication.
_GOOGLE_SESSION_COOKIE_NAMES = frozenset({
    "sid", "hsid", "ssid", "sapisid", "lsid", "sidcc",
    "__secure-1psid", "__secure-3psid",
    "__secure-1papisid", "__secure-3papisid",
    "__secure-1psidcc", "__secure-3psidcc",
})


def _has_google_session_cookies(cookies: list[dict[str, Any]] | None) -> bool:
    """True when the cookie jar carries an authenticated Google session."""
    for cookie in cookies or []:
        name = (cookie.get("name") or "").lower()
        if name not in _GOOGLE_SESSION_COOKIE_NAMES:
            continue
        domain = (cookie.get("domain") or "").lstrip(".").lower()
        if domain == "google.com" or domain.endswith(".google.com"):
            return True
    return False


def _clear_worker_event_loop() -> None:
    try:
        asyncio.set_event_loop(None)
    except Exception:
        pass


class BrowserSession:
    def __init__(
        self,
        port: int,
        *,
        auth_file: str | None = None,
        profile_dir: str | None = None,
        browser_identity_key: str | None = None,
    ):
        self.port = port
        self._auth_file = auth_file or settings.auth_file or self._discover_active_auth_file()
        self._profile_dir = profile_dir or self._derive_profile_dir(self._auth_file)
        self._browser_identity_key = browser_identity_key
        self._hook_page = None
        self._ctx = None
        self._browser = None
        self._cf = None
        self._playwright = None
        self._snap_key: str | None = None
        self._templates: dict[str, dict[str, Any]] = {}
        self._bootstrap_template: dict[str, Any] | None = None
        self._models_cache: tuple[float, list[str]] | None = None
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="aistudio-browser",
            initializer=_clear_worker_event_loop,
        )
        self._closed = False

    async def ensure_context(self):
        started = time.perf_counter()
        try:
            result = await self._run_sync(self._ensure_browser_sync)
            log.info("[probe] ensure_context total=%.3fs result=ready", time.perf_counter() - started)
            return result
        except Exception as exc:
            log.info(
                "[probe] ensure_context total=%.3fs result=error type=%s",
                time.perf_counter() - started,
                type(exc).__name__,
            )
            raise

    async def ensure_authenticated(self) -> None:
        """确认当前懒加载的浏览器上下文仍持有真实 Google 会话。"""
        await self._run_sync(self._verify_authenticated_sync)

    async def switch_auth(self, auth_file: str | None) -> None:
        await self._run_sync(self._switch_auth_sync, auth_file)

    async def ensure_hook_page(self):
        await self._run_sync(self._ensure_hook_page_sync)
        return True

    @staticmethod
    def _is_aistudio_page_url(url: str | None) -> bool:
        """Check the actual host, not a continue= query parameter."""
        try:
            return (urlparse(url or "").hostname or "").lower() == "aistudio.google.com"
        except Exception:
            return False

    async def ensure_botguard_service(self):
        await self._run_sync(self._ensure_botguard_service_sync)
        return True

    async def discover_models(self, force: bool = False) -> list[str]:
        """Discover Gemini model labels exposed by the signed-in AI Studio UI."""
        return await self._run_sync(self._discover_models_sync, force)

    async def capture_template(self, model: str) -> dict[str, Any]:
        return await self._run_sync(self._capture_template_sync, model)

    async def upload_images(self, image_paths: list[str]) -> list[str]:
        return await self._run_sync(self._upload_images_sync, image_paths)

    async def generate_snapshot(self, contents: list[AistudioContent]) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, lambda: self._generate_snapshot_sync(contents))

    async def close(self) -> None:
        """关闭浏览器、Playwright driver 和专用工作线程。"""
        if getattr(self, "_closed", False):
            return
        try:
            await self._run_sync(self._close_sync)
        finally:
            self._executor.shutdown(wait=True, cancel_futures=False)
            self._closed = True

    async def release_context(self) -> None:
        """关闭当前浏览器上下文但保留执行器，释放账号 profile 文件锁。

        下次请求会自动重新启动浏览器。用于删除/退出活跃账号前解锁其
        持久化 profile 目录（Windows 上被占用的目录无法删除）。
        """
        await self._run_sync(self._close_sync)

    async def send_hooked_request(self, *, body: str, timeout_ms: int) -> tuple[int, bytes]:
        return await self._run_sync(self._send_hooked_request_sync, body, timeout_ms)

    async def send_streaming_request(self, *, body: str, timeout_ms: int):
        """Send a streaming request, yielding ("status", int) and ("chunk", bytes) events."""
        queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        cancel_event = threading.Event()

        def _stream_worker():
            try:
                log.debug("[stream] worker started")
                self._send_streaming_request_sync(body, timeout_ms, queue, loop, cancel_event)
                log.debug("[stream] worker finished")
            except Exception as e:
                log.debug(f"[stream] worker exception: {e}")
                loop.call_soon_threadsafe(queue.put_nowait, ("error", e))
                loop.call_soon_threadsafe(queue.put_nowait, None)

        executor_task = loop.run_in_executor(self._executor, _stream_worker)
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                tag, data = item
                if tag == "error":
                    raise data
                yield tag, data
        finally:
            cancel_event.set()
            await executor_task

    async def _run_sync(self, func, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, lambda: func(*args))

    @staticmethod
    def _discover_active_auth_file() -> str | None:
        try:
            store = AccountStore()
            account = store.get_active_account()
            if account is None:
                return None
            path = store.get_auth_path_optional(account.id, require_exists=False)
            return str(path) if path is not None else None
        except Exception:
            return None

    @staticmethod
    def _derive_profile_dir(auth_file: str | None) -> str | None:
        if not auth_file:
            fallback_auth_file = BrowserSession._discover_active_auth_file()
            if not fallback_auth_file:
                return None
            auth_file = fallback_auth_file
        return str(Path(auth_file).resolve().parent / "profile")

    def _bootstrap_google_session_sync(self, page) -> None:
        """Visit Google surfaces so Chromium can materialize a stable profile."""
        # The auth cookies have already been imported. Visiting the Google
        # login shell first adds several seconds and can wait on long-lived
        # Google background requests; AI Studio itself is the useful check.
        for url in (AI_STUDIO_URL, AI_STUDIO_URL_FALLBACK):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(500)
                if "accounts.google.com" not in (page.url or ""):
                    return
            except Exception:
                continue
        raise RuntimeError(f"bootstrap stayed on login flow: url={page.url}")

    def _get_captured_info(self) -> tuple[str, dict[str, str]]:
        """Get captured URL and headers from template."""
        for tpl in self._templates.values():
            if tpl.get("url"):
                url = tpl["url"]
                headers = {k: v for k, v in tpl.get("headers", {}).items() if k.lower() not in ("host", "content-length")}
                return url, headers
        raise RuntimeError("no captured URL available for replay")

    def _send_streaming_request_sync(
        self,
        body: str,
        timeout_ms: int,
        queue: asyncio.Queue,
        loop: asyncio.AbstractEventLoop,
        cancel_event: threading.Event,
    ):
        """Sync method: sends XHR request and consumes page-side stream events."""
        import time as _t
        _t0 = _t.time()

        page, captured_url, captured_headers = self._prepare_streaming_sync()
        if is_camoufox_engine():
            captured_headers = {"content-type": "application/json"}
        log.debug(f"[stream] prep done in {_t.time()-_t0:.1f}s, url={captured_url}")

        timeout_s = timeout_ms / 1000
        rid = uuid.uuid4().hex[:8]

        # Start XHR in page context. Each request gets an isolated state object
        # keyed by rid, allowing multiple concurrent XHRs on the same page.
        page.evaluate("""(args) => {
            const rid = args.rid;
            if (!window.__streams) window.__streams = {};

            const existing = window.__streams[rid];
            if (existing && existing.xhr && existing.xhr.readyState !== 4) {
                try { existing.xhr.abort(); } catch (e) {}
            }

            const state = {
                xhr: null,
                events: [],
                waiter: null,
                recvPos: 0,
                statusSent: false,
            };
            window.__streams[rid] = state;

            function push(event) {
                if (state.waiter) {
                    const waiter = state.waiter;
                    state.waiter = null;
                    waiter(event);
                    return;
                }
                state.events.push(event);
            }

            function pushStatus(xhr) {
                if (state.statusSent || xhr.readyState < 2) return;
                state.statusSent = true;
                push({type: 'status', status: xhr.status || 0});
            }

            function pushChunk(xhr) {
                if (xhr.readyState < 3) return;
                const chunk = xhr.responseText.substring(state.recvPos);
                if (!chunk) return;
                state.recvPos = xhr.responseText.length;
                push({type: 'chunk', text: chunk});
            }

            if (!window.__stream_next) window.__stream_next = {};
            window.__stream_next[rid] = function(timeoutMs) {
                if (state.events.length) return Promise.resolve(state.events.shift());
                return new Promise((resolve) => {
                    let done = false;
                    const timer = setTimeout(() => {
                        if (done) return;
                        done = true;
                        if (state.waiter === finish) state.waiter = null;
                        resolve({type: 'idle'});
                    }, timeoutMs);
                    const finish = (event) => {
                        if (done) return;
                        done = true;
                        clearTimeout(timer);
                        resolve(event);
                    };
                    state.waiter = finish;
                });
            };

            if (!window.__stream_abort) window.__stream_abort = {};
            window.__stream_abort[rid] = function() {
                if (state.xhr && state.xhr.readyState !== 4) {
                    try { state.xhr.abort(); } catch (e) {}
                }
            };

            var xhr = new XMLHttpRequest();
            xhr.open('POST', args.url);
            var h = args.headers;
            for (var k in h) {
                xhr.setRequestHeader(k, h[k]);
            }
            xhr.withCredentials = true;
            xhr.timeout = args.timeout * 1000;

            xhr.onreadystatechange = function() {
                pushStatus(xhr);
                pushChunk(xhr);
            };
            xhr.onprogress = function() {
                pushStatus(xhr);
                pushChunk(xhr);
            };
            xhr.onload = function() {
                pushStatus(xhr);
                pushChunk(xhr);
                push({type: 'done'});
            };
            xhr.onerror = function() {
                push({type: 'error', message: 'network error'});
            };
            xhr.ontimeout = function() {
                push({type: 'error', message: 'timeout'});
            };
            xhr.onabort = function() {
                push({type: 'aborted'});
            };

            state.xhr = xhr;
            xhr.send(args.body);
        }""", {
            "url": captured_url,
            "headers": captured_headers,
            "body": body,
            "timeout": timeout_s,
            "rid": rid,
        })

        deadline = _t.time() + timeout_s
        status_sent = False
        while _t.time() < deadline:
            if cancel_event.is_set():
                log.debug("[stream] cancellation requested for %s", rid)
                page.evaluate("rid => { if (window.__stream_abort && window.__stream_abort[rid]) window.__stream_abort[rid](); }", rid)
                break

            event = page.evaluate("rid => window.__stream_next[rid](250)", rid)
            event_type = event.get("type")

            if event_type == "idle":
                continue
            if event_type == "status":
                status = event.get("status", 0)
                log.debug(f"[stream] got status={status} after {_t.time()-_t0:.1f}s")
                loop.call_soon_threadsafe(queue.put_nowait, ("status", status))
                status_sent = True
                continue
            if event_type == "chunk":
                text = event.get("text") or ""
                if text:
                    loop.call_soon_threadsafe(queue.put_nowait, ("chunk", text.encode("utf-8")))
                continue
            if event_type == "error":
                message = event.get("message", "unknown error")
                log.debug(f"[stream] error after {_t.time()-_t0:.1f}s: {message}")
                loop.call_soon_threadsafe(queue.put_nowait, ("error", RuntimeError(f"streaming request failed: {message}")))
                loop.call_soon_threadsafe(queue.put_nowait, None)
                return
            if event_type in ("done", "aborted"):
                break

        if not status_sent:
            log.debug(f"[stream] timeout after {_t.time()-_t0:.1f}s before response status")
            loop.call_soon_threadsafe(queue.put_nowait, ("error", RuntimeError("streaming request timeout: no response status")))
            loop.call_soon_threadsafe(queue.put_nowait, None)
            return

        # Signal completion
        loop.call_soon_threadsafe(queue.put_nowait, None)

    def _prepare_streaming_sync(self):
        """Prepare page for streaming request. Returns (page, url, headers)."""
        page = self._ensure_botguard_service_sync()
        if not self._templates:
            # Template not yet captured — capture one so we have a URL
            from aistudio_api.config import DEFAULT_TEXT_MODEL
            try:
                self._capture_template_sync(DEFAULT_TEXT_MODEL)
            except Exception as e:
                log.warning("auto template capture failed: %s", e)
        url, headers = self._get_captured_info()
        return page, url, headers

    def _switch_auth_sync(self, auth_file: str | None) -> None:
        self._auth_file = auth_file
        self._profile_dir = self._derive_profile_dir(auth_file)
        self._templates.clear()
        self._bootstrap_template = None
        self._models_cache = None
        self._close_sync()

    def _discover_models_sync(self, force: bool = False) -> list[str]:
        """Read the model picker from the current AI Studio page."""
        now = time.time()
        if not force and self._models_cache and now - self._models_cache[0] < 300:
            return list(self._models_cache[1])

        page = self._ensure_botguard_service_sync()
        values: list[str] = []
        try:
            values.extend(page.evaluate(
                """() => Array.from(document.querySelectorAll(
                    'button,[role="button"],[role="option"],[role="menuitem"],option,[data-model]'
                )).flatMap(el => [
                    el.innerText, el.textContent, el.getAttribute('aria-label'),
                    el.getAttribute('data-model'), el.getAttribute('data-value'),
                    el.getAttribute('data-testid'), el.getAttribute('title'), el.getAttribute('value')
                ]).filter(Boolean)"""
            ) or [])
            page.evaluate(
                """() => {
                    const nodes = Array.from(document.querySelectorAll(
                        'button,[role="button"],[role="combobox"],[aria-haspopup]'
                    ));
                    const target = nodes.find(el => {
                        const text = `${el.innerText || ''} ${el.getAttribute('aria-label') || ''}`;
                        return /model|gemini|gemma/i.test(text) && el.offsetParent !== null;
                    });
                    if (target) {
                        target.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                        target.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                        target.click();
                    }
                    return Boolean(target);
                }"""
            )
            page.wait_for_timeout(350)
            values.extend(page.evaluate(
                """() => [document.body?.innerText || '', ...Array.from(document.querySelectorAll(
                    '[role="option"],[role="menuitem"],[data-model],[data-value]'
                )).flatMap(el => [el.innerText, el.textContent, el.getAttribute('data-model'), el.getAttribute('data-value')])].filter(Boolean)"""
            ) or [])
        except Exception as exc:
            log.warning("AI Studio model discovery failed: %s", exc)

        models = filter_gemini_models(values)
        self._models_cache = (now, models)
        log.info("AI Studio discovered %d Gemini models", len(models))
        return list(models)

    def _ensure_browser_sync(self):
        if self._ctx is not None and self._hook_page is not None and not self._hook_page.is_closed():
            return self._ctx

        import time as _t
        _t0 = _t.time()

        self._close_sync()

        # Chromium backend: auth.json
        if not is_camoufox_engine():
            return self._ensure_browser_chromium_sync(_t0)

        # Legacy mode: Camoufox + auth.json
        from camoufox.sync_api import Camoufox
        from aistudio_api.config import build_camoufox_proxy

        self._cf = Camoufox(
            headless=settings.browser_headless,
            main_world_eval=True,
            proxy=build_camoufox_proxy(settings.proxy_url),
        )
        self._browser = self._cf.__enter__()
        self._ctx = self._browser.new_context(**build_browser_context_options())
        self._apply_auth_file_sync()
        self._hook_page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        sync_maximize_page_window(self._hook_page)
        log.debug(f"[timing] browser launched in {_t.time()-_t0:.1f}s")
        self._goto_aistudio_sync(self._hook_page)
        log.debug(f"[timing] page loaded in {_t.time()-_t0:.1f}s")
        self._install_hooks_sync(self._hook_page)
        log.debug(f"[timing] hooks installed in {_t.time()-_t0:.1f}s")
        return self._ctx

    def _verify_authenticated_sync(self) -> None:
        page = self._hook_page
        if page is None or page.is_closed():
            raise RuntimeError("浏览器页面尚未准备好")
        current_url = page.url or ""
        if "accounts.google.com" in current_url or not self._context_has_google_session():
            raise RuntimeError("当前账号的 Google 会话已失效，请重新登录或激活其他账号")

    def _rotate_google_session_sync(self, page) -> None:
        """通过 ServiceLogin 被动续登刷新短时会话 token（如 __Secure-1PSIDTS）。

        登录浏览器导出的会话里，短 TTL token 在账号闲置几分钟后过期：长效
        cookie 仍有效（页面正常加载、判定已登录），但 GenerateContent 会被
        Google 以 PERMISSION_DENIED 拒绝。访问 ServiceLogin?continue=... 让
        Google 用长效 cookie 在当前浏览器里重新签发整套会话 token。这是上
        游项目 bootstrap 流程的一部分，延迟激活的账号必须走这一步。
        """
        try:
            page.goto(GOOGLE_LOGIN_BOOTSTRAP_URL, wait_until="domcontentloaded", timeout=15000)
            page.wait_for_timeout(2500)
            log.info(
                "[session-rotate] ServiceLogin 续登完成: url_host=%s",
                (page.url or "").split("/", 3)[2] if "/" in (page.url or "") else "",
            )
        except Exception as exc:
            log.debug("[session-rotate] bootstrap visit failed: %s", exc)

    def _ensure_browser_chromium_sync(self, _t0: float):
        """Chromium backend: prefer per-account persistent profile, fallback to auth.json."""
        import time as _t

        profile_dir = self._profile_dir
        log.info(
            "[probe] chromium.init auth_file=%s profile_dir=%s",
            bool(self._auth_file),
            bool(profile_dir),
        )
        if profile_dir:
            profile_path = Path(profile_dir)
            profile_path.mkdir(parents=True, exist_ok=True)
            self._ctx = sync_launch_persistent_context(
                profile_dir,
                stable_fingerprint_key=(
                    getattr(self, "_browser_identity_key", None) or profile_dir
                ),
                **build_browser_context_options(),
            )
            log.info("[probe] chromium.launch persistent=%.3fs", _t.time() - _t0)
            self._browser = None
            self._cf = None
            self._playwright = None
        else:
            self._browser, self._cf, self._playwright = sync_launch_browser()
            self._ctx = self._browser.new_context(**build_browser_context_options())
            log.info("[probe] chromium.launch persistent=false elapsed=%.3fs", _t.time() - _t0)

        # 系统 Chrome/Edge 二进制在 CDP 附加时 navigator.webdriver 为 true；
        # 统一注入抹平（CloakBrowser 二进制天然为 false，注入无害）。
        try:
            from aistudio_api.infrastructure.browser.browser_engine import (
                _HIDE_WEBDRIVER_SCRIPT,
            )

            self._ctx.add_init_script(_HIDE_WEBDRIVER_SCRIPT)
        except Exception:
            pass

        self._hook_page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        sync_maximize_page_window(self._hook_page)

        # First, see whether the persistent profile / current context is already
        # authenticated. AI Studio no longer server-redirects signed-out visits,
        # so a non-login URL alone misreads an empty profile as logged in.
        # Require real Google session cookies before trusting the profile.
        try:
            check_started = _t.time()
            self._hook_page.goto("https://aistudio.google.com/", wait_until="domcontentloaded", timeout=15000)
            has_session = self._context_has_google_session()
            log.info(
                "[probe] chromium.profile_check elapsed=%.3fs url_host=%s session=%s",
                _t.time() - check_started,
                (self._hook_page.url or "").split("/", 3)[2] if "/" in (self._hook_page.url or "") else "",
                has_session,
            )
            if "accounts.google.com" not in (self._hook_page.url or "") and has_session:
                profile_label = "profile" if profile_dir else "auth.json cache"
                log.info("[chromium-auth] %s hit", profile_label)
                self._rotate_google_session_sync(self._hook_page)
                # ServiceLogin normally returns to the generic AI Studio root,
                # whose remembered/default model can differ from the API's
                # configured model.  Merely checking the host let a request for
                # Gemini 3.7 Flash capture a Gemini 3 Flash Preview template and
                # Google rejected the replay with PERMISSION_DENIED.  Always
                # enter the explicit default-model URL after session rotation.
                self._goto_aistudio_sync(self._hook_page)
                # Persist cookies refreshed by ServiceLogin so additional
                # isolated request workers inherit the proven live session,
                # not the older account snapshot they were initially cloned
                # from.
                self._save_cookies_sync()
                self._install_hooks_sync(self._hook_page)
                log.debug(f"[timing] page loaded (cached) in {_t.time()-_t0:.1f}s")
                return self._ctx
            log.info("[chromium-auth] context lacks Google session cookies; trying auth.json")
        except Exception as e:
            log.debug("[chromium-auth] initial profile check failed: %s", e)

        # Rebuild the session from auth.json whenever the current context lacks
        # one (fresh profile, expired profile, or non-persistent context). This
        # branch is only reached when the live session check above failed, so
        # injecting auth.json cannot pollute an existing authenticated profile.
        if self._auth_file and Path(self._auth_file).exists():
            try:
                data = json.loads(Path(self._auth_file).read_text())
                cached = data.get("cookies") or []
                if _has_google_session_cookies(cached):
                    self._ctx.add_cookies(cached)
                    self._rotate_google_session_sync(self._hook_page)
                    self._goto_aistudio_sync(self._hook_page)
                    if (
                        "accounts.google.com" not in (self._hook_page.url or "")
                        and self._context_has_google_session()
                    ):
                        log.info("[chromium-auth] auth.json seeded context (%d cookies)", len(cached))
                        self._save_cookies_sync()
                        log.info("[probe] chromium.auth_seed elapsed=%.3fs", _t.time() - _t0)
                        self._install_hooks_sync(self._hook_page)
                        log.debug(f"[timing] page loaded (cached) in {_t.time()-_t0:.1f}s")
                        return self._ctx
                    log.info("[chromium-auth] auth.json appears expired")
                else:
                    log.info("[chromium-auth] auth.json has no session cookies")
            except Exception as e:
                log.debug("[chromium-auth] auth.json load failed: %s", e)

        log.debug(f"[timing] browser launched in {_t.time()-_t0:.1f}s")
        self._goto_aistudio_sync(self._hook_page)
        log.info("[probe] chromium.goto_aistudio elapsed=%.3fs", _t.time() - _t0)
        log.debug(f"[timing] page loaded in {_t.time()-_t0:.1f}s")
        self._install_hooks_sync(self._hook_page)
        log.info("[probe] chromium.install_hooks elapsed=%.3fs", _t.time() - _t0)
        log.debug(f"[timing] hooks installed in {_t.time()-_t0:.1f}s")
        return self._ctx

    def _apply_auth_file_sync(self):
        """Legacy mode: load cookies from auth.json."""
        if self._auth_file and Path(self._auth_file).exists():
            log.info(f"Loading auth from: {self._auth_file}")
            data = json.loads(Path(self._auth_file).read_text())
            cookies = data.get("cookies") or []
            if cookies:
                self._ctx.add_cookies(cookies)
                log.info(f"Added {len(cookies)} cookies to context")
        else:
            log.warning(f"No auth_file! self._auth_file={self._auth_file}")


    def _context_has_google_session(self) -> bool:
        try:
            return _has_google_session_cookies(self._ctx.cookies())
        except Exception:
            return False

    def _save_cookies_sync(
        self,
        *,
        auth_file: str | None = None,
        cookies: list[dict[str, Any]] | None = None,
    ) -> None:
        """将 cookie 保存回 auth.json。"""
        target_auth_file = auth_file or self._auth_file
        if not target_auth_file:
            return
        try:
            current_cookies = cookies
            if current_cookies is None:
                if self._ctx is None:
                    return
                current_cookies = self._ctx.cookies()
            if not current_cookies:
                return
            auth_path = Path(target_auth_file)
            # 读取现有的 origins 数据（如果有）
            origins = []
            existing_session = False
            if auth_path.exists():
                try:
                    existing = json.loads(auth_path.read_text())
                    origins = existing.get("origins", [])
                    existing_session = _has_google_session_cookies(existing.get("cookies") or [])
                except Exception:
                    pass
            if existing_session and not _has_google_session_cookies(current_cookies):
                # 登录流程刚导出的会话不允许被未登录上下文的匿名 cookie
                # （仅 NID 之类）覆盖，否则一次误判就毁掉账号凭据。
                log.warning(
                    "跳过保存：当前上下文无 Google 会话 cookie，保留 %s 中已有会话",
                    target_auth_file,
                )
                return
            auth_path.parent.mkdir(parents=True, exist_ok=True)
            auth_path.write_text(json.dumps({"cookies": current_cookies, "origins": origins}, indent=2))
            log.info(f"Saved {len(current_cookies)} cookies to {target_auth_file}")
        except Exception as e:
            log.debug(f"Failed to save cookies: {e}")

    def _ensure_hook_page_sync(self):
        self._ensure_browser_sync()
        if not self._is_aistudio_page_url(self._hook_page.url):
            self._goto_aistudio_sync(self._hook_page)
        self._install_hooks_sync(self._hook_page)
        return self._hook_page

    def _ensure_botguard_service_sync(self):
        import time as _t
        _t0 = _t.time()
        page = self._ensure_hook_page_sync()
        if page.evaluate("mw:!!window.__bg_service") and self._bootstrap_template:
            log.debug(f"[timing] botguard cached, took {_t.time()-_t0:.1f}s")
            return page

        captured: dict[str, Any] = {}
        route_pattern = "**/*GenerateContent*"

        def on_route(route, request):
            if "GenerateContent" not in request.url or "Count" in request.url:
                route.continue_()
                return
            body = request.post_data
            if body and not captured:
                captured["url"] = request.url
                captured["headers"] = dict(request.headers)
                captured["body"] = body
            # Capturing a request template must never create a cloud chat or
            # consume generation quota. The real user request is replayed only
            # after its content replaces this blocked bootstrap body.
            route.abort()

        page.evaluate(DIALOG_CLEANUP_JS)
        textarea = page.query_selector("textarea")
        if textarea is None:
            # Debug: show page state
            try:
                dbg_url = page.url
                dbg_title = page.title()
                dbg_body = page.evaluate("() => document.body?.innerText?.substring(0, 300) || ''")
            except Exception:
                dbg_url = dbg_title = dbg_body = '<error>'
            raise RuntimeError(f"textarea not found while capturing BotGuardService; url={dbg_url}, title={dbg_title}, body={dbg_body[:200]}")
        original_text = self._read_textarea_value_sync(textarea)
        page.route(route_pattern, on_route)
        try:
            textarea.fill(BOTGUARD_BOOTSTRAP_PROMPT)
            page.wait_for_timeout(800)
            page.evaluate(DIALOG_CLEANUP_JS)
            if not self._click_run_button_sync(page):
                raise RuntimeError("failed to trigger send while capturing BotGuardService")

            for _ in range(45):
                page.wait_for_timeout(1000)
                botguard_ready = page.evaluate("mw:!!window.__bg_service")
                if botguard_ready and captured:
                    if self._bootstrap_template is None:
                        self._bootstrap_template = dict(captured)
                    self._restore_textarea_value_sync(textarea, original_text)
                    log.info(
                        "[probe] botguard.template_ready network=blocked elapsed=%.3fs",
                        _t.time() - _t0,
                    )
                    return page

            raise RuntimeError(
                "BotGuardService 初始化超时：未能在本地截获请求模板"
            )
        finally:
            page.unroute(route_pattern, on_route)
            self._restore_textarea_value_sync(textarea, original_text)

    def _capture_template_sync(self, model: str) -> dict[str, Any]:
        import time as _t
        _t0 = _t.time()
        if model in self._templates:
            log.debug(f"[timing] template cached for {model}")
            return self._templates[model]

        self._ensure_botguard_service_sync()
        if self._bootstrap_template:
            captured = dict(self._bootstrap_template)
            self._templates[model] = captured
            log.debug(f"[timing] reused blocked bootstrap template for {model} in {_t.time()-_t0:.1f}s")
            return captured
        raise RuntimeError("request template unavailable after local capture")

    def _generate_snapshot_sync(self, contents: list[AistudioContent]) -> str:
        page = self._ensure_botguard_service_sync()
        if not self._snap_key:
            raise RuntimeError("Snapshot function not detected")

        # 计算 content hash（包含图片数据，与 camoufox-api 一致）
        hash_parts: list[str] = []
        for content in contents:
            for part in content.parts:
                if part.inline_data:
                    hash_parts.append(part.inline_data[1])  # base64 data
                if part.text:
                    hash_parts.append(str(part.text))
        content_hash = sha256(" ".join(hash_parts).encode("utf-8")).hexdigest()

        page.evaluate(
            """
mw:((hash) => {
    const dms = window.default_MakerSuite;
    const service = window.__bg_service;
    const snapKey = window.__snap_key;
    if (!dms || !service || !snapKey || typeof dms[snapKey] !== 'function') {
        window.__sr = '';
        window.__sl = 0;
        window.__snap_error = 'service_unavailable';
        return;
    }
    window.__sr = '';
    window.__sl = 0;
    window.__snap_error = '';
    const result = dms[snapKey](service, hash);
    if (result instanceof Promise) {
        result.then((snapshot) => {
            window.__sr = snapshot || '';
            window.__sl = snapshot ? snapshot.length : 0;
        }).catch((error) => {
            window.__snap_error = String(error);
        });
        return;
    }
    window.__sr = result || '';
    window.__sl = result ? result.length : 0;
})(%s)
"""
            % json.dumps(content_hash)
        )
        for _ in range(20):
            if page.evaluate("mw:(window.__sl || 0)") > 0:
                break
            page.wait_for_timeout(500)

        snapshot = page.evaluate("mw:window.__sr")
        if snapshot:
            return snapshot
        error = page.evaluate("mw:window.__snap_error || ''")
        raise RuntimeError(f"Snapshot generation failed: {error or 'unknown'}")

    def _upload_images_sync(self, image_paths: list[str]) -> list[str]:
        if not image_paths:
            return []

        # 尝试非 UI 方式上传（更快、更可靠）
        # 需要在主线程中获取 cookies，因为 Playwright 的同步 API 有 greenlet 限制
        try:
            if self._ctx is not None:
                cookies = self._ctx.cookies()
                return self._upload_images_via_api_sync(image_paths, cookies)
        except Exception as e:
            # 如果非 UI 方式失败，回退到 UI 方式
            log.debug("Non-UI upload failed, falling back to UI: %s", e)

        # UI 方式上传（原有逻辑）
        page = self._ensure_botguard_service_sync()
        self._wait_until_idle_sync(page)
        uploaded_ids: list[str] = []

        def on_response(response):
            if "content.googleapis.com/upload/drive/v3/files" not in response.url:
                return
            try:
                payload = json.loads(response.text())
            except Exception:
                return
            file_id = payload.get("id")
            if file_id:
                uploaded_ids.append(file_id)

        page.on("response", on_response)
        try:
            for image_path in image_paths:
                target_count = len(uploaded_ids) + 1
                page.evaluate(DIALOG_CLEANUP_JS)
                upload_btn = page.locator('[aria-label="Insert images, videos, audio, or files"]').first
                if not upload_btn.is_visible(timeout=3000):
                    raise RuntimeError("upload button not visible")
                upload_btn.click()
                page.wait_for_timeout(1500)
                page.evaluate(DIALOG_CLEANUP_JS)
                upload_files_btn = page.locator("text=Upload files").first
                if not upload_files_btn.is_visible(timeout=3000):
                    upload_btn.click()
                    page.wait_for_timeout(1000)
                    upload_files_btn = page.locator("text=Upload files").first
                if not upload_files_btn.is_visible(timeout=3000):
                    raise RuntimeError("upload files button not visible")
                with page.expect_file_chooser(timeout=10000) as chooser_info:
                    upload_files_btn.click()
                chooser_info.value.set_files(image_path)

                deadline = time.time() + 30
                while time.time() < deadline:
                    if len(uploaded_ids) >= target_count:
                        break
                    page.wait_for_timeout(500)
                page.wait_for_timeout(1500)
        finally:
            page.remove_listener("response", on_response)

        if len(uploaded_ids) != len(image_paths):
            raise RuntimeError(f"image upload incomplete: expected={len(image_paths)} uploaded={len(uploaded_ids)}")
        return uploaded_ids

    def _upload_images_via_api_sync(self, image_paths: list[str], cookies: list[dict]) -> list[str]:
        """通过 Playwright 的 setInputFiles 方法上传图片（非 UI 点击方式）"""
        page = self._hook_page
        if page is None:
            raise RuntimeError("Hook page not initialized")

        uploaded_ids: list[str] = []

        def on_response(response):
            if "content.googleapis.com/upload/drive/v3/files" not in response.url:
                return
            try:
                payload = json.loads(response.text())
            except Exception:
                return
            file_id = payload.get("id")
            if file_id:
                uploaded_ids.append(file_id)

        page.on("response", on_response)
        try:
            # 找到文件输入元素（如果有的话）
            file_input = page.query_selector('input[type="file"]')

            if file_input:
                # 直接使用 setInputFiles 方法上传
                for image_path in image_paths:
                    target_count = len(uploaded_ids) + 1
                    file_input.set_input_files(image_path)

                    # 等待上传完成
                    deadline = time.time() + 30
                    while time.time() < deadline:
                        if len(uploaded_ids) >= target_count:
                            break
                        page.wait_for_timeout(500)
                    page.wait_for_timeout(1000)
            else:
                # 如果没有 file input，尝试创建一个
                page.evaluate("""
                    () => {
                        const input = document.createElement('input');
                        input.type = 'file';
                        input.id = '__api_file_input__';
                        input.style.display = 'none';
                        input.accept = 'image/*';
                        document.body.appendChild(input);

                        // 监听文件选择事件
                        input.addEventListener('change', (e) => {
                            const file = e.target.files[0];
                            if (file) {
                                // 触发上传逻辑
                                window.__api_upload_file = file;
                            }
                        });
                    }
                """)

                file_input = page.query_selector('#__api_file_input__')
                if not file_input:
                    raise RuntimeError("Failed to create file input")

                for image_path in image_paths:
                    target_count = len(uploaded_ids) + 1
                    file_input.set_input_files(image_path)
                    page.wait_for_timeout(1000)

                    # 触发上传
                    page.evaluate("""
                        () => {
                            if (window.__api_upload_file) {
                                // 模拟拖放或触发上传按钮
                                const event = new Event('change', { bubbles: true });
                                const input = document.querySelector('#__api_file_input__');
                                if (input) input.dispatchEvent(event);
                            }
                        }
                    """)

                    # 等待上传完成
                    deadline = time.time() + 30
                    while time.time() < deadline:
                        if len(uploaded_ids) >= target_count:
                            break
                        page.wait_for_timeout(500)
                    page.wait_for_timeout(1000)

        finally:
            page.remove_listener("response", on_response)

        if len(uploaded_ids) != len(image_paths):
            raise RuntimeError(f"image upload incomplete: expected={len(image_paths)} uploaded={len(uploaded_ids)}")
        return uploaded_ids

    def _send_hooked_request_sync(self, body: str, timeout_ms: int) -> tuple[int, bytes]:
        import time as _t
        _t0 = _t.time()
        page = self._ensure_botguard_service_sync()
        log.debug(f"[timing] botguard ready in {_t.time()-_t0:.1f}s")
        captured_url, captured_headers = self._get_captured_info()
        if is_camoufox_engine():
            captured_headers = {"content-type": "application/json"}

        # Replay via XHR in browser context (same approach as non-streaming replay_v2)
        timeout_s = timeout_ms / 1000
        result = page.evaluate("""(args) => {
            return new Promise((resolve) => {
                var xhr = new XMLHttpRequest();
                xhr.open('POST', args.url);
                var h = args.headers;
                for (var k in h) {
                    xhr.setRequestHeader(k, h[k]);
                }
                xhr.withCredentials = true;
                xhr.timeout = args.timeout * 1000;
                xhr.onload = function() {
                    resolve({status: xhr.status, body: xhr.responseText});
                };
                xhr.onerror = function() {
                    resolve({status: 0, body: 'network error'});
                };
                xhr.ontimeout = function() {
                    resolve({status: 0, body: 'timeout'});
                };
                xhr.send(args.body);
            });
        }""", {
            "url": captured_url,
            "headers": captured_headers,
            "body": body,
            "timeout": timeout_s,
        })

        status = result.get("status", 0)
        raw_text = result.get("body", "")
        log.debug(f"[timing] replay done in {_t.time()-_t0:.1f}s, status={status}")
        if status == 0:
            raise RuntimeError(f"replay failed: {raw_text}")
        return status, raw_text.encode("utf-8")

    def _verify_account_identity_sync(self, page) -> None:
        """校验浏览器实际登录账号与期望账号是否一致，防止 cookies 交叉污染。

        Google/AI Studio 的 SPA 页面经常不会把当前邮箱直接渲染到 HTML。
        因此“页面中找不到期望邮箱”不能单独作为账号不一致的证据；只有
        页面明确暴露了另一个邮箱时才拒绝，避免误删刚刚成功登录的 profile。
        """
        auth_file = self._auth_file
        if not auth_file:
            return
        meta_path = Path(auth_file).parent / "meta.json"
        if not meta_path.exists():
            return
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            return
        expected_email = meta.get("email") or ""
        if not expected_email:
            return

        # 从页面获取当前登录状态
        try:
            page_html = page.content()
        except Exception:
            return

        if expected_email in page_html:
            return  # 一致，通过校验

        # AI Studio 已完成登录且页面正常加载，但邮箱可能只存在于内部
        # 状态或后续异步请求中。此时跳过严格邮箱匹配，避免误判并删除 profile。
        current_url = page.url or ""
        if "accounts.google.com" not in current_url:
            log.info(
                "[account-guard] 页面未直接暴露账号邮箱，按已登录 AI Studio 上下文继续: expected=%s",
                expected_email,
            )
            return

        # 不一致 — 防止污染
        account_id = meta.get("id", "unknown")
        log.warning(
            "[account-guard] 页面未登录期望账号 %s (%s)，拒绝保存 cookies 以防交叉污染",
            expected_email,
            account_id,
        )
        # 删除被污染的 profile 目录
        if self._profile_dir:
            profile_path = Path(self._profile_dir)
            if profile_path.exists():
                log.warning("[account-guard] 删除被污染的 profile 目录: %s", profile_path)
                shutil.rmtree(profile_path, ignore_errors=True)
        raise RuntimeError(
            f"页面未登录期望的账号 {expected_email} ({account_id})，"
            f"已删除 profile 缓存，请重新导入该账号的 cookies"
        )

    def _goto_aistudio_sync(self, page) -> None:
        import time as _t
        last_exc = None
        for url in (AI_STUDIO_URL, AI_STUDIO_URL_FALLBACK):
            try:
                _t0 = _t.time()
                log.info("[probe] aistudio.navigate_start url=%s", url)
                # Google keeps analytics and streaming requests open. Waiting
                # for networkidle made account activation needlessly take 30s
                # per attempt; DOMContentLoaded plus the UI readiness loop is
                # sufficient for hooks and request capture.
                page.goto(url, wait_until="domcontentloaded", timeout=15000)
                log.debug(f"[timing] goto {url} took {_t.time()-_t0:.1f}s")
                # 检查是否被重定向到登录页
                current_url = page.url or ""
                if "accounts.google.com" in current_url and "signin" in current_url:
                    raise RuntimeError(
                        f"Cookie 认证失败，已被重定向到 Google 登录页。"
                        f" (url={current_url})"
                    )
                self._wait_for_aistudio_ui_sync(page, _t0)
                log.info("[probe] aistudio.navigate_done url=%s elapsed=%.3fs", url, _t.time() - _t0)
                return
            except Exception as exc:
                log.warning("[probe] aistudio.navigate_failed url=%s elapsed=%.3fs type=%s", url, _t.time() - _t0, type(exc).__name__)
                last_exc = exc
        if last_exc is not None:
            raise last_exc

    def _wait_for_aistudio_ui_sync(self, page, started: float) -> None:
        """Wait for the SPA UI after navigation without navigating again."""
        import time as _t

        has_dms = False
        has_textarea = False
        # Keep the original 60-second readiness budget for slow networks, but
        # poll more frequently during the first 10 seconds. This shortens the
        # normal path without changing the readiness conditions or timeout.
        for index in range(90):
            page.wait_for_timeout(250 if index < 40 else 1000)
            current_url = page.url or ""
            if "accounts.google.com" in current_url:
                raise RuntimeError(
                    f"Cookie 认证失败，AI Studio 已重定向到 Google 登录页。 (url={current_url})"
                )
            has_dms = page.evaluate("mw:!!window.default_MakerSuite")
            has_textarea = page.query_selector("textarea") is not None
            if has_dms and has_textarea:
                log.info("[probe] aistudio.ui_ready elapsed=%.3fs", _t.time() - started)
                self._verify_account_identity_sync(page)
                self._save_cookies_sync()
                return
            if has_dms and index > 20:
                page.evaluate(DIALOG_CLEANUP_JS)
        log.info(
            "[probe] aistudio.ui_partial elapsed=%.3fs dms=%s textarea=%s",
            _t.time() - started,
            has_dms,
            has_textarea,
        )
        self._verify_account_identity_sync(page)
        self._save_cookies_sync()

    def _install_hooks_sync(self, page) -> None:
        import time as _t
        started = _t.time()
        result = page.evaluate(INSTALL_HOOKS_JS)
        if result == "already_hooked":
            log.info("[probe] hooks.done result=already_hooked elapsed=%.3fs", _t.time() - started)
            return
        if isinstance(result, str) and result.startswith("hooked:"):
            self._snap_key = result.split(":", 1)[1]
            log.info("[probe] hooks.done result=installed elapsed=%.3fs", _t.time() - started)
            return
        for _ in range(3):
            page.wait_for_timeout(2000)
            result = page.evaluate(INSTALL_HOOKS_JS)
            if result == "already_hooked":
                log.info("[probe] hooks.done result=already_hooked_retry elapsed=%.3fs", _t.time() - started)
                return
            if isinstance(result, str) and result.startswith("hooked:"):
                self._snap_key = result.split(":", 1)[1]
                log.info("[probe] hooks.done result=installed_retry elapsed=%.3fs", _t.time() - started)
                return
        page_url = page.url if page else "(no page)"
        page_title = ""
        try:
            page_title = page.title()
        except Exception:
            pass
        log.error(
            "[probe] hooks.failed elapsed=%.3fs result=%s url=%s title=%r",
            _t.time() - started,
            result,
            page_url,
            page_title,
        )
        raise RuntimeError(f"Hook install failed: {result} (url={page_url}, title={page_title!r})")

    def _click_run_button_sync(self, page) -> bool:
        # Ctrl+Enter is the most reliable trigger on the new AI Studio layout.
        # The visible "Run" button is a ctrl-enter-submit control and clicking it
        # via ElementHandle can be flaky.  Ensure the textarea is focused first so
        # the shortcut reaches the right target.
        try:
            textarea = page.query_selector("textarea")
            if textarea is not None:
                textarea.focus()
            page.keyboard.press("Control+Enter")
            return True
        except Exception:
            pass

        # 2. Fallback: locate the real submit button by its CSS class, avoiding
        #    false matches from category cards that happen to contain "Run".
        try:
            button = page.query_selector("button.ctrl-enter-submits")
            if button is None:
                button = page.locator("button", has_text="Run").filter(
                    has=page.locator("keyboard_return")
                ).first
                button = button.element_handle(timeout=2000)
        except Exception:
            button = None
        if button is None:
            return False
        try:
            button.click()
            return True
        except Exception:
            return False

    def _has_run_button_sync(self, page) -> bool:
        # Idle = no Stop button visible AND submit button present.
        # During generation AI Studio shows a "Stop" button; checking for its
        # absence is more reliable than the submit button's disabled state.
        try:
            if page.query_selector("button:has-text('Stop')") is not None:
                return False
            return page.query_selector("button.ctrl-enter-submits") is not None
        except Exception:
            return False

    def _wait_until_idle_sync(self, page) -> None:
        for _ in range(60):
            if self._has_run_button_sync(page):
                return
            page.wait_for_timeout(1000)
        raise RuntimeError("page never became idle")

    def _read_textarea_value_sync(self, textarea) -> str:
        try:
            return textarea.input_value()
        except Exception:
            return ""

    def _restore_textarea_value_sync(self, textarea, value: str) -> None:
        try:
            current = textarea.input_value()
        except Exception:
            current = None
        if current == value:
            return
        try:
            textarea.fill(value)
        except Exception:
            pass

    def _close_sync(self) -> None:
        if self._ctx is not None:
            try:
                self._ctx.close()
            except Exception:
                pass
        if self._browser is not None and self._cf is None:
            try:
                self._browser.close()
            except Exception:
                pass
        if self._cf is not None:
            try:
                self._cf.__exit__(None, None, None)
            except Exception:
                pass
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
        self._hook_page = None
        self._ctx = None
        self._browser = None
        self._cf = None
        self._playwright = None
        self._snap_key = None
        self._templates.clear()
        self._bootstrap_template = None

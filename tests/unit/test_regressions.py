"""Regression tests for the desktop/login failure modes found in manual QA."""

import asyncio

import pytest
from fastapi import HTTPException

from aistudio_api.api import state
from aistudio_api.api import routes_accounts
from aistudio_api.api import routes_system
from aistudio_api.application import api_service_common
from aistudio_api.application.account_service import AccountService
from aistudio_api.infrastructure.account.login_service import LoginService, LoginSession, LoginStatus
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.gateway.session import BrowserSession


def test_aistudio_url_check_uses_host_not_continue_query():
    assert BrowserSession._is_aistudio_page_url("https://aistudio.google.com/app/prompts/new_chat")
    assert not BrowserSession._is_aistudio_page_url(
        "https://accounts.google.com/v3/signin/identifier?continue=https://aistudio.google.com"
    )


def test_desktop_shutdown_is_local_only_and_invokes_registered_callback():
    from types import SimpleNamespace

    called = []
    runtime = SimpleNamespace(desktop_shutdown=lambda: called.append(True))

    async def scenario():
        response = await routes_system.shutdown_desktop_app(
            SimpleNamespace(client=SimpleNamespace(host="127.0.0.1")),
            runtime,
        )
        assert response["ok"] is True
        await asyncio.sleep(0.25)

    asyncio.run(scenario())
    assert called == [True]

    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            routes_system.shutdown_desktop_app(
                SimpleNamespace(client=SimpleNamespace(host="192.0.2.10")),
                runtime,
            )
        )
    assert caught.value.status_code == 403


def test_desktop_shutdown_rejects_non_desktop_server():
    from types import SimpleNamespace

    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            routes_system.shutdown_desktop_app(
                SimpleNamespace(client=SimpleNamespace(host="::1")),
                SimpleNamespace(desktop_shutdown=None),
            )
        )
    assert caught.value.status_code == 409


def test_cached_profile_reenters_explicit_default_model_after_session_rotation(monkeypatch, tmp_path):
    """回归：ServiceLogin 回到通用首页时仍须显式进入 3.7 Flash 页面。"""
    class Context:
        pages = []

        def __init__(self, page):
            self.pages = [page]

        def add_init_script(self, _script):
            return None

    class Page:
        url = "https://aistudio.google.com/"

        def goto(self, *_args, **_kwargs):
            return None

    page = Page()
    session = BrowserSession.__new__(BrowserSession)
    session._auth_file = str(tmp_path / "auth.json")
    session._profile_dir = str(tmp_path / "profile")
    session._ctx = None
    session._browser = None
    session._cf = None
    session._playwright = None
    session._hook_page = None
    session._context_has_google_session = lambda: True
    session._rotate_google_session_sync = lambda current: setattr(
        current, "url", "https://aistudio.google.com/"
    )
    navigated = []
    session._goto_aistudio_sync = lambda current: navigated.append(current.url)
    session._install_hooks_sync = lambda _page: None

    monkeypatch.setattr(
        "aistudio_api.infrastructure.gateway.session.sync_launch_persistent_context",
        lambda *_args, **_kwargs: Context(page),
    )
    monkeypatch.setattr(
        "aistudio_api.infrastructure.gateway.session.build_browser_context_options",
        lambda: {},
    )
    monkeypatch.setattr(
        "aistudio_api.infrastructure.gateway.session.sync_maximize_page_window",
        lambda _page: None,
    )

    session._ensure_browser_chromium_sync(0.0)

    assert navigated == ["https://aistudio.google.com/"]


def _account_service_with_login(session_status=LoginStatus.PENDING):
    login_service = LoginService()
    login_service._sessions["s1"] = LoginSession(session_id="s1", status=session_status)
    return AccountService(account_store=None, login_service=login_service)


def test_get_login_status_delegates_to_login_service():
    """回归：AccountService 曾把调用写成不存在的 LoginService.get_login_status，
    导致登录状态轮询接口全部 500、前端收不到完成事件。"""
    service = _account_service_with_login()
    session = service.get_login_status("s1")
    assert session is not None and session.session_id == "s1"
    assert service.get_login_status("missing") is None


def test_api_request_is_rejected_while_login_browser_is_active():
    original = state.runtime_state.login_in_progress
    original_ready = state.runtime_state.ready
    original_busy_lock = state.runtime_state.busy_lock
    original_account_service = state.runtime_state.account_service
    state.runtime_state.login_in_progress = True
    state.runtime_state.ready = True
    state.runtime_state.busy_lock = asyncio.Semaphore(1)
    state.runtime_state.account_service = _account_service_with_login(LoginStatus.PENDING)
    try:
        with pytest.raises(HTTPException) as caught:
            api_service_common.require_busy_lock()
        assert caught.value.status_code == 503
        assert caught.value.detail["type"] == "login_in_progress"
    finally:
        state.runtime_state.login_in_progress = original
        state.runtime_state.ready = original_ready
        state.runtime_state.busy_lock = original_busy_lock
        state.runtime_state.account_service = original_account_service


def test_stale_login_flag_self_heals_after_login_ends():
    """登录结束后（前端停止轮询导致标志没人复位）不应继续拦截 API 请求。"""
    original = state.runtime_state.login_in_progress
    original_ready = state.runtime_state.ready
    original_busy_lock = state.runtime_state.busy_lock
    original_account_service = state.runtime_state.account_service
    state.runtime_state.login_in_progress = True
    state.runtime_state.ready = True
    state.runtime_state.busy_lock = asyncio.Semaphore(1)
    state.runtime_state.account_service = _account_service_with_login(LoginStatus.COMPLETED)
    try:
        lock = api_service_common.require_busy_lock()
        assert lock is state.runtime_state.busy_lock
        assert state.runtime_state.login_in_progress is False
    finally:
        state.runtime_state.login_in_progress = original
        state.runtime_state.ready = original_ready
        state.runtime_state.busy_lock = original_busy_lock
        state.runtime_state.account_service = original_account_service


def test_existing_active_account_is_warmed_before_first_api_request():
    """回归：重启后已有 active account 也必须先建立浏览器上下文。"""
    class Account:
        id = "acc_existing"

    class Service:
        def get_active_account(self):
            return Account()

    class Client:
        def __init__(self):
            self.warmups = 0

        async def warmup(self):
            self.warmups += 1

    original_service = state.runtime_state.account_service
    original_client = state.runtime_state.client
    original_rotator = state.runtime_state.rotator
    client = Client()
    state.runtime_state.account_service = Service()
    state.runtime_state.client = client
    state.runtime_state.rotator = None
    try:
        asyncio.run(api_service_common.ensure_active_account(0))
        assert client.warmups == 1
    finally:
        state.runtime_state.account_service = original_service
        state.runtime_state.client = original_client
        state.runtime_state.rotator = original_rotator


def test_login_start_returns_session_and_sets_busy_state():
    class Service:
        async def start_login(self, name):
            return "login_test"

    original = state.runtime_state.login_in_progress
    try:
        result = asyncio.run(routes_accounts.login_start(routes_accounts.LoginStartRequest(), Service()))
        assert result.session_id == "login_test"
        assert state.runtime_state.login_in_progress is True
    finally:
        state.runtime_state.login_in_progress = original


def test_login_start_clears_busy_state_when_start_fails():
    class Service:
        async def start_login(self, name):
            raise RuntimeError("登录服务不可用")

    original = state.runtime_state.login_in_progress
    try:
        with pytest.raises(HTTPException) as caught:
            asyncio.run(routes_accounts.login_start(routes_accounts.LoginStartRequest(), Service()))
        assert caught.value.status_code == 409
        assert state.runtime_state.login_in_progress is False
    finally:
        state.runtime_state.login_in_progress = original


@pytest.mark.parametrize("status", [LoginStatus.COMPLETED, LoginStatus.FAILED])
def test_terminal_login_status_releases_api_busy_state(status):
    service = _account_service_with_login(status)
    original = state.runtime_state.login_in_progress
    state.runtime_state.login_in_progress = True
    try:
        result = asyncio.run(routes_accounts.login_status("s1", service))
        assert result.status == status.value
        assert state.runtime_state.login_in_progress is False
    finally:
        state.runtime_state.login_in_progress = original


def test_capture_request_retries_after_navigation_context_error():
    class Capture:
        def __init__(self):
            self.calls = 0

        async def capture(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("Execution context was destroyed, most likely because of a navigation")
            return "captured"

    class Session:
        def __init__(self):
            self.recovered = 0

        async def ensure_hook_page(self):
            self.recovered += 1

    client = AIStudioClient.__new__(AIStudioClient)
    client._capture_service = Capture()
    client._session = Session()

    result = asyncio.run(client.capture_request("你好"))
    assert result == "captured"
    assert client._capture_service.calls == 2
    assert client._session.recovered == 1


def test_client_close_releases_browser_session_once():
    """桌面应用退出时必须关闭后台浏览器，重复清理也不能二次关闭。"""
    class Session:
        def __init__(self):
            self.closes = 0

        async def close(self):
            self.closes += 1

    session = Session()
    client = AIStudioClient.__new__(AIStudioClient)
    client._session = session

    async def close_twice():
        await client.close()
        await client.close()

    asyncio.run(close_twice())
    assert session.closes == 1
    assert client._session is None


def test_login_service_close_cancels_pending_workers():
    """关闭 App 时仍打开的登录窗口必须进入 worker 的清理分支。"""
    async def scenario():
        service = LoginService()
        cleanup_reached = asyncio.Event()

        async def worker():
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_reached.set()

        task = asyncio.create_task(worker())
        service._tasks["login_pending"] = task
        service._profile_users["login_pending"] = None
        await asyncio.sleep(0)

        await service.close()

        assert cleanup_reached.is_set()
        assert task.cancelled()
        assert service._tasks == {}
        assert service._profile_users == {}

    asyncio.run(scenario())


def test_browser_session_rejects_context_without_google_session():
    class Page:
        url = "https://aistudio.google.com/app/prompts/new_chat"

        def is_closed(self):
            return False

    session = BrowserSession.__new__(BrowserSession)
    session._hook_page = Page()
    session._context_has_google_session = lambda: False
    with pytest.raises(RuntimeError, match="Google 会话已失效"):
        session._verify_authenticated_sync()


def test_logout_active_account_automatically_activates_oldest_remaining():
    from types import SimpleNamespace

    old = SimpleNamespace(id="old", email="old@example.com", name="old", created_at="2026-01-01")
    replacement = SimpleNamespace(id="replacement", email="new@example.com", name="new", created_at="2026-01-02")

    class Session:
        pass

    class Client:
        _session = Session()

    class Service:
        def __init__(self):
            self.activated = []

        def get_active_account(self):
            return old

        async def logout_account(self, account_id, browser_session):
            assert account_id == old.id
            return {"remaining_accounts": 1, "message": "已退出登录"}

        def list_accounts(self):
            return [replacement]

        async def activate_account(self, account_id, browser_session, snapshot_cache, busy_lock):
            self.activated.append(account_id)
            return replacement

    service = Service()
    original_client = state.runtime_state.client
    original_switching = state.runtime_state.account_switching
    original_cache = state.runtime_state.snapshot_cache
    original_lock = state.runtime_state.busy_lock
    state.runtime_state.client = Client()
    state.runtime_state.account_switching = False
    state.runtime_state.snapshot_cache = object()
    state.runtime_state.busy_lock = None
    try:
        result = asyncio.run(routes_accounts.logout_account(old.id, service, state.runtime_state))
        assert result["activated_account_id"] == replacement.id
        assert service.activated == [replacement.id]
        assert state.runtime_state.account_switching is False
    finally:
        state.runtime_state.client = original_client
        state.runtime_state.account_switching = original_switching
        state.runtime_state.snapshot_cache = original_cache
        state.runtime_state.busy_lock = original_lock


def test_logout_active_account_falls_back_when_first_replacement_fails():
    from types import SimpleNamespace

    active = SimpleNamespace(id="active", email="active@example.com", name="active", created_at="2026-01-01")
    first = SimpleNamespace(id="first", email="first@example.com", name="first", created_at="2026-01-02")
    second = SimpleNamespace(id="second", email="second@example.com", name="second", created_at="2026-01-03")

    class Client:
        _session = object()

    class Service:
        def __init__(self):
            self.attempts = []

        def get_active_account(self):
            return active

        async def logout_account(self, account_id, browser_session):
            return {"remaining_accounts": 2, "message": "已退出登录"}

        def list_accounts(self):
            return [second, first]

        async def activate_account(self, account_id, *args):
            self.attempts.append(account_id)
            if account_id == first.id:
                raise RuntimeError("first profile unavailable")
            return second

    service = Service()
    original_client = state.runtime_state.client
    original_switching = state.runtime_state.account_switching
    state.runtime_state.client = Client()
    state.runtime_state.account_switching = False
    try:
        result = asyncio.run(routes_accounts.logout_account(active.id, service, state.runtime_state))
        assert service.attempts == [first.id, second.id]
        assert result["activated_account_id"] == second.id
        assert state.runtime_state.account_switching is False
    finally:
        state.runtime_state.client = original_client
        state.runtime_state.account_switching = original_switching


def test_logout_active_account_reports_when_all_replacements_fail():
    from types import SimpleNamespace

    active = SimpleNamespace(id="active", email="active@example.com", created_at="2026-01-01")
    first = SimpleNamespace(id="first", email="first@example.com", created_at="2026-01-02")
    second = SimpleNamespace(id="second", email="second@example.com", created_at="2026-01-03")

    class Client:
        _session = object()

    class Service:
        def __init__(self):
            self.attempts = []

        def get_active_account(self):
            return active

        async def logout_account(self, account_id, browser_session):
            return {"remaining_accounts": 2, "message": "已退出登录"}

        def list_accounts(self):
            return [second, first]

        async def activate_account(self, account_id, *args):
            self.attempts.append(account_id)
            raise RuntimeError(f"{account_id} unavailable")

    service = Service()
    original_client = state.runtime_state.client
    original_switching = state.runtime_state.account_switching
    state.runtime_state.client = Client()
    state.runtime_state.account_switching = False
    try:
        with pytest.raises(HTTPException) as caught:
            asyncio.run(routes_accounts.logout_account(active.id, service, state.runtime_state))
        assert caught.value.status_code == 409
        assert "账号已退出" in caught.value.detail
        assert service.attempts == [first.id, second.id]
        assert state.runtime_state.account_switching is False
    finally:
        state.runtime_state.client = original_client
        state.runtime_state.account_switching = original_switching


def test_logout_does_not_delete_account_when_request_workers_cannot_close(monkeypatch):
    class Service:
        def __init__(self):
            self.logout_called = False

        def get_active_account(self):
            return None

        async def logout_account(self, account_id, browser_session):
            self.logout_called = True
            raise AssertionError("account must not be deleted while requests are still active")

    async def fail_to_close(runtime, account_id):
        raise RuntimeError("该账号仍有 API 请求正在执行，请稍后再试")

    service = Service()
    monkeypatch.setattr(routes_accounts, "_close_request_workers", fail_to_close)
    original_switching = state.runtime_state.account_switching
    state.runtime_state.account_switching = False
    try:
        with pytest.raises(HTTPException) as caught:
            asyncio.run(routes_accounts.logout_account("busy", service, state.runtime_state))
        assert caught.value.status_code == 409
        assert "API 请求正在执行" in caught.value.detail
        assert service.logout_called is False
        assert state.runtime_state.account_switching is False
    finally:
        state.runtime_state.account_switching = original_switching


def test_logout_inactive_account_does_not_switch_current_account():
    from types import SimpleNamespace

    active = SimpleNamespace(id="active")

    class Client:
        _session = object()

    class Service:
        def get_active_account(self):
            return active

        async def logout_account(self, account_id, browser_session):
            return {"remaining_accounts": 1, "message": "已退出登录"}

        async def activate_account(self, *args):
            raise AssertionError("inactive account logout must not switch accounts")

    original_client = state.runtime_state.client
    original_switching = state.runtime_state.account_switching
    state.runtime_state.client = Client()
    state.runtime_state.account_switching = False
    try:
        result = asyncio.run(routes_accounts.logout_account("inactive", Service(), state.runtime_state))
        assert "activated_account_id" not in result
        assert state.runtime_state.account_switching is False
    finally:
        state.runtime_state.client = original_client
        state.runtime_state.account_switching = original_switching


def test_manual_activation_failure_always_releases_switching_state():
    class Client:
        _session = object()

    class Service:
        async def activate_account(self, *args):
            raise RuntimeError("profile unavailable")

    original_client = state.runtime_state.client
    original_switching = state.runtime_state.account_switching
    state.runtime_state.client = Client()
    state.runtime_state.account_switching = False
    try:
        with pytest.raises(HTTPException) as caught:
            asyncio.run(routes_accounts.activate_account("broken", "manual", Service(), state.runtime_state))
        assert caught.value.status_code == 503
        assert caught.value.detail == "账号暂时无法完成初始化，请稍后重试"
        assert state.runtime_state.account_switching is False
    finally:
        state.runtime_state.client = original_client
        state.runtime_state.account_switching = original_switching


def test_runtime_activation_prepares_request_pool_without_starting_control_browser():
    from types import SimpleNamespace

    account = SimpleNamespace(
        id="selected",
        name="Selected",
        email="selected@example.com",
        created_at="2026-01-01",
        last_used=None,
    )

    class ControlSession:
        def __init__(self):
            self.auth_paths = []

        async def switch_auth(self, auth_path):
            self.auth_paths.append(auth_path)

        async def ensure_context(self):
            raise AssertionError("control browser must remain closed")

    class Pool:
        def __init__(self):
            self.prepared = []

        async def prepare_account(self, account_id):
            self.prepared.append(account_id)

    class Service:
        async def activate_account(self, account_id, session, snapshot_cache, busy_lock):
            assert account_id == account.id
            await session.switch_auth("selected/auth.json")
            await session.ensure_context()
            return account

    control = ControlSession()
    pool = Pool()
    runtime = SimpleNamespace(
        client=SimpleNamespace(_session=control),
        request_pool=pool,
        snapshot_cache=object(),
        busy_lock=None,
        account_switching=False,
    )
    result = asyncio.run(routes_accounts.activate_account(account.id, "manual", Service(), runtime))
    assert result.id == account.id
    assert control.auth_paths == ["selected/auth.json"]
    assert pool.prepared == [account.id]
    assert runtime.account_switching is False


def test_default_model_listing_does_not_start_browser_discovery():
    from aistudio_api.api.routes_openai import _available_model_ids

    class Client:
        async def discover_models(self, force=False):
            raise AssertionError("default model listing must stay browser-free")

    models = asyncio.run(_available_model_ids(Client(), force=False))
    assert models[0] == "gemini-3.7-flash"


def test_logout_reports_failed_account_deletion():
    from aistudio_api.infrastructure.account.account_store import AccountMeta

    account = AccountMeta(
        id="acc_locked",
        name="locked",
        email="locked@example.com",
        created_at="2026-01-01T00:00:00Z",
        last_used=None,
    )

    class Store:
        def get_account(self, account_id):
            return account if account_id == account.id else None

        def get_active_account(self):
            return None

        def delete_account(self, account_id):
            return False

        def list_accounts(self):
            return [account]

    class Login:
        async def clear_login_profile(self):
            return None

    service = AccountService(Store(), Login())
    with pytest.raises(RuntimeError, match="账号目录仍被占用"):
        asyncio.run(service.logout_account(account.id))


def test_completed_relogin_invalidates_cached_request_workers():
    session = LoginSession(
        session_id="relogin",
        status=LoginStatus.COMPLETED,
        account_id="acc_existing",
    )

    class Service:
        def get_login_status(self, session_id):
            return session if session_id == session.session_id else None

    class Pool:
        def __init__(self):
            self.closed_accounts = []

        async def close_account(self, account_id):
            self.closed_accounts.append(account_id)

    pool = Pool()
    original_pool = state.runtime_state.request_pool
    original_login = state.runtime_state.login_in_progress
    original_switching = state.runtime_state.account_switching
    state.runtime_state.request_pool = pool
    state.runtime_state.login_in_progress = True
    state.runtime_state.account_switching = False
    try:
        result = asyncio.run(routes_accounts.login_status(session.session_id, Service()))
        assert result.status == "completed"
        assert pool.closed_accounts == [session.account_id]
        assert state.runtime_state.login_in_progress is False
        assert state.runtime_state.account_switching is False
    finally:
        state.runtime_state.request_pool = original_pool
        state.runtime_state.login_in_progress = original_login
        state.runtime_state.account_switching = original_switching


def test_logout_closes_request_workers_before_deleting_account():
    events = []

    class Client:
        _session = object()

    class Pool:
        async def close_account(self, account_id):
            events.append(("pool_closed", account_id))

    class Service:
        def get_active_account(self):
            return None

        async def logout_account(self, account_id, browser_session):
            events.append(("account_deleted", account_id))
            return {"remaining_accounts": 0, "message": "已退出登录"}

    original_client = state.runtime_state.client
    original_pool = state.runtime_state.request_pool
    original_switching = state.runtime_state.account_switching
    state.runtime_state.client = Client()
    state.runtime_state.request_pool = Pool()
    state.runtime_state.account_switching = False
    try:
        result = asyncio.run(routes_accounts.logout_account("acc_logout", Service(), state.runtime_state))
        assert result["remaining_accounts"] == 0
        assert events == [
            ("pool_closed", "acc_logout"),
            ("account_deleted", "acc_logout"),
        ]
        assert state.runtime_state.account_switching is False
    finally:
        state.runtime_state.client = original_client
        state.runtime_state.request_pool = original_pool
        state.runtime_state.account_switching = original_switching


def test_botguard_template_capture_blocks_synthetic_request(monkeypatch):
    class Textarea:
        def fill(self, _value):
            return None

    class Request:
        url = "https://example.test/GenerateContent"
        post_data = "[\"models/gemini-3.7-flash\"]"
        headers = {"content-type": "application/json"}

    class Route:
        def __init__(self):
            self.aborted = 0
            self.continued = 0

        def abort(self):
            self.aborted += 1

        def continue_(self):
            self.continued += 1

    class Page:
        url = "https://aistudio.google.com/app/prompts/new_chat"

        def __init__(self):
            self.route_handler = None
            self.botguard_checks = 0
            self.loop_ticks = 0
            self.clicked = False
            self.request_sent = False
            self.route_state = Route()

        def evaluate(self, expression):
            if expression == "mw:!!window.__bg_service":
                self.botguard_checks += 1
                return self.botguard_checks > 1
            return None

        def query_selector(self, selector):
            return Textarea() if selector == "textarea" else None

        def route(self, _pattern, handler):
            self.route_handler = handler

        def unroute(self, _pattern, handler):
            if self.route_handler is handler:
                self.route_handler = None

        def wait_for_timeout(self, milliseconds):
            if not self.clicked or milliseconds != 1000:
                return
            self.loop_ticks += 1
            if not self.request_sent:
                self.request_sent = True
                self.route_handler(self.route_state, Request())

    def build():
        session = BrowserSession(port=9222)
        page = Page()
        monkeypatch.setattr(session, "_ensure_hook_page_sync", lambda: page)
        monkeypatch.setattr(session, "_read_textarea_value_sync", lambda _textarea: "")
        monkeypatch.setattr(session, "_restore_textarea_value_sync", lambda *_args: None)
        monkeypatch.setattr(session, "_wait_until_idle_sync", lambda _page: None)

        def click(_page):
            page.clicked = True
            return True

        monkeypatch.setattr(session, "_click_run_button_sync", click)
        return session, page

    session, page = build()
    assert session._ensure_botguard_service_sync() is page
    assert page.loop_ticks == 1
    assert page.route_state.aborted == 1
    assert page.route_state.continued == 0
    assert page.route_handler is None
    assert session._bootstrap_template is not None


def test_prepare_account_warms_pool_without_changing_preferred_account():
    from types import SimpleNamespace

    account = SimpleNamespace(id="new-account")

    class Service:
        def list_accounts(self):
            return [account]

    class Pool:
        def __init__(self):
            self.prepared = []

        async def prepare_account(self, account_id):
            self.prepared.append(account_id)

    pool = Pool()
    runtime = SimpleNamespace(request_pool=pool)

    result = asyncio.run(routes_accounts.prepare_account(account.id, Service(), runtime))

    assert result == {"ok": True, "account_id": account.id}
    assert pool.prepared == [account.id]

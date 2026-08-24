import asyncio
import sys
import types

import pytest

sys.modules.setdefault("dotenv", types.SimpleNamespace(load_dotenv=lambda *args, **kwargs: None))
playwright_module = sys.modules.setdefault("playwright", types.ModuleType("playwright"))
async_api_module = sys.modules.setdefault("playwright.async_api", types.ModuleType("playwright.async_api"))
setattr(playwright_module, "async_api", async_api_module)
setattr(async_api_module, "async_playwright", lambda: None)

from aistudio_api.infrastructure.account import login_service as login_module
from aistudio_api.infrastructure.account.login_service import (
    LoginService,
    LoginSession,
    LoginStatus,
    _extract_email_from_storage_state,
    _normalize_email,
)


def test_api_key_store_generates_and_supports_rotation(tmp_path, monkeypatch):
    from aistudio_api.infrastructure.auth.api_key_store import ApiKeyStore

    monkeypatch.setattr(login_module.settings, "api_keys", frozenset())
    store = ApiKeyStore(tmp_path / "api_keys.json")
    created, secret = store.create("test key")
    assert secret.startswith("sk-aistudio-")
    assert store.verify(secret)
    rotated, rotated_secret = store.rotate("rotated key")
    assert store.verify(rotated_secret)
    store.revoke(created["id"])
    assert not store.verify(secret)
    store.revoke(rotated["id"])
    assert not store.verify(rotated_secret)


def test_deleted_environment_key_is_not_reimported(tmp_path, monkeypatch):
    from aistudio_api.infrastructure.auth.api_key_store import ApiKeyStore

    legacy_key = "sk-legacy-test-key"
    monkeypatch.setattr(login_module.settings, "api_keys", frozenset({legacy_key}))
    store = ApiKeyStore(tmp_path / "api_keys.json")

    first = store.list_public()
    assert len(first) == 1
    assert first[0]["name"] == "环境变量 API Key"

    store.revoke(first[0]["id"])
    assert store.list_public() == []


def test_normalize_email_extracts_address_from_google_page_text():
    assert _normalize_email("Signed in as Jiayex2277@gmail.com") == "jiayex2277@gmail.com"
    assert _normalize_email("no email here") is None


def test_extract_email_from_storage_state_checks_metadata_without_requiring_email_key():
    storage_state = {
        "origins": [
            {
                "origin": "https://aistudio.google.com",
                "localStorage": [{"name": "user", "value": "jiayex2277@gmail.com"}],
            }
        ],
        "cookies": [],
    }
    assert _extract_email_from_storage_state(storage_state) == "jiayex2277@gmail.com"


class FakePage:
    def __init__(self, *, close_on_goto: bool = False) -> None:
        self.url = ""
        self.main_frame = types.SimpleNamespace(url="")
        self._handlers: dict[str, list] = {}
        self._close_on_goto = close_on_goto

    def on(self, event: str, callback) -> None:
        self._handlers.setdefault(event, []).append(callback)

    async def goto(self, url: str, wait_until: str | None = None, timeout: int | None = None) -> None:
        self.url = url
        if self._close_on_goto:
            await self.emit("close")

    async def evaluate(self, script: str, *args):
        return None

    async def wait_for_timeout(self, milliseconds: float) -> None:
        return None

    async def emit(self, event: str, *args) -> None:
        for callback in self._handlers.get(event, []):
            result = callback(*args)
            if asyncio.iscoroutine(result):
                await result


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self._page = page
        self.pages: list[FakePage] = []
        self._handlers: dict[str, list] = {}

    def on(self, event: str, callback) -> None:
        self._handlers.setdefault(event, []).append(callback)

    async def new_page(self) -> FakePage:
        return self._page

    async def storage_state(self) -> dict:
        return {"cookies": [], "origins": []}

    async def close(self) -> None:
        return None


class FakeBrowser:
    def __init__(self, context: FakeContext) -> None:
        self._context = context
        self._handlers: dict[str, list] = {}

    def on(self, event: str, callback) -> None:
        self._handlers.setdefault(event, []).append(callback)

    async def new_context(self, **kwargs) -> FakeContext:
        return self._context

    async def close(self) -> None:
        await self.emit("disconnected")

    async def emit(self, event: str, *args) -> None:
        for callback in self._handlers.get(event, []):
            result = callback(*args)
            if asyncio.iscoroutine(result):
                await result


class FakeManager:
    def __init__(self, browser: FakeBrowser) -> None:
        self._browser = browser

    async def start(self) -> None:
        return None

    async def launch_browser(self, playwright) -> FakeBrowser:
        return self._browser

    async def stop(self) -> None:
        return None


class FakePlaywright:
    async def stop(self) -> None:
        return None


class FakeAsyncPlaywrightStarter:
    async def start(self) -> FakePlaywright:
        return FakePlaywright()


class FakeAccountStore:
    def __init__(self) -> None:
        self.saved = False

    def save_account(self, **kwargs):
        self.saved = True
        raise AssertionError("closed login flow should not save account")


def test_login_session_fails_immediately_when_browser_window_is_closed(monkeypatch):
    page = FakePage(close_on_goto=True)
    context = FakeContext(page)
    store = FakeAccountStore()

    async def fake_launch_login_context(*, headless, profile_dir):
        return types.SimpleNamespace(
            context=context, playwright=FakePlaywright(), backend="system:chrome"
        )

    monkeypatch.setattr(login_module, "async_launch_login_context", fake_launch_login_context)
    monkeypatch.setattr(login_module, "is_camoufox_engine", lambda: False)
    monkeypatch.setattr(login_module, "describe_browser_backend", lambda: "chromium")
    monkeypatch.setattr(login_module, "build_browser_context_options", lambda headless=None: {})

    async def fake_maximize_page_window(page, *, headless):
        return None

    async def fake_terminal_login_loop(self, session_id, page, login_done, *, headless):
        await asyncio.sleep(3600)

    monkeypatch.setattr(login_module, "async_maximize_page_window", fake_maximize_page_window)
    monkeypatch.setattr(LoginService, "_terminal_login_loop", fake_terminal_login_loop)
    monkeypatch.setattr("playwright.async_api.async_playwright", lambda: FakeAsyncPlaywrightStarter())

    service = LoginService()
    session = LoginSession(session_id="login_test")
    service._sessions[session.session_id] = session

    asyncio.run(
        asyncio.wait_for(
            service._login_worker(
                session.session_id,
                store,
                None,
                headless=False,
                ui_locale=None,
            ),
            timeout=1,
        )
    )

    assert session.status == LoginStatus.FAILED
    assert session.error == "登录窗口已关闭"
    assert store.saved is False


def test_build_login_url_opens_google_account_chooser_directly():
    service = LoginService()
    url = service._build_login_url()
    assert url.startswith("https://accounts.google.com/v3/signin/accountchooser?")
    assert "continue=https%3A%2F%2Faistudio.google.com%2Fapp%2Fprompts%2Fnew_chat" in url
    assert "followup=https%3A%2F%2Faistudio.google.com%2Fapp%2Fprompts%2Fnew_chat" in url
    assert "flowName=GlifWebSignIn" in url
    assert "flowEntry=ServiceLogin" in url

    localized = service._build_login_url(ui_locale="zh-CN")
    assert "hl=zh-CN" in localized


def test_login_entry_keeps_loaded_google_form_when_original_navigation_times_out():
    class RedirectedGooglePage:
        url = ""

        async def goto(self, url, **_kwargs):
            self.url = "https://accounts.google.com/v3/signin/identifier?continue=test"
            raise RuntimeError("Timeout 30000ms exceeded")

        async def evaluate(self, _script):
            return "interactive"

    async def scenario():
        recovered = await LoginService._goto_login_entry(
            RedirectedGooglePage(),
            "https://accounts.google.com/v3/signin/accountchooser?continue=test",
        )
        assert recovered is True

    asyncio.run(scenario())


def test_login_entry_does_not_hide_timeout_before_google_form_is_usable():
    class BrokenPage:
        url = "https://accounts.google.com/v3/signin/identifier?continue=test"

        async def goto(self, url, **_kwargs):
            raise RuntimeError("Timeout 30000ms exceeded")

        async def evaluate(self, _script):
            return "loading"

    async def scenario():
        with pytest.raises(RuntimeError, match="Timeout 30000ms"):
            await LoginService._goto_login_entry(
                BrokenPage(),
                "https://accounts.google.com/v3/signin/accountchooser?continue=test",
            )

    asyncio.run(scenario())


def test_start_login_allocates_isolated_profile_for_concurrent_login(tmp_path, monkeypatch):
    monkeypatch.setattr(login_module.settings, "login_profile_dir", str(tmp_path / "login-profile"))
    service = LoginService()

    async def scenario():
        store = FakeAccountStore()
        first = await service.start_login(store)
        second = await service.start_login(store)
        # 每次登录都用独立的全新临时 profile：复用带会话的持久 profile
        # 会被 Google 的自动续登（pli=1）打断用户输入。
        assert service._profile_users[first] == tmp_path / "login-profile" / "parallel" / first
        assert service._profile_users[second] == tmp_path / "login-profile" / "parallel" / second
        for task in service._tasks.values():
            task.cancel()
        await asyncio.gather(*service._tasks.values(), return_exceptions=True)

    asyncio.run(scenario())


def test_clear_login_profile_removes_dir_and_respects_active_session(tmp_path, monkeypatch):
    profile = tmp_path / "login-profile"
    profile.mkdir()
    (profile / "Default").mkdir()
    monkeypatch.setattr(login_module.settings, "login_profile_dir", str(profile))
    service = LoginService()

    asyncio.run(service.clear_login_profile())
    assert not profile.exists()

    # 登录窗口打开中时拒绝清除
    profile.mkdir()

    async def locked_scenario():
        service._profile_users["login_test"] = profile
        try:
            await service.clear_login_profile()
            raise AssertionError("expected RuntimeError")
        except RuntimeError as exc:
            assert "登录窗口打开中" in str(exc)
        finally:
            service._profile_users.clear()

    asyncio.run(locked_scenario())
    assert profile.exists()


def test_logout_account_clears_profile_only_for_last_account(monkeypatch, tmp_path):
    from aistudio_api.application.account_service import AccountService
    from aistudio_api.infrastructure.account.account_store import AccountMeta

    cleared: list[str] = []

    class FakeStore:
        def __init__(self, accounts):
            self._accounts = {a.id: a for a in accounts}
            self.active_id = accounts[0].id if accounts else None

        def get_account(self, account_id):
            return self._accounts.get(account_id)

        def get_active_account(self):
            return self._accounts.get(self.active_id)

        def delete_account(self, account_id):
            return self._accounts.pop(account_id, None) is not None

        def list_accounts(self):
            return list(self._accounts.values())

    class FakeLogin:
        async def clear_login_profile(self):
            cleared.append("cleared")

    def meta(account_id):
        return AccountMeta(
            id=account_id,
            name=account_id,
            email=f"{account_id}@example.com",
            created_at="2026-01-01T00:00:00",
            last_used=None,
        )

    service = AccountService(FakeStore([meta("a1"), meta("a2")]), FakeLogin())
    result = asyncio.run(service.logout_account("a1"))
    assert result["remaining_accounts"] == 1
    assert result["profile_cleared"] is False
    assert cleared == []

    result = asyncio.run(service.logout_account("a2"))
    assert result["remaining_accounts"] == 0
    assert result["profile_cleared"] is True
    assert cleared == ["cleared"]

    assert asyncio.run(service.logout_account("missing")) is None


def test_delete_account_survives_locked_profile_dir(tmp_path, monkeypatch):
    from aistudio_api.infrastructure.account.account_store import AccountStore

    store = AccountStore(tmp_path)
    meta = store.save_account(
        name="locked", email="locked@example.com",
        storage_state={"cookies": [], "origins": []},
    )
    assert store.get_account(meta.id) is not None

    def locked_rmtree(path, *args, **kwargs):
        raise PermissionError("file in use")

    monkeypatch.setattr(
        "aistudio_api.infrastructure.account.account_store.shutil.rmtree", locked_rmtree
    )
    # Windows 上 profile 被浏览器占用时：注册表仍要清理，目录改名延后删除
    assert store.delete_account(meta.id) is True
    assert store.get_account(meta.id) is None
    assert list(tmp_path.glob(".deleted-*")) != []


def test_saving_new_account_preserves_existing_active_account(tmp_path):
    from aistudio_api.infrastructure.account.account_store import AccountStore

    store = AccountStore(tmp_path)
    first = store.save_account(
        name="first", email="first@example.com",
        storage_state={"cookies": [], "origins": []},
    )
    second = store.save_account(
        name="second", email="second@example.com",
        storage_state={"cookies": [], "origins": []},
    )

    assert store.get_active_account().id == first.id
    assert second.id != first.id

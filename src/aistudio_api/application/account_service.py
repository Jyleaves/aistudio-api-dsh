"""账号管理应用服务，协调 account_store 和 login_service。"""

from __future__ import annotations

import logging
from typing import Any

from aistudio_api.infrastructure.account.account_store import AccountStore, AccountMeta
from aistudio_api.infrastructure.account.login_service import LoginService, LoginSession

logger = logging.getLogger("aistudio.account")


class AccountService:
    """账号管理服务。"""

    def __init__(
        self,
        account_store: AccountStore,
        login_service: LoginService,
    ) -> None:
        self._store = account_store
        self._login = login_service

    def list_accounts(self) -> list[AccountMeta]:
        """列出所有账号。"""
        return self._store.list_accounts()

    def get_account(self, account_id: str) -> AccountMeta | None:
        """获取单个账号。"""
        return self._store.get_account(account_id)

    def get_active_account(self) -> AccountMeta | None:
        """获取当前活跃账号。"""
        return self._store.get_active_account()

    async def start_login(
        self,
        name: str | None = None,
        *,
        headless: bool = False,
        ui_locale: str | None = None,
    ) -> str:
        """启动登录流程，返回 session_id。"""
        return await self._login.start_login(
            self._store,
            name,
            headless=headless,
            ui_locale=ui_locale,
        )

    def get_login_status(self, session_id: str) -> LoginSession | None:
        """获取登录状态。"""
        return self._login.get_status(session_id)

    async def clear_login_profile(self) -> None:
        """清除持久化登录档案（Google 账号选择器列表）。"""
        await self._login.clear_login_profile()

    async def activate_account(
        self,
        account_id: str,
        browser_session: Any,
        snapshot_cache: Any,
        busy_lock: Any = None,  # None = skip lock (caller already holds it)
        keep_snapshot_cache: bool = False,
    ) -> AccountMeta | None:
        """切换到指定账号。

        Args:
            account_id: 目标账号 ID
            browser_session: BrowserSession 实例
            snapshot_cache: SnapshotCache 实例
            busy_lock: asyncio.Lock，确保切换时无请求在飞行中。None 则跳过锁
            keep_snapshot_cache: 是否保留 snapshot 缓存（默认 False，避免切号后复用旧 snapshot）

        Returns:
            切换后的账号元数据，或 None（如果账号不存在）
        """
        # 验证账号存在
        account = self._store.get_account(account_id)
        if account is None:
            return None

        async def _do_switch():
            # 获取 auth 路径
            auth_path = self._store.get_auth_path_optional(account_id, require_exists=False)
            if auth_path is None:
                logger.error("账号 %s 的账号目录不存在", account_id)
                return None

            # 切换 BrowserSession 的 auth
            await browser_session.switch_auth(str(auth_path))
            await browser_session.ensure_context()

            # snapshot 按账号命名空间隔离。切号时不再清空其他账号的缓存，
            # 这样大 PDF 在账号轮换后可以复用该账号自己的已捕获请求。
            if snapshot_cache is not None and hasattr(snapshot_cache, "set_namespace"):
                snapshot_cache.set_namespace(account_id)
            elif not keep_snapshot_cache and snapshot_cache is not None:
                snapshot_cache.clear()
                logger.info("已清除 snapshot 缓存")

            # 更新注册表
            self._store.set_active_account(account_id)

            logger.info("已切换到账号: %s (%s)", account_id, account.name)
            return account

        # 获取 busy_lock 确保无请求在飞行中
        if busy_lock is not None:
            async with busy_lock:
                return await _do_switch()
        else:
            return await _do_switch()

    def delete_account(self, account_id: str) -> bool:
        """删除账号。"""
        return self._store.delete_account(account_id)

    async def logout_account(
        self,
        account_id: str,
        browser_session: Any = None,
    ) -> dict | None:
        """退出登录：删除本地账号记录，并按需清除本机 Google 登录档案。

        最后一个账号被移除时顺带清空登录档案（login-profile），这样下次
        "添加账号"不会再列出已退出的账号；还有其他账号时保留档案，避免
        影响它们的一键授权。活跃账号的 profile 正被后台浏览器占用时，
        先关闭后台浏览器释放文件锁再删除。
        """
        account = self._store.get_account(account_id)
        if account is None:
            return None
        active = self._store.get_active_account()
        if browser_session is not None and active is not None and active.id == account_id:
            try:
                await browser_session.release_context()
                logger.info("已释放后台浏览器以删除账号 %s", account_id)
            except Exception as exc:
                logger.warning("释放后台浏览器失败，将尝试延后清理账号目录: %s", exc)
        self._store.delete_account(account_id)
        remaining = len(self._store.list_accounts())
        profile_cleared = False
        message = "已退出登录并删除账号记录"
        if remaining == 0:
            try:
                await self._login.clear_login_profile()
                profile_cleared = True
                message = "已退出登录并删除账号记录，同时清除本机 Google 登录档案"
            except RuntimeError as exc:
                message = f"已退出登录并删除账号记录；{exc}"
        return {
            "account_id": account_id,
            "remaining_accounts": remaining,
            "profile_cleared": profile_cleared,
            "message": message,
        }

    def update_account(
        self,
        account_id: str,
        name: str | None = None,
        email: str | None = None,
    ) -> AccountMeta | None:
        """更新账号显示名称和邮箱。"""
        return self._store.update_account(account_id, name=name, email=email)

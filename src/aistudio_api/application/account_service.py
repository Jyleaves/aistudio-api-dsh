"""账号管理应用服务，协调 account_store 和 login_service。"""

from __future__ import annotations

import asyncio
import logging
import time
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

    def is_login_active(self) -> bool:
        """是否仍有进行中（pending）的登录会话。"""
        return self._login.has_pending_session()

    async def close(self) -> None:
        """停止尚未完成的登录流程并关闭对应浏览器。"""
        await self._login.close()

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

        started = time.perf_counter()
        logger.info("[probe] account.activate start account_id=%s", account_id)

        async def _do_switch():
            # 获取 auth 路径
            auth_path = self._store.get_auth_path_optional(account_id, require_exists=False)
            if auth_path is None:
                logger.error("账号 %s 的账号目录不存在", account_id)
                return None

            # 切换 BrowserSession 的 auth
            await browser_session.switch_auth(str(auth_path))
            logger.info("[probe] account.activate switch_auth elapsed=%.3fs", time.perf_counter() - started)

            # 登录流程刚复制的 profile 带 profile.fresh 标记，必须保留——
            # 里面是刚验证过的 Google 会话；误删它会让后台浏览器以未登录
            # 状态启动。旧 profile（无标记）也不再强制删除：会话是否有效
            # 由 BrowserSession 启动时的会话 cookie 校验判断，过期会自动
            # 回退到 auth.json 重建，无需在此销毁整个 profile。
            profile_path = self._store.get_profile_path(account_id)
            if profile_path is not None:
                fresh_marker = profile_path.parent / "profile.fresh"
                if fresh_marker.exists():
                    try:
                        fresh_marker.unlink()
                    except OSError:
                        pass
                    logger.info("[probe] account.activate fresh_profile_kept")
            await browser_session.ensure_context()
            logger.info("[probe] account.activate ensure_context elapsed=%.3fs", time.perf_counter() - started)

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
            logger.info("[probe] account.activate total=%.3fs result=ready", time.perf_counter() - started)
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
        # Removing a Chromium profile can involve thousands of small files.
        # Keep that filesystem work off the API event loop so the desktop UI
        # and unrelated requests remain responsive during logout.
        deleted = await asyncio.to_thread(self._store.delete_account, account_id)
        if not deleted:
            logger.error("退出登录时删除账号失败: %s", account_id)
            raise RuntimeError("账号目录仍被占用，退出登录失败，请关闭相关浏览器窗口后重试")
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

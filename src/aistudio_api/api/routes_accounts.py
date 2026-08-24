"""账号管理路由。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from aistudio_api.api.dependencies import get_account_service, get_runtime_state
from aistudio_api.api.state import runtime_state
from aistudio_api.domain.errors import AuthError
import logging

log = logging.getLogger("aistudio.routes_accounts")

router = APIRouter(prefix="/accounts")


class _PooledActivationSession:
    """Make account activation prepare the same browser used by API traffic."""

    def __init__(self, control_session, request_pool, account_id: str) -> None:
        self._control_session = control_session
        self._request_pool = request_pool
        self._account_id = account_id

    async def switch_auth(self, auth_path: str | None) -> None:
        # Keep the legacy control client pointed at the selected account, but
        # leave it closed. Running it beside a request worker gives one Google
        # account two competing desktop sessions and can make valid requests
        # fail with "The caller does not have permission".
        if self._control_session is not None:
            await self._control_session.switch_auth(auth_path)

    async def ensure_context(self) -> None:
        await self._request_pool.prepare_account(self._account_id)


async def activate_runtime_account(
    account_service,
    runtime,
    account_id: str,
    *,
    busy_lock=None,
):
    """Activate and verify an account through the real request execution path."""
    control_session = runtime.client._session if runtime.client else None
    request_pool = getattr(runtime, "request_pool", None)
    if control_session is None and request_pool is None:
        raise RuntimeError("服务未就绪")
    activation_session = (
        _PooledActivationSession(control_session, request_pool, account_id)
        if request_pool is not None
        else control_session
    )
    return await account_service.activate_account(
        account_id,
        activation_session,
        runtime.snapshot_cache,
        busy_lock,
    )


class LoginStartRequest(BaseModel):
    name: str | None = None


class LoginStartResponse(BaseModel):
    session_id: str


class AccountResponse(BaseModel):
    id: str
    name: str
    email: str | None
    created_at: str
    last_used: str | None


class LoginStatusResponse(BaseModel):
    session_id: str
    status: str
    account_id: str | None = None
    email: str | None = None
    error: str | None = None


class UpdateAccountRequest(BaseModel):
    name: str | None = None
    email: str | None = None


async def _close_request_workers(runtime, account_id: str) -> None:
    pool = getattr(runtime, "request_pool", None)
    if pool is None:
        return
    try:
        await asyncio.wait_for(pool.close_account(account_id), timeout=30)
    except TimeoutError as exc:
        raise RuntimeError("该账号仍有 API 请求正在执行，请稍后再试") from exc


@router.post("/login/start", response_model=LoginStartResponse)
async def login_start(
    req: LoginStartRequest,
    account_service=Depends(get_account_service),
):
    """启动 Google 登录流程。"""
    runtime_state.login_in_progress = True
    try:
        session_id = await account_service.start_login(req.name)
    except RuntimeError as exc:
        runtime_state.login_in_progress = False
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception:
        runtime_state.login_in_progress = False
        raise
    return LoginStartResponse(session_id=session_id)


@router.get("/login/status/{session_id}", response_model=LoginStatusResponse)
async def login_status(
    session_id: str,
    account_service=Depends(get_account_service),
):
    """查询登录状态。"""
    session = account_service.get_login_status(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="登录会话不存在")
    if session.status.value != "pending":
        runtime_state.login_in_progress = False
        if session.status.value == "completed" and session.account_id:
            runtime_state.account_switching = True
            try:
                await _close_request_workers(runtime_state, session.account_id)
            finally:
                runtime_state.account_switching = False
    return LoginStatusResponse(
        session_id=session.session_id,
        status=session.status.value,
        account_id=session.account_id,
        email=session.email,
        error=session.error,
    )


@router.get("", response_model=list[AccountResponse])
async def list_accounts(
    account_service=Depends(get_account_service),
):
    """列出所有账号。"""
    accounts = account_service.list_accounts()
    return [
        AccountResponse(
            id=a.id,
            name=a.name,
            email=a.email,
            created_at=a.created_at,
            last_used=a.last_used,
        )
        for a in accounts
    ]


@router.get("/active", response_model=AccountResponse)
async def get_active_account(
    account_service=Depends(get_account_service),
):
    """获取当前活跃账号。"""
    account = account_service.get_active_account()
    if account is None:
        raise HTTPException(status_code=404, detail="没有活跃账号")
    return AccountResponse(
        id=account.id,
        name=account.name,
        email=account.email,
        created_at=account.created_at,
        last_used=account.last_used,
    )


@router.post("/{account_id}/activate", response_model=AccountResponse)
async def activate_account(
    account_id: str,
    source: str = "manual",
    account_service=Depends(get_account_service),
    runtime_state=Depends(get_runtime_state),
):
    """切换到指定账号。"""
    import time

    started = time.perf_counter()
    log.info("[probe] activate.request source=%s account_id=%s", source, account_id)
    busy_lock = runtime_state.busy_lock
    if runtime_state.client is None and getattr(runtime_state, "request_pool", None) is None:
        raise HTTPException(status_code=503, detail="服务未就绪")

    runtime_state.account_switching = True
    try:
        try:
            account = await activate_runtime_account(
                account_service,
                runtime_state,
                account_id,
                busy_lock=busy_lock,
            )
        except (AuthError, RuntimeError) as exc:
            log.warning(
                "账号初始化未完成 source=%s account_id=%s: %s",
                source,
                account_id,
                exc,
            )
            raise HTTPException(
                status_code=503,
                detail="账号暂时无法完成初始化，请稍后重试",
            ) from exc
    finally:
        runtime_state.account_switching = False
        log.info(
            "[probe] activate.request_done source=%s account_id=%s elapsed=%.3fs",
            source,
            account_id,
            time.perf_counter() - started,
        )
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在或切换失败")
    return AccountResponse(
        id=account.id,
        name=account.name,
        email=account.email,
        created_at=account.created_at,
        last_used=account.last_used,
    )


@router.post("/{account_id}/prepare")
async def prepare_account(
    account_id: str,
    account_service=Depends(get_account_service),
    runtime_state=Depends(get_runtime_state),
):
    """Warm one logged-in account without changing the preferred account."""
    if not any(account.id == account_id for account in account_service.list_accounts()):
        raise HTTPException(status_code=404, detail="账号不存在")
    pool = getattr(runtime_state, "request_pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="服务未就绪")
    try:
        await pool.prepare_account(account_id)
    except (AuthError, RuntimeError) as exc:
        log.warning("账号后台初始化失败 account_id=%s: %s", account_id, exc)
        raise HTTPException(status_code=503, detail="账号暂时无法完成初始化，请稍后重试") from exc
    return {"ok": True, "account_id": account_id}


@router.delete("/{account_id}")
async def delete_account(
    account_id: str,
    account_service=Depends(get_account_service),
    runtime_state=Depends(get_runtime_state),
):
    """删除账号。"""
    runtime_state.account_switching = True
    try:
        await _close_request_workers(runtime_state, account_id)
        active = account_service.get_active_account()
        if active is not None and active.id == account_id:
            browser_session = runtime_state.client._session if runtime_state.client else None
            if browser_session is not None:
                try:
                    await browser_session.release_context()
                except Exception as e:
                    log.warning("删除账号前释放后台浏览器失败: %s", e)
        success = account_service.delete_account(account_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        runtime_state.account_switching = False
    if not success:
        raise HTTPException(status_code=404, detail="账号不存在")
    return {"ok": True}


@router.post("/{account_id}/logout")
async def logout_account(
    account_id: str,
    account_service=Depends(get_account_service),
    runtime_state=Depends(get_runtime_state),
):
    """退出登录：删除账号记录；最后一个账号同时清除本机 Google 登录档案。"""
    browser_session = runtime_state.client._session if runtime_state.client else None
    active_before = account_service.get_active_account()
    should_activate_replacement = active_before is not None and active_before.id == account_id
    runtime_state.account_switching = True
    try:
        await _close_request_workers(runtime_state, account_id)
        result = await account_service.logout_account(account_id, browser_session)
        if result is not None and should_activate_replacement and result["remaining_accounts"]:
            # AccountStore chooses a replacement in the registry when the
            # active account is removed, but that alone does not initialize
            # BrowserSession. Prepare the oldest usable replacement so traffic
            # can continue without any user action.
            remaining = sorted(
                account_service.list_accounts(),
                key=lambda item: str(getattr(item, "created_at", "")),
            )
            activation_error = None
            for candidate in remaining:
                try:
                    activated = await activate_runtime_account(
                        account_service,
                        runtime_state,
                        candidate.id,
                        busy_lock=runtime_state.busy_lock,
                    )
                    if activated is not None:
                        result["activated_account_id"] = activated.id
                        result["message"] += f"；账号 {activated.email or activated.name} 已可用"
                        break
                except Exception as exc:
                    activation_error = exc
                    log.warning("退出后自动激活账号失败 account_id=%s: %s", candidate.id, exc)
            else:
                raise HTTPException(
                    status_code=409,
                    detail=f"账号已退出，但剩余账号自动激活失败：{activation_error or '没有可用账号'}",
                )
    except RuntimeError as exc:
        log.error("退出账号失败 account_id=%s: %s", account_id, exc)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        runtime_state.account_switching = False
    if result is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    return result


@router.put("/{account_id}", response_model=AccountResponse)
async def update_account(
    account_id: str,
    req: UpdateAccountRequest,
    account_service=Depends(get_account_service),
):
    """更新账号显示名称和邮箱。"""
    if req.name is None and req.email is None:
        raise HTTPException(status_code=400, detail="至少提供名称或邮箱")
    account = account_service.update_account(account_id, name=req.name, email=req.email)
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    return AccountResponse(
        id=account.id,
        name=account.name,
        email=account.email,
        created_at=account.created_at,
        last_used=account.last_used,
    )

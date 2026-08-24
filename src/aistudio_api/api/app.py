"""FastAPI application entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from aistudio_api.infrastructure.gateway.client import AIStudioClient

from .routes_anthropic import router as anthropic_router
from .dependencies import require_api_key
from .routes_accounts import router as accounts_router
from .routes_gemini import router as gemini_router
from .routes_openai import router as openai_router
from .routes_system import protected_router as system_protected_router
from .routes_system import public_router as system_public_router
from .state import runtime_state

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("aistudio.server")


@asynccontextmanager
async def lifespan(app: FastAPI):
    from aistudio_api.config import settings
    from aistudio_api.infrastructure.auth.api_key_store import get_api_key_store
    from aistudio_api.infrastructure.account.account_store import AccountStore
    from aistudio_api.infrastructure.account.login_service import LoginService
    from aistudio_api.application.account_service import AccountService
    from aistudio_api.application.account_rotator import init_rotator, RotationMode

    # Initialize the persistent store only. The first API Key is created from
    # the web UI so it is never silently written to .env or logs.
    get_api_key_store().ensure()

    client = AIStudioClient(
        port=runtime_state.browser_port,
    )
    runtime_state.client = client
    from aistudio_api.config import settings as app_settings
    runtime_state.busy_lock = asyncio.Semaphore(app_settings.max_concurrency)

    # 注入 snapshot 缓存引用，切号时需要清除
    from aistudio_api.infrastructure.gateway.client import _snapshot_cache
    runtime_state.snapshot_cache = _snapshot_cache

    # 初始化账号管理服务
    account_store = AccountStore()
    login_service = LoginService()
    account_service = AccountService(account_store, login_service)
    runtime_state.account_service = account_service
    # API 与 UI 先就绪；账号浏览器随后在后台并行初始化。已经完成初始化
    # 的账号可以立即处理请求，不需要等待其余账号。
    runtime_state.ready = True
    runtime_state.ready_message = "已就绪，正在准备账号"

    # 初始化账号轮询器
    rotation_mode = getattr(settings, "account_rotation_mode", "round_robin")
    cooldown = getattr(settings, "account_cooldown_seconds", 60)
    rotator = init_rotator(
        account_store,
        mode=RotationMode(rotation_mode),
        cooldown_seconds=cooldown,
    )
    runtime_state.rotator = rotator
    from aistudio_api.application.request_client_pool import RequestClientPool

    runtime_state.request_pool = RequestClientPool(
        account_store,
        max_concurrency=app_settings.max_concurrency,
        port=runtime_state.browser_port,
        rotator=rotator,
        control_client=client,
    )

    account_warmup_task: asyncio.Task | None = None
    warmup_account_count = len(account_store.list_accounts())
    if warmup_account_count:
        runtime_state.ready_message = f"已就绪，正在后台初始化 {warmup_account_count} 个账号"

        async def warm_all_accounts() -> None:
            logger.info("开始后台并行初始化全部账号 accounts=%d", warmup_account_count)
            failures = await runtime_state.request_pool.prepare_all_accounts()
            if failures:
                runtime_state.ready_message = (
                    f"已就绪，{warmup_account_count - len(failures)} 个账号可用，"
                    f"{len(failures)} 个账号异常"
                )
                logger.warning("全部账号初始化完成，异常账号=%s", sorted(failures))
            else:
                runtime_state.ready_message = f"已就绪，{warmup_account_count} 个账号可用"
                logger.info("全部账号后台初始化完成 accounts=%d", warmup_account_count)

        account_warmup_task = asyncio.create_task(
            warm_all_accounts(),
            name="aistudio-account-warmup",
        )

    logger.info(
        "Client initialized (browser=%s, port=%s, rotation=%s, accounts=%d)",
        settings.browser_engine,
        runtime_state.browser_port,
        rotator.mode,
        len(account_store.list_accounts()),
    )

    try:
        yield
    finally:
        logger.info("Shutting down")
        runtime_state.ready = False
        runtime_state.ready_message = "正在关闭..."
        if account_warmup_task is not None and not account_warmup_task.done():
            account_warmup_task.cancel()
            await asyncio.gather(account_warmup_task, return_exceptions=True)
        try:
            await account_service.close()
            logger.info("登录任务与登录浏览器已关闭")
        except Exception:
            logger.exception("关闭登录任务时出错")
        try:
            request_pool = runtime_state.request_pool
            if request_pool is not None:
                await request_pool.close()
            logger.info("并发请求浏览器池已关闭")
        except Exception:
            logger.exception("关闭并发请求浏览器池时出错")
        try:
            await client.close()
            logger.info("后台浏览器与工作线程已关闭")
        except Exception:
            logger.exception("关闭后台浏览器时出错")
        runtime_state.client = None
        runtime_state.busy_lock = None
        runtime_state.request_pool = None
        runtime_state.account_service = None
        runtime_state.rotator = None
        runtime_state.ready_message = "正在初始化..."


app = FastAPI(title="AI Studio API", lifespan=lifespan)
app.include_router(system_public_router)
app.include_router(system_protected_router, dependencies=[Depends(require_api_key)])
app.include_router(gemini_router, dependencies=[Depends(require_api_key)])
app.include_router(openai_router, dependencies=[Depends(require_api_key)])
app.include_router(anthropic_router, dependencies=[Depends(require_api_key)])
app.include_router(accounts_router, dependencies=[Depends(require_api_key)])

# 挂载静态文件
import os
static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def root():
    return RedirectResponse(url="/static/index.html")


@app.get("/login")
async def login_page():
    return RedirectResponse(url="/static/login.html")


@app.get("/auth/check")
async def auth_check(request: Request, response: Response):
    """检查认证状态，用于前端判断是否需要登录。"""
    from aistudio_api.config import settings
    from aistudio_api.infrastructure.auth.api_key_store import (
        create_local_session,
        is_local_session_valid,
    )

    local_session = False
    local_host = request.client and request.client.host in {"127.0.0.1", "::1", "localhost"}
    if settings.local_ui_auto_login and local_host:
        session_token = request.cookies.get("asp_session")
        if not is_local_session_valid(session_token):
            session_token = create_local_session()
            response.set_cookie(
                "asp_session",
                session_token,
                max_age=86400,
                httponly=True,
                samesite="lax",
                secure=False,
            )
        local_session = True
    return {"auth_enabled": settings.auth_enabled, "local_session": local_session}


@app.post("/auth/logout")
async def auth_logout(request: Request, response: Response):
    from aistudio_api.infrastructure.auth.api_key_store import revoke_local_session

    revoke_local_session(request.cookies.get("asp_session"))
    response.delete_cookie("asp_session")
    return {"ok": True}


def main():
    import sys

    from aistudio_api.config import settings

    parser = argparse.ArgumentParser(description="AI Studio OpenAI-compatible API Server")
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument("--browser-port", type=int, default=settings.browser_port)
    parser.add_argument("--camoufox-port", type=int, dest="browser_port", help=argparse.SUPPRESS)
    args = parser.parse_args()

    runtime_state.browser_port = args.browser_port

    import uvicorn

    logger.info("Starting server on port %s", args.port)
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=args.port,
        log_level="info",
        log_config=None if sys.stderr is None else uvicorn.config.LOGGING_CONFIG,
    )

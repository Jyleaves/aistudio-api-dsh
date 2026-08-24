"""Shared helpers for API service handlers."""

from __future__ import annotations

import base64
import logging
import mimetypes
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

from aistudio_api.api.response_models import (
    HealthResponse,
    ImageGenerationResponse,
    ImageResponseData,
    ModelStatsResponse,
    StatsResponse,
    StatsTotalsResponse,
)
from aistudio_api.api.state import runtime_state
from aistudio_api.config import settings
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.gateway.wire_types import AistudioPart

logger = logging.getLogger("aistudio.server")
MAX_RETRIES = max(1, int(settings.account_max_retries))


@dataclass
class RequestExecution:
    client: AIStudioClient
    account_id: str | None
    worker_id: str
    succeeded: bool = False


def mark_request_worker_unhealthy(worker_id: str) -> None:
    request_pool = runtime_state.request_pool
    if request_pool is not None and worker_id != "legacy":
        request_pool.mark_worker_unhealthy(worker_id)


def validate_image_request_options(*, size: str, n: int) -> None:
    if n != 1:
        raise HTTPException(
            400,
            detail={"message": "Only n=1 is currently supported", "type": "invalid_request_error"},
        )
    if AIStudioClient.resolve_image_size(size) is None:
        raise HTTPException(
            400,
            detail={"message": f"Unsupported image size '{size}'", "type": "invalid_request_error"},
        )


async def build_inline_image_parts(image_files: list) -> list[AistudioPart]:
    parts: list[AistudioPart] = []
    for image_file in image_files:
        mime = image_file.content_type or mimetypes.guess_type(image_file.filename or "")[0] or "image/png"
        content = await image_file.read()
        parts.append(AistudioPart(inline_data=(mime, base64.b64encode(content).decode("ascii"))))
    return parts


async def try_switch_account() -> bool:
    """尝试切换到下一个可用账号。返回是否成功切换。"""
    rotator = runtime_state.rotator
    if rotator is None:
        return False

    next_account = await rotator.get_next_account()
    if next_account is None:
        return False

    account_service = runtime_state.account_service
    client = runtime_state.client
    if not all([account_service, client]):
        return False

    result = await account_service.activate_account(
        next_account.id,
        client._session,
        runtime_state.snapshot_cache,
        None,  # skip lock — caller already holds it
        keep_snapshot_cache=False,
    )
    return result is not None


def require_busy_lock():
    busy_lock = runtime_state.busy_lock
    if busy_lock is None:
        raise HTTPException(503, detail={"message": "Server not ready", "type": "service_unavailable"})
    if not runtime_state.ready:
        raise HTTPException(503, detail={"message": "服务正在初始化，请稍候", "type": "service_initializing"})
    if runtime_state.account_switching:
        raise HTTPException(503, detail={"message": "账号正在初始化，请稍候", "type": "account_initializing"})
    if runtime_state.login_in_progress:
        # 登录标志只在登录会话真正进行中时才拦截请求；否则自愈复位，
        # 避免前端轮询中断（如页面被关闭）导致标志永久卡住、API 全部 503。
        account_svc = runtime_state.account_service
        if account_svc is not None and hasattr(account_svc, "is_login_active") and account_svc.is_login_active():
            raise HTTPException(503, detail={"message": "登录窗口正在使用浏览器，请登录完成后再发送请求", "type": "login_in_progress"})
        runtime_state.login_in_progress = False
        logger.info("登录会话已结束，自动复位 login_in_progress 标志")
    return busy_lock


def require_service_ready() -> None:
    """Validate global state before an API request enters the execution queue."""
    require_busy_lock()


@asynccontextmanager
async def acquire_request_client(
    fallback_client: AIStudioClient,
    *,
    attempt: int = 0,
    exclude_account_ids: set[str] | None = None,
):
    """Lease an isolated client, with a compatibility fallback for unit/CLI use."""
    require_service_ready()
    request_pool = runtime_state.request_pool
    if request_pool is not None:
        try:
            async with request_pool.lease(
                exclude_account_ids=exclude_account_ids,
            ) as lease:
                execution = RequestExecution(
                    client=lease.client,
                    account_id=lease.account_id,
                    worker_id=lease.worker_id,
                )
                try:
                    yield execution
                finally:
                    # Only a completed API operation proves that Google accepts
                    # this browser's current GenerateContent credentials.
                    if execution.succeeded:
                        await request_pool.mark_worker_verified(lease.worker_id)
            return
        except RuntimeError as exc:
            raise HTTPException(
                503,
                detail={"message": str(exc), "type": "account_unavailable"},
            ) from exc

    async def _legacy_execution():
        await ensure_active_account(attempt)
        account_service = runtime_state.account_service
        account = account_service.get_active_account() if account_service else None
        return RequestExecution(
            client=fallback_client,
            account_id=getattr(account, "id", None),
            worker_id="legacy",
        )

    busy_lock = require_busy_lock()
    async with busy_lock:
        yield await _legacy_execution()


async def ensure_active_account(attempt: int) -> None:
    if attempt != 0:
        return
    account_svc = runtime_state.account_service
    if not account_svc:
        return

    active_account = account_svc.get_active_account()
    client = runtime_state.client
    if active_account is None:
        await try_switch_account()
        return

    # The registry remembers the active account across restarts, but the
    # browser context is intentionally lazy.  Previously the first request
    # skipped initialization because an active account already existed; a
    # manual activation of another account then appeared to "fix" Playground
    # only because it happened to call ensure_context().
    if client is None:
        return
    try:
        await client.warmup()
        logger.info("[probe] active account ready account_id=%s", active_account.id)
    except Exception as exc:
        logger.warning("当前账号初始化失败 account_id=%s: %s", active_account.id, exc)
        # Keep the existing rotation fallback for an invalid/expired active
        # account, while preserving it whenever warmup succeeds.
        if not await try_switch_account():
            raise


def record_rotator_event(event: str, account_id: str | None = None) -> None:
    rotator = runtime_state.rotator
    account_service = runtime_state.account_service
    account = account_service.get_active_account() if account_service else None
    resolved_account_id = account_id or (account.id if account is not None else None)
    if not rotator or resolved_account_id is None:
        return
    if event == "success":
        rotator.record_success(resolved_account_id)
    elif event == "rate_limited":
        rotator.record_rate_limited(resolved_account_id)
    elif event == "error":
        rotator.record_error(resolved_account_id)


def image_response(output: Any) -> ImageGenerationResponse:
    data: list[ImageResponseData] = []
    for img in output.images:
        b64 = base64.b64encode(img.data).decode("ascii")
        data.append(ImageResponseData(b64_json=b64, revised_prompt=output.text or ""))
    return ImageGenerationResponse(created=int(time.time()), data=data)


def health_response() -> HealthResponse:
    busy_lock = runtime_state.busy_lock
    request_pool = runtime_state.request_pool
    if runtime_state.account_switching:
        message = "正在切换账号..."
    else:
        message = runtime_state.ready_message
    return HealthResponse(
        status="ok",
        busy=(
            bool(getattr(request_pool, "saturated", False))
            if request_pool is not None
            else (busy_lock.locked() if busy_lock else False)
        ),
        ready=runtime_state.ready and not runtime_state.account_switching,
        message=message,
    )


def stats_response() -> StatsResponse:
    stats = dict(runtime_state.model_stats)
    totals = StatsTotalsResponse(
        requests=sum(s["requests"] for s in stats.values()),
        success=sum(s["success"] for s in stats.values()),
        rate_limited=sum(s["rate_limited"] for s in stats.values()),
        errors=sum(s["errors"] for s in stats.values()),
        prompt_tokens=sum(s["prompt_tokens"] for s in stats.values()),
        completion_tokens=sum(s["completion_tokens"] for s in stats.values()),
        total_tokens=sum(s["total_tokens"] for s in stats.values()),
    )
    models = {name: ModelStatsResponse(**values) for name, values in stats.items()}
    return StatsResponse(models=models, totals=totals)

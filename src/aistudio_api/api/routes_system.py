"""System and metadata routes."""

from __future__ import annotations

import asyncio
import os
import subprocess
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from aistudio_api.application.api_service import health_response, stats_response
from aistudio_api.api.response_models import HealthResponse, StatsResponse
from aistudio_api.api.dependencies import get_runtime_state

public_router = APIRouter()
protected_router = APIRouter()


class ApiKeyCreateRequest(BaseModel):
    name: str | None = None


def _run_git(*args: str, timeout: int = 45) -> subprocess.CompletedProcess[str]:
    from aistudio_api.config import PROJECT_ROOT

    return subprocess.run(
        ["git", *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _update_check_sync() -> dict:
    current = _run_git("rev-parse", "--short", "HEAD")
    upstream = _run_git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    dirty = _run_git("status", "--porcelain")
    result = {
        "current": current.stdout.strip() if current.returncode == 0 else None,
        "upstream": upstream.stdout.strip() if upstream.returncode == 0 else None,
        "dirty": bool(dirty.stdout.strip()),
        "update_available": False,
        "error": None,
    }
    if upstream.returncode != 0:
        result["error"] = "当前分支没有配置上游 Git 分支"
        return result
    fetched = _run_git("fetch", "--prune", timeout=60)
    if fetched.returncode != 0:
        result["error"] = (fetched.stderr or "Git fetch 失败").strip()[-500:]
        return result
    counts = _run_git("rev-list", "--left-right", "--count", "HEAD...@{u}")
    if counts.returncode == 0:
        parts = counts.stdout.split()
        behind = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        result["update_available"] = behind > 0
        result["behind"] = behind
        result["ahead"] = int(parts[0]) if parts and parts[0].isdigit() else 0
    return result


@protected_router.get("/update/check")
async def check_update():
    return await asyncio.to_thread(_update_check_sync)


@protected_router.post("/update")
async def start_update():
    from aistudio_api.config import PROJECT_ROOT, settings

    check = await asyncio.to_thread(_update_check_sync)
    if check.get("error"):
        raise HTTPException(400, detail=check["error"])
    if check.get("dirty"):
        raise HTTPException(409, detail="存在未提交修改，已停止更新以避免覆盖本地代码")
    script = PROJECT_ROOT / "update-aistudio-api.ps1"
    if not script.exists():
        raise HTTPException(404, detail="未找到更新脚本")
    log_path = PROJECT_ROOT / "data" / "update.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    shell = "powershell.exe" if os.name == "nt" else "pwsh"
    with log_path.open("a", encoding="utf-8") as log_file:
        subprocess.Popen(
            [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-Port", str(settings.port), "-Restart"],
            cwd=PROJECT_ROOT,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
    return {"ok": True, "message": "更新已开始，反代会自动重启", "log": str(log_path)}


def _public_key_result(item: dict, secret: str | None = None) -> dict:
    result = dict(item)
    if secret is not None:
        result["key"] = secret
    return result


@protected_router.get("/auth/api-keys")
async def list_api_keys():
    from aistudio_api.infrastructure.auth.api_key_store import get_api_key_store

    return {"keys": get_api_key_store().list_public()}


@protected_router.post("/auth/api-keys")
async def create_api_key(payload: ApiKeyCreateRequest):
    from aistudio_api.infrastructure.auth.api_key_store import get_api_key_store

    item, secret = get_api_key_store().create(payload.name)
    return _public_key_result(item, secret)


@protected_router.post("/auth/api-keys/rotate")
async def rotate_api_key(payload: ApiKeyCreateRequest):
    from aistudio_api.infrastructure.auth.api_key_store import get_api_key_store

    store = get_api_key_store()
    item, secret = store.rotate(payload.name)
    return _public_key_result(item, secret)


@protected_router.post("/auth/api-keys/{key_id}/revoke")
async def revoke_api_key(key_id: str):
    from aistudio_api.infrastructure.auth.api_key_store import get_api_key_store

    try:
        return get_api_key_store().revoke(key_id)
    except KeyError:
        raise HTTPException(404, detail="API Key 不存在")
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))


@public_router.get("/health", response_model=HealthResponse)
async def health():
    return health_response()


@protected_router.get("/stats", response_model=StatsResponse)
async def stats():
    return stats_response()


# ========== 模型配置 API ==========

@protected_router.get("/config/model-defaults")
async def get_model_defaults_config():
    """获取模型默认配置。"""
    import yaml
    from aistudio_api.infrastructure.gateway.model_defaults import (
        _resolve_config_path,
        _default_config,
    )

    path = _resolve_config_path(None)
    if not path.exists():
        return _default_config().get("model_defaults", {})
    try:
        content = yaml.safe_load(path.read_text()) or {}
        return content.get("model_defaults", {})
    except Exception as e:
        from fastapi import HTTPException
        raise HTTPException(500, detail=f"读取配置失败: {e}")


class ModelDefaultsConfigPayload(BaseModel):
    profiles: list[dict]
    models: dict[str, dict]


@protected_router.post("/config/model-defaults")
async def save_model_defaults_config(payload: ModelDefaultsConfigPayload):
    """保存模型默认配置并清理缓存。"""
    import yaml
    from aistudio_api.infrastructure.gateway.model_defaults import (
        _resolve_config_path,
        _compiled_profiles,
        _compiled_model_overrides,
    )

    path = _resolve_config_path(None)
    data = {
        "model_defaults": {
            "profiles": payload.profiles,
            "models": payload.models,
        }
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(
                data,
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
            ),
            encoding="utf-8",
        )
        _compiled_profiles.cache_clear()
        _compiled_model_overrides.cache_clear()
        return {"ok": True}
    except Exception as e:
        from fastapi import HTTPException
        raise HTTPException(500, detail=f"保存配置失败: {e}")


# ========== 轮询管理 API ==========

class RotationModeRequest(BaseModel):
    mode: str  # round_robin, lru, least_rl
    cooldown_seconds: int | None = None


@protected_router.get("/rotation")
async def get_rotation_status(runtime_state=Depends(get_runtime_state)):
    """获取轮询状态。"""
    rotator = runtime_state.rotator
    if rotator is None:
        return {"enabled": False, "message": "轮询器未初始化"}

    return {
        "enabled": True,
        "mode": rotator.mode.value,
        "cooldown_seconds": rotator.cooldown_seconds,
        "accounts": rotator.get_all_stats(),
    }


@protected_router.post("/rotation/mode")
async def set_rotation_mode(
    req: RotationModeRequest,
    runtime_state=Depends(get_runtime_state),
):
    """设置轮询模式。"""
    rotator = runtime_state.rotator
    if rotator is None:
        raise HTTPException(503, detail="轮询器未初始化")

    try:
        from aistudio_api.application.account_rotator import RotationMode
        rotator.mode = RotationMode(req.mode)
        if req.cooldown_seconds is not None:
            rotator.cooldown_seconds = req.cooldown_seconds
        return {
            "ok": True,
            "mode": rotator.mode.value,
            "cooldown_seconds": rotator.cooldown_seconds,
        }
    except ValueError:
        raise HTTPException(400, detail=f"无效的轮询模式: {req.mode}，可选: round_robin, lru, least_rl")


@protected_router.get("/rotation/accounts")
async def get_rotation_accounts(runtime_state=Depends(get_runtime_state)):
    """获取所有账号的轮询统计。"""
    rotator = runtime_state.rotator
    if rotator is None:
        raise HTTPException(503, detail="轮询器未初始化")

    return rotator.get_all_stats()


@protected_router.post("/rotation/next")
async def force_next_account(runtime_state=Depends(get_runtime_state)):
    """强制切换到下一个可用账号。"""
    rotator = runtime_state.rotator
    if rotator is None:
        raise HTTPException(503, detail="轮询器未初始化")

    # 获取下一个账号
    next_account = await rotator.get_next_account()
    if next_account is None:
        raise HTTPException(404, detail="没有可用的账号")

    # 切换账号
    account_service = runtime_state.account_service
    client = runtime_state.client
    busy_lock = runtime_state.busy_lock

    if not all([account_service, client, busy_lock]):
        raise HTTPException(503, detail="服务未就绪")

    result = await account_service.activate_account(
        next_account.id,
        client._session,
        runtime_state.snapshot_cache,
        busy_lock,
        keep_snapshot_cache=False,
    )

    if result is None:
        raise HTTPException(500, detail="切换失败")

    return {
        "ok": True,
        "account": {
            "id": result.id,
            "name": result.name,
            "email": result.email,
        },
    }

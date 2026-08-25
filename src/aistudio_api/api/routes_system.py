"""System and metadata routes."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel

from aistudio_api.application.api_service import health_response, stats_response
from aistudio_api.api.response_models import HealthResponse, StatsResponse
from aistudio_api.api.dependencies import get_runtime_state

public_router = APIRouter()
protected_router = APIRouter()

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class ApiKeyCreateRequest(BaseModel):
    name: str | None = None


class SettingsPayload(BaseModel):
    port: int | None = None
    browser_engine: str | None = None
    browser_port: int | None = None
    browser_headless: bool | None = None
    browser_channel: str | None = None
    browser_executable_path: str | None = None
    browser_chromium_sandbox: bool | None = None
    browser_python: str | None = None
    login_browser: str | None = None
    login_browser_channel: str | None = None
    auth_file: str | None = None
    api_key_store_path: str | None = None
    proxy_url: str | None = None
    local_ui_auto_login: bool | None = None
    timeout_replay: int | None = None
    timeout_stream: int | None = None
    timeout_capture: int | None = None
    snapshot_cache_ttl: int | None = None
    snapshot_cache_max: int | None = None
    accounts_dir: str | None = None
    tmp_dir: str | None = None
    dump_raw_response: bool | None = None
    dump_raw_response_dir: str | None = None
    account_rotation_mode: str | None = None
    account_cooldown_seconds: int | None = None
    account_max_retries: int | None = None
    max_concurrency: int | None = None
    max_idle_browsers: int | None = None
    default_text_model: str | None = None
    default_image_model: str | None = None


_SETTINGS_FIELDS = {
    "port": ("AISTUDIO_PORT", "int"),
    "browser_engine": ("AISTUDIO_BROWSER", "str"),
    "browser_port": ("AISTUDIO_BROWSER_PORT", "int"),
    "browser_headless": ("AISTUDIO_BROWSER_HEADLESS", "bool"),
    "browser_channel": ("AISTUDIO_BROWSER_CHANNEL", "str"),
    "browser_executable_path": ("AISTUDIO_BROWSER_EXECUTABLE", "str"),
    "browser_chromium_sandbox": ("AISTUDIO_CHROMIUM_SANDBOX", "bool"),
    "browser_python": ("AISTUDIO_BROWSER_PYTHON", "str"),
    "login_browser": ("AISTUDIO_LOGIN_BROWSER", "str"),
    "login_browser_channel": ("AISTUDIO_LOGIN_BROWSER_CHANNEL", "str"),
    "auth_file": ("AISTUDIO_AUTH_FILE", "str"),
    "api_key_store_path": ("AISTUDIO_API_KEY_STORE", "str"),
    "proxy_url": ("AISTUDIO_PROXY", "str"),
    "local_ui_auto_login": ("AISTUDIO_LOCAL_UI_AUTO_LOGIN", "bool"),
    "timeout_replay": ("AISTUDIO_TIMEOUT_REPLAY", "int"),
    "timeout_stream": ("AISTUDIO_TIMEOUT_STREAM", "int"),
    "timeout_capture": ("AISTUDIO_TIMEOUT_CAPTURE", "int"),
    "snapshot_cache_ttl": ("AISTUDIO_SNAPSHOT_CACHE_TTL", "int"),
    "snapshot_cache_max": ("AISTUDIO_SNAPSHOT_CACHE_MAX", "int"),
    "accounts_dir": ("AISTUDIO_ACCOUNTS_DIR", "str"),
    "tmp_dir": ("AISTUDIO_TMP_DIR", "str"),
    "dump_raw_response": ("AISTUDIO_DUMP_RAW_RESPONSE", "bool"),
    "dump_raw_response_dir": ("AISTUDIO_DUMP_RAW_RESPONSE_DIR", "str"),
    "account_rotation_mode": ("AISTUDIO_ACCOUNT_ROTATION_MODE", "str"),
    "account_cooldown_seconds": ("AISTUDIO_ACCOUNT_COOLDOWN_SECONDS", "int"),
    "account_max_retries": ("AISTUDIO_ACCOUNT_MAX_RETRIES", "int"),
    "max_concurrency": ("AISTUDIO_MAX_CONCURRENCY", "int"),
    "max_idle_browsers": ("AISTUDIO_MAX_IDLE_BROWSERS", "int"),
    "default_text_model": ("AISTUDIO_DEFAULT_TEXT_MODEL", "str"),
    "default_image_model": ("AISTUDIO_DEFAULT_IMAGE_MODEL", "str"),
}


def _settings_snapshot() -> dict:
    from aistudio_api.config import settings

    values = {field: getattr(settings, field) for field in _SETTINGS_FIELDS}
    values["browser_executable_path"] = values["browser_executable_path"] or ""
    values["browser_channel"] = values["browser_channel"] or ""
    values["browser_python"] = values["browser_python"] or ""
    values["login_browser_channel"] = values["login_browser_channel"] or ""
    values["proxy_url"] = values["proxy_url"] or ""
    values["auth_file"] = values["auth_file"] or ""
    return {"settings": values, "restart_required": True}


def _write_env_settings(values: dict[str, object]) -> None:
    from aistudio_api.config import USER_DATA_ROOT

    env_path = USER_DATA_ROOT / ".env"
    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    for field, value in values.items():
        env_name, kind = _SETTINGS_FIELDS[field]
        if kind == "bool":
            serialized = "1" if value else "0"
        else:
            serialized = str(value or "")
        replacement = f"{env_name}={serialized}"
        found = False
        for index, line in enumerate(lines):
            if line.strip().startswith(f"{env_name}="):
                lines[index] = replacement
                found = True
                break
        if not found:
            lines.append(replacement)
    env_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


@protected_router.get("/settings")
async def get_settings():
    return _settings_snapshot()


@protected_router.post("/settings")
async def save_settings(payload: SettingsPayload):
    from aistudio_api.config import resolve_project_path, settings

    raw = payload.dict(exclude_none=True)
    if not raw:
        return _settings_snapshot()
    if raw.get("browser_engine") not in (None, "chromium", "cloakbrowser", "camoufox"):
        raise HTTPException(400, detail="browser_engine 只能是 chromium、cloakbrowser 或 camoufox")
    if raw.get("login_browser") not in (None, "auto", "chromium", "system", "cloakbrowser"):
        raise HTTPException(400, detail="login_browser 只能是 chromium、auto、system 或 cloakbrowser")
    if raw.get("account_rotation_mode") not in (None, "round_robin", "lru", "least_rl"):
        raise HTTPException(400, detail="account_rotation_mode 无效")
    for name in ("port", "browser_port", "timeout_replay", "timeout_stream", "timeout_capture", "snapshot_cache_ttl", "snapshot_cache_max", "account_cooldown_seconds", "account_max_retries", "max_concurrency"):
        if name in raw and raw[name] < 1:
            raise HTTPException(400, detail=f"{name} 必须大于 0")
    if "max_idle_browsers" in raw and raw["max_idle_browsers"] < 0:
        raise HTTPException(400, detail="max_idle_browsers 不能小于 0")
    try:
        _write_env_settings(raw)
        for field, value in raw.items():
            if field in {"accounts_dir", "tmp_dir", "dump_raw_response_dir", "browser_executable_path", "auth_file", "api_key_store_path"}:
                value = resolve_project_path(value, "") if value else None
            if field == "browser_channel" and value == "":
                value = None
            if field == "login_browser_channel" and value == "":
                value = None
            if field == "proxy_url" and value == "":
                value = None
            if field == "browser_python" and value == "":
                value = None
            setattr(settings, field, value)
        return _settings_snapshot()
    except OSError as exc:
        raise HTTPException(500, detail=f"保存 .env 失败: {exc}")


def _run_git(*args: str, timeout: int = 45) -> subprocess.CompletedProcess[str]:
    from aistudio_api.config import PROJECT_ROOT, settings
    from aistudio_api.infrastructure.update_network import resolve_update_network, update_subprocess_environment

    return subprocess.run(
        ["git", *args],
        cwd=PROJECT_ROOT,
        env=update_subprocess_environment(resolve_update_network(settings.proxy_url)),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        creationflags=_CREATE_NO_WINDOW,
    )


def _update_check_sync() -> dict:
    if getattr(sys, "frozen", False):
        return {
            "current": None,
            "upstream": None,
            "dirty": False,
            "update_available": False,
            "error": "桌面版更新检查将在 GitHub Releases 更新通道上线后启用",
        }
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
    from aistudio_api.infrastructure.update_service import update_service

    return await asyncio.to_thread(update_service.check)


@protected_router.post("/update/download")
async def download_update():
    from aistudio_api.infrastructure.update_service import update_service

    try:
        return await asyncio.to_thread(update_service.start_download)
    except RuntimeError as exc:
        raise HTTPException(409, detail=str(exc))


@protected_router.get("/update/status")
async def update_status():
    from aistudio_api.infrastructure.update_service import update_service

    return update_service.status()


@protected_router.get("/update/plugin/check")
async def check_plugin_update():
    from aistudio_api.infrastructure.plugin_update_service import plugin_update_service

    return await asyncio.to_thread(plugin_update_service.check)


@protected_router.get("/update/plugin/status")
async def plugin_update_status():
    from aistudio_api.infrastructure.plugin_update_service import plugin_update_service

    return plugin_update_service.status()


@protected_router.post("/update/plugin")
async def start_plugin_update():
    from aistudio_api.infrastructure.plugin_update_service import plugin_update_service

    try:
        return await asyncio.to_thread(plugin_update_service.start_update)
    except RuntimeError as exc:
        raise HTTPException(409, detail=str(exc))


@protected_router.post("/update")
async def start_update(runtime_state=Depends(get_runtime_state)):
    from aistudio_api.infrastructure.update_service import update_service

    if getattr(sys, "frozen", False):
        try:
            result = await asyncio.to_thread(update_service.install)
        except RuntimeError as exc:
            raise HTTPException(409, detail=str(exc))
        callback = getattr(runtime_state, "desktop_shutdown", None)
        if callback is not None:
            def close_after_response() -> None:
                try:
                    callback()
                except Exception:
                    import logging

                    logging.getLogger("aistudio.server").exception("更新前关闭桌面应用失败")

            # Give FastAPI enough time to flush the response, then use the
            # normal desktop shutdown path so browser workers release their
            # profiles before the installer replaces application files.
            asyncio.get_running_loop().call_later(0.5, close_after_response)
        return result
    from aistudio_api.config import PROJECT_ROOT, USER_DATA_ROOT, settings

    check = await asyncio.to_thread(_update_check_sync)
    if check.get("error"):
        raise HTTPException(400, detail=check["error"])
    if check.get("dirty"):
        raise HTTPException(409, detail="存在未提交修改，已停止更新以避免覆盖本地代码")
    script = PROJECT_ROOT / "update-aistudio-api.ps1"
    if not script.exists():
        raise HTTPException(404, detail="未找到更新脚本")
    log_path = USER_DATA_ROOT / "data" / "update.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    shell = "powershell.exe" if os.name == "nt" else "pwsh"
    with log_path.open("a", encoding="utf-8") as log_file:
        from aistudio_api.infrastructure.update_network import resolve_update_network, update_subprocess_environment

        subprocess.Popen(
            [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-Port", str(settings.port), "-Restart"],
            cwd=PROJECT_ROOT,
            env=update_subprocess_environment(resolve_update_network(settings.proxy_url)),
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


@protected_router.post("/app/shutdown")
async def shutdown_desktop_app(
    request: Request,
    runtime_state=Depends(get_runtime_state),
):
    """Close the desktop window and let the normal lifespan cleanup run."""
    client_host = request.client.host if request.client else ""
    if client_host not in {"127.0.0.1", "::1", "localhost"}:
        raise HTTPException(403, detail="仅允许本机关闭桌面应用")
    callback = getattr(runtime_state, "desktop_shutdown", None)
    if callback is None:
        raise HTTPException(409, detail="当前实例不是桌面应用或窗口尚未就绪")

    def close_after_response() -> None:
        try:
            callback()
        except Exception:
            import logging

            logging.getLogger("aistudio.server").exception("桌面应用正常关闭失败")

    # Let FastAPI flush the JSON response before destroying the native window.
    asyncio.get_running_loop().call_later(0.2, close_after_response)
    return {"ok": True, "message": "Asteria 正在正常关闭"}


@protected_router.get("/stats", response_model=StatsResponse)
async def stats():
    return stats_response()


@protected_router.get("/runtime/request-pool")
async def request_pool_status(runtime_state=Depends(get_runtime_state)):
    pool = runtime_state.request_pool
    if pool is None:
        return {
            "enabled": False,
            "max_concurrency": 0,
            "max_idle_browsers": 0,
            "active": 0,
            "workers": 0,
            "verified_workers": 0,
            "saturated": False,
            "ready_accounts": [],
            "standby_accounts": [],
            "initializing_accounts": [],
            "failed_accounts": {},
        }
    return {
        "enabled": True,
        "max_concurrency": int(getattr(pool, "max_concurrency", 0)),
        "max_idle_browsers": int(getattr(pool, "max_idle_browsers", 0)),
        "active": int(getattr(pool, "active_count", 0)),
        "workers": int(getattr(pool, "worker_count", 0)),
        "verified_workers": int(getattr(pool, "verified_worker_count", 0)),
        "saturated": bool(getattr(pool, "saturated", False)),
        "ready_accounts": sorted(getattr(pool, "ready_account_ids", set())),
        "standby_accounts": sorted(getattr(pool, "standby_account_ids", set())),
        "initializing_accounts": sorted(getattr(pool, "initializing_account_ids", set())),
        "failed_accounts": dict(getattr(pool, "failed_account_errors", {})),
    }


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

    from aistudio_api.api.routes_accounts import activate_runtime_account

    result = await activate_runtime_account(
        account_service,
        runtime_state,
        next_account.id,
        busy_lock=busy_lock,
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

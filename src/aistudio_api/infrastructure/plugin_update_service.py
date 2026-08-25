"""Updater for the optional dsh-gemini-aistudio companion plugin."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from aistudio_api.infrastructure.update_network import (
    open_update_url,
    proxy_mode_label,
    resolve_update_network,
    update_subprocess_environment,
)
from aistudio_api.infrastructure.update_service import _version_tuple
from aistudio_api.version import APP_NAME, APP_VERSION


PLUGIN_NAME = "dsh-gemini-aistudio"
PLUGIN_REPOSITORY = "Jyleaves/dsh-gemini-aistudio"
PLUGIN_RELEASES_URL = f"https://api.github.com/repos/{PLUGIN_REPOSITORY}/releases/latest"
PLUGIN_GIT_URL = f"https://github.com/{PLUGIN_REPOSITORY}.git"
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
_LOGGER = logging.getLogger("aistudio.plugin_update")


@dataclass(slots=True)
class PluginUpdateState:
    status: str = "idle"
    installed: bool = False
    managed: bool = False
    current: str | None = None
    latest: str | None = None
    available: bool = False
    checking: bool = False
    updating: bool = False
    message: str = ""
    error: str | None = None
    proxy_mode: str = "direct"
    proxy_label: str = "直连"
    restart_required: bool = False


class PluginUpdateService:
    def __init__(self, dsh_home: str | Path | None = None, configured_proxy: str | None = None) -> None:
        root = Path(dsh_home) if dsh_home is not None else Path(os.getenv("DSH_HOME") or Path.home() / ".dsh")
        self._profile_dir = root / "profiles" / "web"
        self._package_path = self._profile_dir / "node_modules" / PLUGIN_NAME / "package.json"
        self._profile_package_path = self._profile_dir / "package.json"
        self._configured_proxy = configured_proxy
        self._lock = threading.RLock()
        self._state = PluginUpdateState()

    def status(self) -> dict:
        with self._lock:
            return asdict(self._state)

    def _set(self, **values: object) -> None:
        with self._lock:
            for key, value in values.items():
                setattr(self._state, key, value)

    def _proxy_url(self) -> str | None:
        if self._configured_proxy is not None:
            return self._configured_proxy
        try:
            from aistudio_api.config import settings

            return settings.proxy_url
        except Exception:
            return None

    @staticmethod
    def _read_json(path: Path) -> dict:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}

    def _installation(self) -> tuple[bool, bool, str | None]:
        package = self._read_json(self._package_path)
        current = str(package.get("version") or "").strip() or None
        installed = bool(current)
        profile = self._read_json(self._profile_package_path)
        dependencies = profile.get("dependencies") if isinstance(profile.get("dependencies"), dict) else {}
        specifier = str(dependencies.get(PLUGIN_NAME) or "").strip().lower()
        development = specifier.startswith(("link:", "file:", "workspace:"))
        return installed, installed and not development, current

    def check(self) -> dict:
        with self._lock:
            if self._state.updating:
                return asdict(self._state)
            self._state.checking = True
            self._state.error = None

        installed, managed, current = self._installation()
        route = resolve_update_network(self._proxy_url())
        self._set(
            installed=installed,
            managed=managed,
            current=current,
            available=False,
            proxy_mode=route.proxy_mode,
            proxy_label=proxy_mode_label(route.proxy_mode),
        )
        request = urllib.request.Request(
            PLUGIN_RELEASES_URL,
            headers={"Accept": "application/vnd.github+json", "User-Agent": f"{APP_NAME}/{APP_VERSION}"},
        )
        try:
            with open_update_url(request, 15, route) as response:
                release = json.loads(response.read().decode("utf-8"))
            latest = str(release.get("tag_name") or "").lstrip("vV")
            if not _VERSION_PATTERN.fullmatch(latest):
                raise RuntimeError("插件 Release 版本号无效")
            available = bool(current and _version_tuple(latest) > _version_tuple(current))
            if not installed:
                status, message = "not_installed", "未检测到 dsh-gemini-aistudio"
            elif not managed:
                status, message = "development", "当前使用本地开发链接，请在源码仓库更新"
            elif available:
                status, message = "available", f"发现插件新版本 {latest}"
            else:
                status, message = "latest", "插件已经是最新版本"
            self._set(
                status=status,
                latest=latest,
                available=available and managed,
                checking=False,
                message=message,
                error=None,
            )
        except Exception as exc:
            _LOGGER.warning("Unable to check dsh-gemini-aistudio release: %s", exc)
            self._set(
                status="error",
                checking=False,
                latest=None,
                error="检查插件更新失败：无法连接 GitHub，请检查网络或代理设置",
                message="无法连接插件更新服务器",
            )
        return self.status()

    def start_update(self) -> dict:
        with self._lock:
            if self._state.updating:
                return asdict(self._state)
            if not self._state.installed:
                raise RuntimeError("未检测到 dsh-gemini-aistudio")
            if not self._state.managed:
                raise RuntimeError("本地开发链接不能由 Asteria 自动更新")
            if not self._state.available or not self._state.latest:
                raise RuntimeError("没有可用的插件更新")
            latest = self._state.latest
            self._state.status = "updating"
            self._state.updating = True
            self._state.error = None
            self._state.message = "正在更新插件"
        threading.Thread(target=self._update, args=(latest,), daemon=True, name="asteria-plugin-update").start()
        return self.status()

    def _update(self, latest: str) -> None:
        route = resolve_update_network(self._proxy_url())
        executable = shutil.which("dsh.cmd") if os.name == "nt" else shutil.which("dsh")
        if not executable:
            executable = shutil.which("dsh")
        if not executable:
            self._set(status="error", updating=False, error="未找到 dsh 命令", message="插件更新失败")
            return

        package = f"{PLUGIN_GIT_URL}#v{latest}"
        arguments = [executable, "plugin", "--profile", "web", "add", package]
        if os.name == "nt" and Path(executable).suffix.lower() in {".cmd", ".bat"}:
            arguments = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", *arguments]
        try:
            result = subprocess.run(
                arguments,
                cwd=self._profile_dir,
                env=update_subprocess_environment(route),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
                check=False,
                creationflags=_NO_WINDOW,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "dsh plugin 执行失败").strip()[-1200:]
                if "EPERM" in detail or "operation not permitted" in detail.lower():
                    detail = f"{detail}\n请关闭正在运行的 dsh 后重试。"
                raise RuntimeError(detail)
            installed, managed, current = self._installation()
            if current != latest:
                raise RuntimeError(f"更新命令已完成，但检测到的插件版本仍为 {current or '未知'}")
            self._set(
                status="updated",
                installed=installed,
                managed=managed,
                current=current,
                available=False,
                updating=False,
                restart_required=True,
                message="插件更新完成，重启 dsh 后生效",
                error=None,
                proxy_mode=route.proxy_mode,
                proxy_label=proxy_mode_label(route.proxy_mode),
            )
        except subprocess.TimeoutExpired:
            self._set(status="error", updating=False, error="插件更新超时，请稍后重试", message="插件更新失败")
        except Exception as exc:
            self._set(status="error", updating=False, error=str(exc), message="插件更新失败")


plugin_update_service = PluginUpdateService()

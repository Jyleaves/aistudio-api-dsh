"""Updater for the optional dsh-gemini-aistudio companion plugin."""

from __future__ import annotations

import json
import hashlib
import logging
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime
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
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
_DOWNLOAD_CHUNK_SIZE = 256 * 1024
_DOWNLOAD_ATTEMPTS = 3
_INSTALL_TIMEOUT_SECONDS = 120
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
    progress: int = 0
    asset_name: str | None = None
    asset_size: int | None = None
    downloaded_bytes: int = 0
    proxy_mode: str = "direct"
    proxy_label: str = "直连"
    restart_required: bool = False
    log_path: str = ""


class PluginUpdateService:
    def __init__(
        self,
        dsh_home: str | Path | None = None,
        configured_proxy: str | None = None,
        update_root: str | Path | None = None,
        log_path: str | Path | None = None,
        dsh_running_probe=None,
    ) -> None:
        root = Path(dsh_home) if dsh_home is not None else Path(os.getenv("DSH_HOME") or Path.home() / ".dsh")
        self._profile_dir = root / "profiles" / "web"
        self._package_path = self._profile_dir / "node_modules" / PLUGIN_NAME / "package.json"
        self._profile_package_path = self._profile_dir / "package.json"
        self._configured_proxy = configured_proxy
        self._lock = threading.RLock()
        if update_root is None:
            try:
                from aistudio_api.config import USER_DATA_ROOT

                data_root = USER_DATA_ROOT / "data"
                self._update_root = data_root / "tmp" / "plugin-updates"
                self._log_path = Path(log_path) if log_path is not None else data_root / "plugin-update.log"
            except Exception:
                self._update_root = Path(tempfile.gettempdir()) / "Asteria" / "plugin-updates"
                self._log_path = Path(log_path) if log_path is not None else self._update_root / "plugin-update.log"
        else:
            self._update_root = Path(update_root)
            self._log_path = Path(log_path) if log_path is not None else self._update_root / "plugin-update.log"
        self._state = PluginUpdateState(log_path=str(self._log_path))
        self._release: dict | None = None
        self._package: Path | None = None
        self._dsh_running_probe = dsh_running_probe or self._default_dsh_running

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
    def _default_dsh_running() -> bool:
        try:
            with socket.create_connection(("127.0.0.1", 3080), timeout=0.2):
                return True
        except OSError:
            return False

    def _log(self, message: str) -> None:
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as output:
                output.write(f"[{datetime.now().isoformat(timespec='seconds')}] {message.rstrip()}\n")
        except OSError as exc:
            _LOGGER.warning("Unable to write plugin update log: %s", exc)

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
        specifier = str(dependencies.get(PLUGIN_NAME) or "").strip()
        lowered = specifier.lower()
        development = lowered.startswith(("link:", "workspace:")) or (
            lowered.startswith("file:") and not self._is_cached_release_specifier(specifier)
        )
        return installed, installed and not development, current

    def _is_cached_release_specifier(self, specifier: str) -> bool:
        """Return whether a file dependency points at Asteria's verified cache.

        User-owned file/link dependencies remain protected as development
        installs. Packages downloaded and verified by Asteria are managed so a
        later release can replace them without falling back to GitHub cloning.
        """
        if not specifier.lower().startswith("file:"):
            return False
        raw_path = specifier[5:].strip()
        if re.match(r"^/[A-Za-z]:/", raw_path):
            raw_path = raw_path[1:]
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = self._profile_dir / candidate
        try:
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(self._update_root.resolve(strict=False))
        except (OSError, ValueError):
            return False
        return resolved.name.lower().startswith(f"{PLUGIN_NAME}-") and resolved.suffix.lower() == ".tgz"

    def _pnpm_store_root(self) -> Path | None:
        """Recover the store that owns the profile's existing node_modules.

        pnpm refuses to mutate a profile when its current default store differs
        from the store recorded at installation time. This commonly happens
        after moving dsh to another drive or changing pnpm major versions.
        """
        modules_file = self._profile_dir / "node_modules" / ".modules.yaml"
        try:
            raw = modules_file.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None
        store_value = ""
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                store_value = str(data.get("storeDir") or "")
        except json.JSONDecodeError:
            match = re.search(r"(?m)^storeDir:\s*['\"]?(.+?)['\"]?\s*$", raw)
            if match:
                store_value = match.group(1)
        if not store_value:
            return None
        store_path = Path(store_value)
        # pnpm records the versioned content store (…/v11), while its
        # store-dir setting expects the parent and appends that suffix itself.
        if re.fullmatch(r"v\d+", store_path.name, re.IGNORECASE):
            store_path = store_path.parent
        return store_path

    def _install_environment(self, route) -> dict[str, str]:
        environment = update_subprocess_environment(route)
        store_root = self._pnpm_store_root()
        if store_root is not None:
            environment["PNPM_CONFIG_STORE_DIR"] = str(store_root)
        return environment

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
            assets = release.get("assets") if isinstance(release.get("assets"), list) else []
            asset_name = f"{PLUGIN_NAME}-{latest}.tgz"
            asset = next((item for item in assets if item.get("name") == asset_name), None)
            checksum_asset = next((item for item in assets if item.get("name") == f"{asset_name}.sha256"), None)
            release_error = None
            if available and managed and not asset:
                release_error = "该版本缺少插件更新包，请使用 dsh 命令手动更新"
            elif available and managed and not checksum_asset:
                release_error = "该版本缺少 SHA-256 校验文件，请使用 dsh 命令手动更新"
            if not installed:
                status, message = "not_installed", "未检测到 dsh-gemini-aistudio"
            elif not managed:
                status, message = "development", "当前使用本地开发链接，请在源码仓库更新"
            elif available:
                status, message = "available", f"发现插件新版本 {latest}"
            else:
                status, message = "latest", "插件已经是最新版本"
            self._release = release
            self._set(
                status=status,
                latest=latest,
                available=available and managed,
                checking=False,
                message=message,
                error=release_error,
                progress=0,
                asset_name=asset_name if asset else None,
                asset_size=int(asset.get("size") or 0) if asset else None,
                downloaded_bytes=0,
                restart_required=False,
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
            if self._state.error:
                raise RuntimeError(self._state.error)
            latest = self._state.latest
            self._state.status = "downloading"
            self._state.updating = True
            self._state.error = None
            self._state.progress = 0
            self._state.message = "正在下载插件更新包"
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

        try:
            package = self._download_and_verify(latest, route)
            with self._lock:
                self._package = package
            if self._dsh_running_probe():
                self._set(
                    status="blocked",
                    updating=False,
                    progress=90,
                    downloaded_bytes=package.stat().st_size,
                    message="更新包已下载并校验。请关闭 dsh Web 后再次点击更新",
                    error=None,
                )
                self._log("Install deferred because dsh Web is listening on 127.0.0.1:3080")
                return

            self._set(status="installing", progress=92, message="正在安装插件")
            # Naming the dependency explicitly makes pnpm replace the old Git/
            # registry specifier before dependency resolution. Passing only the
            # tarball path can make pnpm resolve the old source first, recreating
            # the slow/failing network path this updater is intended to avoid.
            package_specifier = f"{PLUGIN_NAME}@file:{package.as_posix()}"
            arguments = [executable, "plugin", "--profile", "web", "add", package_specifier]
            if os.name == "nt" and Path(executable).suffix.lower() in {".cmd", ".bat"}:
                arguments = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", *arguments]
            self._log(f"Running local package install for {PLUGIN_NAME} {latest}")
            result = subprocess.run(
                arguments,
                cwd=self._profile_dir,
                env=self._install_environment(route),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_INSTALL_TIMEOUT_SECONDS,
                check=False,
                creationflags=_NO_WINDOW,
            )
            self._log(f"dsh plugin exited with code {result.returncode}\n{result.stdout}\n{result.stderr}")
            if result.returncode != 0:
                detail = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part and part.strip())
                detail = (detail or "dsh plugin 执行失败")[-2400:]
                if "EPERM" in detail or "operation not permitted" in detail.lower():
                    detail = f"{detail}\n请关闭正在运行的 dsh 后重试。"
                raise RuntimeError(detail)
            self._set(status="verifying", progress=98, message="正在验证已安装版本")
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
                progress=100,
                downloaded_bytes=package.stat().st_size,
                restart_required=True,
                message="插件更新完成，重启 dsh 后生效",
                error=None,
                proxy_mode=route.proxy_mode,
                proxy_label=proxy_mode_label(route.proxy_mode),
            )
        except subprocess.TimeoutExpired:
            self._log("dsh plugin install timed out")
            self._set(status="error", updating=False, error="插件安装超时。请关闭 dsh 后重试", message="插件安装失败")
        except Exception as exc:
            self._log(f"Plugin update failed: {exc}")
            self._set(status="error", updating=False, error=str(exc), message="插件更新失败")

    def _release_asset(self, name: str) -> dict | None:
        if not self._release:
            return None
        assets = self._release.get("assets") if isinstance(self._release.get("assets"), list) else []
        return next((item for item in assets if item.get("name") == name), None)

    def _expected_sha256(self, asset: dict, route) -> str:
        checksum = self._release_asset(f"{asset['name']}.sha256")
        if not checksum:
            raise RuntimeError("插件更新包缺少 SHA-256 校验文件")
        request = urllib.request.Request(
            str(checksum["browser_download_url"]),
            headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"},
        )
        with open_update_url(request, 15, route) as response:
            value = response.read().decode("utf-8", errors="replace").strip().split()[0]
        if not _SHA256_PATTERN.fullmatch(value):
            raise RuntimeError("插件 SHA-256 校验文件格式无效")
        return value.lower()

    def _download_and_verify(self, latest: str, route) -> Path:
        asset_name = f"{PLUGIN_NAME}-{latest}.tgz"
        asset = self._release_asset(asset_name)
        if not asset:
            raise RuntimeError("未找到插件更新包")
        expected = self._expected_sha256(asset, route)
        self._update_root.mkdir(parents=True, exist_ok=True)
        target = self._update_root / asset_name
        partial = target.with_suffix(target.suffix + ".part")
        if target.exists() and self._file_sha256(target) == expected:
            self._set(status="verifying", progress=90, downloaded_bytes=target.stat().st_size, message="正在使用已校验的更新缓存")
            return target
        target.unlink(missing_ok=True)

        last_error: Exception | None = None
        for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
            try:
                self._download_once(asset, partial, route)
                self._set(status="verifying", progress=85, message="正在校验插件更新包")
                if self._file_sha256(partial) != expected:
                    partial.unlink(missing_ok=True)
                    raise RuntimeError("插件更新包 SHA-256 校验失败")
                os.replace(partial, target)
                self._set(progress=90, downloaded_bytes=target.stat().st_size)
                return target
            except Exception as exc:
                last_error = exc
                if attempt >= _DOWNLOAD_ATTEMPTS:
                    break
                delay = 2 ** (attempt - 1)
                self._set(status="downloading", message=f"下载中断，{delay} 秒后重试（{attempt + 1}/{_DOWNLOAD_ATTEMPTS}）")
                time.sleep(delay)
        raise last_error or RuntimeError("插件更新包下载失败")

    def _download_once(self, asset: dict, partial: Path, route) -> None:
        total = int(asset.get("size") or 0)
        offset = partial.stat().st_size if partial.exists() else 0
        if total and offset > total:
            partial.unlink(missing_ok=True)
            offset = 0
        if total and offset == total:
            self._set(progress=80, downloaded_bytes=offset, message="正在校验已下载内容")
            return
        headers = {"Accept": "application/octet-stream", "User-Agent": f"{APP_NAME}/{APP_VERSION}"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        request = urllib.request.Request(str(asset["browser_download_url"]), headers=headers)
        with open_update_url(request, 30, route) as response:
            status = getattr(response, "status", None)
            if status is None and hasattr(response, "getcode"):
                status = response.getcode()
            resumed = bool(offset and status == 206)
            mode = "ab" if resumed else "wb"
            downloaded = offset if resumed else 0
            with partial.open(mode) as output:
                while True:
                    chunk = response.read(_DOWNLOAD_CHUNK_SIZE)
                    if not chunk:
                        break
                    output.write(chunk)
                    downloaded += len(chunk)
                    progress = min(80, int(downloaded * 80 / total)) if total else 0
                    self._set(progress=progress, downloaded_bytes=downloaded, message="正在下载插件更新包")
                output.flush()
                os.fsync(output.fileno())
        if total and downloaded != total:
            raise OSError(f"插件更新包下载不完整：{downloaded}/{total} 字节")

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(_DOWNLOAD_CHUNK_SIZE):
                digest.update(chunk)
        return digest.hexdigest().lower()


plugin_update_service = PluginUpdateService()

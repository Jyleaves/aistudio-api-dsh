"""GitHub Releases based desktop update service.

The desktop build updates from published release assets verified by SHA-256,
never from the source checkout. Existing browser and user-data directories are
intentionally not part of the incremental update asset.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path

from aistudio_api.infrastructure.update_network import (
    open_update_url,
    proxy_mode_label,
    resolve_update_network,
)
from aistudio_api.version import APP_NAME, APP_VERSION, GITHUB_RELEASES_URL

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_DOWNLOAD_CHUNK_SIZE = 1024 * 1024
_DOWNLOAD_ATTEMPTS = 3
_CACHE_RETENTION_SECONDS = 7 * 24 * 60 * 60
_CACHE_MAX_INSTALLERS = 2
_MIN_INCREMENTAL_VERSION = "1.0.0"
_LOGGER = logging.getLogger("aistudio.update")


def _version_tuple(value: str) -> tuple[int, ...]:
    raw = value.lstrip("vV").split("-")[0]
    result = []
    for part in raw.split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        result.append(int(digits or 0))
    return tuple(result or [0])


@dataclass
class UpdateState:
    status: str = "idle"
    current: str = APP_VERSION
    latest: str | None = None
    available: bool = False
    progress: int = 0
    message: str = ""
    error: str | None = None
    asset_name: str | None = None
    asset_size: int | None = None
    downloaded_bytes: int = 0
    resumed: bool = False
    proxy_mode: str = "direct"
    proxy_label: str = "直连"


class UpdateService:
    def __init__(
        self,
        current_version: str = APP_VERSION,
        update_root: str | Path | None = None,
        configured_proxy: str | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._current_version = current_version
        self._state = UpdateState(current=current_version)
        self._release: dict | None = None
        self._installer: Path | None = None
        self._configured_proxy = configured_proxy
        self._update_root = Path(update_root) if update_root is not None else Path(tempfile.gettempdir()) / "Asteria" / "updates"
        if getattr(sys, "frozen", False):
            self._cleanup_update_cache()

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

    def _open(self, request: urllib.request.Request, timeout: int):
        route = resolve_update_network(self._proxy_url())
        self._set(proxy_mode=route.proxy_mode, proxy_label=proxy_mode_label(route.proxy_mode))
        return open_update_url(request, timeout, route)

    def check(self) -> dict:
        if not getattr(sys, "frozen", False):
            self._set(status="source", message="开发环境不使用桌面版更新通道")
            return self.status()
        request = urllib.request.Request(
            GITHUB_RELEASES_URL,
            headers={"Accept": "application/vnd.github+json", "User-Agent": f"{APP_NAME}/{self._current_version}"},
        )
        try:
            with self._open(request, timeout=15) as response:
                release = json.loads(response.read().decode("utf-8"))
            tag = str(release.get("tag_name") or "")
            latest = tag.lstrip("vV")
            assets = release.get("assets") or []
            asset = next((item for item in assets if item.get("name") == f"Asteria-update-{latest}.exe"), None)
            checksum_asset = next(
                (item for item in assets if item.get("name") == f"Asteria-update-{latest}.exe.sha256"),
                None,
            )
            available = bool(latest and _version_tuple(latest) > _version_tuple(self._current_version))
            release_error = None
            if available and _version_tuple(self._current_version) < _version_tuple(_MIN_INCREMENTAL_VERSION):
                release_error = (
                    f"当前版本低于可直接更新的最低版本 {_MIN_INCREMENTAL_VERSION}，"
                    "请下载完整安装包"
                )
            elif available and not asset:
                release_error = "该版本缺少增量更新包，请下载安装包"
            elif available and not checksum_asset:
                release_error = "该版本缺少 SHA-256 校验文件，请下载安装包"
            self._release = release
            self._set(
                status="available" if available else "latest",
                latest=latest or None,
                available=available,
                progress=0,
                downloaded_bytes=0,
                resumed=False,
                message=(f"发现新版本 {latest}" if available else "当前已经是最新版本"),
                error=release_error,
                asset_name=(asset.get("name") if asset else None),
                asset_size=(asset.get("size") if asset else None),
            )
        except Exception as exc:
            _LOGGER.warning("Unable to check Asteria release: %s", exc)
            self._set(
                status="error",
                error="检查更新失败：无法连接 GitHub，请检查网络或代理设置",
                message="无法连接更新服务器",
            )
        return self.status()

    def start_download(self) -> dict:
        with self._lock:
            if self._state.status == "downloading":
                return asdict(self._state)
            if not self._release or not self._state.available:
                raise RuntimeError("没有可用更新")
            if self._state.error:
                raise RuntimeError(self._state.error)
            asset_name = self._state.asset_name
            assets = self._release.get("assets") or []
            asset = next((item for item in assets if item.get("name") == asset_name), None)
            if not asset:
                raise RuntimeError("未找到增量更新包")
            self._set(
                status="downloading",
                progress=0,
                downloaded_bytes=0,
                resumed=False,
                message="正在下载更新",
                error=None,
            )
        threading.Thread(target=self._download, args=(asset,), daemon=True, name="asteria-update").start()
        return self.status()

    def _download(self, asset: dict) -> None:
        self._update_root.mkdir(parents=True, exist_ok=True)
        target = self._update_root / str(asset["name"])
        partial = target.with_suffix(target.suffix + ".part")
        try:
            expected = self._expected_sha256(asset)
            if not expected:
                raise RuntimeError("更新包缺少 SHA-256 校验文件")

            if target.exists():
                if self._file_sha256(target) == expected.lower():
                    with self._lock:
                        self._installer = target
                    self._set(
                        status="ready",
                        progress=100,
                        downloaded_bytes=target.stat().st_size,
                        message="已使用校验通过的更新缓存",
                    )
                    return
                target.unlink(missing_ok=True)

            last_error: Exception | None = None
            for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
                try:
                    self._download_once(asset, partial)
                    if self._file_sha256(partial) != expected.lower():
                        partial.unlink(missing_ok=True)
                        raise RuntimeError("更新包校验失败")
                    os.replace(partial, target)
                    with self._lock:
                        self._installer = target
                    self._set(
                        status="ready",
                        progress=100,
                        downloaded_bytes=target.stat().st_size,
                        message="更新包已校验并准备好",
                    )
                    return
                except Exception as exc:
                    last_error = exc
                    if attempt >= _DOWNLOAD_ATTEMPTS:
                        break
                    delay = 2 ** (attempt - 1)
                    self._set(message=f"下载中断，{delay} 秒后重试（{attempt + 1}/{_DOWNLOAD_ATTEMPTS}）")
                    time.sleep(delay)
            raise last_error or RuntimeError("更新下载失败")
        except Exception as exc:
            self._set(status="error", error=str(exc), message="更新下载失败", progress=0)

    def _download_once(self, asset: dict, partial: Path) -> None:
        total = int(asset.get("size") or 0)
        offset = partial.stat().st_size if partial.exists() else 0
        if total and offset > total:
            partial.unlink(missing_ok=True)
            offset = 0
        if total and offset == total:
            self._set(progress=99, downloaded_bytes=offset, resumed=offset > 0)
            return

        headers = {
            "Accept": "application/octet-stream",
            "User-Agent": f"{APP_NAME}/{self._current_version}",
        }
        if offset:
            headers["Range"] = f"bytes={offset}-"
        request = urllib.request.Request(str(asset["browser_download_url"]), headers=headers)

        with self._open(request, timeout=60) as response:
            status = getattr(response, "status", None)
            if status is None and hasattr(response, "getcode"):
                status = response.getcode()
            resumed = bool(offset and status == 206)
            mode = "ab" if resumed else "wb"
            downloaded = offset if resumed else 0
            self._set(resumed=resumed, downloaded_bytes=downloaded)
            with partial.open(mode) as output:
                while True:
                    chunk = response.read(_DOWNLOAD_CHUNK_SIZE)
                    if not chunk:
                        break
                    output.write(chunk)
                    downloaded += len(chunk)
                    progress = min(99, int(downloaded * 100 / total)) if total else 0
                    prefix = "正在断点续传" if resumed else "正在下载更新"
                    self._set(
                        progress=progress,
                        downloaded_bytes=downloaded,
                        message=prefix,
                    )
                output.flush()
                os.fsync(output.fileno())
        if total and downloaded != total:
            raise OSError(f"更新包下载不完整：{downloaded}/{total} 字节")

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(_DOWNLOAD_CHUNK_SIZE):
                digest.update(chunk)
        return digest.hexdigest().lower()

    def _cleanup_update_cache(self) -> None:
        if not self._update_root.exists():
            return
        now = time.time()
        current_name = f"Asteria-update-{self._current_version}.exe"
        try:
            for partial in self._update_root.glob("Asteria-update-*.exe.part"):
                if now - partial.stat().st_mtime > _CACHE_RETENTION_SECONDS:
                    self._safe_unlink(partial)

            retained: list[Path] = []
            for installer in self._update_root.glob("Asteria-update-*.exe"):
                expired = now - installer.stat().st_mtime > _CACHE_RETENTION_SECONDS
                if installer.name == current_name or expired:
                    self._safe_unlink(installer)
                elif installer.exists():
                    retained.append(installer)
            retained.sort(key=lambda item: item.stat().st_mtime, reverse=True)
            for installer in retained[_CACHE_MAX_INSTALLERS:]:
                self._safe_unlink(installer)
            if not any(self._update_root.iterdir()):
                self._update_root.rmdir()
        except OSError as exc:
            _LOGGER.warning("Unable to clean desktop update cache: %s", exc)

    @staticmethod
    def _safe_unlink(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            _LOGGER.info("Deferred update cache cleanup for %s: %s", path, exc)

    def _expected_sha256(self, asset: dict) -> str | None:
        if not self._release:
            return None
        checksum_name = f"{asset['name']}.sha256"
        checksum_asset = next((item for item in self._release.get("assets", []) if item.get("name") == checksum_name), None)
        if not checksum_asset:
            return None
        request = urllib.request.Request(
            str(checksum_asset["browser_download_url"]),
            headers={"User-Agent": f"{APP_NAME}/{self._current_version}"},
        )
        with self._open(request, timeout=15) as response:
            text = response.read().decode("utf-8", errors="replace")
        return text.strip().split()[0] if text.strip() else None

    def install(self) -> dict:
        with self._lock:
            installer = self._installer
        if not installer or not installer.exists():
            raise RuntimeError("更新包尚未下载完成")
        if not getattr(sys, "frozen", False):
            raise RuntimeError("开发环境不能执行桌面版更新")
        install_dir = Path(sys.executable).resolve().parent
        subprocess.Popen(
            [str(installer), "/VERYSILENT", "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS", f"/DIR={install_dir}"],
            cwd=str(install_dir),
            creationflags=_NO_WINDOW,
        )
        self._set(status="installing", message="正在安装更新，应用即将重启")
        return self.status()


update_service = UpdateService()

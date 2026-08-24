"""GitHub Releases based desktop update service.

The desktop build updates from signed release assets, never from the source
checkout. Existing browser and user-data directories are intentionally not
part of the incremental update asset.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path

from aistudio_api.version import APP_NAME, APP_VERSION, GITHUB_RELEASES_URL

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


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


class UpdateService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state = UpdateState()
        self._release: dict | None = None
        self._installer: Path | None = None

    def status(self) -> dict:
        with self._lock:
            return asdict(self._state)

    def _set(self, **values: object) -> None:
        with self._lock:
            for key, value in values.items():
                setattr(self._state, key, value)

    def check(self) -> dict:
        if not getattr(sys, "frozen", False):
            self._set(status="source", message="开发环境不使用桌面版更新通道")
            return self.status()
        request = urllib.request.Request(
            GITHUB_RELEASES_URL,
            headers={"Accept": "application/vnd.github+json", "User-Agent": f"{APP_NAME}/{APP_VERSION}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                release = json.loads(response.read().decode("utf-8"))
            tag = str(release.get("tag_name") or "")
            latest = tag.lstrip("vV")
            assets = release.get("assets") or []
            asset = next((item for item in assets if item.get("name") == f"Asteria-update-{latest}.exe"), None)
            available = bool(latest and _version_tuple(latest) > _version_tuple(APP_VERSION))
            self._release = release
            self._set(
                status="available" if available else "latest",
                latest=latest or None,
                available=available,
                progress=0,
                message=(f"发现新版本 {latest}" if available else "当前已经是最新版本"),
                error=(None if asset or not available else "该版本缺少增量更新包，请下载安装包"),
                asset_name=(asset.get("name") if asset else None),
                asset_size=(asset.get("size") if asset else None),
            )
        except Exception as exc:
            self._set(status="error", error=f"检查更新失败：{exc}", message="无法连接更新服务器")
        return self.status()

    def start_download(self) -> dict:
        with self._lock:
            if self._state.status == "downloading":
                return asdict(self._state)
            if not self._release or not self._state.available:
                raise RuntimeError("没有可用更新")
            asset_name = self._state.asset_name
            assets = self._release.get("assets") or []
            asset = next((item for item in assets if item.get("name") == asset_name), None)
            if not asset:
                raise RuntimeError("未找到增量更新包")
            self._set(status="downloading", progress=0, message="正在下载更新", error=None)
        threading.Thread(target=self._download, args=(asset,), daemon=True, name="asteria-update").start()
        return self.status()

    def _download(self, asset: dict) -> None:
        temp_root = Path(tempfile.gettempdir()) / "Asteria" / "updates"
        temp_root.mkdir(parents=True, exist_ok=True)
        target = temp_root / str(asset["name"])
        try:
            request = urllib.request.Request(
                str(asset["browser_download_url"]),
                headers={"Accept": "application/octet-stream", "User-Agent": f"{APP_NAME}/{APP_VERSION}"},
            )
            total = int(asset.get("size") or 0)
            downloaded = 0
            digest = hashlib.sha256()
            with urllib.request.urlopen(request, timeout=60) as response, target.open("wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    digest.update(chunk)
                    downloaded += len(chunk)
                    progress = min(99, int(downloaded * 100 / total)) if total else 0
                    self._set(progress=progress, message=f"正在下载更新 {progress}%")
            expected = self._expected_sha256(asset)
            if expected and digest.hexdigest().lower() != expected.lower():
                target.unlink(missing_ok=True)
                raise RuntimeError("更新包校验失败")
            with self._lock:
                self._installer = target
            self._set(status="ready", progress=100, message="更新包已准备好")
        except Exception as exc:
            target.unlink(missing_ok=True)
            self._set(status="error", error=str(exc), message="更新下载失败", progress=0)

    def _expected_sha256(self, asset: dict) -> str | None:
        if not self._release:
            return None
        checksum_name = f"{asset['name']}.sha256"
        checksum_asset = next((item for item in self._release.get("assets", []) if item.get("name") == checksum_name), None)
        if not checksum_asset:
            return None
        request = urllib.request.Request(
            str(checksum_asset["browser_download_url"]),
            headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
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

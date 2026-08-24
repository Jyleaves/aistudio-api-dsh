"""Regression tests for the GitHub Releases desktop update channel."""

import hashlib
import json
import os
import time

from aistudio_api.infrastructure import update_service as update_module
from aistudio_api.infrastructure.update_service import UpdateService


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


class _BytesResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.offset = 0
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        if size < 0:
            size = len(self.payload) - self.offset
        chunk = self.payload[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


class _InterruptedResponse(_BytesResponse):
    def __init__(self, payload):
        super().__init__(payload)
        self.failed = False

    def read(self, size=-1):
        if self.failed:
            raise OSError("connection reset")
        self.failed = True
        return super().read(size)


def test_source_checkout_never_checks_git_for_desktop_updates(monkeypatch):
    monkeypatch.delattr(update_module.sys, "frozen", raising=False)
    service = UpdateService()
    result = service.check()
    assert result["status"] == "source"
    assert result["available"] is False


def test_frozen_update_check_selects_small_incremental_asset(monkeypatch, tmp_path):
    monkeypatch.setattr(update_module.sys, "frozen", True, raising=False)
    release = {
        "tag_name": "v1.0.1",
        "assets": [
            {"name": "Asteria-setup-1.0.1.exe", "size": 200_000_000},
            {"name": "Asteria-update-1.0.1.exe", "size": 25_000_000},
            {"name": "Asteria-update-1.0.1.exe.sha256", "size": 96},
        ],
    }
    monkeypatch.setattr(update_module.urllib.request, "urlopen", lambda *args, **kwargs: _Response(release))
    result = UpdateService(current_version="1.0.0", update_root=tmp_path).check()
    assert result["available"] is True
    assert result["current"] == "1.0.0"
    assert result["latest"] == "1.0.1"
    assert result["asset_name"] == "Asteria-update-1.0.1.exe"
    assert result["asset_size"] == 25_000_000


def test_frozen_update_check_reports_missing_incremental_asset(monkeypatch, tmp_path):
    monkeypatch.setattr(update_module.sys, "frozen", True, raising=False)
    release = {"tag_name": "v1.0.1", "assets": [{"name": "Asteria-setup-1.0.1.exe"}]}
    monkeypatch.setattr(update_module.urllib.request, "urlopen", lambda *args, **kwargs: _Response(release))
    result = UpdateService(current_version="1.0.0", update_root=tmp_path).check()
    assert result["available"] is True
    assert "缺少增量更新包" in result["error"]


def test_frozen_update_check_requires_checksum_asset(monkeypatch, tmp_path):
    monkeypatch.setattr(update_module.sys, "frozen", True, raising=False)
    release = {
        "tag_name": "v1.0.1",
        "assets": [{"name": "Asteria-update-1.0.1.exe", "size": 25_000_000}],
    }
    monkeypatch.setattr(update_module.urllib.request, "urlopen", lambda *args, **kwargs: _Response(release))
    result = UpdateService(current_version="1.0.0", update_root=tmp_path).check()
    assert result["available"] is True
    assert "SHA-256" in result["error"]


def test_version_below_supported_baseline_requires_full_installer(monkeypatch, tmp_path):
    monkeypatch.setattr(update_module.sys, "frozen", True, raising=False)
    release = {
        "tag_name": "v1.0.2",
        "assets": [
            {"name": "Asteria-update-1.0.2.exe", "size": 25_000_000},
            {"name": "Asteria-update-1.0.2.exe.sha256", "size": 96},
        ],
    }
    monkeypatch.setattr(update_module.urllib.request, "urlopen", lambda *args, **kwargs: _Response(release))

    result = UpdateService(current_version="0.9.9", update_root=tmp_path).check()

    assert result["available"] is True
    assert "最低版本 1.0.0" in result["error"]
    assert "完整安装包" in result["error"]


def test_incremental_update_download_is_verified_before_install(monkeypatch, tmp_path):
    payload = b"asteria-1.0.1-update"
    digest = hashlib.sha256(payload).hexdigest()
    installer_asset = {
        "name": "Asteria-update-1.0.1.exe",
        "size": len(payload),
        "browser_download_url": "https://example.invalid/update.exe",
    }
    checksum_asset = {
        "name": "Asteria-update-1.0.1.exe.sha256",
        "browser_download_url": "https://example.invalid/update.exe.sha256",
    }

    def fake_urlopen(request, **_kwargs):
        if request.full_url.endswith(".sha256"):
            return _BytesResponse(f"{digest} *Asteria-update-1.0.1.exe\n".encode())
        return _BytesResponse(payload)

    monkeypatch.setattr(update_module.urllib.request, "urlopen", fake_urlopen)
    service = UpdateService(current_version="1.0.0", update_root=tmp_path)
    service._release = {"assets": [installer_asset, checksum_asset]}
    service._download(installer_asset)

    result = service.status()
    assert result["status"] == "ready"
    assert result["progress"] == 100
    assert result["downloaded_bytes"] == len(payload)
    assert service._installer.read_bytes() == payload
    assert not (tmp_path / "Asteria-update-1.0.1.exe.part").exists()


def test_interrupted_download_resumes_from_partial_file(monkeypatch, tmp_path):
    payload = b"asteria-resumable-update"
    digest = hashlib.sha256(payload).hexdigest()
    installer_asset = {
        "name": "Asteria-update-1.0.2.exe",
        "size": len(payload),
        "browser_download_url": "https://example.invalid/update.exe",
    }
    checksum_asset = {
        "name": "Asteria-update-1.0.2.exe.sha256",
        "browser_download_url": "https://example.invalid/update.exe.sha256",
    }
    installer_calls = 0
    ranges = []

    def fake_urlopen(request, **_kwargs):
        nonlocal installer_calls
        if request.full_url.endswith(".sha256"):
            return _BytesResponse(f"{digest} *Asteria-update-1.0.2.exe\n".encode())
        ranges.append(request.get_header("Range"))
        installer_calls += 1
        if installer_calls == 1:
            return _InterruptedResponse(payload[:8])
        return _BytesResponse(payload[8:], status=206)

    monkeypatch.setattr(update_module.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(update_module.time, "sleep", lambda _seconds: None)
    service = UpdateService(current_version="1.0.1", update_root=tmp_path)
    service._release = {"assets": [installer_asset, checksum_asset]}
    service._download(installer_asset)

    assert service.status()["status"] == "ready"
    assert service.status()["resumed"] is True
    assert ranges == [None, "bytes=8-"]
    assert service._installer.read_bytes() == payload


def test_range_ignored_by_server_restarts_partial_download(monkeypatch, tmp_path):
    payload = b"complete-update"
    digest = hashlib.sha256(payload).hexdigest()
    installer_asset = {
        "name": "Asteria-update-1.0.2.exe",
        "size": len(payload),
        "browser_download_url": "https://example.invalid/update.exe",
    }
    checksum_asset = {
        "name": "Asteria-update-1.0.2.exe.sha256",
        "browser_download_url": "https://example.invalid/update.exe.sha256",
    }
    partial = tmp_path / "Asteria-update-1.0.2.exe.part"
    partial.write_bytes(b"stale")

    def fake_urlopen(request, **_kwargs):
        if request.full_url.endswith(".sha256"):
            return _BytesResponse(f"{digest} *Asteria-update-1.0.2.exe\n".encode())
        assert request.get_header("Range") == "bytes=5-"
        return _BytesResponse(payload, status=200)

    monkeypatch.setattr(update_module.urllib.request, "urlopen", fake_urlopen)
    service = UpdateService(current_version="1.0.1", update_root=tmp_path)
    service._release = {"assets": [installer_asset, checksum_asset]}
    service._download(installer_asset)

    assert service.status()["status"] == "ready"
    assert service.status()["resumed"] is False
    assert service._installer.read_bytes() == payload


def test_checksum_failure_removes_untrusted_download(monkeypatch, tmp_path):
    payload = b"tampered-update"
    installer_asset = {
        "name": "Asteria-update-1.0.2.exe",
        "size": len(payload),
        "browser_download_url": "https://example.invalid/update.exe",
    }
    checksum_asset = {
        "name": "Asteria-update-1.0.2.exe.sha256",
        "browser_download_url": "https://example.invalid/update.exe.sha256",
    }

    def fake_urlopen(request, **_kwargs):
        if request.full_url.endswith(".sha256"):
            return _BytesResponse(f"{'0' * 64} *Asteria-update-1.0.2.exe\n".encode())
        return _BytesResponse(payload)

    monkeypatch.setattr(update_module.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(update_module.time, "sleep", lambda _seconds: None)
    service = UpdateService(current_version="1.0.1", update_root=tmp_path)
    service._release = {"assets": [installer_asset, checksum_asset]}
    service._download(installer_asset)

    assert service.status()["status"] == "error"
    assert "校验失败" in service.status()["error"]
    assert not (tmp_path / installer_asset["name"]).exists()
    assert not (tmp_path / f"{installer_asset['name']}.part").exists()


def test_frozen_startup_cleans_stale_update_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(update_module.sys, "frozen", True, raising=False)
    now = time.time()
    current = tmp_path / "Asteria-update-1.0.2.exe"
    stale_partial = tmp_path / "Asteria-update-1.0.3.exe.part"
    current.write_bytes(b"installed")
    stale_partial.write_bytes(b"old-partial")
    os.utime(stale_partial, (now - update_module._CACHE_RETENTION_SECONDS - 1,) * 2)
    candidates = []
    for index, version in enumerate(("1.0.3", "1.0.4", "1.0.5", "1.0.6")):
        candidate = tmp_path / f"Asteria-update-{version}.exe"
        candidate.write_bytes(version.encode())
        os.utime(candidate, (now - index,) * 2)
        candidates.append(candidate)
    unrelated = tmp_path / "keep-me.txt"
    unrelated.write_text("unrelated", encoding="utf-8")

    UpdateService(current_version="1.0.2", update_root=tmp_path)

    assert not current.exists()
    assert not stale_partial.exists()
    assert candidates[0].exists()
    assert candidates[1].exists()
    assert not candidates[2].exists()
    assert not candidates[3].exists()
    assert unrelated.exists()


def test_old_release_is_not_reported_as_update(monkeypatch, tmp_path):
    monkeypatch.setattr(update_module.sys, "frozen", True, raising=False)
    release = {"tag_name": "v0.9.9", "assets": []}
    monkeypatch.setattr(update_module.urllib.request, "urlopen", lambda *args, **kwargs: _Response(release))
    result = UpdateService(update_root=tmp_path).check()
    assert result["available"] is False
    assert result["status"] == "latest"


def test_install_launches_incremental_installer_next_to_current_exe(monkeypatch, tmp_path):
    monkeypatch.setattr(update_module.sys, "frozen", True, raising=False)
    exe_dir = tmp_path / "Asteria"
    exe_dir.mkdir()
    monkeypatch.setattr(update_module.sys, "executable", str(exe_dir / "aistudio-api.exe"))
    installer = tmp_path / "Asteria-update-1.0.1.exe"
    installer.write_bytes(b"installer")
    launched = {}

    def fake_popen(args, **kwargs):
        launched["args"] = args
        launched["kwargs"] = kwargs

    monkeypatch.setattr(update_module.subprocess, "Popen", fake_popen)
    service = UpdateService(update_root=tmp_path / "updates")
    service._installer = installer
    result = service.install()
    assert result["status"] == "installing"
    assert launched["args"][0] == str(installer)
    assert "/CLOSEAPPLICATIONS" in launched["args"]
    assert f"/DIR={exe_dir}" in launched["args"]

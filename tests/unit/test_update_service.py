"""Regression tests for the GitHub Releases desktop update channel."""

import hashlib
import json

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
    def __init__(self, payload):
        self.payload = payload
        self.offset = 0

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


def test_source_checkout_never_checks_git_for_desktop_updates(monkeypatch):
    monkeypatch.delattr(update_module.sys, "frozen", raising=False)
    service = UpdateService()
    result = service.check()
    assert result["status"] == "source"
    assert result["available"] is False


def test_frozen_update_check_selects_small_incremental_asset(monkeypatch):
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
    result = UpdateService(current_version="1.0.0").check()
    assert result["available"] is True
    assert result["current"] == "1.0.0"
    assert result["latest"] == "1.0.1"
    assert result["asset_name"] == "Asteria-update-1.0.1.exe"
    assert result["asset_size"] == 25_000_000


def test_frozen_update_check_reports_missing_incremental_asset(monkeypatch):
    monkeypatch.setattr(update_module.sys, "frozen", True, raising=False)
    release = {"tag_name": "v1.0.1", "assets": [{"name": "Asteria-setup-1.0.1.exe"}]}
    monkeypatch.setattr(update_module.urllib.request, "urlopen", lambda *args, **kwargs: _Response(release))
    result = UpdateService(current_version="1.0.0").check()
    assert result["available"] is True
    assert "缺少增量更新包" in result["error"]


def test_frozen_update_check_requires_checksum_asset(monkeypatch):
    monkeypatch.setattr(update_module.sys, "frozen", True, raising=False)
    release = {
        "tag_name": "v1.0.1",
        "assets": [{"name": "Asteria-update-1.0.1.exe", "size": 25_000_000}],
    }
    monkeypatch.setattr(update_module.urllib.request, "urlopen", lambda *args, **kwargs: _Response(release))
    result = UpdateService(current_version="1.0.0").check()
    assert result["available"] is True
    assert "SHA-256" in result["error"]


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

    monkeypatch.setattr(update_module.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(update_module.urllib.request, "urlopen", fake_urlopen)
    service = UpdateService(current_version="1.0.0")
    service._release = {"assets": [installer_asset, checksum_asset]}
    service._download(installer_asset)

    result = service.status()
    assert result["status"] == "ready"
    assert result["progress"] == 100
    assert service._installer.read_bytes() == payload


def test_old_release_is_not_reported_as_update(monkeypatch):
    monkeypatch.setattr(update_module.sys, "frozen", True, raising=False)
    release = {"tag_name": "v0.9.9", "assets": []}
    monkeypatch.setattr(update_module.urllib.request, "urlopen", lambda *args, **kwargs: _Response(release))
    result = UpdateService().check()
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
    service = UpdateService()
    service._installer = installer
    result = service.install()
    assert result["status"] == "installing"
    assert launched["args"][0] == str(installer)
    assert "/CLOSEAPPLICATIONS" in launched["args"]
    assert f"/DIR={exe_dir}" in launched["args"]

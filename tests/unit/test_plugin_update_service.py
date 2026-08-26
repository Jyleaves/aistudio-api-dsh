"""Regression tests for the dsh-gemini-aistudio updater."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
from types import SimpleNamespace

from aistudio_api.infrastructure import plugin_update_service as plugin_module
from aistudio_api.infrastructure.plugin_update_service import PluginUpdateService


class _Response:
    def __init__(self, payload: bytes, *, status: int = 200):
        self._stream = io.BytesIO(payload)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size: int = -1):
        return self._stream.read(size)

    def getcode(self):
        return self.status


def _install_plugin(root, version="0.1.6", specifier="github:Jyleaves/dsh-gemini-aistudio#v0.1.6"):
    profile = root / "profiles" / "web"
    package_dir = profile / "node_modules" / "dsh-gemini-aistudio"
    package_dir.mkdir(parents=True)
    (package_dir / "package.json").write_text(json.dumps({"version": version}), encoding="utf-8")
    (profile / "package.json").write_text(
        json.dumps({"dependencies": {"dsh-gemini-aistudio": specifier}}),
        encoding="utf-8",
    )
    return package_dir / "package.json"


def _release(version: str, package_bytes: bytes, *, checksum: str | None = None, include_package: bool = True):
    package_name = f"dsh-gemini-aistudio-{version}.tgz"
    checksum = checksum or hashlib.sha256(package_bytes).hexdigest()
    assets = []
    if include_package:
        assets.append({
            "name": package_name,
            "size": len(package_bytes),
            "browser_download_url": "https://downloads.test/plugin.tgz",
        })
        assets.append({
            "name": f"{package_name}.sha256",
            "size": 64 + len(package_name) + 3,
            "browser_download_url": "https://downloads.test/plugin.tgz.sha256",
        })
    return {"tag_name": f"v{version}", "assets": assets}, checksum


def _mock_release(monkeypatch, package_bytes=b"npm-package", version="0.1.7", **release_options):
    release, checksum = _release(version, package_bytes, **release_options)

    def fake_open(request, _timeout, _route):
        url = request.full_url
        if url == plugin_module.PLUGIN_RELEASES_URL:
            return _Response(json.dumps(release).encode("utf-8"))
        if url.endswith(".sha256"):
            name = f"dsh-gemini-aistudio-{version}.tgz"
            return _Response(f"{checksum}  {name}\n".encode("utf-8"))
        if url.endswith("plugin.tgz"):
            return _Response(package_bytes)
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(plugin_module, "open_update_url", fake_open)


def _service(tmp_path, **kwargs):
    return PluginUpdateService(
        dsh_home=tmp_path,
        update_root=tmp_path / "updates",
        log_path=tmp_path / "plugin-update.log",
        dsh_running_probe=lambda: False,
        **kwargs,
    )


def test_check_finds_release_package_and_checksum(monkeypatch, tmp_path):
    _install_plugin(tmp_path)
    package_bytes = b"npm-package"
    _mock_release(monkeypatch, package_bytes)

    result = _service(tmp_path, configured_proxy="http://127.0.0.1:7890").check()

    assert result["installed"] is True
    assert result["managed"] is True
    assert result["current"] == "0.1.6"
    assert result["latest"] == "0.1.7"
    assert result["available"] is True
    assert result["asset_name"] == "dsh-gemini-aistudio-0.1.7.tgz"
    assert result["asset_size"] == len(package_bytes)
    assert result["proxy_mode"] == "asteria"
    assert result["error"] is None


def test_check_rejects_release_without_verified_package_assets(monkeypatch, tmp_path):
    _install_plugin(tmp_path)
    _mock_release(monkeypatch, include_package=False)

    result = _service(tmp_path).check()

    assert result["status"] == "available"
    assert "缺少插件更新包" in result["error"]


def test_local_development_link_is_never_overwritten(monkeypatch, tmp_path):
    _install_plugin(tmp_path, version="0.1.6", specifier="link:E:/Project/dsh-gemini-aistudio")
    _mock_release(monkeypatch)
    result = _service(tmp_path).check()

    assert result["status"] == "development"
    assert result["managed"] is False
    assert result["available"] is False


def test_verified_cached_file_dependency_remains_managed(monkeypatch, tmp_path):
    cached = tmp_path / "updates" / "dsh-gemini-aistudio-0.1.6.tgz"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"old-package")
    _install_plugin(tmp_path, specifier=f"file:{cached.as_posix()}")
    _mock_release(monkeypatch)

    result = _service(tmp_path).check()

    assert result["managed"] is True
    assert result["available"] is True


def test_user_owned_file_dependency_is_never_overwritten(monkeypatch, tmp_path):
    source = tmp_path / "source" / "dsh-gemini-aistudio.tgz"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"development-package")
    _install_plugin(tmp_path, specifier=f"file:{source.as_posix()}")
    _mock_release(monkeypatch)

    result = _service(tmp_path).check()

    assert result["status"] == "development"
    assert result["managed"] is False


def test_update_downloads_verifies_and_installs_a_local_package(monkeypatch, tmp_path):
    package_path = _install_plugin(tmp_path)
    modules_file = package_path.parent.parent / ".modules.yaml"
    modules_file.write_text(
        json.dumps({"storeDir": str(tmp_path / "original-store" / "v11")}),
        encoding="utf-8",
    )
    package_bytes = b"verified-npm-package"
    _mock_release(monkeypatch, package_bytes)
    service = _service(tmp_path, configured_proxy="http://127.0.0.1:7890")
    assert service.check()["available"] is True
    captured = {}
    monkeypatch.setattr(plugin_module.shutil, "which", lambda name: r"C:\Tools\dsh.cmd" if name == "dsh.cmd" else None)

    def fake_run(arguments, **kwargs):
        captured["arguments"] = arguments
        captured["kwargs"] = kwargs
        package_path.write_text(json.dumps({"version": "0.1.7"}), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="installed", stderr="")

    monkeypatch.setattr(plugin_module.subprocess, "run", fake_run)
    service._update("0.1.7")

    result = service.status()
    assert result["status"] == "updated"
    assert result["current"] == "0.1.7"
    assert result["progress"] == 100
    assert result["restart_required"] is True
    local_package = next(value for value in captured["arguments"] if str(value).endswith(".tgz"))
    assert local_package == (
        "dsh-gemini-aistudio@file:"
        + (tmp_path / "updates" / "dsh-gemini-aistudio-0.1.7.tgz").as_posix()
    )
    assert not any(str(value).startswith("https://") for value in captured["arguments"])
    assert captured["kwargs"]["env"]["HTTPS_PROXY"] == "http://127.0.0.1:7890"
    assert captured["kwargs"]["env"]["PNPM_CONFIG_STORE_DIR"] == str(tmp_path / "original-store")
    assert "exited with code 0" in (tmp_path / "plugin-update.log").read_text(encoding="utf-8")


def test_pnpm_store_root_supports_legacy_yaml_modules_file(tmp_path):
    package_path = _install_plugin(tmp_path)
    modules_file = package_path.parent.parent / ".modules.yaml"
    modules_file.write_text(
        f"storeDir: '{(tmp_path / 'legacy-store' / 'v10').as_posix()}'\n",
        encoding="utf-8",
    )

    assert _service(tmp_path)._pnpm_store_root() == tmp_path / "legacy-store"


def test_verified_download_waits_for_dsh_to_close_and_reuses_cache(monkeypatch, tmp_path):
    package_path = _install_plugin(tmp_path)
    package_bytes = b"cached-package"
    calls = {"downloads": 0, "installs": 0}
    release, checksum = _release("0.1.7", package_bytes)

    def fake_open(request, _timeout, _route):
        if request.full_url == plugin_module.PLUGIN_RELEASES_URL:
            return _Response(json.dumps(release).encode("utf-8"))
        if request.full_url.endswith(".sha256"):
            return _Response(f"{checksum}  plugin.tgz\n".encode("utf-8"))
        calls["downloads"] += 1
        return _Response(package_bytes)

    running = {"value": True}
    monkeypatch.setattr(plugin_module, "open_update_url", fake_open)
    monkeypatch.setattr(plugin_module.shutil, "which", lambda _name: r"C:\Tools\dsh.cmd")
    service = PluginUpdateService(
        dsh_home=tmp_path,
        update_root=tmp_path / "updates",
        log_path=tmp_path / "plugin-update.log",
        dsh_running_probe=lambda: running["value"],
    )
    service.check()

    def fake_run(*_args, **_kwargs):
        calls["installs"] += 1
        package_path.write_text(json.dumps({"version": "0.1.7"}), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(plugin_module.subprocess, "run", fake_run)
    service._update("0.1.7")
    assert service.status()["status"] == "blocked"
    assert calls == {"downloads": 1, "installs": 0}

    running["value"] = False
    service._update("0.1.7")
    assert service.status()["status"] == "updated"
    assert calls == {"downloads": 1, "installs": 1}


def test_checksum_failure_never_invokes_dsh(monkeypatch, tmp_path):
    _install_plugin(tmp_path)
    _mock_release(monkeypatch, b"corrupt-package", checksum="0" * 64)
    monkeypatch.setattr(plugin_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(plugin_module.shutil, "which", lambda _name: r"C:\Tools\dsh.cmd")
    monkeypatch.setattr(plugin_module.subprocess, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not install")))
    service = _service(tmp_path)
    service.check()
    service._update("0.1.7")

    result = service.status()
    assert result["status"] == "error"
    assert "SHA-256 校验失败" in result["error"]
    assert not (tmp_path / "updates" / "dsh-gemini-aistudio-0.1.7.tgz").exists()


def test_update_retries_an_interrupted_package_download(monkeypatch, tmp_path):
    package_path = _install_plugin(tmp_path)
    package_bytes = b"retry-package"
    release, checksum = _release("0.1.7", package_bytes)
    attempts = {"package": 0}

    def fake_open(request, _timeout, _route):
        if request.full_url == plugin_module.PLUGIN_RELEASES_URL:
            return _Response(json.dumps(release).encode("utf-8"))
        if request.full_url.endswith(".sha256"):
            return _Response(f"{checksum}  plugin.tgz\n".encode("utf-8"))
        attempts["package"] += 1
        if attempts["package"] == 1:
            raise OSError("connection reset")
        return _Response(package_bytes)

    monkeypatch.setattr(plugin_module, "open_update_url", fake_open)
    monkeypatch.setattr(plugin_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(plugin_module.shutil, "which", lambda _name: r"C:\Tools\dsh.cmd")

    def fake_run(*_args, **_kwargs):
        package_path.write_text(json.dumps({"version": "0.1.7"}), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(plugin_module.subprocess, "run", fake_run)
    service = _service(tmp_path)
    service.check()
    service._update("0.1.7")

    assert service.status()["status"] == "updated"
    assert attempts["package"] == 2


def test_update_reports_windows_file_lock_help(monkeypatch, tmp_path):
    _install_plugin(tmp_path)
    _mock_release(monkeypatch)
    service = _service(tmp_path)
    service.check()
    monkeypatch.setattr(plugin_module.shutil, "which", lambda _name: r"C:\Tools\dsh.cmd")
    monkeypatch.setattr(
        plugin_module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout="", stderr="EPERM: operation not permitted"),
    )
    service._update("0.1.7")

    result = service.status()
    assert result["status"] == "error"
    assert "关闭正在运行的 dsh" in result["error"]


def test_update_error_includes_pnpm_stdout_before_wrapper_stderr(monkeypatch, tmp_path):
    _install_plugin(tmp_path)
    _mock_release(monkeypatch)
    service = _service(tmp_path)
    service.check()
    monkeypatch.setattr(plugin_module.shutil, "which", lambda _name: r"C:\Tools\dsh.cmd")
    monkeypatch.setattr(
        plugin_module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout="ERR_PNPM_UNEXPECTED_STORE: Unexpected store location",
            stderr="dsh: pnpm failed in profile directory",
        ),
    )
    service._update("0.1.7")

    assert "ERR_PNPM_UNEXPECTED_STORE" in service.status()["error"]
    assert "dsh: pnpm failed" in service.status()["error"]


def test_update_reports_install_timeout_and_log_path(monkeypatch, tmp_path):
    _install_plugin(tmp_path)
    _mock_release(monkeypatch)
    service = _service(tmp_path)
    service.check()
    monkeypatch.setattr(plugin_module.shutil, "which", lambda _name: r"C:\Tools\dsh.cmd")
    monkeypatch.setattr(
        plugin_module.subprocess,
        "run",
        lambda *args, **_kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired(args[0], 120)),
    )
    service._update("0.1.7")

    result = service.status()
    assert result["status"] == "error"
    assert "安装超时" in result["error"]
    assert result["log_path"] == str(tmp_path / "plugin-update.log")
    assert "timed out" in (tmp_path / "plugin-update.log").read_text(encoding="utf-8")


def test_start_update_rejects_a_local_development_link(monkeypatch, tmp_path):
    _install_plugin(tmp_path, specifier="link:E:/Project/dsh-gemini-aistudio")
    _mock_release(monkeypatch)
    service = _service(tmp_path)
    service.check()

    try:
        service.start_update()
    except RuntimeError as exc:
        assert "本地开发链接" in str(exc)
    else:
        raise AssertionError("local development link must not be overwritten")

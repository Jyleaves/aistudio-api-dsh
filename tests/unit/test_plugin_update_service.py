"""Regression tests for the dsh-gemini-aistudio updater."""

import json
from types import SimpleNamespace

from aistudio_api.infrastructure import plugin_update_service as plugin_module
from aistudio_api.infrastructure.plugin_update_service import PluginUpdateService


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


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


def test_check_finds_new_plugin_release(monkeypatch, tmp_path):
    _install_plugin(tmp_path)
    monkeypatch.setattr(
        plugin_module,
        "open_update_url",
        lambda *_args, **_kwargs: _Response({"tag_name": "v0.1.7"}),
    )

    result = PluginUpdateService(dsh_home=tmp_path, configured_proxy="http://127.0.0.1:7890").check()

    assert result["installed"] is True
    assert result["managed"] is True
    assert result["current"] == "0.1.6"
    assert result["latest"] == "0.1.7"
    assert result["available"] is True
    assert result["proxy_mode"] == "asteria"


def test_local_development_link_is_never_overwritten(monkeypatch, tmp_path):
    _install_plugin(tmp_path, version="0.1.6", specifier="link:E:/Project/dsh-gemini-aistudio")
    monkeypatch.setattr(
        plugin_module,
        "open_update_url",
        lambda *_args, **_kwargs: _Response({"tag_name": "v0.1.7"}),
    )
    service = PluginUpdateService(dsh_home=tmp_path)

    result = service.check()

    assert result["status"] == "development"
    assert result["managed"] is False
    assert result["available"] is False


def test_update_uses_dsh_cli_release_tag_and_proxy(monkeypatch, tmp_path):
    package_path = _install_plugin(tmp_path)
    service = PluginUpdateService(dsh_home=tmp_path, configured_proxy="http://127.0.0.1:7890")
    captured = {}
    monkeypatch.setattr(plugin_module.shutil, "which", lambda name: r"C:\Tools\dsh.cmd" if name == "dsh.cmd" else None)

    def fake_run(arguments, **kwargs):
        captured["arguments"] = arguments
        captured["kwargs"] = kwargs
        package_path.write_text(json.dumps({"version": "0.1.7"}), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(plugin_module.subprocess, "run", fake_run)

    service._update("0.1.7")

    result = service.status()
    assert result["status"] == "updated"
    assert result["current"] == "0.1.7"
    assert result["restart_required"] is True
    assert "plugin" in captured["arguments"]
    assert "--profile" in captured["arguments"]
    assert "https://github.com/Jyleaves/dsh-gemini-aistudio.git#v0.1.7" in captured["arguments"]
    assert captured["kwargs"]["env"]["HTTPS_PROXY"] == "http://127.0.0.1:7890"


def test_update_reports_windows_file_lock_help(monkeypatch, tmp_path):
    _install_plugin(tmp_path)
    service = PluginUpdateService(dsh_home=tmp_path)
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


def test_start_update_rejects_a_local_development_link(monkeypatch, tmp_path):
    _install_plugin(tmp_path, specifier="link:E:/Project/dsh-gemini-aistudio")
    monkeypatch.setattr(
        plugin_module,
        "open_update_url",
        lambda *_args, **_kwargs: _Response({"tag_name": "v0.1.7"}),
    )
    service = PluginUpdateService(dsh_home=tmp_path)
    service.check()

    try:
        service.start_update()
    except RuntimeError as exc:
        assert "本地开发链接" in str(exc)
    else:
        raise AssertionError("local development link must not be overwritten")

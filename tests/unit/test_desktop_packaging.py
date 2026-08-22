from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_default_cli_mode_is_desktop_app():
    from aistudio_api.main import build_parser

    args = build_parser().parse_args([])

    assert args.command is None


def test_frozen_config_prefers_config_next_to_exe(monkeypatch, tmp_path):
    from aistudio_api.infrastructure.gateway import model_defaults

    exe_dir = tmp_path / "installed"
    exe_dir.mkdir()
    beside_exe = exe_dir / "config.yaml"
    beside_exe.write_text("models: {}\n", encoding="utf-8")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "Asteria.exe"))
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "internal"), raising=False)

    assert model_defaults._resolve_config_path(None) == beside_exe


def test_frozen_config_falls_back_to_bundled_copy(monkeypatch, tmp_path):
    from aistudio_api.infrastructure.gateway import model_defaults

    internal = tmp_path / "internal"
    internal.mkdir()
    bundled = internal / "config.yaml"
    bundled.write_text("models: {}\n", encoding="utf-8")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Asteria.exe"))
    monkeypatch.setattr(sys, "_MEIPASS", str(internal), raising=False)

    assert model_defaults._resolve_config_path(None) == bundled


def test_release_packaging_uses_brand_and_icon():
    spec = (ROOT / "aistudio-api.spec").read_text(encoding="utf-8")
    installer = (ROOT / "installer.iss").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert "icon='image/app-icon/icon.ico'" in spec
    assert 'SetupIconFile=image\\app-icon\\icon.ico' in installer
    assert '#define MyAppName "Asteria"' in installer
    assert "Asteria-setup-*.exe" in workflow


def test_release_artifact_name_and_version_are_consistent():
    installer = (ROOT / "installer.iss").read_text(encoding="utf-8")
    package = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert '#define MyAppVersion "1.0.0"' in installer
    assert 'version = "1.0.0"' in package
    assert "OutputBaseFilename=Asteria-setup-{#MyAppVersion}" in installer

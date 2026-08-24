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
    assert "name='Asteria'" in spec
    assert 'SetupIconFile=image\\app-icon\\icon.ico' in installer
    assert '#define MyAppName "Asteria"' in installer
    assert '#define MyAppExeName "Asteria.exe"' in installer
    assert 'Source: "dist\\Asteria\\*"' in installer
    assert "Asteria-setup-*.exe" in workflow
    assert "version=windows_version_info" in spec
    assert "StringStruct('ProductVersion', project_version)" in spec


def test_release_artifact_name_and_version_are_consistent():
    installer = (ROOT / "installer.iss").read_text(encoding="utf-8")
    updater = (ROOT / "installer-update.iss").read_text(encoding="utf-8")
    package = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert '#define MyAppVersion "1.0.2"' in installer
    assert '#define MyAppVersion "1.0.2"' in updater
    assert 'version = "1.0.2"' in package
    assert "OutputBaseFilename=Asteria-setup-{#MyAppVersion}" in installer
    assert "OutputBaseFilename=Asteria-update-{#MyAppVersion}" in updater
    assert "skipifsilent" not in next(line for line in updater.splitlines() if line.startswith("Filename:"))
    assert "runasoriginaluser" in next(line for line in updater.splitlines() if line.startswith("Filename:"))


def test_release_bundle_excludes_non_runtime_playwright_assets():
    spec = (ROOT / "aistudio-api.spec").read_text(encoding="utf-8")
    updater = (ROOT / "installer-update.iss").read_text(encoding="utf-8")

    assert "'playwright/driver/package/types/'" in spec
    assert "'playwright/driver/package/lib/vite/'" in spec
    assert "package/lib/server" in spec
    assert "a.datas = [item for item in a.datas if _keep_collected_runtime_data(item)]" in spec
    assert 'Name: "{app}\\_internal\\playwright\\driver\\package\\types"' in updater
    assert 'Name: "{app}\\_internal\\playwright\\driver\\package\\lib\\vite"' in updater


def test_windows_build_prunes_non_runtime_browser_payload():
    build_script = (ROOT / "build-windows.ps1").read_text(encoding="utf-8")

    assert '$keptLocales = @("en-US.pak", "zh-CN.pak")' in build_script
    assert '"chromedriver.exe"' in build_script
    assert '"setup.exe"' in build_script
    assert "Refusing to prune browser files outside dist" in build_script
    assert "Copied Chromium runtime does not contain chrome.exe" in build_script
    assert "Refusing to prune browser runtime outside bundle" in build_script
    assert "browsers.json" in build_script
    assert "does not match Playwright" in build_script
    assert 'chromium-$($expectedChromium.revision)' in build_script
    assert "Refusing to refresh browser cache outside project" in build_script


def test_server_startup_warms_only_active_account_without_blocking_ui():
    source = (ROOT / "src" / "aistudio_api" / "api" / "app.py").read_text(encoding="utf-8")

    assert "runtime_state.ready = True" in source
    assert "prepare_account(active_account.id)" in source
    assert "prepare_all_accounts()" not in source
    assert "asyncio.create_task(" in source
    assert 'name="aistudio-account-warmup"' in source
    assert "account_warmup_task.cancel()" in source

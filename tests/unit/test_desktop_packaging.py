from __future__ import annotations

import json
import sys
import tomllib
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
    assert "Asteria-update-*.exe" in workflow
    assert "version=windows_version_info" in spec
    assert "StringStruct('ProductVersion', project_version)" in spec


def test_frozen_desktop_runtime_keeps_the_pywebview_api():
    spec = (ROOT / "aistudio-api.spec").read_text(encoding="utf-8")
    build_script = (ROOT / "build-windows.ps1").read_text(encoding="utf-8")
    main = (ROOT / "src" / "aistudio_api" / "main.py").read_text(encoding="utf-8")

    assert "collect_data_files('webview', include_py_files=True)" in spec
    assert "Start-Process -FilePath $packagedExe" in build_script
    assert "-Wait -PassThru -WindowStyle Hidden" in build_script
    assert "$packagingSmoke.ExitCode" in build_script
    assert "packaged pywebview is missing" in main
    assert '("create_window", "start")' in main


def test_release_artifact_name_and_version_are_consistent():
    installer = (ROOT / "installer.iss").read_text(encoding="utf-8")
    updater = (ROOT / "installer-update.iss").read_text(encoding="utf-8")
    package = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project_version = package["project"]["version"]

    assert f'#define MyAppVersion "{project_version}"' in installer
    assert f'#define MyAppVersion "{project_version}"' in updater
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


def test_windows_build_requires_pinned_cloakbrowser_runtime():
    build_script = (ROOT / "build-windows.ps1").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    browser_lock = json.loads((ROOT / "browser-runtime.json").read_text(encoding="utf-8"))

    assert 'Join-Path $projectBrowserRoot "chrome.exe"' in build_script
    assert "browser-runtime.json" in build_script
    assert "Get-FileHash -LiteralPath $projectChromePath -Algorithm SHA256" in build_script
    assert "Playwright-downloaded Chromium" in build_script
    assert "playwright install chromium" not in build_script.lower()
    assert "playwright install chromium" not in workflow.lower()
    assert ".\\build-windows.ps1 -UpdateOnly" in workflow
    assert "dist/Asteria-setup-*.exe" not in workflow
    assert browser_lock["source_archive"] not in workflow
    assert 'if (-not $UpdateOnly) {' in build_script
    assert browser_lock["chrome_version"] == "146.0.7680.177"
    assert browser_lock["chrome_sha256"] == "03f53661a5c47e7b0a661bee2bce8a0d302b7a60834c328df417561fa0636d80"


def test_server_startup_warms_only_active_account_without_blocking_ui():
    source = (ROOT / "src" / "aistudio_api" / "api" / "app.py").read_text(encoding="utf-8")

    assert "runtime_state.ready = True" in source
    assert "prepare_account(active_account.id)" in source
    assert "prepare_all_accounts()" not in source
    assert "asyncio.create_task(" in source
    assert 'name="aistudio-account-warmup"' in source
    assert "account_warmup_task.cancel()" in source

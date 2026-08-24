# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_all
import site
from pathlib import Path

# PyInstaller 6 inspects the user-site directory while resolving DLL parent
# paths. On locked-down Windows profiles that directory may be unreadable even
# though the virtualenv itself is healthy.
site.getusersitepackages = lambda: str(Path.cwd() / '.pyinstaller-no-user-site')

datas = [('src/aistudio_api/static', 'aistudio_api/static'), ('config.yaml', '.')]
binaries = []
hiddenimports = ['webview.platforms.edgechromium', 'webview.platforms.winforms', 'uvicorn.logging', 'uvicorn.loops.auto', 'uvicorn.protocols.http.auto', 'uvicorn.protocols.http.h11_impl', 'uvicorn.lifespan.on']
datas += collect_data_files('webview')
tmp_ret = collect_all('playwright')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('curl_cffi')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('clr_loader')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


def _keep_runtime_data(item):
    """Drop package metadata and documentation that are not needed at runtime."""
    source = str(item[0]).replace('\\', '/')
    name = source.rsplit('/', 1)[-1].lower()
    if '/.dist-info/' in source.lower() or '/__pycache__/' in source.lower():
        return False
    if name.endswith(('.pdb', '.pyi')) or name in {
        'py.typed', 'license', 'license.txt', 'notice', 'notice.txt',
        'readme', 'readme.md', 'thirdpartynotices.txt',
    }:
        return False
    return True


datas = [item for item in datas if _keep_runtime_data(item)]


a = Analysis(
    ['src/aistudio_api/main.py'],
    pathex=['src'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Asteria',
    icon='image/app-icon/icon.ico',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Asteria',
)

# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller 6.x spec.  Build on Windows with:
#
#     .venv\Scripts\python.exe -m PyInstaller --noconfirm --clean xhttp.spec
#
# Cross-compiling a Windows .exe from Linux is not supported by PyInstaller;
# run build.bat on Windows (or a Windows CI runner).

from PyInstaller.utils.hooks import collect_submodules

hiddenimports = (
    collect_submodules("h2")
    + collect_submodules("h11")
    + collect_submodules("socks5")
)

a = Analysis(
    ["run.py"],
    pathex=["."],
    binaries=[],
    datas=[],
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
    a.binaries,
    a.datas,
    [],
    name="xhttp",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version="version_info.txt",
)

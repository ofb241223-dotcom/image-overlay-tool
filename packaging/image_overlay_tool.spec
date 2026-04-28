# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import sys


project_root = Path(SPECPATH).parent
icon_png = project_root / "packaging" / "app_icon.png"
icon_ico = project_root / "packaging" / "app_icon.ico"
icon_icns = project_root / "packaging" / "app_icon.icns"

if sys.platform == "win32" and icon_ico.exists():
    icon_path = str(icon_ico)
elif sys.platform == "darwin" and icon_icns.exists():
    icon_path = str(icon_icns)
else:
    icon_path = None

a = Analysis(
    [str(project_root / "merge_logo_gui.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[str(project_root / "packaging" / "hooks")],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "_scproxy",
        "_winapi",
        "_winreg",
        "_wmi",
        "java",
        "java.lang",
        "msvcrt",
        "nt",
        "numpy",
        "olefile",
        "vms_lib",
        "winreg",
        "xmlrpclib",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Image Overlay Tool",
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
    icon=icon_path,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="Image Overlay Tool",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Image Overlay Tool.app",
        icon=icon_path,
        bundle_identifier="local.image-overlay-tool",
    )

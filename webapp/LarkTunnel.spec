# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the LarkTunnel desktop app (one-folder build).
#   webapp\build.bat            -> dist\LarkTunnel\LarkTunnel.exe
# Bundled read-only assets: static/ (UI) and config/config.js (table registry).
# Nothing user-specific is bundled: credentials/settings live in
# %APPDATA%\LarkTunnel on each machine (see apppaths.py / app_settings.py).
import os

here = os.path.abspath(SPECPATH)            # webapp/
root = os.path.dirname(here)                # LarkTunnel/

a = Analysis(
    [os.path.join(here, "desktop.py")],
    pathex=[here],
    binaries=[],
    datas=[
        (os.path.join(here, "static"), "static"),
        (os.path.join(root, "config", "config.js"), "config"),
    ],
    hiddenimports=[
        "webview.platforms.edgechromium", "webview.platforms.winforms",
        "clr_loader", "clr_loader.netfx", "pythonnet",
        "server", "appointment_sync", "appointment_create", "inventory_import",
        "verify_assignments", "audit_store", "audit_view", "identity_harvest",
        "file_parse", "sync_jobs", "lark_client", "app_settings",
        "access_control", "ops_log", "apppaths",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pydoc", "test"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LarkTunnel",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                 # windowed; logs -> %APPDATA%\LarkTunnel\logs\desktop.log
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(here, "static", "larktunnel.ico")
    if os.path.isfile(os.path.join(here, "static", "larktunnel.ico")) else None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="LarkTunnel",
)

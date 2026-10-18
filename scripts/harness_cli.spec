import os

from PyInstaller.utils.hooks import collect_submodules, copy_metadata


hiddenimports = [
    *collect_submodules("httpx"),
    *collect_submodules("httpcore"),
    *collect_submodules("anyio"),
    "mcp",
    "mcp.client.stdio",
    "mcp.client.streamable_http",
]
datas = [
    *copy_metadata("harness-pro"),
    *copy_metadata("mcp"),
]
entry_path = os.path.join(SPECPATH, "harness_cli_entry.py")
repo_root = os.path.dirname(SPECPATH)

a = Analysis(
    [entry_path],
    pathex=[repo_root],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["fastapi", "uvicorn", "starlette", "tkinter"],
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
    name=os.environ.get("HARNESS_CLI_BUILD_NAME", "harness"),
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
)

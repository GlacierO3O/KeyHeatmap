# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_submodules, collect_dynamic_libs

encodings_all = collect_submodules('encodings')

backend_hidden = []
for _m in ('fastapi', 'uvicorn', 'pymysql', 'pydantic', 'starlette', 'anyio', 'h11', 'sniffio', 'typing_extensions'):
    backend_hidden += collect_submodules(_m)
backend_hidden += collect_dynamic_libs('pydantic_core')

a = Analysis(
    ['keyheatmap.py'],
    pathex=[],
    binaries=[],
    datas=[('assets', 'assets'), ('backend', 'backend'), ('dist/tray_helper.exe', 'tray_helper')],
    hiddenimports=['pymysql', 'numpy', 'numpy.core._multiarray_umath', 'numpy.core._methods', 'numpy.lib.format', 'numpy.random', 'numpy.linalg', 'numpy.fft', 'numpy.ctypeslib', 'scipy', 'scipy.ndimage', 'pystray', 'PIL', 'PIL.Image', 'PIL.ImageDraw', 'pynput'] + backend_hidden + encodings_all,
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
    name='keyheatmap',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['keyheatmap.ico'],
    uac_admin=True,
)

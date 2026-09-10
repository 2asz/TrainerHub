# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'unittest'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='TrainerHub',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,   # 加壳exe必被杀软误报隔离（2026-09-02 实锤：两次打包产物被静默删除），关闭换稳定
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version='version_info.txt'   # 文件属性显示公司/版本/描述，降低杀软启发式误报
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,   # 同上
    upx_exclude=[],
    name='TrainerHub',
)

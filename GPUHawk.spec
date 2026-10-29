# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['gpuhawk.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # qfluentwidgets 的亚克力效果会可选导入 numpy/scipy/PIL，顺藤摸瓜会把 torch 等几 GB 的库打进来
    excludes=['torch', 'torchgen', 'torchvision', 'scipy', 'numpy', 'PIL', 'colorthief', 'matplotlib',
              'pandas', 'sympy', 'numba', 'llvmlite', 'IPython', 'cv2', 'tensorflow', 'sklearn', 'tkinter'],
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
    name='GPUHawk',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['app.ico'],
)

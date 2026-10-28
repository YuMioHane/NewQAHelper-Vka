# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['qa_re.pyw'],
    pathex=[],
    binaries=[],
    datas=[('tpl', 'tpl'), ('_names.json', '.'), ('_xp_table.json', '.'), ('_market_names.json', '.'),
           ('_stages.json', '.'), ('_class_stats.json', '.'), ('_tree.json', '.')],
    hiddenimports=[],
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
    name='QA助手RE',
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
)

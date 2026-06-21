# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ['installer.py'],
    pathex=[],
    binaries=[],
    datas=[('icon.png', '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Finishing Tool Installer',
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
    icon='icon.png',
)

app = BUNDLE(
    exe,
    name='Finishing Tool Installer.app',
    icon='icon.png',
    bundle_identifier='com.finishingtool.installer',
    version='1.0',
    info_plist={
        'CFBundleName': 'Finishing Tool Installer',
        'CFBundleDisplayName': 'Finishing Tool Installer',
        'CFBundleShortVersionString': '1.0',
        'CFBundleVersion': '1.0',
        'NSHighResolutionCapable': True,
    },
)

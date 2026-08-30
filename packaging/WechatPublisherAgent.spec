# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules


ROOT = Path(SPECPATH).parent
datas = []
for relative in ("config", "templates", "src/cs_uia_service/publish"):
    path = ROOT / relative
    if path.exists():
        datas.append((str(path), relative.replace("/", "\\")))

hiddenimports = collect_submodules("paddleocr")
try:
    package_datas, package_binaries, package_hidden = collect_all(
        "paddleocr",
        exclude_datas=["**/tests/**", "**/__pycache__/**"],
    )
    datas += package_datas
    hiddenimports += package_hidden
except Exception:
    pass

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "matplotlib.tests", "numpy.tests"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="WechatPublisherAgent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    name="WechatPublisherAgent",
)

# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_all,
    collect_data_files,
    collect_submodules,
    copy_metadata,
    get_package_paths,
)


ROOT = Path(SPECPATH).parent
datas = []
binaries = []
for relative in ("config", "src/cs_uia_service/publish"):
    path = ROOT / relative
    if path.exists():
        datas.append((str(path), relative.replace("/", "\\")))

# Runtime calibration may create private screenshots under templates/icons.
# Ship only reviewed navigation assets in the installer.
for filename in (
    "moments_tab.png",
    "discover_tab.png",
    "moments_discover_item.png",
):
    path = ROOT / "templates" / "icons" / filename
    if path.exists():
        datas.append((str(path), "templates\\icons"))

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

# PaddleOCR 3.x loads its pipeline definitions from PaddleX package data at
# runtime. PyInstaller follows the Python imports but does not collect these
# YAML files automatically, so the packaged OCR engine cannot find "OCR".
datas += collect_data_files("paddlex", includes=["configs/**"])

# PaddleX validates OCR extras with importlib.metadata before constructing the
# pipeline. Preserve both the import modules and their distribution metadata.
ocr_runtime_packages = {
    "imagesize": "imagesize",
    "opencv-contrib-python": None,
    "pyclipper": "pyclipper",
    "pypdfium2": "pypdfium2",
    "python-bidi": "bidi",
    "shapely": "shapely",
}
for distribution, module in ocr_runtime_packages.items():
    datas += copy_metadata(distribution)
    if module:
        hiddenimports += collect_submodules(module)

# The upstream Paddle PyInstaller hook omits this CPU inference dependency.
# Without it the packaged predictor fails with Windows loader error 126.
_, paddle_package = get_package_paths("paddle")
binaries.append((str(Path(paddle_package) / "libs" / "mklml.dll"), "paddle/libs"))

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
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
    icon=str(ROOT / "assets" / "agent-icon.ico"),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    name="WechatPublisherAgent",
)

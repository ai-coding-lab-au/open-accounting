# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


ROOT = Path(SPECPATH).parent
BACKEND = ROOT / "backend"
FRONTEND_DIST = ROOT / "frontend" / "dist"

datas = []
if FRONTEND_DIST.exists():
    datas.append((str(FRONTEND_DIST), "frontend-dist"))
# Bundled CJK font (Noto Sans SC, OFL-1.1) + its license — pdf_fonts.py
# resolves it relative to the app package, so keep the app/assets layout.
datas.append((str(BACKEND / "app" / "assets"), "app/assets"))

hiddenimports = (
    collect_submodules("app")
    # reportlab is the portable build's PDF renderer. It would land in the
    # bundle via static import analysis anyway, but collect it explicitly so
    # a future lazy-import refactor can't silently strip PDF generation from
    # the frozen app (smoke_portable.py is the runtime backstop).
    + collect_submodules("reportlab")
    + collect_submodules("uvicorn")
    + collect_submodules("pydantic_settings")
)

a = Analysis(
    [str(BACKEND / "app" / "desktop.py")],
    pathex=[str(BACKEND)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # pytest/pygments are dependency-graph strays (bundle bloat only);
    # playwright is deliberately not part of the portable build — ReportLab
    # is its official PDF renderer (bundling playwright without a browser
    # would only add dead weight).
    excludes=["pytest", "_pytest", "pygments", "playwright"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="OpenAccounting",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX-compressed DLLs raise AV/SmartScreen false-positive rates on an
    # already-unsigned exe; the size win isn't worth it for a local app.
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="OpenAccounting",
)

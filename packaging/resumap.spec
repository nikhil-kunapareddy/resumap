# PyInstaller spec for Resumap.app: Python, every dependency and the UI in one
# bundle, so nobody needs Python, uv or a virtualenv to run it.
# Build with packaging/build_macos.sh, which also signs and zips it.

from importlib.metadata import version
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent
VERSION = version("resumap")

a = Analysis(
    [str(ROOT / "packaging" / "resumap_app.py")],
    pathex=[str(ROOT / "backend")],
    datas=[(str(ROOT / "frontend"), "frontend")],
    # uvicorn picks its loop and protocol modules by name at runtime, and the
    # routers are found through resumap.api.routers.ALL.
    hiddenimports=collect_submodules("uvicorn") + collect_submodules("resumap"),
    excludes=["tkinter", "pytest", "mypy", "ruff"],
)

# claude-agent-sdk carries its own 200MB+ copy of the claude binary. Resumap
# always runs the user's own `claude`, so the copy stays out of the bundle.
_BUNDLED = "claude_agent_sdk/_bundled"
a.datas = [d for d in a.datas if _BUNDLED not in d[0].replace("\\", "/")]
a.binaries = [b for b in a.binaries if _BUNDLED not in b[0].replace("\\", "/")]

pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Resumap",
    console=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Resumap")
app = BUNDLE(
    coll,
    name="Resumap.app",
    bundle_identifier="local.resumap.app",
    version=VERSION,
    info_plist={
        "CFBundleDisplayName": "Resumap",
        "CFBundleShortVersionString": VERSION,
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
    },
)

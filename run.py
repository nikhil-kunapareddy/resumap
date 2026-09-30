#!/usr/bin/env python3
"""Start Resumap and open it in the browser.

Run with any Python 3.10+: if the project's virtualenv exists, this re-execs
into it so you never have to remember to activate anything.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND_DIR = ROOT / "backend"  # the `resumap` package lives here
VENV_DIR = ROOT / ".venv"
VENV_PY = VENV_DIR / "bin" / "python"


def _reexec_into_venv() -> None:
    if not VENV_PY.exists():
        return
    # Compare prefixes rather than executables. A venv built from a conda or
    # pyenv interpreter symlinks .venv/bin/python straight at that interpreter,
    # so resolving both sides makes an unactivated run look like it is already
    # inside the venv -- and it then runs against the wrong site-packages.
    if Path(sys.prefix).resolve() == VENV_DIR.resolve():
        return
    os.execv(str(VENV_PY), [str(VENV_PY), str(Path(__file__).resolve()), *sys.argv[1:]])


def main() -> None:
    _reexec_into_venv()

    try:
        import uvicorn
    except ModuleNotFoundError:
        sys.exit(
            "Dependencies are not installed.\n"
            "Run:  uv sync\n"
            "  or: python3 -m venv .venv && .venv/bin/pip install -e ."
        )

    sys.path.insert(0, str(BACKEND_DIR))
    from resumap.config import HOST, PORT
    from resumap.main import app

    services = app.state.services
    services.paths.ensure()
    services.store.init()
    services.settings.apply_saved()

    url = f"http://{HOST}:{PORT}"
    print(f"\n  Resumap  ->  {url}")
    why = services.gateway.status()
    if why:
        print(f"  WARNING: the {services.gateway.registry.current_name()} backend is not ready.")
        print(f"           {why}")
    else:
        print(f"  Backend: {services.gateway.registry.current_name()}")
    print("  Ctrl-C to stop.\n")

    if "--no-browser" not in sys.argv:
        threading.Thread(
            target=lambda: (time.sleep(1.2), webbrowser.open(url)), daemon=True
        ).start()

    uvicorn.run("resumap.main:app", host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()

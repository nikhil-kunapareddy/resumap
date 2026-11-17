#!/usr/bin/env python3
"""Start Resumap and open it in the browser.

Run with any Python 3.10+: if the project's virtualenv exists, this re-execs
into it so you never have to remember to activate anything.
"""

from __future__ import annotations

import os
import sys
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
        import uvicorn  # noqa: F401 - only checked for; the launcher runs it
    except ModuleNotFoundError:
        sys.exit(
            "Dependencies are not installed.\n"
            "Run:  uv sync\n"
            "  or: python3 -m venv .venv && .venv/bin/pip install -e ."
        )

    sys.path.insert(0, str(BACKEND_DIR))
    from resumap.launcher import serve

    serve(open_browser="--no-browser" not in sys.argv)


if __name__ == "__main__":
    main()

"""Entry script of the packaged app (see resumap.spec): a double-click opens
Resumap, starting its server first if needed; `--serve` is the server itself.

From a checkout it does the same, which is how the launcher is tried without a
build: `.venv/bin/python packaging/resumap_app.py`.
"""

import sys
from pathlib import Path

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from resumap.launcher import main  # noqa: E402

if __name__ == "__main__":
    main()

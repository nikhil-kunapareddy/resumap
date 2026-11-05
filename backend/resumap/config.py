"""Runtime configuration for Resumap."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values, load_dotenv


def user_data_dir(platform: str = sys.platform, home: Path | None = None) -> Path:
    """Where the packaged app keeps its database, uploads, logs and optional .env."""
    home = home or Path.home()
    if platform == "darwin":
        return home / "Library" / "Application Support" / "Resumap"
    if platform == "win32":
        return Path(os.getenv("APPDATA") or (home / "AppData" / "Roaming")) / "Resumap"
    return Path(os.getenv("XDG_DATA_HOME") or (home / ".local" / "share")) / "resumap"


# True inside the PyInstaller app (packaging/), where there is no checkout: the
# UI ships inside the bundle and everything written goes to the user's folder.
FROZEN = bool(getattr(sys, "frozen", False))

# From a checkout the package lives at <project>/backend/resumap, so the
# project root -- which holds .env, data/ and the frontend -- is two levels up.
BACKEND_DIR = Path(__file__).resolve().parent.parent
BASE_DIR = BACKEND_DIR.parent
if FROZEN:
    FRONTEND_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR)) / "frontend"
    HOME_DIR = user_data_dir()
else:
    FRONTEND_DIR = BASE_DIR / "frontend"
    HOME_DIR = BASE_DIR

ENV_FILE = HOME_DIR / ".env"
load_dotenv(ENV_FILE)
# What .env itself set, so a CLI backend can be kept from inheriting a key
# that was put there for the api backend (see backends.cli_env).
DOTENV_KEYS = {k: v for k, v in dotenv_values(ENV_FILE).items() if v}

# Point a second instance at its own storage (testing, or a throwaway profile)
# so it cannot touch the resumes and exports your main instance holds.
DATA_DIR = Path(
    os.getenv("RESUMAP_DATA_DIR") or (HOME_DIR if FROZEN else BASE_DIR / "data")
).expanduser()
# The working directory CLI backends run in: the checkout, or the app's own
# folder (never the inside of the app bundle).
WORK_DIR = DATA_DIR if FROZEN else BASE_DIR
UPLOAD_DIR = DATA_DIR / "uploads"
EXPORT_DIR = DATA_DIR / "exports"
DB_PATH = DATA_DIR / "resumap.db"
LOG_DIR = Path(os.getenv("RESUMAP_LOG_DIR") or (DATA_DIR / "logs")).expanduser()
LOG_LEVEL = os.getenv("RESUMAP_LOG_LEVEL", "INFO")

# Opus 5 for the rewriting work: it is the quality-sensitive step and the
# volume here is a handful of bullets per application.
MODEL = os.getenv("RESUMAP_MODEL", "claude-opus-5")

# Who serves the model calls when nothing has been chosen in the settings
# panel yet: "api" (ANTHROPIC_API_KEY, billed per token), "claude_code",
# "codex" or "pi" (local CLIs, each on its own sign-in). The panel's choice,
# once saved, wins over this. See app/backends/ for what each one does.
BACKEND = os.getenv("RESUMAP_BACKEND", "api").strip().lower() or "api"

# Paths to the CLI backends' binaries, only needed when they are not on the
# PATH this process inherited (launching from a GUI rather than a shell).
CLAUDE_CLI = os.getenv("RESUMAP_CLAUDE_CLI", "").strip() or None
CODEX_CLI = os.getenv("RESUMAP_CODEX_CLI", "").strip() or None
PI_CLI = os.getenv("RESUMAP_PI_CLI", "").strip() or None

# Goes in front of every exported filename: resume_companyname_mmdd.pdf
DEFAULT_FIRST_NAME = os.getenv("RESUMAP_FIRST_NAME", "resume")

HOST = os.getenv("RESUMAP_HOST", "127.0.0.1")
# The app defaults to a port of its own, so it and a checkout's run.py can run
# side by side without one opening the other's data.
PORT = int(os.getenv("RESUMAP_PORT") or (8765 if FROZEN else 8000))


def api_key() -> str | None:
    key = os.getenv("ANTHROPIC_API_KEY")
    return key.strip() if key and key.strip() else None


@dataclass(frozen=True)
class Paths:
    """Where one instance keeps its files. The app is handed one, so a test (or a
    second instance) can point everything at storage of its own."""

    db: Path
    uploads: Path
    exports: Path
    logs: Path

    @classmethod
    def from_env(cls) -> Paths:
        return cls(db=DB_PATH, uploads=UPLOAD_DIR, exports=EXPORT_DIR, logs=LOG_DIR)

    def ensure(self) -> None:
        for d in (self.db.parent, self.uploads, self.exports, self.logs):
            d.mkdir(parents=True, exist_ok=True)

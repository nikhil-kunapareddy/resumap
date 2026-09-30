"""Shared fixtures: throwaway storage, a scripted backend and a test client.

The environment is set before anything imports ``app.config``, which reads it
at import time. That keeps every test off the real ``data/`` directory, away
from the settings in a developer's ``.env``, and away from any real model.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "backend"), str(ROOT)]

_SESSION = Path(tempfile.mkdtemp(prefix="resumap-tests-"))
os.environ.update({
    "RESUMAP_DATA_DIR": str(_SESSION / "data"),
    "RESUMAP_LOG_DIR": str(_SESSION / "logs"),
    "RESUMAP_BACKEND": "api",
    "RESUMAP_MODEL": "fake-model",
    "RESUMAP_FIRST_NAME": "resume",
    "RESUMAP_LOG_LEVEL": "WARNING",
    "RESUMAP_LOG_PROMPTS": "0",
    "RESUMAP_LOG_TEXT": "0",
    # Set, so load_dotenv leaves it alone: no test can reach a billed API.
    "ANTHROPIC_API_KEY": "",
})

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import backends, main  # noqa: E402
from app.storage import Database, Store  # noqa: E402
from tests.fakes import FakeBackend  # noqa: E402
from tests.fixtures import resumes  # noqa: E402


@pytest.fixture(scope="session")
def resume_files(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("resumes")
    return {
        "pdf": resumes.build_pdf(d / "jane_doe.pdf"),
        "docx": resumes.build_docx(d / "jane_doe.docx"),
        "txt": resumes.build_txt(d / "jane_doe.txt"),
    }


@pytest.fixture
def storage(tmp_path, monkeypatch) -> Path:
    """A fresh SQLite file plus upload and export folders for one test.

    The app builds its Store from ``main.DB_PATH`` at startup, so pointing
    that at the test's folder is all a TestClient needs.
    """
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "resumap.db")
    for name in ("UPLOAD_DIR", "EXPORT_DIR"):
        folder = tmp_path / name.split("_")[0].lower()
        folder.mkdir()
        monkeypatch.setattr(main, name, folder)
    Database(tmp_path / "resumap.db").init()
    return tmp_path


@pytest.fixture
def store(storage) -> Store:
    """A Store over the test's database: the same file the app writes to."""
    return Store(Database(storage / "resumap.db"))


@pytest.fixture
def fake(monkeypatch) -> FakeBackend:
    """Route every backend lookup, whatever its name, to one scripted backend."""
    backend = FakeBackend()
    monkeypatch.setattr(backends, "get", lambda name=None: backend)
    return backend


@pytest.fixture
def client(storage, fake):
    with TestClient(main.app) as c:
        yield c


def upload(client, path: Path, label: str = "Jane") -> dict:
    with path.open("rb") as fh:
        r = client.post("/api/resumes", files={"file": (path.name, fh)},
                        data={"label": label})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def resume(client, resume_files) -> dict:
    """The sample PDF, uploaded."""
    return upload(client, resume_files["pdf"])


@pytest.fixture
def job(client, resume) -> dict:
    """A job for the sample posting against the uploaded resume."""
    r = client.post("/api/jobs", json={"resume_id": resume["id"],
                                       "text": resumes.JOB_DESCRIPTION})
    assert r.status_code == 200, r.text
    return r.json()

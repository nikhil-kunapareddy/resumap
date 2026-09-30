"""Shared fixtures: throwaway storage, a scripted backend and a test client.

The app is built with ``create_app(paths, registry)``: its own folder per test,
and a registry whose every backend name leads to one scripted fake. The
environment is still set before anything imports ``app.config``, so nothing
can fall back to the real ``data/`` directory or a developer's ``.env``.
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

from app.config import Paths  # noqa: E402
from app.llm import BackendRegistry  # noqa: E402
from app.llm.backends import NAMES  # noqa: E402
from app.main import create_app  # noqa: E402
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
def paths(tmp_path) -> Paths:
    """A fresh folder of the test's own for the database, uploads, exports and logs."""
    p = Paths(db=tmp_path / "resumap.db", uploads=tmp_path / "upload",
              exports=tmp_path / "export", logs=tmp_path / "logs")
    p.ensure()
    return p


@pytest.fixture
def store(paths) -> Store:
    """A Store over the test's database: the same file the app writes to."""
    s = Store(Database(paths.db))
    s.init()
    return s


@pytest.fixture
def fake() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def registry(fake) -> BackendRegistry:
    """Every real backend name, each leading to the one scripted backend."""
    return BackendRegistry({name: fake for name in NAMES}, selected="api")


@pytest.fixture
def client(paths, registry):
    with TestClient(create_app(paths, registry)) as c:
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

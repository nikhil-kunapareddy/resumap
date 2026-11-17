"""What the double-click app relies on: an API key saved from Settings (there
is no .env to edit), the Quit button (there is no terminal), the launcher that
finds or starts the server, and the folder it keeps its data in."""

from __future__ import annotations

import os
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from resumap import launcher
from resumap.config import user_data_dir
from resumap.llm import BackendRegistry
from resumap.llm.backends import NAMES, AnthropicApiBackend
from resumap.main import create_app
from tests.conftest import LOCAL

KEY = "sk-ant-test-0123456789"


def keyed_registry(fake) -> BackendRegistry:
    """The scripted backend everywhere except `api`, which is the real one with
    no key in the environment."""
    api = AnthropicApiBackend("fake-model", lambda: None)
    return BackendRegistry(
        {n: (api if n == "api" else fake) for n in NAMES}, selected="claude_code"
    )


def api_row(client) -> dict:
    return next(b for b in client.get("/api/backends").json()["backends"] if b["name"] == "api")


# -- the API key ---------------------------------------------------------------


def test_an_api_key_saved_in_settings_makes_the_api_backend_ready(paths, fake, store):
    with TestClient(create_app(paths, keyed_registry(fake)), base_url=LOCAL) as client:
        row = api_row(client)
        assert (row["accepts_key"], row["key_source"], row["ready"]) == (True, None, False)
        assert "Settings" in row["detail"]

        r = client.put("/api/backends/api/key", json={"key": f"  {KEY}  "})
        assert r.status_code == 200
        assert KEY not in r.text  # never sent back
        row = api_row(client)
        assert (row["key_source"], row["ready"]) == ("settings", True)
        assert store.settings.get("key.api") == KEY
        # Held by the backend only: no CLI subprocess can inherit it.
        assert KEY not in os.environ.values()

    # A restart puts it back live.
    with TestClient(create_app(paths, keyed_registry(fake)), base_url=LOCAL) as client:
        assert api_row(client)["key_source"] == "settings"

        r = client.put("/api/backends/api/key", json={"key": ""})
        assert r.status_code == 200
        assert (api_row(client)["key_source"], api_row(client)["ready"]) == (None, False)
        assert store.settings.get("key.api") == ""


def test_a_backend_that_signs_in_on_its_own_takes_no_key(client):
    rows = client.get("/api/backends").json()["backends"]
    assert all(b["accepts_key"] is False and b["key_source"] is None for b in rows)
    assert client.put("/api/backends/claude_code/key", json={"key": KEY}).status_code == 400
    assert client.put("/api/backends/gemini/key", json={"key": KEY}).status_code == 404


def test_a_key_with_spaces_in_it_is_rejected(paths, fake):
    with TestClient(create_app(paths, keyed_registry(fake)), base_url=LOCAL) as client:
        r = client.put("/api/backends/api/key", json={"key": "sk-ant two words"})
        assert r.status_code == 400
        assert api_row(client)["key_source"] is None


# -- quitting ------------------------------------------------------------------


def test_quit_is_refused_unless_whoever_started_the_server_allows_it(client):
    assert client.get("/api/status").json()["can_quit"] is False
    assert client.post("/api/quit").status_code == 409


def test_quit_calls_the_stop_it_was_given(paths, registry):
    app = create_app(paths, registry)
    stopped: list[bool] = []
    app.state.services.lifecycle.allow_quit(lambda: stopped.append(True))
    with TestClient(app, base_url=LOCAL) as client:
        assert client.get("/api/status").json()["can_quit"] is True
        r = client.post("/api/quit")
        assert (r.status_code, r.json(), stopped) == (200, {"ok": True}, [True])


# -- the launcher --------------------------------------------------------------


def test_api_responses_carry_the_header_the_launcher_looks_for(client):
    assert client.get("/api/status").headers.get(launcher.MARKER)


def _serve(marker: bool) -> Iterator[int]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            if marker:
                self.send_header(launcher.MARKER, "run")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def resumap_port() -> Iterator[int]:
    yield from _serve(marker=True)


@pytest.fixture
def other_port() -> Iterator[int]:
    yield from _serve(marker=False)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_probe_tells_a_running_resumap_from_another_program(resumap_port, other_port):
    assert launcher.probe("127.0.0.1", resumap_port) == "resumap"
    assert launcher.probe("127.0.0.1", other_port) == "other"
    assert launcher.probe("127.0.0.1", _free_port()) == "free"


def test_find_port_steps_past_ports_other_programs_hold(other_port, monkeypatch):
    states = {other_port: "other", other_port + 1: "other", other_port + 2: "free"}
    monkeypatch.setattr(launcher, "probe", lambda host, port: states.get(port, "other"))
    assert launcher.find_port("127.0.0.1", other_port) == (other_port + 2, "free")
    assert launcher.find_port("127.0.0.1", other_port, span=2) == (None, "other")


def test_merged_path_keeps_order_drops_repeats_and_missing_dirs(tmp_path, monkeypatch):
    a, b, home_bin = tmp_path / "a", tmp_path / "b", tmp_path / "home" / "bin"
    for d in (a, b, home_bin):
        d.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    sep = os.pathsep
    merged = launcher.merged_path(f"{a}{sep}{b}", f"{b}{sep}{tmp_path / 'missing'}", "~/bin")
    assert merged.split(sep) == [str(a), str(b), str(home_bin)]


@pytest.mark.parametrize(
    "args, port", [(["--serve"], None), (["--serve", "--port", "8770"], 8770), (["--port"], None)]
)
def test_the_server_port_comes_from_the_command_line(args, port):
    assert launcher._port_arg(args) == port


# -- where the app keeps its data ----------------------------------------------


def test_the_app_keeps_its_data_in_the_platforms_user_folder(monkeypatch):
    home = Path("/Users/jane")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    assert user_data_dir("darwin", home) == home / "Library/Application Support/Resumap"
    assert user_data_dir("linux", home) == home / ".local/share/resumap"
    assert user_data_dir("win32", home) == home / "AppData/Roaming/Resumap"


def test_the_server_log_starts_afresh_once_it_outgrows_its_limit(tmp_path):
    log = tmp_path / "server.log"
    launcher.rotate_log(log, limit=10)  # nothing there yet: nothing to do
    log.write_text("short")
    launcher.rotate_log(log, limit=10)
    assert log.read_text() == "short"

    log.write_text("x" * 11)
    launcher.rotate_log(log, limit=10)
    assert not log.exists()
    assert (tmp_path / "server.log.1").read_text() == "x" * 11

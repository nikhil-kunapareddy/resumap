"""Only this machine's own pages may drive the local API (api/guard.py)."""

from __future__ import annotations

from resumap.api.guard import LOOPBACK, trusted_hosts


def test_a_request_addressed_to_another_host_is_refused(client):
    # What a DNS-rebinding page sends: the port is ours, the name is theirs.
    r = client.get("/api/resumes", headers={"host": "rebound.example.com:8000"})
    assert r.status_code == 403


def test_every_loopback_name_is_answered(client):
    for host in ("127.0.0.1:8000", "localhost:8000", "[::1]:8000"):
        assert client.get("/api/resumes", headers={"host": host}).status_code == 200


def test_a_write_from_another_website_is_refused(client):
    r = client.post("/api/quit", headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post(
        "/api/resumes",
        headers={"origin": "null"},
        files={"file": ("cv.txt", b"x", "text/plain")},
    )
    assert r.status_code == 403


def test_the_ui_own_origin_and_originless_clients_are_let_through(client):
    same = {"origin": "http://127.0.0.1", "host": "127.0.0.1"}
    # 409 is the test app refusing to quit: the guard let the request through.
    assert client.post("/api/quit", headers=same).status_code == 409
    assert client.post("/api/quit").status_code == 409


def test_reads_carry_no_origin_check(client):
    r = client.get("/api/resumes", headers={"origin": "https://evil.example"})
    assert r.status_code == 200


def test_the_trusted_hosts_follow_the_bind_address():
    assert trusted_hosts("127.0.0.1") == LOOPBACK
    assert trusted_hosts("192.168.1.20") == LOOPBACK | {"192.168.1.20"}
    assert trusted_hosts("0.0.0.0") is None
    assert trusted_hosts("::") is None

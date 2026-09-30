"""Failure is data: what still works, and what is reported, when a backend fails.

With no backend, upload, parse and map keep working (JD points come from
parsing/jd.py, not a model) while transform, corrections and outreach return
503. A backend that answers badly costs one bullet or one draft, never the
batch or the state already saved.
"""
from __future__ import annotations

from tests.conftest import upload
from tests.fixtures.resumes import JOB_DESCRIPTION, JOB_POINTS

DOWN = "The claude CLI is signed out."


def test_without_a_backend_the_first_three_steps_still_work(client, fake, resume_files):
    fake.down = DOWN
    status = client.get("/api/status").json()
    assert (status["ready"], status["detail"]) == (False, DOWN)

    resume = upload(client, resume_files["pdf"])
    r = client.post("/api/jobs", json={"resume_id": resume["id"], "text": JOB_DESCRIPTION})
    assert r.status_code == 200
    job = r.json()
    assert [p["text"] for p in job["points"]] == JOB_POINTS
    assert job["source"] == "verbatim" and job["warning"].startswith(DOWN)

    r = client.post(f"/api/jobs/{job['id']}/mappings",
                    json={"block_id": "bul005", "point_ids": ["jd01"]})
    assert r.status_code == 200
    # The preview is built locally, so it needs no backend either.
    preview = client.get(f"/api/jobs/{job['id']}/prompt", params={"block_id": "bul005"})
    assert preview.status_code == 200

    for method, path, body in (
        ("post", "transform", {}),
        ("post", "chat", {"message": "Tighten it"}),
        ("post", "outreach", {}),
    ):
        r = getattr(client, method)(f"/api/jobs/{job['id']}/{path}", json=body)
        assert (r.status_code, r.json()["detail"]) == (503, DOWN), path
    assert fake.calls == []

    # Export needs no model: an untouched resume still renders.
    assert client.post(f"/api/jobs/{job['id']}/export").json()["pages"] == 1


def test_a_failed_bullet_is_reported_and_its_siblings_are_saved(client, fake, job):
    def flaky(system, user):
        if "Reduced nightly batch runtime" in user:
            raise TimeoutError("no answer in 120s")
        return {"rewritten": "Fine.", "keywords_used": [], "substitutions": [],
                "defensible": True}
    fake.respond("Rewrite", flaky)
    for box in ("bul005", "bul006", "bul007"):
        client.post(f"/api/jobs/{job['id']}/mappings", json={"block_id": box,
                                                             "point_ids": ["jd01"]})
    out = client.post(f"/api/jobs/{job['id']}/transform", json={}).json()
    failed = [r for r in out["results"] if not r["ok"]]
    assert [(r["block_id"], r["error"]) for r in failed] == [
        ("bul006", "TimeoutError: no answer in 120s")]
    assert sorted(out["rewrites"]) == ["bul005", "bul007"]


def test_a_reroll_that_fails_keeps_the_previous_rewrite(client, fake, job):
    client.post(f"/api/jobs/{job['id']}/mappings", json={"block_id": "bul005",
                                                         "point_ids": ["jd01"]})
    first = client.post(f"/api/jobs/{job['id']}/transform", json={}).json()
    fake.respond("Rewrite", None)
    again = client.post(f"/api/jobs/{job['id']}/transform", json={"block_ids": ["bul005"]})
    assert again.json()["results"][0]["ok"] is False
    assert again.json()["rewrites"] == first["rewrites"]


def test_a_broken_outreach_reply_is_a_502_and_saves_nothing(client, fake, job):
    fake.respond("Outreach", None)
    r = client.post(f"/api/jobs/{job['id']}/outreach", json={})
    assert r.status_code == 502 and "no parsable outreach" in r.json()["detail"]
    assert client.get(f"/api/jobs/{job['id']}/outreach").json() == {"outreach": None}


def test_a_broken_correction_reply_is_a_502_and_changes_nothing(client, fake, job):
    fake.respond("PdfRevision", None)
    r = client.post(f"/api/jobs/{job['id']}/chat", json={"message": "Tighten it"})
    assert r.status_code == 502
    chat = client.get(f"/api/jobs/{job['id']}/chat").json()
    assert (chat["messages"], chat["rewrites"]) == ([], {})


def test_a_failing_test_call_reports_why(client, fake):
    def unauthorised(system, user):
        raise RuntimeError("401 Unauthorized")
    fake.respond("Ping", unauthorised)
    r = client.post("/api/backends/api/test")
    assert r.json() == {"ok": False, "detail": "401 Unauthorized", "ms": None}

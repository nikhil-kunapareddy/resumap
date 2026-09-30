"""The HTTP API, step by step, the way the UI drives it.

Upload, job description, map, transform, review, export, corrections chat and
outreach, all against throwaway storage and the scripted backend.
"""
from __future__ import annotations

import io
import re

import pdfplumber
import pytest

from resumap.rendering.pdfgen import LAYOUT_DEFAULTS
from tests.conftest import upload
from tests.fakes import EXPERIENCE_TEXT, INVENTED_TERM, REPLACED_TEXT, tailored
from tests.fixtures.resumes import BULLETS, JOB_DESCRIPTION, JOB_POINTS

EXP_NOTES = "Wrote a Go CLI that lints Airflow DAGs before deploy; 40 stars on GitHub."


# -- helpers ----------------------------------------------------------------

def text_of(resume: dict, block_id: str) -> str:
    return next(b["text"] for b in resume["blocks"] if b["id"] == block_id)


def map_box(client, job: dict, block_id: str, *points: str) -> None:
    r = client.post(f"/api/jobs/{job['id']}/mappings",
                    json={"block_id": block_id, "point_ids": list(points)})
    assert r.status_code == 200, r.text


def add_experience(client, text: str = EXP_NOTES, kind: str = "project") -> dict:
    r = client.post("/api/experiences", json={"kind": kind, "title": "DAG linter",
                                              "text": text})
    assert r.status_code == 200, r.text
    return r.json()


def transform(client, job: dict, **body) -> dict:
    r = client.post(f"/api/jobs/{job['id']}/transform", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def patch_rewrite(client, job: dict, block_id: str, **body):
    return client.patch(f"/api/jobs/{job['id']}/rewrites/{block_id}", json=body)


def rewrites(client, job: dict) -> dict:
    return client.get(f"/api/jobs/{job['id']}").json()["rewrites"]


def export(client, job: dict) -> tuple[dict, str]:
    """Export, download, and return the response plus the PDF's text layer."""
    r = client.post(f"/api/jobs/{job['id']}/export")
    assert r.status_code == 200, r.text
    out = r.json()
    pdf = client.get(out["url"])
    assert pdf.status_code == 200
    with pdfplumber.open(io.BytesIO(pdf.content)) as doc:
        text = " ".join(p.extract_text() or "" for p in doc.pages)
    return out, re.sub(r"\s+", " ", text)


# -- upload -----------------------------------------------------------------

def test_upload_returns_blocks_without_internal_geometry(client, resume):
    assert resume["name"] == "Jane Doe"
    assert [b["text"] for b in resume["blocks"] if b["mappable"]] == BULLETS
    assert all("_line" not in b for b in resume["blocks"])
    [listed] = client.get("/api/resumes").json()
    assert (listed["id"], listed["label"], listed["job_count"]) == (resume["id"], "Jane", 0)


def test_upload_writes_the_parse_report(client, resume):
    r = client.get(f"/api/debug/parse/{resume['id']}")
    assert r.status_code == 200
    for b in resume["blocks"]:
        assert b["id"] in r.text


def test_upload_rejects_an_unsupported_file_type(client, tmp_path):
    png = tmp_path / "resume.png"
    png.write_bytes(b"\x89PNG")
    with png.open("rb") as fh:
        r = client.post("/api/resumes", files={"file": (png.name, fh)})
    assert r.status_code == 400 and "Unsupported file type" in r.json()["detail"]


def test_upload_rejects_a_file_with_no_text_and_keeps_nothing(client, paths, tmp_path):
    empty = tmp_path / "empty.txt"
    empty.write_text("\n\n")
    with empty.open("rb") as fh:
        r = client.post("/api/resumes", files={"file": (empty.name, fh)})
    assert r.status_code == 400 and "No text found" in r.json()["detail"]
    assert list(paths.uploads.iterdir()) == []
    assert client.get("/api/resumes").json() == []


def test_deleting_a_resume_removes_its_file_and_jobs(client, paths, resume, job):
    assert client.delete(f"/api/resumes/{resume['id']}").json() == {"ok": True}
    assert client.get(f"/api/resumes/{resume['id']}").status_code == 404
    assert client.get(f"/api/jobs/{job['id']}").status_code == 404
    assert list(paths.uploads.iterdir()) == []


# -- job description --------------------------------------------------------

def test_job_points_are_verbatim_and_tagged(job):
    assert [p["text"] for p in job["points"]] == JOB_POINTS
    assert [p["id"] for p in job["points"]] == ["jd01", "jd02", "jd03", "jd04", "jd05"]
    assert (job["company"], job["role"]) == ("Contoso", "Data Engineer")
    assert job["source"] == "verbatim, labelled by Fake" and job["warning"] is None
    assert all(INVENTED_TERM not in p["keywords"] for p in job["points"])


@pytest.mark.parametrize("text, message", [
    ("Too short.", "too short"),
    ("Benefits\n- Free lunch and snacks every single day of the week.", "Could not find"),
])
def test_a_job_description_without_requirements_is_rejected(client, resume, text, message):
    r = client.post("/api/jobs", json={"resume_id": resume["id"], "text": text})
    assert r.status_code == 400 and message in r.json()["detail"]


def test_a_job_for_a_missing_resume_is_404(client):
    r = client.post("/api/jobs", json={"resume_id": 99, "text": JOB_DESCRIPTION})
    assert r.status_code == 404


def test_job_detail_carries_everything_the_ui_needs(client, job):
    detail = client.get(f"/api/jobs/{job['id']}").json()
    assert set(detail) == {"id", "resume_id", "company", "role", "mode", "raw", "points",
                           "mappings", "box_modes", "rewrites", "outreach", "resume"}
    assert detail["raw"] == JOB_DESCRIPTION
    assert detail["resume"]["name"] == "Jane Doe"


def test_patching_a_job_changes_only_what_was_sent_and_returns_the_job(client, job):
    r = client.patch(f"/api/jobs/{job['id']}", json={"role": "Staff Data Engineer",
                                                     "mode": "truthful"})
    assert set(r.json()) == {"id", "resume_id", "company", "role", "mode", "created_at"}
    assert (r.json()["id"], r.json()["company"], r.json()["role"], r.json()["mode"]) == (
        job["id"], "Contoso", "Staff Data Engineer", "truthful")
    detail = client.get(f"/api/jobs/{job['id']}").json()
    assert (detail["company"], detail["role"], detail["mode"]) == (
        "Contoso", "Staff Data Engineer", "truthful")


def test_dropping_a_point_unwires_it_from_every_box(client, job):
    map_box(client, job, "bul005", "jd01", "jd02")
    r = client.put(f"/api/jobs/{job['id']}/points",
                   json={"keep": ["jd01", "jd03", "jd04", "jd05"]})
    assert r.status_code == 200
    assert (r.json()["dropped"], r.json()["unwired"]) == (["jd02"], 1)
    assert client.get(f"/api/jobs/{job['id']}").json()["mappings"] == {"bul005": ["jd01"]}
    r = client.post(f"/api/jobs/{job['id']}/mappings",
                    json={"block_id": "bul006", "point_ids": ["jd02"]})
    assert r.status_code == 400


@pytest.mark.parametrize("keep", [["jd01", "jd99"], []])
def test_keeping_unknown_or_no_points_is_rejected(client, job, keep):
    assert client.put(f"/api/jobs/{job['id']}/points", json={"keep": keep}).status_code == 400


# -- map --------------------------------------------------------------------

def test_mapping_to_an_unknown_point_is_rejected(client, job):
    r = client.post(f"/api/jobs/{job['id']}/mappings",
                    json={"block_id": "bul005", "point_ids": ["jd01", "jd42"]})
    assert r.status_code == 400 and "jd42" in r.json()["detail"]


def test_box_modes(client, job):
    url = f"/api/jobs/{job['id']}/box-modes"
    assert client.post(url, json={"block_id": "bul006", "mode": "replace"}).status_code == 200
    assert client.get(f"/api/jobs/{job['id']}").json()["box_modes"] == {"bul006": "replace"}
    assert client.post(url, json={"block_id": "bul006", "mode": "banana"}).status_code == 422
    assert client.post(url, json={"block_id": "exp001", "mode": "replace"}).status_code == 400
    assert client.post(url, json={"block_id": "bul006", "mode": ""}).status_code == 200
    assert client.get(f"/api/jobs/{job['id']}").json()["box_modes"] == {}


# -- transform --------------------------------------------------------------

def test_transform_needs_a_mapped_box(client, job):
    r = client.post(f"/api/jobs/{job['id']}/transform", json={})
    assert r.status_code == 400 and "Nothing to transform" in r.json()["detail"]


def test_transform_realigns_replaces_and_writes_from_experiences(client, fake, resume, job):
    exp = add_experience(client)
    map_box(client, job, "bul005", "jd01")
    map_box(client, job, "bul006", "jd02")
    client.post(f"/api/jobs/{job['id']}/box-modes", json={"block_id": "bul006",
                                                          "mode": "replace"})
    map_box(client, job, exp["key"], "jd04")

    out = transform(client, job)
    assert sorted((r["block_id"], r["ok"]) for r in out["results"]) == sorted([
        (exp["key"], True), ("bul005", True), ("bul006", True)])
    rws = out["rewrites"]
    assert {bid: rw["status"] for bid, rw in rws.items()} == {
        "bul005": "pending", "bul006": "pending", exp["key"]: "pending"}

    realigned = rws["bul005"]
    assert realigned["original"] == text_of(resume, "bul005")
    assert realigned["rewritten"] == tailored(text_of(resume, "bul005"))
    assert realigned["meta"]["substitutions"][0]["grounded"] is False
    assert realigned["meta"]["replaced"] is False

    replaced = rws["bul006"]
    assert replaced["rewritten"] == REPLACED_TEXT and replaced["meta"]["replaced"] is True

    written = rws[exp["key"]]
    assert (written["original"], written["rewritten"]) == (EXP_NOTES, EXPERIENCE_TEXT)
    assert written["meta"]["placement"] == "new:Open Source"
    assert written["meta"]["placement_user"] is False

    assert len(fake.calls_for("Rewrite")) == 2 and len(fake.calls_for("NewBullet")) == 1


@pytest.mark.parametrize("box, setup", [("bul005", None), ("bul006", "replace"), ("exp", None)])
def test_prompt_preview_is_exactly_what_transform_sends(client, fake, job, box, setup):
    if box == "exp":
        box = add_experience(client)["key"]
    map_box(client, job, box, "jd01")
    if setup:
        client.post(f"/api/jobs/{job['id']}/box-modes", json={"block_id": box, "mode": setup})
    preview = client.get(f"/api/jobs/{job['id']}/prompt", params={"block_id": box}).json()
    transform(client, job)
    sent = fake.calls[-1]
    assert (preview["system"], preview["user"]) == (sent.system, sent.user)


def test_prompt_preview_of_an_unmapped_box_is_rejected(client, job):
    r = client.get(f"/api/jobs/{job['id']}/prompt", params={"block_id": "bul005"})
    assert r.status_code == 400


def test_a_reroll_keeps_a_placement_the_user_chose(client, job):
    exp = add_experience(client)
    map_box(client, job, exp["key"], "jd04")
    transform(client, job)
    r = patch_rewrite(client, job, exp["key"], placement="ent004")
    assert r.status_code == 200 and r.json()["meta"]["placement_user"] is True
    transform(client, job, block_ids=[exp["key"]])
    assert rewrites(client, job)[exp["key"]]["meta"]["placement"] == "ent004"


@pytest.mark.parametrize("placement", ["ent999", "bul005", "new:", "new:" + "X" * 41])
def test_a_placement_must_name_an_entry_a_section_or_a_short_heading(client, job, placement):
    exp = add_experience(client)
    map_box(client, job, exp["key"], "jd04")
    transform(client, job)
    assert patch_rewrite(client, job, exp["key"], placement=placement).status_code == 400


# -- review -----------------------------------------------------------------

@pytest.fixture
def transformed(client, job) -> dict:
    """bul005, bul009 and bul010 mapped and transformed, all pending."""
    for box, point in (("bul005", "jd01"), ("bul009", "jd03"), ("bul010", "jd02")):
        map_box(client, job, box, point)
    transform(client, job)
    return job


def test_review_status_changes(client, transformed):
    job = transformed
    r = patch_rewrite(client, job, "bul005", status="accepted")
    assert r.status_code == 200 and r.json()["status"] == "accepted"
    r = patch_rewrite(client, job, "bul009", text="  Designed a PostgreSQL model.  ")
    assert (r.json()["status"], r.json()["rewritten"]) == ("edited", "Designed a PostgreSQL model.")
    assert patch_rewrite(client, job, "bul010", status="banana").status_code == 422
    assert patch_rewrite(client, job, "bul013", status="accepted").status_code == 404


def test_an_unknown_status_is_rejected_even_with_text(client, transformed):
    before = rewrites(client, transformed)["bul005"]
    r = patch_rewrite(client, transformed, "bul005", text="New text.", status="banana")
    assert r.status_code == 422 and r.json()["detail"].startswith("status: ")
    assert rewrites(client, transformed)["bul005"] == before


def test_accept_all_accepts_only_what_is_pending(client, transformed):
    job = transformed
    patch_rewrite(client, job, "bul010", status="rejected")
    rws = client.post(f"/api/jobs/{job['id']}/accept-all").json()
    assert {bid: rw["status"] for bid, rw in rws.items()} == {
        "bul005": "accepted", "bul009": "accepted", "bul010": "rejected"}


# -- export -----------------------------------------------------------------

def test_export_applies_accepted_and_edited_rewrites_only(client, resume, transformed):
    job = transformed
    patch_rewrite(client, job, "bul005", status="accepted")
    patch_rewrite(client, job, "bul009", text="Designed a PostgreSQL model that cut query times tenfold.")
    out, text = export(client, job)
    assert (out["applied"], out["total"], out["pages"]) == (2, 3, 1)
    assert out["check"]["ok"] is True
    assert tailored(text_of(resume, "bul005")) in text
    assert "Designed a PostgreSQL model that cut query times tenfold." in text
    # Pending: the source wording, byte for byte.
    assert text_of(resume, "bul010") in text
    for untouched in ("bul006", "bul007", "bul013"):
        assert text_of(resume, untouched) in text


def test_an_accepted_experience_bullet_is_spliced_into_the_export(client, job):
    exp = add_experience(client)
    map_box(client, job, exp["key"], "jd04")
    transform(client, job)
    _, text = export(client, job)
    assert EXPERIENCE_TEXT not in text
    patch_rewrite(client, job, exp["key"], status="accepted")
    out, text = export(client, job)
    assert out["pages"] == 1
    assert "Open Source" in text and EXPERIENCE_TEXT in text


def test_the_export_filename_follows_the_settings(client, job):
    r = client.post("/api/settings", json={"first_name": "Jane Doe!"})
    assert r.json() == {"first_name": "janedoe"}
    out, _ = export(client, job)
    assert re.fullmatch(r"janedoe_contoso_\d{4}\.pdf", out["filename"])


def test_preview_and_fit(client, job):
    r = client.get(f"/api/jobs/{job['id']}/preview.pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    fit = client.get(f"/api/jobs/{job['id']}/fit").json()
    assert fit["pages"] == 1 and "blocks" not in fit and "path" not in fit


@pytest.mark.parametrize("name", ["missing.pdf", "../resumap.db"])
def test_download_serves_only_existing_exports(client, name):
    assert client.get(f"/api/download/{name}").status_code == 404


# -- corrections chat -------------------------------------------------------

def chat_edit(fake, block_id: str, text: str, font: float | None = None) -> None:
    fake.respond("PdfRevision", {
        "reply": "Done.",
        "edits": [{"block_id": block_id, "text": text, "substitutions": [],
                   "defensible": True}],
        "layout": [{"setting": "font_size", "value": font}] if font else [],
        "justify": "unchanged",
    })


def test_a_correction_edits_the_pdf_and_can_be_undone(client, fake, job):
    chat_edit(fake, "bul007", "Led 4 engineers through a 120-DAG Airflow migration.", font=95)
    r = client.post(f"/api/jobs/{job['id']}/chat", json={"message": "Shorten the Airflow line"})
    assert r.status_code == 200, r.text
    state = r.json()
    rw = state["rewrites"]["bul007"]
    assert (rw["status"], rw["meta"]["source"]) == ("edited", "chat")
    assert state["layout"]["font"] == 0.95
    user, assistant = state["messages"]
    assert (user["role"], assistant["role"], assistant["can_undo"]) == ("user", "assistant", True)
    assert "before" not in assistant["meta"]
    _, text = export(client, job)
    assert "Led 4 engineers through a 120-DAG Airflow migration." in text

    undone = client.post(f"/api/jobs/{job['id']}/chat/{assistant['id']}/undo").json()
    assert "bul007" not in undone["rewrites"]
    assert undone["layout"] == LAYOUT_DEFAULTS
    assert undone["messages"][1]["can_undo"] is False
    again = client.post(f"/api/jobs/{job['id']}/chat/{assistant['id']}/undo")
    assert again.status_code == 400


def test_only_the_latest_correction_can_be_undone(client, fake, job):
    chat_edit(fake, "bul007", "First edit of the Airflow line.")
    first = client.post(f"/api/jobs/{job['id']}/chat", json={"message": "one"}).json()
    chat_edit(fake, "bul007", "Second edit of the Airflow line.")
    client.post(f"/api/jobs/{job['id']}/chat", json={"message": "two"})
    first_id = first["messages"][1]["id"]
    assert client.post(f"/api/jobs/{job['id']}/chat/{first_id}/undo").status_code == 409


def test_a_correction_needs_a_message(client, job):
    assert client.post(f"/api/jobs/{job['id']}/chat", json={"message": "  "}).status_code == 400


# -- outreach ---------------------------------------------------------------

def test_outreach_quotes_what_the_exported_pdf_will_say(client, fake, resume, transformed):
    job = transformed
    patch_rewrite(client, job, "bul005", status="accepted")
    r = client.post(f"/api/jobs/{job['id']}/outreach", json={"recruiter": "Sam"})
    assert r.status_code == 200, r.text
    draft = r.json()["outreach"]
    assert draft["recruiter"] == "Sam" and draft["linkedin"].startswith("Hi Sam")
    assert draft["meta"]["linkedin_limit"] == 300
    sent = fake.calls_for("Outreach")[0].user
    assert tailored(text_of(resume, "bul005")) in sent       # accepted: tailored wording
    assert text_of(resume, "bul005") not in sent
    assert text_of(resume, "bul009") in sent                 # mapped, not accepted
    assert text_of(resume, "bul013") not in sent             # neither


def test_outreach_before_any_mapping_uses_the_resumes_own_points(client, fake, resume, job):
    client.post(f"/api/jobs/{job['id']}/outreach", json={})
    sent = fake.calls_for("Outreach")[0].user
    assert all(bullet in sent for bullet in BULLETS)


def test_an_outreach_draft_can_be_edited(client, job):
    url = f"/api/jobs/{job['id']}/outreach"
    assert client.patch(url, json={"linkedin": "Hi."}).status_code == 404
    client.post(url, json={})
    r = client.patch(url, json={"linkedin": "  Hi Sam, short note.  ", "recruiter": "Sam"})
    draft = r.json()["outreach"]
    assert (draft["linkedin"], draft["recruiter"]) == ("Hi Sam, short note.", "Sam")
    assert client.get(url).json()["outreach"]["linkedin"] == "Hi Sam, short note."


def test_liked_messages_are_kept_and_sent_as_style_references(client, fake, job):
    body = "Hi Alex, I loved your talk on data contracts and would like to connect."
    r = client.post("/api/outreach-examples", json={"kind": "linkedin", "body": body})
    assert r.status_code == 200
    example = r.json()
    assert client.get("/api/outreach-examples").json()[0]["id"] == example["id"]
    client.post(f"/api/jobs/{job['id']}/outreach", json={})
    assert body in fake.calls_for("Outreach")[0].user
    assert client.delete(f"/api/outreach-examples/{example['id']}").status_code == 200
    assert client.delete(f"/api/outreach-examples/{example['id']}").status_code == 404


@pytest.mark.parametrize("payload, status", [
    ({"kind": "tweet", "body": "A long enough message body to pass the length check."}, 422),
    ({"kind": "email", "body": "Too short."}, 400),
    ({"kind": "email", "body": "x" * 4001}, 400),
])
def test_outreach_examples_are_validated(client, payload, status):
    assert client.post("/api/outreach-examples", json=payload).status_code == status


# -- experiences ------------------------------------------------------------

@pytest.mark.parametrize("payload, status", [
    ({"kind": "hobby", "text": "Ran a marathon in under four hours."}, 422),
    ({"kind": "project", "text": "Too short."}, 400),
])
def test_experiences_are_validated(client, payload, status):
    assert client.post("/api/experiences", json=payload).status_code == status


def test_deleting_an_experience_unpicks_it_in_every_job(client, resume, job):
    other = client.post("/api/jobs", json={"resume_id": resume["id"],
                                           "text": JOB_DESCRIPTION}).json()
    exp = add_experience(client)
    map_box(client, job, exp["key"], "jd04")
    map_box(client, other, exp["key"], "jd01")
    transform(client, job)
    r = client.delete(f"/api/experiences/{exp['id']}")
    assert r.json() == {"ok": True, "mappings": 2, "rewrites": 1}
    for j in (job, other):
        detail = client.get(f"/api/jobs/{j['id']}").json()
        assert exp["key"] not in detail["mappings"] and exp["key"] not in detail["rewrites"]
    assert client.delete(f"/api/experiences/{exp['id']}").status_code == 404


# -- settings, backends and prompts -----------------------------------------

def test_status_reports_the_live_backend(client):
    status = client.get("/api/status").json()
    assert (status["ready"], status["label"], status["model"]) == (True, "Fake", "fake-model")
    assert status["first_name"] == "resume"


def test_a_backend_is_tested_with_a_real_call_before_it_is_saved(client, fake, store):
    r = client.post("/api/backends", json={"backend": "claude_code", "model": "claude-opus-5-5"})
    assert r.status_code == 200 and r.json()["backend"] == "claude_code"
    assert store.settings.get("backend") == "claude_code"

    fake.respond("Ping", None)
    r = client.post("/api/backends", json={"backend": "codex"})
    assert r.status_code == 400 and r.json()["detail"].startswith("Not switched")
    assert store.settings.get("backend") == "claude_code"


@pytest.mark.parametrize("payload", [{"backend": "gemini"},
                                     {"backend": "api", "model": "two words"}])
def test_an_invalid_backend_choice_is_rejected(client, payload):
    assert client.post("/api/backends", json=payload).status_code == 400


def test_the_settings_panel_lists_every_backend(client):
    listed = client.get("/api/backends").json()
    assert [b["name"] for b in listed["backends"]] == ["claude_code", "codex", "pi", "api"]
    assert client.post("/api/backends/api/test").json()["ok"] is True
    assert client.post("/api/backends/gemini/test").status_code == 404


def test_an_edited_prompt_is_what_transform_sends(client, fake, job):
    r = client.put("/api/prompts/rewrite", json={"text": "CUSTOM REWRITE RULES"})
    assert r.status_code == 200 and r.json()["custom"] is True
    map_box(client, job, "bul005", "jd01")
    transform(client, job)
    assert fake.calls_for("Rewrite")[-1].system.startswith("CUSTOM REWRITE RULES")

    default = next(p["default"] for p in client.get("/api/prompts").json()["prompts"]
                   if p["key"] == "rewrite")
    assert client.put("/api/prompts/rewrite", json={"text": default}).json()["custom"] is False
    client.put("/api/prompts/rewrite", json={"text": "CUSTOM AGAIN"})
    assert client.delete("/api/prompts/rewrite").json()["custom"] is False


@pytest.mark.parametrize("method, key, payload, status", [
    ("put", "nonsense", {"text": "x"}, 404),
    ("put", "rewrite", {"text": "x" * 20001}, 400),
    ("delete", "nonsense", None, 404),
])
def test_prompt_edits_are_validated(client, method, key, payload, status):
    call = getattr(client, method)
    r = call(f"/api/prompts/{key}", json=payload) if payload else call(f"/api/prompts/{key}")
    assert r.status_code == status


def test_usage_is_recorded_per_operation(client, job):
    map_box(client, job, "bul005", "jd01")
    transform(client, job)
    by_op = client.get("/api/debug/usage").json()["by_op"]
    assert {"jd_parse", "rewrite"} <= set(by_op)

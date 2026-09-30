"""The typed contract: domain models, API schemas and the vocabularies they share.

The models only help if they are complete and true, so these check that every
route declares its response, that what the app stores still fits the domain
models, and that each enum agrees with the constants it replaced.
"""
from __future__ import annotations

import typing

import pytest
from fastapi.routing import APIRoute
from pydantic import ValidationError

from resumap.api.routers import ALL as ROUTERS
from resumap.domain import (
    INCLUDED_STATUSES,
    ChatMessage,
    Experience,
    ExperienceKind,
    JDCategory,
    Job,
    Justify,
    Layout,
    OutreachDraft,
    OutreachExample,
    OutreachKind,
    Resume,
    ResumeDoc,
    Rewrite,
    RewriteMode,
    RewriteStatus,
    Run,
)
from resumap.llm import EDITABLE_PROMPTS, bullets, outreach, prompts, schemas
from resumap.main import create_app
from resumap.parsing.resume import parse_resume
from resumap.rendering.pdfgen import LAYOUT_DEFAULTS, clean_layout

# Routes that answer with a file or plain text rather than JSON.
NON_JSON = {"/api/jobs/{job_id}/preview.pdf", "/api/download/{filename}",
            "/api/debug/parse/{resume_id}"}


# -- the API contract -------------------------------------------------------

def api_routes() -> list[APIRoute]:
    return [r for router in ROUTERS for r in router.routes if isinstance(r, APIRoute)]


def openapi() -> dict:
    return create_app().openapi()


def test_every_json_route_declares_its_response_model():
    untyped = sorted(f"{sorted(r.methods)[0]} {r.path}" for r in api_routes()
                     if r.path not in NON_JSON and r.response_model is None)
    assert untyped == []


def test_the_openapi_document_describes_every_json_response():
    spec = openapi()
    for route in api_routes():
        if route.path in NON_JSON:
            continue
        for method in route.methods:
            op = spec["paths"][route.path][method.lower()]
            schema = op["responses"]["200"]["content"]["application/json"]["schema"]
            assert schema, f"{method} {route.path} has an empty response schema"


def test_validation_errors_are_documented_as_the_body_actually_sent():
    spec = openapi()
    op = spec["paths"]["/api/jobs"]["post"]
    ref = op["responses"]["422"]["content"]["application/json"]["schema"]["$ref"]
    assert ref.endswith("/ErrorOut")


def test_a_validation_error_has_one_readable_detail(client, resume):
    r = client.post("/api/jobs", json={"resume_id": resume["id"], "text": "x" * 50,
                                       "mode": "reckless"})
    assert r.status_code == 422
    body = r.json()
    assert body["detail"] == ("mode: Input should be 'aggressive', 'bridge' or 'truthful'")
    assert body["errors"][0]["loc"] == ["body", "mode"]


def test_a_missing_field_names_the_field(client):
    r = client.post("/api/jobs", json={"text": "x" * 50})
    assert (r.status_code, r.json()["detail"]) == (422, "resume_id: Field required")


# -- stored data fits the domain models -------------------------------------

@pytest.mark.parametrize("fmt", ["pdf", "docx", "txt"])
def test_a_parsed_resume_round_trips_through_the_model_unchanged(resume_files, fmt):
    doc = parse_resume(str(resume_files[fmt]))
    assert ResumeDoc.model_validate(doc).model_dump(by_alias=True) == doc


def test_every_stored_row_fits_its_entity(client, fake, store, resume, job):
    exp = client.post("/api/experiences", json={"kind": "project", "text": "Wrote a Go CLI "
                                                "that lints Airflow DAGs before deploy."}).json()
    for box, point in (("bul005", "jd01"), (exp["key"], "jd04")):
        client.post(f"/api/jobs/{job['id']}/mappings", json={"block_id": box,
                                                             "point_ids": [point]})
    client.post(f"/api/jobs/{job['id']}/transform", json={})
    client.post(f"/api/jobs/{job['id']}/outreach", json={"recruiter": "Sam"})
    client.post("/api/outreach-examples", json={"kind": "email", "body": "Hi Alex, thanks "
                                                "for the call today."})
    fake.respond("PdfRevision", {"reply": "Done.", "edits": [], "justify": "unchanged",
                                 "layout": [{"setting": "font_size", "value": 95}]})
    client.post(f"/api/jobs/{job['id']}/chat", json={"message": "Smaller type"})

    # Repositories validate every row they read, so reading each one back is
    # the check; the isinstance asserts only confirm what came back.
    assert isinstance(store.resumes.get(resume["id"]), Resume)
    assert isinstance(store.jobs.get(job["id"]), Job)
    rewrites = store.rewrites.for_job(job["id"])
    assert len(rewrites) == 2 and all(isinstance(rw, Rewrite) for rw in rewrites.values())
    assert isinstance(store.outreach.get(job["id"]), OutreachDraft)
    assert all(isinstance(e, Experience) for e in store.experiences.all())
    assert all(isinstance(e, OutreachExample) for e in store.examples.all())
    messages = store.chat.for_job(job["id"])
    assert len(messages) == 2 and all(isinstance(m, ChatMessage) for m in messages)
    Layout.model_validate(store.layouts.get(job["id"]))


def test_entities_reject_unknown_fields_and_documents_keep_them():
    with pytest.raises(ValidationError):
        OutreachExample.model_validate({"id": 1, "kind": "email", "body": "Hi.",
                                        "created_at": 0.0, "surprise": True})
    run = Run.model_validate({"text": "LinkedIn", "bold": False, "italic": False,
                              "added_later": "kept"})
    assert run.model_dump()["added_later"] == "kept"


# -- the enums agree with the constants that use them -----------------------

def test_rewrite_modes_match_the_mode_rules_and_editable_prompts():
    assert set(prompts.MODE_RULES) == set(RewriteMode)
    modes = {k[5:] for k in EDITABLE_PROMPTS if k.startswith("mode.")}
    assert modes == {m.value for m in RewriteMode}


def test_experience_and_outreach_kinds_match_their_labels():
    assert set(bullets.EXPERIENCE_KINDS) == set(ExperienceKind)
    assert set(outreach.OUTREACH_EXAMPLE_KINDS) == set(OutreachKind)


def test_jd_categories_match_what_the_model_may_answer():
    category = schemas.JDTag.model_fields["category"].annotation
    assert set(typing.get_args(category)) == {c.value for c in JDCategory}


def test_layout_defaults_match_the_renderer():
    assert Layout().model_dump() == LAYOUT_DEFAULTS
    for justify in Justify:
        assert clean_layout({"justify": justify.value})["justify"] == justify.value


def test_only_accepted_and_edited_rewrites_reach_the_pdf():
    assert {s for s in RewriteStatus if s.included} == INCLUDED_STATUSES == {
        RewriteStatus.ACCEPTED, RewriteStatus.EDITED}
    # Statuses come back from SQLite as plain strings.
    assert "accepted" in INCLUDED_STATUSES and "pending" not in INCLUDED_STATUSES

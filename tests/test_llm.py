"""llm/: what goes out in each prompt, and what the gateway checks on the way back.

The model's reply is never trusted for the rules the rest of the app depends
on, so these run every call against the scripted backend and assert on the
validation around it rather than on the prompt wording.
"""

from __future__ import annotations

import asyncio

import pytest

from resumap import obs
from resumap.llm import (
    BackendUnavailable,
    ModelGateway,
    PromptStore,
    bullets,
    outreach,
    prompts,
    tagging,
)
from resumap.llm.schemas import JDTag
from resumap.rendering.pdfgen import clean_layout
from tests.fakes import INVENTED_TERM, tailored
from tests.fixtures.resumes import JOB_DESCRIPTION, JOB_POINTS

POINT = {
    "id": "jd01",
    "text": "Build streaming pipelines with Kafka and Python.",
    "category": "requirement",
    "keywords": ["Kafka"],
}
BULLET = "Built a streaming ingestion pipeline in Python and Kafka for 2 million events a day."


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def gateway(registry, paths) -> ModelGateway:
    """The real gateway, over the scripted backend and the default prompts."""
    return ModelGateway(registry, PromptStore(), paths.logs)


def rewrite_args(**over) -> dict:
    return {
        "bullet": BULLET,
        "section": "EXPERIENCE",
        "jd_points": [POINT],
        "role": "Data Engineer",
        "company": "Contoso",
        "mode": "aggressive",
        "extra": "",
        "max_chars": 120,
        **over,
    }


# -- JD labelling -----------------------------------------------------------


def test_apply_tags_keeps_only_keywords_that_occur_in_the_point():
    points = [
        dict(POINT, keywords=[]),
        {"id": "jd02", "text": "Own Spark jobs.", "category": "requirement", "keywords": []},
    ]
    tagging.apply_tags(
        points,
        [JDTag(id="jd01", category="tooling", keywords=["kafka", "Kubernetes", " ", "Python"])],
    )
    assert points[0]["category"] == "tooling"
    assert points[0]["keywords"] == ["kafka", "Python"]
    # An untagged point keeps its placeholder labels.
    assert (points[1]["category"], points[1]["keywords"]) == ("requirement", [])


def test_parse_jd_labels_points_without_touching_their_text(fake, gateway):
    out = run(gateway.parse_jd(JOB_DESCRIPTION))
    assert [p["text"] for p in out["points"]] == JOB_POINTS
    assert out["source"] == "verbatim, labelled by Fake"
    assert (out["company"], out["role"]) == ("Contoso", "Data Engineer")
    for p in out["points"]:
        assert INVENTED_TERM not in p["keywords"]
        assert all(k.lower() in p["text"].lower() for k in p["keywords"])
    assert out["points"][0]["keywords"] == ["Kafka", "Python"]


def test_parse_jd_keeps_the_points_when_the_reply_does_not_validate(fake, gateway):
    fake.respond("JDTags", None)
    out = run(gateway.parse_jd(JOB_DESCRIPTION))
    assert [p["text"] for p in out["points"]] == JOB_POINTS
    assert all(p["keywords"] == [] for p in out["points"])
    assert out["source"] == "verbatim" and "word for word" in out["warning"]


def test_parse_jd_needs_no_backend(fake, gateway):
    fake.down = "No backend configured."
    out = run(gateway.parse_jd(JOB_DESCRIPTION))
    assert [p["text"] for p in out["points"]] == JOB_POINTS
    assert out["source"] == "verbatim"
    assert out["warning"].startswith("No backend configured.")
    assert fake.calls == []


# -- prompts ----------------------------------------------------------------


def test_rewrite_request_carries_the_mode_rules_and_the_budget():
    system, user = bullets.rewrite_request(
        PromptStore(), **rewrite_args(mode="truthful", extra="Lead with scale.")
    )
    assert prompts.REWRITE_SYSTEM in system
    assert prompts.MODE_RULES["truthful"] in system
    assert system.endswith("Lead with scale.")
    assert f"<original_length>{len(BULLET)} characters</original_length>" in user
    assert "<max_length>120 characters</max_length>" in user


def test_an_unknown_mode_falls_back_to_aggressive():
    system, _ = bullets.rewrite_request(PromptStore(), **rewrite_args(mode="reckless"))
    assert prompts.MODE_RULES["aggressive"] in system


def test_prompt_overrides_replace_defaults_and_ignore_unknown_or_blank_keys():
    store = PromptStore({"rewrite": "CUSTOM RULES", "mode.bridge": "  ", "not-a-prompt": "x"})
    assert store.text("rewrite") == "CUSTOM RULES"
    assert store.text("mode.bridge") == prompts.MODE_RULES["bridge"]
    system, _ = bullets.rewrite_request(store, **rewrite_args())
    assert system.startswith("CUSTOM RULES")
    assert [p["key"] for p in store.describe() if p["custom"]] == ["rewrite"]
    store.set_overrides({})
    assert not store.is_custom("rewrite")


# -- rewriting --------------------------------------------------------------


def test_rewrite_returns_the_validated_reply(fake, gateway):
    out = run(gateway.rewrite_bullet(**rewrite_args()))
    assert out["rewritten"] == tailored(BULLET)
    assert out["substitutions"][0]["grounded"] is False
    assert fake.calls_for("Rewrite")[0].model == "fake-model"


def test_a_leading_bullet_glyph_in_the_reply_is_stripped(fake, gateway):
    fake.respond(
        "Rewrite",
        {
            "rewritten": "• Built pipelines.",
            "keywords_used": [],
            "substitutions": [],
            "defensible": True,
        },
    )
    assert run(gateway.rewrite_bullet(**rewrite_args()))["rewritten"] == "Built pipelines."


def test_one_failing_bullet_never_blocks_its_siblings(fake, gateway):
    def flaky(system, user):
        if "Reduced" in user:
            raise RuntimeError("provider timeout")
        return {"rewritten": "Fine.", "keywords_used": [], "substitutions": [], "defensible": True}

    fake.respond("Rewrite", flaky)
    jobs = [
        {"block_id": bid, "args": rewrite_args(bullet=text)}
        for bid, text in (
            ("bul001", BULLET),
            ("bul002", "Reduced batch runtime by 40% with Spark tuning."),
            ("bul003", "Led 4 engineers through an Airflow migration."),
        )
    ]
    results = run(gateway.rewrite_many(jobs))
    assert [(r["block_id"], r["ok"]) for r in results] == [
        ("bul001", True),
        ("bul002", False),
        ("bul003", True),
    ]
    assert results[1]["error"] == "RuntimeError: provider timeout"


def test_an_unparsable_rewrite_is_reported_as_a_failed_bullet(fake, gateway):
    fake.respond("Rewrite", None)
    [res] = run(gateway.rewrite_many([{"block_id": "bul001", "args": rewrite_args()}]))
    assert res["ok"] is False and "no parsable rewrite" in res["error"]


def test_an_unparsable_reply_counts_as_a_call_and_one_failure(fake, gateway):
    fake.respond("Rewrite", None)
    before = obs.totals()
    run(gateway.rewrite_many([{"block_id": "bul001", "args": rewrite_args()}]))
    after = obs.totals()
    calls = before["by_op"].get("rewrite", {}).get("calls", 0)
    assert after["by_op"]["rewrite"]["calls"] == calls + 1
    assert after["failures"] == before["failures"] + 1


def test_a_call_that_raises_counts_as_one_failure(fake, gateway):
    def boom(system, user):
        raise RuntimeError("provider timeout")

    fake.respond("Rewrite", boom)
    before = obs.totals()["failures"]
    run(gateway.rewrite_many([{"block_id": "bul001", "args": rewrite_args()}]))
    assert obs.totals()["failures"] == before + 1


def test_rewriting_needs_a_backend(fake, gateway):
    fake.down = "Signed out."
    with pytest.raises(BackendUnavailable, match="Signed out."):
        run(gateway.rewrite_bullet(**rewrite_args()))


# -- experiences ------------------------------------------------------------

EXPERIENCE = {
    "id": 1,
    "key": "exp001",
    "kind": "project",
    "title": "DAG linter",
    "text": "Wrote a Go CLI that lints Airflow DAGs; 40 stars on GitHub.",
}
PLACEMENTS = [
    {"id": "ent004", "kind": "entry", "text": "Senior Data Engineer", "section": "EXPERIENCE"}
]


def _experience(fake, gateway, placement: str, new_section: str = "", kind: str = "project") -> str:
    fake.respond(
        "NewBullet",
        {
            "bullet": "Built a DAG linter.",
            "keywords_used": [],
            "substitutions": [],
            "defensible": True,
            "placement": placement,
            "new_section": new_section,
        },
    )
    out = run(
        gateway.write_experience_bullet(
            experience=dict(EXPERIENCE, kind=kind),
            jd_points=[POINT],
            role="Data Engineer",
            company="Contoso",
            placements=PLACEMENTS,
            examples=[BULLET],
        )
    )
    return out["placement"]


def test_a_known_placement_is_kept(fake, gateway):
    assert _experience(fake, gateway, "ent004") == "ent004"


def test_an_unknown_placement_becomes_a_section_of_its_own(fake, gateway):
    assert _experience(fake, gateway, "ent999", "Open Source") == "new:Open Source"
    assert _experience(fake, gateway, "new", "") == "new:PROJECTS"
    assert _experience(fake, gateway, "new", "", kind="work") == "new:ADDITIONAL EXPERIENCE"
    assert _experience(fake, gateway, "new", "X" * 60) == "new:" + "X" * 40


# -- outreach and corrections -----------------------------------------------


def test_an_overlong_linkedin_note_is_surfaced_not_truncated(fake, gateway):
    note = "Hi Sam, " + "I build Kafka pipelines. " * 20
    fake.respond(
        "Outreach",
        {
            "linkedin_subject": "Hi",
            "linkedin": note,
            "email_subject": "Hi",
            "email_body": "Hi.",
            "highlights_used": [],
        },
    )
    out = run(
        gateway.draft_outreach("Jane Doe", "Sam", "Data Engineer", "Contoso", [BULLET], [POINT])
    )
    assert len(note.strip()) > outreach.LINKEDIN_LIMIT
    assert out["linkedin"] == note.strip()


def test_revise_pdf_drops_unknown_blocks_and_converts_layout_units(fake, gateway):
    fake.respond(
        "PdfRevision",
        {
            "reply": "Tightened it.",
            "edits": [
                {
                    "block_id": "bul005",
                    "text": "• Built pipelines.",
                    "substitutions": [],
                    "defensible": True,
                },
                {"block_id": "bul999", "text": "Ghost.", "substitutions": [], "defensible": True},
                {
                    "block_id": "bul006",
                    "text": "Same text.",
                    "substitutions": [],
                    "defensible": True,
                },
            ],
            "layout": [
                {"setting": "font_size", "value": 90},
                {"setting": "side_margins", "value": 50},
            ],
            "justify": "on",
        },
    )
    # The shape main._chat_blocks hands over.
    blocks = [
        {
            "id": bid,
            "kind": "bullet",
            "section": "EXPERIENCE",
            "text": text,
            "original": text,
            "lines": 1,
            "source_lines": 1,
            "chars_per_line": 100,
        }
        for bid, text in (("bul005", "Old text."), ("bul006", "Same text."))
    ]
    out = run(
        gateway.revise_pdf(
            blocks=blocks,
            layout=clean_layout(None),
            fit={},
            geometry={},
            role="",
            company="",
            jd_points=[],
            history=[],
            message="Tighten",
        )
    )
    assert [e["block_id"] for e in out["edits"]] == ["bul005"]
    assert out["edits"][0]["text"] == "Built pipelines."
    assert out["layout"] == {"font": 0.9, "margin_x": 50.0, "justify": "on"}

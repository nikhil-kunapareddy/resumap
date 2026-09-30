"""The exact (system, user) pair each model call sends, pinned.

Prompts are tuned by hand, so a refactor must not change a byte of them. Each
snapshot in ``tests/golden/prompts`` is one call's system prompt and message,
built from the sample resume and posting. After an intended prompt change,
regenerate with ``RESUMAP_UPDATE_GOLDEN=1 pytest tests/test_prompts_golden.py``
and read the diff.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from resumap.llm import bullets, corrections, outreach, tagging
from resumap.llm.prompts import PromptStore
from resumap.parsing.resume import parse_resume
from resumap.rendering.pdfgen import clean_layout
from tests.fixtures.resumes import JOB_DESCRIPTION, JOB_POINTS

GOLDEN = Path(__file__).parent / "golden" / "prompts"
SPLIT = "\n\n======== user ========\n\n"

POINTS = [
    {
        "id": f"jd{i:02d}",
        "text": t,
        "category": "requirement",
        "keywords": [w for w in ("Kafka", "Python", "Spark", "Airflow") if w in t],
        "dropped": False,
    }
    for i, t in enumerate(JOB_POINTS, start=1)
]
ROLE, COMPANY = "Data Engineer", "Contoso"


PROMPTS = PromptStore()  # the defaults: no edits


@pytest.fixture(scope="module")
def doc(resume_files) -> dict:
    return parse_resume(str(resume_files["pdf"]))


def check(name: str, system: str, user: str) -> None:
    path = GOLDEN / f"{name}.txt"
    got = system + SPLIT + user + "\n"
    if os.getenv("RESUMAP_UPDATE_GOLDEN"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(got, encoding="utf-8")
    assert got == path.read_text(encoding="utf-8"), f"prompt {name} changed"


def text_of(doc: dict, block_id: str) -> str:
    return next(b["text"] for b in doc["blocks"] if b["id"] == block_id)


@pytest.mark.parametrize("mode", ["aggressive", "bridge", "truthful"])
def test_rewrite(doc, mode):
    check(
        f"rewrite_{mode}",
        *bullets.rewrite_request(
            PROMPTS,
            bullet=text_of(doc, "bul005"),
            section="EXPERIENCE",
            jd_points=POINTS[:1],
            role=ROLE,
            company=COMPANY,
            mode=mode,
            extra="",
            max_chars=212,
        ),
    )


def test_rewrite_with_a_note(doc):
    check(
        "rewrite_note",
        *bullets.rewrite_request(
            PROMPTS,
            bullet=text_of(doc, "bul006"),
            section="EXPERIENCE",
            jd_points=POINTS[1:3],
            role=ROLE,
            company=COMPANY,
            mode="bridge",
            extra="Lead with the cost saving.",
            max_chars=105,
        ),
    )


def test_replace(doc):
    check(
        "replace",
        *bullets.replace_request(
            PROMPTS,
            bullet=text_of(doc, "bul006"),
            section="EXPERIENCE",
            entry="Senior Data Engineer, Northwind Analytics (Jan 2021 - Present)",
            siblings=[text_of(doc, "bul005"), text_of(doc, "bul007")],
            jd_points=POINTS[4:],
            role=ROLE,
            company=COMPANY,
            extra="",
            max_chars=105,
        ),
    )


def test_experience(doc):
    placements = [
        {
            "id": "ent004",
            "kind": "entry",
            "section": "EXPERIENCE",
            "text": "Senior Data Engineer, Northwind Analytics (Jan 2021 - Present)",
        },
        {"id": "sec011", "kind": "section", "section": "", "text": "PROJECTS"},
    ]
    experience = {
        "id": 1,
        "key": "exp001",
        "kind": "project",
        "title": "DAG linter",
        "text": "Wrote a Go CLI that lints Airflow DAGs; 40 stars on GitHub.",
    }
    check(
        "experience",
        *bullets.experience_request(
            PROMPTS,
            experience=experience,
            jd_points=POINTS[3:4],
            role=ROLE,
            company=COMPANY,
            placements=placements,
            examples=[text_of(doc, "bul005")],
            mode="aggressive",
            extra="",
            max_chars=208,
        ),
    )


def test_jd_tagging():
    check("jd_tags", tagging.JD_SYSTEM, tagging.tag_prompt(POINTS, JOB_DESCRIPTION))


def test_outreach(doc):
    examples = [
        {"kind": "linkedin", "subject": "", "body": "Hi Alex, loved your talk."},
        {"kind": "email", "subject": "Thanks", "body": "Hi Alex,\n\nThanks.\n\nJane"},
    ]
    user = outreach.outreach_prompt(
        "Jane Doe",
        "Sam",
        ROLE,
        COMPANY,
        [text_of(doc, "bul005"), text_of(doc, "bul006")],
        POINTS,
        examples,
    )
    check("outreach", outreach.OUTREACH_SYSTEM, user)


def test_corrections_chat(doc):
    blocks = [
        {
            "id": b["id"],
            "kind": b["kind"],
            "section": b["section"],
            "text": b["text"],
            "original": b["text"],
            "new": False,
            "right": b["right"],
            "pending": False,
            "lines": 1,
            "source_lines": 1,
            "chars_per_line": 100,
        }
        for b in doc["blocks"][:6]
    ]
    fit = {"pages": 1, "leading_scale": 1.0, "space_scale": 1.0, "font_scale": 1.0}
    history = [{"role": "user", "text": "Make it fit"}, {"role": "assistant", "text": "Done."}]
    user = corrections.chat_prompt(
        blocks,
        clean_layout({"font": 0.95}),
        fit,
        doc["geometry"],
        ROLE,
        COMPANY,
        POINTS,
        history,
        "Shorten the first bullet",
    )
    check("chat", corrections.PDF_CHAT_SYSTEM, user)

"""parsing/resume.py: a document becomes classified blocks keyed by id.

The snapshots in ``tests/golden`` pin today's classification of the sample
resume in each format: every block's id, kind, section, text and the rule that
kept it from being a point. Block ids are the join key for mappings, rewrites
and export overrides, so a change here is a change to stored data. When one is
intended, regenerate with ``RESUMAP_UPDATE_GOLDEN=1 pytest`` and review the diff.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from resumap.parsing.resume import parse_resume
from tests.fixtures.resumes import BULLETS, NAME

GOLDEN = Path(__file__).parent / "golden"
FORMATS = ("pdf", "docx", "txt")


def _summary(doc: dict) -> list[dict]:
    keys = ("id", "kind", "section", "mappable", "reason", "text", "right")
    return [{k: b[k] for k in keys} for b in doc["blocks"]]


@pytest.fixture(scope="module")
def parsed(resume_files) -> dict[str, dict]:
    return {fmt: parse_resume(str(resume_files[fmt])) for fmt in FORMATS}


@pytest.mark.parametrize("fmt", FORMATS)
def test_classification_matches_snapshot(parsed, fmt):
    path = GOLDEN / f"sample_resume_{fmt}.json"
    got = _summary(parsed[fmt])
    if os.getenv("RESUMAP_UPDATE_GOLDEN"):
        path.write_text(json.dumps(got, indent=2, ensure_ascii=False) + "\n")
    assert got == json.loads(path.read_text())


@pytest.mark.parametrize("fmt", FORMATS)
def test_block_ids_are_unique_and_prefixed_by_kind(parsed, fmt):
    ids = [b["id"] for b in parsed[fmt]["blocks"]]
    assert len(ids) == len(set(ids))
    for b in parsed[fmt]["blocks"]:
        assert b["id"][:3] == b["kind"][:3]
        assert b["id"][3:].isdigit()


@pytest.mark.parametrize("fmt", FORMATS)
def test_only_bullets_and_paragraphs_can_be_points(parsed, fmt):
    for b in parsed[fmt]["blocks"]:
        if b["mappable"]:
            assert b["kind"] in ("bullet", "para")
            assert b["reason"] is None
        else:
            assert b["reason"], f"{b['id']} was excluded without a recorded rule"


@pytest.mark.parametrize("fmt", FORMATS)
def test_sections_list_is_the_section_blocks_in_order(parsed, fmt):
    doc = parsed[fmt]
    assert doc["sections"] == [b["text"] for b in doc["blocks"] if b["kind"] == "section"]


@pytest.mark.parametrize("fmt", ("pdf", "docx"))
def test_every_bullet_is_a_point_with_wrapped_lines_rejoined(parsed, fmt):
    points = [b["text"] for b in parsed[fmt]["blocks"] if b["mappable"]]
    assert points == BULLETS


@pytest.mark.parametrize("fmt", ("pdf", "docx"))
def test_name_and_contact_line_are_recognised(parsed, fmt):
    blocks = parsed[fmt]["blocks"]
    assert parsed[fmt]["name"] == NAME
    assert [b["kind"] for b in blocks[:2]] == ["name", "contact"]


def test_pdf_entries_keep_their_right_aligned_dates(parsed):
    entries = [(b["left"], b["right"]) for b in parsed["pdf"]["blocks"] if b["kind"] == "entry"]
    assert entries == [
        ("Senior Data Engineer, Northwind Analytics", "Jan 2021 - Present"),
        ("Data Engineer, Contoso Group", "Mar 2018 - Dec 2020"),
        ("Open Transit Dashboard", "2022"),
        ("Example University, B.S. Computer Science", "2014 - 2018"),
    ]


def test_pdf_source_geometry_is_recorded_for_the_renderer(parsed):
    doc = parsed["pdf"]
    assert doc["geometry"]["width"] == pytest.approx(612.0)
    assert doc["geometry"]["height"] == pytest.approx(792.0)
    assert doc["body_size"] == pytest.approx(10.0)
    assert all("_line" in b for b in doc["blocks"])


# -- known defects ------------------------------------------------------------
# Pinned as strict xfails: each one passes the day the defect is fixed, which
# fails the run until the marker is removed.

@pytest.mark.xfail(strict=True, reason="a skills line with a label is read as a section heading")
def test_a_labelled_skills_line_is_not_a_section(parsed):
    assert "Languages: Python, SQL, Scala, TypeScript" not in parsed["pdf"]["sections"]


@pytest.mark.xfail(strict=True,
                   reason="in TXT an entry line right after a bullet is merged into it")
def test_txt_entry_after_a_bullet_stays_its_own_block(parsed):
    bullet = next(b for b in parsed["txt"]["blocks"] if b["text"].startswith("Led a team"))
    assert "Contoso Group" not in bullet["text"]


@pytest.mark.xfail(strict=True, reason="in TXT the contact line is merged into the name")
def test_txt_name_is_just_the_name(parsed):
    assert parsed["txt"]["name"] == NAME

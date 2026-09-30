"""parsing/jd.py: a pasted posting becomes points without a word changing."""
from __future__ import annotations

import re

import pytest

from resumap.parsing.jd import MAX_POINTS, parse_jd_verbatim, split_points
from tests.fixtures.resumes import JOB_DESCRIPTION, JOB_POINTS


def test_sample_posting_yields_its_requirements_exactly():
    assert split_points(JOB_DESCRIPTION) == JOB_POINTS


def test_every_point_is_verbatim_from_the_paste():
    flat = re.sub(r"\s+", " ", JOB_DESCRIPTION)
    for point in split_points(JOB_DESCRIPTION):
        assert point in flat


def test_one_line_is_one_point_with_nothing_merged_split_or_deduplicated():
    jd = ("Requirements:\n"
          "- Ship features weekly. Review code daily.\n"
          "- Write tests for every change.\n"
          "- Write tests for every change.\n"
          "- Mentor junior engineers.\n")
    assert split_points(jd) == [
        "Ship features weekly. Review code daily.",
        "Write tests for every change.",
        "Write tests for every change.",
        "Mentor junior engineers.",
    ]


@pytest.mark.parametrize("glyph", ["•", "*", "-", "–", "1.", "2)"])
def test_a_leading_bullet_glyph_is_dropped(glyph):
    assert split_points(f"Requirements:\n{glyph} Operate Kafka clusters in production.") == [
        "Operate Kafka clusters in production."]


def test_runs_of_spaces_collapse():
    assert split_points("Requirements:\n- Tune   Spark\tjobs  for cost.") == [
        "Tune Spark jobs for cost."]


def test_a_hard_wrapped_requirement_is_rejoined():
    jd = ("Requirements:\n"
          "- Partner with analysts to design data models that make\n"
          "  reporting fast and self-serve.\n")
    assert split_points(jd) == [
        "Partner with analysts to design data models that make reporting fast and self-serve."]


def test_blurb_before_the_first_heading_is_dropped():
    jd = "We are a fast-growing startup.\n\nRequirements:\n- Know SQL well enough to teach it."
    assert split_points(jd) == ["Know SQL well enough to teach it."]


def test_without_any_heading_every_line_is_kept():
    jd = "Build pipelines in Python.\nOperate Kafka in production."
    assert split_points(jd) == ["Build pipelines in Python.", "Operate Kafka in production."]


def test_boilerplate_sections_and_sentences_are_dropped():
    jd = ("Requirements:\n"
          "- Operate Kafka in production.\n"
          "Benefits\n"
          "- Free lunch on Fridays.\n"
          "Salary: $150,000 - $180,000\n"
          "We are an equal opportunity employer.\n")
    assert split_points(jd) == ["Operate Kafka in production."]


def test_a_heading_the_splitter_does_not_know_ends_boilerplate_and_is_kept():
    jd = ("Requirements:\n- Operate Kafka.\nBenefits\n- Free lunch.\n"
          "Tech stack\n- Python and Postgres.")
    assert split_points(jd) == ["Operate Kafka.", "Tech stack", "Python and Postgres."]


def test_a_careers_page_worth_of_points_is_capped():
    jd = "\n".join(f"Requirement number {i} is here." for i in range(MAX_POINTS + 10))
    assert len(split_points(jd)) == MAX_POINTS


def test_parse_jd_verbatim_numbers_points_and_leaves_labels_blank():
    out = parse_jd_verbatim(JOB_DESCRIPTION)
    assert out["company"] == "Contoso"
    assert out["role"] == "Data Engineer"
    assert [p["id"] for p in out["points"]] == [f"jd{i:02d}" for i in range(1, 6)]
    assert [p["text"] for p in out["points"]] == JOB_POINTS
    for p in out["points"]:
        assert (p["category"], p["keywords"], p["dropped"]) == ("requirement", [], False)

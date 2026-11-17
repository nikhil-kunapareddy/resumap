"""The export self-check: the defects that actually shipped before, looked for in every PDF."""

from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Any

from .. import obs
from .pdfgen import ResumeRenderer


def verify_pdf(
    pdf_path: str | Path,
    doc: dict[str, Any],
    source_path: str | None = None,
    layout: dict | None = None,
) -> dict[str, Any]:
    """Check the rendered PDF for the defects that actually shipped before.

    Each check corresponds to a bug that reached a real export: text running
    past the right margin, a text layer with no spaces in it (which is what an
    ATS reads), a substituted dingbat font leaving a stray letter at the head of
    every bullet, glyphs whose text extracts as "(cid:N)", page count, and
    hyperlinks silently dropped -- a resume whose LinkedIn and GitHub links are
    dead text has lost something a reader uses.

    `layout` is the one the PDF was rendered with, so the margin is measured
    where the renderer actually put it rather than where the source had it.
    """
    result: dict[str, Any] = {"ok": True}
    try:
        import pdfplumber

        right = ResumeRenderer(doc, layout=layout).right_edge

        with pdfplumber.open(str(pdf_path)) as pdf:
            result["pages"] = len(pdf.pages)
            result["links"] = sum(len(p.hyperlinks or []) for p in pdf.pages)
            chars = pdf.pages[0].chars
            rows: dict[int, list] = {}
            for c in chars:
                rows.setdefault(round(c["top"] / 4), []).append(c)
            result["overflow_rows"] = sum(
                1 for cs in rows.values() if max(c["x1"] for c in cs) > right + 0.6
            )
            result["fonts"] = sorted({c["fontname"].split("+")[-1] for c in chars})
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)

        words = len(text.split())
        result["words"] = words
        # A spaceless text layer is the signature of per-word placement.
        result["has_spaces"] = text.count(" ") > words * 0.5
        result["dingbat_font"] = any("Dingbat" in f for f in result["fonts"])
        # A glyph with no Unicode mapping, as Times' bullet is when Vera is
        # missing. Reported among the problems only: the API's SelfCheck has
        # no field for the count.
        unmapped = text.count("(cid:")

        if source_path and str(source_path).lower().endswith(".pdf"):

            def norm(s: str) -> str:
                return re.sub(r"\s+", " ", s).strip()

            with pdfplumber.open(source_path) as src:
                original = norm("\n".join(p.extract_text() or "" for p in src.pages))
                # Counted from the same handle the similarity check already
                # opens, so it is a PDF source by construction.
                result["source_links"] = sum(len(p.hyperlinks or []) for p in src.pages)
            result["similarity_pct"] = round(
                difflib.SequenceMatcher(None, original, norm(text)).ratio() * 100, 1
            )

        problems = []
        if result["pages"] > 1:
            problems.append("multi-page")
        if result["overflow_rows"]:
            problems.append(f"{result['overflow_rows']} rows past right margin")
        if not result["has_spaces"]:
            problems.append("text layer has no spaces (ATS would read one long word)")
        if result["dingbat_font"]:
            problems.append("dingbat substitution leaves a stray glyph per bullet")
        if unmapped:
            problems.append(f"{unmapped} glyphs extract as (cid:N), unreadable to an ATS")
        lost = result.get("source_links", 0) - result["links"]
        if lost > 0:
            problems.append(f"{lost} of {result['source_links']} source hyperlinks dropped")
        result["problems"] = problems
        result["ok"] = not problems

        if problems:
            obs.warn(
                "export.selfcheck_failed",
                problems=problems,
                **{
                    k: v
                    for k, v in result.items()
                    if k in ("pages", "overflow_rows", "similarity_pct", "links", "source_links")
                },
            )
        else:
            obs.event(
                "export.selfcheck",
                ok=True,
                pages=result["pages"],
                words=words,
                links=result["links"],
                source_links=result.get("source_links"),
                similarity_pct=result.get("similarity_pct"),
            )
    except Exception as exc:  # noqa: BLE001 - never fail an export over a check
        result["ok"] = None
        result["error"] = f"{type(exc).__name__}: {exc}"
        obs.warn("export.selfcheck_error", error=result["error"])
    return result

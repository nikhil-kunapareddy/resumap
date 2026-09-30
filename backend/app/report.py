"""Human-readable diagnostics: the parse report and the export self-check."""
from __future__ import annotations

import difflib
import os
import re
from pathlib import Path
from typing import Any

from . import obs


def _text_allowed() -> bool:
    return os.getenv("RESUMAP_LOG_TEXT", "0").strip() not in ("", "0", "false", "no")


def parse_report(resume_id: int, label: str, doc: dict[str, Any]) -> str:
    """The block-classification table, with the reason for every rejection.

    This is the view that makes a misclassification obvious: which blocks became
    points, and for the rest, exactly which rule excluded them.
    """
    d = doc.get("diagnostics") or {}
    g = d.get("geometry") or {}

    def linked(b: dict[str, Any]) -> int:
        """How many of a block's runs carry a hyperlink."""
        return sum(1 for r in (b.get("runs") or []) + (b.get("right_runs") or [])
                   if r.get("uri"))

    blocks = doc.get("blocks") or []
    out: list[str] = [
        f"PARSE REPORT  resume={resume_id}  label={label}",
        "=" * 78,
        f"pages {d.get('pages')}   lines {d.get('lines')}   "
        f"blocks {d.get('blocks')}   points {d.get('points')}",
        f"body size {d.get('body_size')}pt   leading {d.get('leading')}pt   "
        f"margins L{g.get('left')} R{g.get('right')} T{g.get('top')} B{g.get('bottom')}",
        f"tab stops {d.get('tab_stops')}   justified lines {d.get('justified_lines')}   "
        f"rules {d.get('rules')}   links {sum(linked(b) for b in blocks)}",
        f"merges {d.get('merges')} {d.get('merge_reasons')}   "
        f"label-guard blocks {d.get('label_guard_blocks')}   "
        f"truncated {d.get('truncated')}",
        f"fonts {', '.join(d.get('fonts') or [])}",
        "",
        f"{'PT':<3}{'ID':<8}{'KIND':<9}{'SECTION':<24}{'LEN':>5}  FLAGS  WHY NOT A POINT",
        "-" * 78,
    ]
    for b in blocks:
        flags = "".join([
            "R" if b.get("right") else "-",
            "J" if b.get("justified") else "-",
            "T" if b.get("truncated") else "-",
            "L" if linked(b) else "-",
        ])
        reason = "" if b.get("mappable") else (b.get("reason") or "?")
        out.append(
            f"{'->' if b.get('mappable') else '':<3}"
            f"{b.get('id', ''):<8}{b.get('kind', ''):<9}"
            f"{(b.get('section') or '-')[:23]:<24}"
            f"{len(b.get('text') or ''):>5}  {flags}   {reason}"
        )
        if _text_allowed():
            out.append(f"       {obs.scrub(b.get('text') or '')[:70]}")

    out += ["", "counts by reason:"]
    for k, v in sorted((d.get("reasons") or {}).items(), key=lambda kv: -kv[1]):
        out.append(f"  {k:<24}{v}")
    return "\n".join(out) + "\n"


def write_parse_report(log_dir: Path, resume_id: int, label: str,
                       doc: dict[str, Any]) -> Path | None:
    try:
        directory = log_dir / "parse"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{resume_id}.txt"
        path.write_text(parse_report(resume_id, label, doc), encoding="utf-8")
        return path
    except Exception:  # noqa: BLE001 - reporting must not break an upload
        return None


def verify_pdf(pdf_path: str | Path, doc: dict[str, Any],
               source_path: str | None = None) -> dict[str, Any]:
    """Check the rendered PDF for the defects that actually shipped before.

    Each check corresponds to a bug that reached a real export: text running
    past the right margin, a text layer with no spaces in it (which is what an
    ATS reads), a substituted dingbat font leaving a stray letter at the head of
    every bullet, page count, and hyperlinks silently dropped -- a resume whose
    LinkedIn and GitHub links are dead text has lost something a reader uses.
    """
    result: dict[str, Any] = {"ok": True}
    try:
        import pdfplumber

        g = doc.get("geometry") or {}
        right = float(g.get("width") or 612.0) - float(g.get("right") or 36.0)

        with pdfplumber.open(str(pdf_path)) as pdf:
            result["pages"] = len(pdf.pages)
            result["links"] = sum(len(p.hyperlinks or []) for p in pdf.pages)
            chars = pdf.pages[0].chars
            rows: dict[int, list] = {}
            for c in chars:
                rows.setdefault(round(c["top"] / 4), []).append(c)
            result["overflow_rows"] = sum(
                1 for cs in rows.values()
                if max(c["x1"] for c in cs) > right + 0.6
            )
            result["fonts"] = sorted({c["fontname"].split("+")[-1] for c in chars})
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)

        words = len(text.split())
        result["words"] = words
        # A spaceless text layer is the signature of per-word placement.
        result["has_spaces"] = text.count(" ") > words * 0.5
        result["dingbat_font"] = any("Dingbat" in f for f in result["fonts"])

        if source_path and str(source_path).lower().endswith(".pdf"):
            def norm(s: str) -> str:
                return re.sub(r"\s+", " ", s).strip()
            with pdfplumber.open(source_path) as src:
                original = norm("\n".join(p.extract_text() or "" for p in src.pages))
                # Counted from the same handle the similarity check already
                # opens, so it is a PDF source by construction.
                result["source_links"] = sum(
                    len(p.hyperlinks or []) for p in src.pages)
            result["similarity_pct"] = round(
                difflib.SequenceMatcher(None, original, norm(text)).ratio() * 100, 1)

        problems = []
        if result["pages"] > 1:
            problems.append("multi-page")
        if result["overflow_rows"]:
            problems.append(f"{result['overflow_rows']} rows past right margin")
        if not result["has_spaces"]:
            problems.append("text layer has no spaces (ATS would read one long word)")
        if result["dingbat_font"]:
            problems.append("dingbat substitution leaves a stray glyph per bullet")
        lost = result.get("source_links", 0) - result["links"]
        if lost > 0:
            problems.append(
                f"{lost} of {result['source_links']} source hyperlinks dropped")
        result["problems"] = problems
        result["ok"] = not problems

        if problems:
            obs.warn("export.selfcheck_failed", problems=problems, **{
                k: v for k, v in result.items()
                if k in ("pages", "overflow_rows", "similarity_pct",
                         "links", "source_links")})
        else:
            obs.event("export.selfcheck", ok=True, pages=result["pages"],
                      words=words, links=result["links"],
                      source_links=result.get("source_links"),
                      similarity_pct=result.get("similarity_pct"))
    except Exception as exc:  # noqa: BLE001 - never fail an export over a check
        result["ok"] = None
        result["error"] = f"{type(exc).__name__}: {exc}"
        obs.warn("export.selfcheck_error", error=result["error"])
    return result

"""Turn extracted lines into a block document: sections, entries, bullets."""

from __future__ import annotations

import re
from typing import Any

from .. import obs
from .layout import BULLET_RE, ExtractedDoc, Line, extract
from .text import continues, ensure_stop, is_dangling, is_terminated

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PHONE_RE = re.compile(r"(\+?\d[\d\s().-]{7,}\d)")
URL_RE = re.compile(r"(https?://|www\.|linkedin\.com|github\.com)", re.I)
DATE_RE = re.compile(
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b"
    r"|\b(?:present|current|ongoing)\b"
    r"|\b(?:19|20)\d{2}\b",
    re.I,
)

SECTION_WORDS = {
    "experience",
    "work experience",
    "professional experience",
    "employment",
    "education",
    "skills",
    "technical skills",
    "projects",
    "summary",
    "objective",
    "profile",
    "certifications",
    "certification",
    "awards",
    "publications",
    "achievements",
    "activities",
    "leadership",
    "interests",
    "coursework",
    "relevant coursework",
    "volunteer",
    "languages",
    "tools",
    "research",
    "training",
    "affiliations",
    "contact",
}


# Section names are classified in three tiers, because a single list cannot
# separate "Academic Projects" (work you did) from "Academic Background"
# (a credential) or "Project Management Skills" (an attribute).

# Tier 1 -- always an attribute, never an accomplishment. Wins outright, so
# "Project Management Skills" is excluded despite containing "project".
HARD_EXCLUDE_HINTS = (
    "skill",
    "tool",
    "technolog",
    "competenc",
    "proficien",
    "expertise",
    "coursework",
    "courses",
    "certificat",
    "licen",
    "language",
    "interest",
    "hobb",
    "reference",
    "publication",
    "patent",
    "summary",
    "objective",
    "profile",
    "contact",
)

# Tier 2 -- work you did. Beats the soft excludes, so "Academic Projects" and
# "Research Experience" produce points.
DOING_SECTION_HINTS = (
    "experience",
    "employment",
    "work history",
    "career",
    "project",
    "research",
    "internship",
    "leadership",
    "volunteer",
    "activit",
    "extracurricular",
    "involvement",
    "fellowship",
    "consulting",
    "freelance",
    "teaching",
    "mentorship",
)

# Tier 3 -- credentials and affiliations. Excluded unless a tier-2 word is
# also present.
SOFT_EXCLUDE_HINTS = (
    "education",
    "academic",
    "award",
    "honor",
    "affiliation",
    "membership",
    "training",
    "about",
    "achievement",
    "accolade",
)

# Irregular or present-tense verbs the "-ed" test misses.
ACTION_VERBS = {
    "built",
    "led",
    "wrote",
    "drove",
    "grew",
    "oversaw",
    "ran",
    "rebuilt",
    "set",
    "took",
    "won",
    "began",
    "chose",
    "cut",
    "held",
    "kept",
    "made",
    "met",
    "sold",
    "taught",
    "thought",
    "brought",
    "dealt",
    "found",
    "sent",
    "spearhead",
    "build",
    "develop",
    "create",
    "implement",
    "manage",
    "own",
    "drive",
    "maintain",
    "run",
    "write",
    "deliver",
    "ship",
    "collaborate",
    "analyze",
    "automate",
    "scale",
    "launch",
    "improve",
    "reduce",
    "optimize",
    "migrate",
    "deploy",
    "coordinate",
    "mentor",
    "serve",
    "serving",
    "working",
    "leading",
    "building",
    "designing",
    "developing",
    "managing",
    "owning",
}
# Deliberately NOT action verbs: "engineer", "architect", "lead", "design",
# "partner", "support" are job titles as often as verbs, and treating
# "Engineer, Contoso Group ..." as an accomplishment defeats header detection.

# Words ending in -ed that are adjectives or company names, not past-tense verbs.
# "United Technologies" must not read as the verb "to unite".
NON_ACTION_ED = {
    "united",
    "advanced",
    "applied",
    "allied",
    "integrated",
    "distributed",
    "limited",
    "unlimited",
    "dedicated",
    "certified",
    "licensed",
    "registered",
    "accredited",
    "based",
    "related",
    "detailed",
    "skilled",
    "experienced",
    "motivated",
    "oriented",
    "focused",
    "combined",
    "mixed",
    "varied",
    "associated",
    "affiliated",
    "specialized",
    "qualified",
    "talented",
}


def _section_allows_points(section: str) -> bool:
    """True when a section holds work you did, rather than attributes you have."""
    norm = re.sub(r"[^a-z ]", " ", (section or "").lower())
    if any(h in norm for h in HARD_EXCLUDE_HINTS):
        return False
    if any(h in norm for h in DOING_SECTION_HINTS):
        return True
    if any(h in norm for h in SOFT_EXCLUDE_HINTS):
        return False
    return False


def _starts_with_action(text: str) -> bool:
    first = re.split(r"[\s,;:]+", text.strip(), maxsplit=1)[0].lower().strip(".,;:()")
    if first in ACTION_VERBS:
        return True
    if first in NON_ACTION_ED:
        return False
    return len(first) > 4 and first.endswith("ed")


# "Languages:", "Backend & Distributed Systems:", "Cloud & Infrastructure:" --
# a labelled item, which always begins a new line rather than continuing one.
LABEL_RE = re.compile(r"^[A-Z][\w &/+.'\-]{0,34}:\s")

ORG_SUFFIX = re.compile(
    r"\b(inc|llc|l\.l\.c|ltd|limited|corp|corporation|co|company|technologies|"
    r"technology|tech|labs|laboratories|solutions|systems|group|holdings|partners|"
    r"associates|consulting|consultancy|university|institute|college|school|academy|"
    r"hospital|health|bank|capital|ventures|studio|studios|agency|foundation|"
    r"gmbh|plc|pvt|sas|nv|ab|oy)\b\.?",
    re.I,
)

SEPARATOR_RE = re.compile(r"\s[|\u2013\u2014\u2022\u00b7\u2011-]\s")

_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
# "September 2020 - December 2022", "Jun 2023 - Present", "2020 to 2022".
DATE_RANGE_RE = re.compile(
    rf"(?:{_MONTH}\s*,?\s*)?(?:19|20)\d{{2}}"
    r"\s*(?:-|\u2013|\u2014|\u2011|to|through|until|\u2192)\s*"
    rf"(?:present|current|now|ongoing|date|(?:{_MONTH}\s*,?\s*)?(?:19|20)\d{{2}})"
    rf"|{_MONTH}\s*(?:19|20)\d{{2}}\s*(?:-|\u2013|\u2014|to)\s*{_MONTH}\s*(?:19|20)\d{{2}}",
    re.I,
)


def _title_case_ratio(text: str) -> float:
    words = re.findall(r"[A-Za-z][\w'\u2019-]*", text)
    if not words:
        return 0.0
    return sum(1 for w in words if w[0].isupper()) / len(words)


def _looks_like_header(text: str) -> bool:
    """A job title, employer, location or date line -- context, not an achievement.

    These leak in as paragraphs whenever a resume puts the title, the company and
    the dates on separate lines, or leaves the title unbolded, so the entry
    classifier never sees a bold line with a date on it.
    """
    words = text.split()
    n = len(words)
    # A full date range is decisive and outranks everything below, because job
    # titles double as verbs: "Engineer, Contoso Group March 2019 - June 2021"
    # opens with "Engineer" but is unmistakably a role header.
    if n <= 20 and DATE_RANGE_RE.search(text):
        return True
    # Otherwise an action-verb opening wins: "Managed AWS, GCP And Azure
    # Migration Projects" is title-cased but is still something you did.
    if _starts_with_action(text):
        return False
    if n <= 16:
        if DATE_RE.search(text):
            return True
        if _title_case_ratio(text) >= 0.7:
            return True
        if SEPARATOR_RE.search(text):
            return True
    return bool(ORG_SUFFIX.search(text)) and n <= 22


def _looks_like_list(text: str) -> bool:
    """Skills-style enumeration: 'Languages: Python, SQL, Bash, R'."""
    body = re.sub(r"^[A-Za-z][\w /&+.\-]{0,30}\s*:\s*", "", text.strip())
    parts = [p.strip() for p in re.split(r"[,;|\u2022/]", body) if p.strip()]
    if len(parts) < 3:
        return False
    avg_words = sum(len(p.split()) for p in parts) / len(parts)
    return avg_words <= 3.5


def _point_reason(block: dict) -> str | None:
    """``None`` if the block is a mappable point, else why it was rejected.

    Returning the reason rather than a bare boolean is what makes a wrong
    classification diagnosable: "skills line kept" and "real bullet dropped"
    look identical in a yes/no answer.
    """
    text = block["text"].strip()
    if block["kind"] not in ("bullet", "para"):
        return f"kind={block['kind']}"
    if len(text) < 30:
        return "too-short"
    if block["kind"] == "para" and len(text) < 60:
        return "para-too-short"
    if not _section_allows_points(block.get("section", "")):
        return "section-excluded"
    # A label-and-list line is a skills inventory even inside an experience
    # section -- unless it actually opens with an action verb.
    if _looks_like_list(text) and not _starts_with_action(text):
        return "list-shaped"
    if _looks_like_header(text):
        return "header-shaped"
    # Unbulleted prose in an experience section is far more often a title,
    # employer or location than an achievement, so require the action-verb
    # opening that every real accomplishment line has.
    if block["kind"] == "para" and not _starts_with_action(text):
        return "para-no-action-verb"
    return None


def _refit_runs(runs: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
    """Re-cut runs so they describe `text` after its full stop was repaired.

    A repair only ever trims words off the end and re-terminates what is left,
    so every surviving word keeps the run it came from. Rebuilding is worth the
    trouble because a run carries more than weight: dropping the list also drops
    the hyperlink on an anchor like "arXiv", which the export then renders as
    dead text. Returns [] when the words no longer line up, leaving the caller's
    dominant-style fallback in charge.
    """
    src: list[dict[str, Any]] = []
    for r in runs:
        src.extend(r for _w in str(r.get("text", "")).split())
    words = text.split()
    if not src or not words or len(words) > len(src):
        return []

    out: list[dict[str, Any]] = []
    owner: list[dict[str, Any]] = []
    for i, w in enumerate(words):
        r = src[i]
        if out and owner[-1] is r:
            out[-1]["text"] += " " + w
        else:
            out.append(
                {
                    "text": w,
                    "bold": bool(r.get("bold")),
                    "italic": bool(r.get("italic")),
                    "uri": r.get("uri", ""),
                    "color": r.get("color", ""),
                    "underline": bool(r.get("underline")),
                }
            )
            owner.append(r)
    return out


def _dominant_size(lines: list[Line]) -> float:
    if not lines:
        return 10.5
    counts: dict[float, int] = {}
    for ln in lines:
        # Weight by character count so a big name doesn't outvote the body.
        counts[round(ln.size, 1)] = counts.get(round(ln.size, 1), 0) + len(ln.text)
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _looks_like_section(line: Line, body: float) -> bool:
    t = line.text.strip()
    if not t or len(t) > 48 or BULLET_RE.match(t):
        return False
    norm = re.sub(r"[^a-z& ]", "", t.lower()).strip()
    if norm in SECTION_WORDS:
        return True
    if len(norm) > 3 and any(norm.startswith(w) or norm.endswith(w) for w in SECTION_WORDS):
        return True
    # Formatting-only signal: a short, bold or fully capitalised standalone line.
    letters = [c for c in t if c.isalpha()]
    all_caps = bool(letters) and all(c.isupper() for c in letters)
    return (all_caps or line.bold) and len(t.split()) <= 5 and line.size >= body - 0.6


def _looks_like_entry(line: Line, body: float) -> bool:
    """A role/company/date header, not a bullet."""
    if BULLET_RE.match(line.text):
        return False
    # A tab stop means a two-column header line, whatever sits in the right
    # column -- "Example University <tab> Springfield, USA" has no date but is
    # still an entry, not a section heading.
    if line.right:
        return True
    return line.bold and bool(DATE_RE.search(line.text)) and len(line.text) < 120


def _split_trailing_date(text: str) -> tuple[str, str]:
    """Peel a trailing date range off a header line.

    Not every right-aligned date leaves a gap wide enough to read as a tab
    stop: on a long line ("Graduate Research Assistant, Distributed Systems
    Laboratory, Example University   July 2021 - May 2023") the date
    is flush right with only a few points to spare. Recognising the date itself
    recovers the second column.
    """
    stripped = text.strip()
    m = DATE_RANGE_RE.search(stripped)
    if m and m.end() >= len(stripped) - 1 and m.start() > 10:
        return stripped[: m.start()].strip(" ,;-\u2013\u2014"), stripped[m.start() :].strip()
    return text, ""


def _finalize_points(blocks: list[dict[str, Any]]) -> None:
    """Make sure no point reads as though it were cut off mid-sentence.

    Only mappable points are rewritten. Every other block keeps its text byte
    for byte, because the exporter renders those straight back into the PDF and
    must not quietly restyle the resume.
    """
    points = [b for b in blocks if b["mappable"]]
    # Follow the resume's own convention: only restore a missing full stop when
    # most of its points already carry one.
    dominant_period = bool(points) and (
        sum(is_terminated(b["text"]) for b in points) * 2 >= len(points)
    )

    for b in blocks:
        b["truncated"] = False
        if b["kind"] not in ("bullet", "para"):
            continue
        text = b["text"].strip()
        if not b["mappable"]:
            b["text"] = text
            continue
        if is_dangling(text):
            # Stops on a connector: the continuation line was never recovered.
            b["truncated"] = True
        if b["truncated"] or dominant_period:
            repaired = ensure_stop(text)
            if repaired and repaired != text:
                text = repaired
                # Runs no longer describe this text. Re-cut them where the words
                # still line up so styling and links survive the fix; a block
                # with a right-aligned half has words the runs never covered, so
                # there the exporter falls back to the dominant style.
                b["runs"] = [] if b.get("right") else _refit_runs(b.get("runs") or [], text)
        b["text"] = text


def parse_resume(path: str) -> dict[str, Any]:
    doc: ExtractedDoc = extract(path)
    lines = doc.lines
    body = _dominant_size(lines)

    blocks: list[dict[str, Any]] = []
    current_section = ""
    counter = 0
    merges: list[dict[str, Any]] = []
    blocked_labels: list[str] = []

    def new_id(kind: str) -> str:
        nonlocal counter
        counter += 1
        return f"{kind[:3]}{counter:03d}"

    # Median baseline step between consecutive body lines. Using the source's
    # own leading is what lets a faithful re-render fit the same page the
    # original did, instead of needing to be compressed to get there.
    steps = sorted(
        b.top - a.top
        for a, b in zip(lines, lines[1:], strict=False)
        if a.page == b.page and abs(a.size - b.size) < 0.6 and 0 < (b.top - a.top) <= a.size * 1.8
    )
    doc_leading = steps[len(steps) // 2] if steps else body * 1.2

    top_of_page0 = min((ln.top for ln in lines if ln.page == 0), default=0.0)
    header_zone = top_of_page0 + 90
    # The rightmost text position in the document approximates the text column's
    # right edge, which is how we tell a wrapped line from a new one.
    content_right = max((ln.x1 for ln in lines), default=558.0)

    for line in lines:
        text = line.text.strip()
        if not text:
            continue

        prev = blocks[-1] if blocks else None
        bullet = BULLET_RE.match(text)
        is_bullet = bullet is not None
        bullet_char = ""
        if bullet is not None:
            bullet_char = bullet.group(1)
            text = BULLET_RE.sub("", text).strip()
            # The glyph is re-drawn by the exporter as a hanging indent, so it
            # must not survive inside the styled runs as well -- otherwise the
            # bullet is painted twice.
            if line.runs:
                first = BULLET_RE.sub("", line.runs[0].text).strip()
                if first:
                    line.runs[0].text = first
                else:
                    line.runs.pop(0)

        # --- wrapped-line continuation -------------------------------------
        if prev is not None and not is_bullet and prev["kind"] in ("bullet", "para"):
            pl = prev["_line"]
            vgap = line.top - pl["bottom"]
            # A wrapped line has two signatures: the line above it ran to the
            # right edge (it had to wrap), and it starts exactly where the
            # parent's *text* starts -- past the bullet glyph, not at it.
            aligned = abs(line.x0 - pl["text_x0"]) <= max(4.0, line.size * 0.6)
            # Wrapping means the line above had no room left for this line's
            # first word. Testing that directly is far more reliable than
            # asking whether the line above reached the right margin -- ragged
            # right edges are normal, and demanding a near-full line truncated
            # any bullet whose wrap point left a wide gap.
            words = text.split()
            est_word_w = len(words[0]) * line.size * 0.5 if words else 0.0
            could_not_fit = (content_right - pl["x1"]) < est_word_w + line.size * 0.4
            by_continuation = continues(prev["text"], text)
            labelled = bool(LABEL_RE.match(text))
            merge = (
                line.page == pl["page"]
                and -1.0 <= vgap <= line.size * 1.1
                and (by_continuation or could_not_fit)
                and not labelled
                and aligned
                and abs(line.size - pl["size"]) < 1.2
                and not _looks_like_section(line, body)
                and not _looks_like_entry(line, body)
            )
            if merge:
                merges.append(
                    {
                        "into": prev["id"],
                        "reason": "continuation" if by_continuation else "could-not-fit",
                        "slack": round(content_right - pl["x1"], 1),
                    }
                )
            elif labelled and aligned and -1.0 <= vgap <= line.size * 1.1:
                # Worth seeing: geometry said "wrap", the label guard said "no".
                blocked_labels.append(prev["id"])
            if merge:
                prev["text"] = (prev["text"] + " " + text).strip()
                prev["justified"] = prev.get("justified") or line.stretched
                # Concatenate the continuation's runs rather than discarding
                # them: the wrap position changes but the sequence of styled
                # text does not, so a bold label that happened to sit on a line
                # that wrapped keeps its weight.
                prev["runs"] = (prev.get("runs") or []) + [r.to_dict() for r in line.runs]
                pl["bottom"] = line.bottom
                pl["x1"] = max(pl["x1"], line.x1)
                continue

        # --- classify -------------------------------------------------------
        # A header line may carry its date flush right without a wide enough
        # gap to register as a tab stop; recover it before classifying.
        if not is_bullet and not line.right and line.page == 0:
            head, tail = _split_trailing_date(text)
            if tail:
                line.left, line.right = head, tail
                text = head + " " + tail

        if is_bullet:
            kind = "bullet"
        elif not blocks and (line.size > body + 0.4 or line.bold) and len(text) < 60:
            kind = "name"
        elif (
            line.page == 0
            and line.top < header_zone
            and (EMAIL_RE.search(text) or PHONE_RE.search(text) or URL_RE.search(text))
        ):
            kind = "contact"
        elif _looks_like_entry(line, body):
            kind = "entry"
        elif _looks_like_section(line, body):
            kind = "section"
            current_section = re.sub(r"\s+", " ", text).strip()
        else:
            kind = "para"

        # Does a horizontal rule sit just under this line? (section underlines)
        rule_below = any(
            r.page == line.page and 0 <= r.top - line.bottom <= line.size * 1.2 for r in doc.rules
        )

        blocks.append(
            {
                "id": new_id(kind),
                "kind": kind,
                "section": "" if kind in ("name", "contact", "section") else current_section,
                "text": text,
                "left": line.left,
                "right": line.right,
                "bullet_char": bullet_char or "•",
                "rule_below": rule_below,
                "mappable": False,
                "runs": [r.to_dict() for r in line.runs],
                "right_runs": [r.to_dict() for r in line.right_runs],
                "justified": line.stretched,
                "_line": {
                    "x0": line.x0,
                    "x1": line.x1,
                    "text_x0": line.text_x0,
                    "top": line.top,
                    "bottom": line.bottom,
                    "size": line.size,
                    "bold": line.bold,
                    "italic": line.italic,
                    "font": line.font,
                    "page": line.page,
                },
            }
        )

    for b in blocks:
        reason = _point_reason(b)
        b["mappable"] = reason is None
        b["reason"] = reason

    # Safety net: if section detection failed badly (unusual headings, a resume
    # with no headings at all) nothing would be mappable. Fall back to shape
    # alone rather than handing the user an empty mapping screen.
    if not any(b["mappable"] for b in blocks):
        for b in blocks:
            t = b["text"].strip()
            b["mappable"] = (
                b["kind"] == "bullet"
                and len(t) >= 30
                and not _looks_like_list(t)
                and not any(
                    h in re.sub(r"[^a-z ]", " ", b.get("section", "").lower())
                    for h in HARD_EXCLUDE_HINTS + SOFT_EXCLUDE_HINTS
                )
            )
            if b["mappable"]:
                b["reason"] = "fallback-shape-only"
        obs.warn(
            "parse.fallback_net",
            recovered=sum(1 for b in blocks if b["mappable"]),
            note="section detection found no points; used shape alone",
        )

    _finalize_points(blocks)

    name = next((b["text"] for b in blocks if b["kind"] == "name"), "")
    if not name and blocks:
        name = blocks[0]["text"]

    g = doc.geometry
    from collections import Counter

    reasons = Counter(b.get("reason") or "point" for b in blocks)
    kinds = Counter(b["kind"] for b in blocks)
    diagnostics: dict[str, Any] = {
        "pages": max((ln.page for ln in lines), default=0) + 1,
        "lines": len(lines),
        "blocks": len(blocks),
        "points": sum(1 for b in blocks if b["mappable"]),
        "kinds": dict(kinds),
        "reasons": dict(reasons),
        "tab_stops": sum(1 for ln in lines if ln.right),
        "justified_lines": sum(1 for ln in lines if ln.stretched),
        "merges": len(merges),
        "merge_reasons": dict(Counter(m["reason"] for m in merges)),
        "label_guard_blocks": len(blocked_labels),
        "rules": len(doc.rules),
        "truncated": sum(1 for b in blocks if b.get("truncated")),
        "fonts": sorted({ln.font.split("+")[-1] for ln in lines})[:8],
        "geometry": {
            "left": round(g.left, 1),
            "right": round(g.right, 1),
            "top": round(g.top, 1),
            "bottom": round(g.bottom, 1),
        },
        "body_size": body,
        "leading": round(doc_leading, 2),
    }
    obs.event("parse.done", **diagnostics)
    if not any(b["mappable"] for b in blocks):
        obs.warn("parse.no_points", blocks=len(blocks), kinds=dict(kinds))

    return {
        "name": name,
        "body_size": body,
        "leading": doc_leading,
        "diagnostics": diagnostics,
        "geometry": {
            "width": g.width,
            "height": g.height,
            "left": g.left,
            "right": g.right,
            "top": g.top,
            "bottom": g.bottom,
        },
        "blocks": blocks,
        "sections": [b["text"] for b in blocks if b["kind"] == "section"],
    }

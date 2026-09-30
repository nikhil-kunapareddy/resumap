"""Verbatim job-description splitter.

One line in, one point out. This is the only thing that ever decides what a
JD point *says* -- the model is allowed to label the points it produces, never
to reword them, so the text on screen is byte for byte what was pasted and the
point count matches the requirements in the posting.
"""
from __future__ import annotations

import re

from .. import obs
from .text import unwrap

BULLET_PREFIX = re.compile(r"^\s*(?:[•◦▪‣·●■*\-–—]|\d+[.)])\s+")
# "Apply" only as a call to action: "Apply technologies used in..." is a duty.
NOISE = re.compile(
    r"^(about (us|the (role|team|company))|who we are|why join|benefits|perks|"
    r"equal opportunity|eeo|we offer|compensation|salary|how to apply|"
    r"apply(?=\s*(now|here|today|online|:|$))|location|job type)\b",
    re.I,
)
# Phrases that disqualify a sentence wherever they appear, not just at the start.
NOISE_ANY = re.compile(
    r"(equal opportunity|affirmative action|without regard to race|"
    r"reasonable accommodation|e-?verify|visa sponsorship is not|"
    r"we are an? .{0,30}employer)",
    re.I,
)
HEADING = re.compile(
    r"^\s*(responsibilities|requirements|qualifications|what you.{0,3}ll do|"
    r"what we.{0,3}re looking for|must have|nice to have|preferred|basic qualifications|"
    r"minimum qualifications|skills|your role|the role|duties)\s*:?\s*$",
    re.I,
)
HAS_LETTER = re.compile(r"[A-Za-z]")

# A skills list can legitimately read "Python" or "SQL", so the floor only
# exists to drop stray punctuation left behind by a paste.
MIN_POINT_CHARS = 3
# A safety valve, not an editorial choice: a paste this long is a whole careers
# page, not one posting. Truncation is logged so the count mismatch is visible.
MAX_POINTS = 120


def guess_company(text: str) -> str:
    """Best-effort company name from the top of a pasted JD."""
    for pat in (
        r"(?:at|join|with)\s+([A-Z][\w&.\-]*(?:\s+[A-Z][\w&.\-]*){0,2})\s+(?:is|are|we|as|you)",
        r"^Company\s*:\s*(.+)$",
        r"^([A-Z][\w&.\-]*(?:\s+[A-Z][\w&.\-]*){0,2})\s+is\s+\w",
    ):
        m = re.search(pat, text, re.M)
        if m:
            return m.group(1).strip()
    for line in text.splitlines()[:6]:
        line = line.strip()
        if 2 <= len(line) <= 40 and not NOISE.match(line) and not HEADING.match(line):
            words = line.split()
            if words and sum(w[:1].isupper() for w in words) >= max(1, len(words) - 1):
                return line
    return ""


def guess_role(text: str) -> str:
    for pat in (r"^(?:Job Title|Title|Role|Position)\s*:\s*(.+)$",):
        m = re.search(pat, text, re.M | re.I)
        if m:
            return m.group(1).strip()
    m = re.search(
        r"\b((?:Senior|Staff|Lead|Principal|Junior|Associate)?\s*"
        r"(?:Software|Data|Machine Learning|ML|Backend|Frontend|Full[- ]?Stack|Cloud|"
        r"Platform|Product|Research|Systems|DevOps|Security|Analytics)?\s*"
        r"(?:Engineer|Scientist|Analyst|Developer|Manager|Architect|Designer))\b",
        text,
    )
    return m.group(1).strip() if m else ""


def _looks_like_heading(line: str) -> bool:
    """Short and unpunctuated, so probably a section label rather than prose."""
    return len(line) <= 60 and not line.rstrip().endswith((".", "!", "?", ","))


def split_points(text: str) -> list[str]:
    """Split a pasted JD into points without rewording any of them.

    Every surviving line becomes exactly one point, carrying its own wording,
    punctuation and sentence count. Nothing is paraphrased, merged, reordered,
    split on sentence boundaries or de-duplicated, so a posting with eleven
    bullets yields eleven points in the order it listed them.

    The only edits are cosmetic, and there are three of them: hard-wrapped
    lines are rejoined, a leading bullet glyph is dropped, and runs of spaces
    collapse to one. Section headings and boilerplate (EEO, benefits, "about
    us", salary, location) are dropped whole; nothing else is.
    """
    # Pasted job descriptions often arrive hard-wrapped at a fixed width, so
    # rejoin split sentences before counting lines -- otherwise one requirement
    # becomes three fragments that each stop mid-clause. `continues` only joins
    # on positive evidence of a cut sentence, so a JD that already puts one
    # requirement per line passes through untouched.
    raw_lines = text.count("\n") + 1
    text = unwrap(
        text,
        is_list_item=lambda ln: bool(BULLET_PREFIX.match(ln)),
        is_heading=lambda ln: bool(HEADING.match(ln)),
    )
    joined = raw_lines - (text.count("\n") + 1)
    if joined:
        obs.event("jd.unwrapped", lines_rejoined=joined, raw_lines=raw_lines)

    # Prose that appears before the first "Responsibilities"/"Requirements"
    # heading is company blurb, not a requirement. If the posting has no
    # headings at all, we have nothing to anchor on and keep everything.
    has_heading = any(HEADING.match(ln) for ln in text.splitlines())
    seen_heading = False
    in_boilerplate = False
    points: list[str] = []
    dropped = 0

    for raw in text.splitlines():
        line = re.sub(r"[ \t]+", " ", raw).strip()
        if not line:
            continue
        if HEADING.match(line):
            seen_heading = True
            in_boilerplate = False
            continue
        if BULLET_PREFIX.match(line):
            # Bullets inherit the section they sit in: the perks under a
            # "Benefits" line are no more a requirement than the line itself.
            line = BULLET_PREFIX.sub("", line).strip()
        elif has_heading and not seen_heading:
            dropped += 1
            continue
        elif NOISE.match(line):
            # Only a heading-shaped line ("Benefits", "About us") opens a
            # boilerplate section. A sentence that merely starts with a noise
            # word drops itself alone, so one false match cannot take every
            # point after it.
            in_boilerplate = _looks_like_heading(line)
        elif _looks_like_heading(line):
            # A heading the pattern above does not know ends the boilerplate
            # section and is kept as a point -- better a stray label in the
            # list than a silently dropped requirement.
            in_boilerplate = False
        if in_boilerplate or NOISE.match(line) or NOISE_ANY.search(line):
            dropped += 1
            continue
        if len(line) < MIN_POINT_CHARS or not HAS_LETTER.search(line):
            continue
        points.append(line)

    if len(points) > MAX_POINTS:
        obs.warn("jd.points_truncated", found=len(points), kept=MAX_POINTS)
        points = points[:MAX_POINTS]
    obs.event("jd.split", points=len(points), boilerplate_dropped=dropped)
    return points


def parse_jd_verbatim(text: str) -> dict:
    """The points exactly as pasted, with placeholder labels.

    `category` and `keywords` are what the model fills in later; the text is
    already final at this stage and nothing downstream may replace it.
    """
    pts = split_points(text)
    obs.event("jd.verbatim", points=len(pts), **obs.tmeta(text, "jd"))
    return {
        "company": guess_company(text),
        "role": guess_role(text),
        "points": [
            {"id": f"jd{i+1:02d}", "text": p, "category": "requirement",
             "keywords": [], "dropped": False}
            for i, p in enumerate(pts)
        ],
    }

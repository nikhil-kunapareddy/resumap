"""Sentence-completeness helpers shared by the resume and JD parsers.

Both parsers reassemble text that a source document broke across lines -- a PDF
bullet wrapping inside its column, or a pasted job description that arrives
hard-wrapped at ~72 characters. Getting that wrong produces points that stop
mid-clause, so the joining rules live in one place.
"""

from __future__ import annotations

import re

# End of a sentence or a self-contained clause.
TERMINATOR_RE = re.compile(r"[.!?][\"')\]]?\s*$")

# A line ending on one of these is unfinished: the thought continues on the
# next line. Trailing punctuation that cannot end a sentence counts too.
# Cannot end an English sentence, so a line ending here is certainly cut off
# and the word is safe to remove when repairing a fragment.
HARD_DANGLING = (
    "and",
    "or",
    "but",
    "nor",
    "the",
    "a",
    "an",
    "of",
    "that",
    "which",
    "who",
    "whom",
    "into",
    "onto",
    "across",
    "through",
    "using",
    "via",
    "per",
    "than",
    "then",
    "while",
    "when",
    "if",
    "its",
    "their",
    "our",
    "his",
    "her",
    "your",
    "my",
    "this",
    "these",
    "those",
    "both",
    "including",
    "include",
    "such",
    "between",
    "among",
    "within",
    "upon",
    "during",
    "against",
    "toward",
    "towards",
    "plus",
    "not",
    "limited",
    "without",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "has",
    "have",
    "had",
    "will",
    "would",
    "can",
    "could",
    "may",
    "might",
    "must",
    "should",
    "do",
    "does",
)

# Prepositions that CAN end a sentence -- "models the ML engineers depend on",
# "the platform we migrated to". These still signal a possible continuation, so
# they count for detection, but stripping them would change the meaning.
SOFT_DANGLING = (
    "to",
    "with",
    "on",
    "in",
    "for",
    "by",
    "from",
    "at",
    "as",
    "about",
    "over",
    "under",
    "after",
    "before",
    "up",
    "down",
    "out",
    "off",
    "around",
    "along",
    # Participles that take an object: a line ending on a bare one of these is
    # almost certainly cut off ("the ingestion path now handling"). Detection
    # only -- peeling them would mangle the sentence.
    "handling",
    "including",
    "spanning",
    "processing",
    "covering",
    "serving",
    "supporting",
    "enabling",
    "involving",
    "comprising",
    "totaling",
    "totalling",
    "representing",
    "exceeding",
    "featuring",
    "containing",
    "targeting",
    "achieving",
    "yielding",
    "leveraging",
    "utilizing",
    "employing",
    "combining",
    "integrating",
    "delivering",
)

DANGLING_WORDS = HARD_DANGLING + SOFT_DANGLING
DANGLING_RE = re.compile(
    r"(?:\b(?:" + "|".join(DANGLING_WORDS) + r")\b|[,;:&+/\-–—])\s*$",
    re.I,
)


# Trailing punctuation that cannot end a sentence (comma, semicolon, dashes...).
PUNCT_TAIL_RE = re.compile("[,;:&+/" + "\u2013\u2014" + "-]+$")


def is_terminated(text: str) -> bool:
    """Does this text end at a sentence boundary?"""
    return bool(TERMINATOR_RE.search(text.strip()))


def is_dangling(text: str) -> bool:
    """Does this text stop mid-thought, implying a continuation line?"""
    return bool(DANGLING_RE.search(text.strip()))


def _can_start_sentence(text: str) -> bool:
    """Could this text plausibly be the start of a new sentence?

    Lower-case letters and digits cannot: a resume line reading "2.4 billion
    events per day" is the tail of the line above it, not a new point.
    """
    ch = text[:1]
    if ch.isupper():
        return True
    # A quote or bracket can legitimately open a sentence.
    return ch in "\"'([\u201c\u2018"


def continues(previous: str, candidate: str) -> bool:
    """Is `candidate` the rest of the sentence begun in `previous`?

    Deliberately strict -- it needs positive evidence of a split sentence, not
    merely the absence of a full stop. Plenty of complete lines carry no
    terminator at all (resume bullets, job titles), and treating those as
    unfinished would glue unrelated lines together.
    """
    prev, cand = previous.strip(), candidate.strip()
    if not prev or not cand:
        return False
    # Cannot open a sentence, and the line above never closed one.
    if not _can_start_sentence(cand) and not is_terminated(prev):
        return True
    # The line above stopped on a connector, a comma or a bare participle.
    return is_dangling(prev)


def ensure_stop(text: str) -> str:
    """Give a point a full stop, so it never reads as though it were cut off."""
    t = text.strip()
    if not t:
        return t
    if is_terminated(t):
        return t
    # Peel trailing connectors and punctuation until the text stands on its
    # own. A full stop placed straight after "and" would only make the fragment
    # look deliberate. Loop, because fragments end on runs like
    # "applications, to" that need two passes.
    # Only HARD_DANGLING is peeled: removing a soft preposition would turn
    # "models the ML engineers depend on" into "... depend".
    dangling_tail = re.compile(r"(?:^|\s+)\b(?:" + "|".join(HARD_DANGLING) + r")\b$", re.I)
    for _ in range(8):
        before = t
        t = PUNCT_TAIL_RE.sub("", t).strip()
        t = dangling_tail.sub("", t).strip()
        if t == before:
            break
    if not t:
        return t
    return t if is_terminated(t) else t + "."


def unwrap(text: str, is_list_item=None, is_heading=None) -> str:
    """Rejoin hard-wrapped lines in pasted plain text.

    A pasted job description usually arrives wrapped at a fixed width, which
    turns one requirement into three fragments. Blank lines, list markers and
    headings all start a fresh line and are never absorbed.
    """
    is_list_item = is_list_item or (lambda _s: False)
    is_heading = is_heading or (lambda _s: False)

    out: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            out.append("")
            continue
        prev = out[-1].strip() if out else ""
        if (
            prev
            and not is_list_item(line)
            and not is_heading(stripped)
            and not is_heading(prev)
            and continues(prev, stripped)
        ):
            out[-1] = out[-1].rstrip() + " " + stripped
        else:
            out.append(line)
    return "\n".join(out)

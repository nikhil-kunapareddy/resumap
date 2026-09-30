"""Labelling the points of a job description the parser already split."""
from __future__ import annotations

from .. import obs
from .schemas import JDTag

# --------------------------------------------------------------------------
# JD labelling
#
# The model never writes a JD point. `parsing/jd.py` splits the paste into
# points verbatim, and the call below only attaches a category and the
# keywords already present in each point -- the schema has no text field, so
# a rewrite is not expressible. Labels are a nice-to-have; the points stand
# on their own when no backend is available.
# --------------------------------------------------------------------------

JD_SYSTEM = """You label the points of a job description that has already been split up.

You are NOT rewriting or re-splitting anything. The user sees these points
exactly as they pasted them; your output cannot change their wording, their
order or how many there are. Return one entry per point, reusing the given id.

For each point:
- `category`: responsibility, requirement, preferred, tooling or soft_skill.
- `keywords`: the tools, technologies, methods and domain terms that literally
  appear in that point, copied character for character as that point spells
  them. These are ATS keywords, so do not normalise "Airflow" to "workflow
  orchestration" or expand an acronym the point did not expand. Never list a
  term the point does not contain. An empty list is the right answer for a
  point that names none.

Also return the hiring company and the job title if the posting states them,
and empty strings if it does not."""



def tag_prompt(points: list[dict], text: str) -> str:
    lines = [f"<job_description>\n{text}\n</job_description>", "<points>"]
    lines += [f"{p['id']}: {p['text']}" for p in points]
    lines.append("</points>")
    lines.append(f"Return exactly {len(points)} tag entries, one per id above.")
    return "\n".join(lines)



def apply_tags(points: list[dict], tags: list[JDTag]) -> None:
    """Write categories and keywords onto the points, in place.

    Keywords are checked back against the point that owns them: the prompt
    asks for substrings, and anything that is not one is the model having
    invented a term rather than found one.
    """
    by_id = {t.id.strip(): t for t in tags}
    untagged, invented = [], 0
    for p in points:
        tag = by_id.get(p["id"])
        if tag is None:
            untagged.append(p["id"])
            continue
        p["category"] = tag.category
        haystack = p["text"].lower()
        kept = []
        for kw in tag.keywords:
            kw = kw.strip()
            if not kw:
                continue
            if kw.lower() in haystack:
                kept.append(kw)
            else:
                invented += 1
        p["keywords"] = kept
    if untagged:
        obs.warn("jd.untagged_points", ids=untagged)
    if invented:
        obs.warn("jd.keywords_not_in_point", dropped=invented)


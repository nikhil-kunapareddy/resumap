"""Model calls: JD parsing, bullet rewriting and recruiter outreach.

The prompts and the schemas live here; whether a call goes to the Anthropic API
or to the local Claude Code install is ``app.backends``' problem.
"""
from __future__ import annotations

import asyncio
from typing import Literal

from pydantic import BaseModel, Field

from . import backends, obs
from .config import LOG_DIR
from .domain import ExperienceKind, OutreachKind, RewriteMode
from .parsing.jd import guess_company, guess_role, parse_jd_verbatim


class BackendUnavailable(RuntimeError):
    """The selected backend cannot run: no API key, or its CLI is missing or signed out."""


def backend_status() -> str | None:
    """Why the configured backend cannot run, or None if it is ready."""
    try:
        return backends.get().unavailable()
    except Exception as exc:  # noqa: BLE001 - an unknown RESUMAP_BACKEND value
        return str(exc)


def _agent() -> str:
    """What to call the live backend in a message to the user."""
    try:
        return backends.get().LABEL
    except Exception:  # noqa: BLE001
        return "The model"


def _used(res: backends.Completion) -> str:
    """The model that answered, as the backend reports it, for logs and costs."""
    return (res.meta or {}).get("model") or backends.model_label()


def _backend() -> backends.Backend:
    why = backend_status()
    if why:
        raise BackendUnavailable(why)
    return backends.get()


# --------------------------------------------------------------------------
# Connection test
#
# A CLI can report itself signed in and still fail every call (an expired
# token that no longer refreshes), so the settings panel's Test button makes
# one real, tiny structured call rather than trusting a status command.
# --------------------------------------------------------------------------

class Ping(BaseModel):
    ok: bool = Field(description="Always true.")
    model: str = Field(description="The name of the model answering, if known, else an empty string.")


async def ping(name: str, model: str | None = None) -> dict:
    """One minimal structured call through backend `name`: what came back, or why not."""
    b = backends.get(name)
    model = model or backends.current_model(name)
    why = b.unavailable()
    if why:
        return {"ok": False, "detail": why}
    try:
        with obs.timer() as t:
            res = await b.complete(model=model,
                                   system="You are a connectivity check. Reply with ok=true.",
                                   user="Reply with ok=true and your model name.",
                                   schema=Ping, max_tokens=200, effort="low")
    except Exception as exc:  # noqa: BLE001 - the error is the answer here
        obs.warn("backend.test_failed", backend=name, error=type(exc).__name__)
        return {"ok": False, "detail": str(exc)[:400]}
    used = (res.meta or {}).get("model") or model or f"{name} default"
    obs.record_usage("backend_test", used, res.usage, t.ms, res.request_id, backend=name)
    if res.parsed is None:
        return {"ok": False, "detail": f"{b.LABEL} answered, but not in the requested JSON shape."}
    reported = res.parsed.model.strip()
    return {"ok": True, "ms": round(t.ms),
            "detail": f"{b.LABEL} answered in {t.ms / 1000:.1f}s"
                      + (f" ({reported})" if reported else "") + "."}


# --------------------------------------------------------------------------
# User-editable prompts
#
# The Map step shows the instructions Transform sends and lets the user
# rewrite them. Only instruction text is editable: the response schema is
# enforced by structured output whatever the prompt says, so an edit changes
# wording and rules but cannot stop the reply being read back. main.py keeps
# the overrides in the settings table and applies them here.
# --------------------------------------------------------------------------

EDITABLE_PROMPTS = {
    "rewrite": "Resume bullets",
    "experience": "Experiences",
    "replace": "Replaced bullets",
    "mode.aggressive": "Mode: aggressive keyword alignment",
    "mode.bridge": "Mode: realign + bridge synonyms",
    "mode.truthful": "Mode: truthful realign",
}

_prompt_overrides: dict[str, str] = {}


def default_prompt(key: str) -> str:
    if key == "rewrite":
        return REWRITE_SYSTEM
    if key == "experience":
        return NEW_BULLET_SYSTEM
    if key == "replace":
        return REPLACE_SYSTEM
    if key.startswith("mode.") and key[5:] in MODE_RULES:
        return MODE_RULES[key[5:]]
    raise KeyError(key)


def prompt_text(key: str) -> str:
    """The instructions in force for `key`: the user's edit, else the default."""
    return _prompt_overrides.get(key) or default_prompt(key)


def set_prompt_overrides(overrides: dict[str, str]) -> None:
    _prompt_overrides.clear()
    _prompt_overrides.update({k: v for k, v in overrides.items()
                              if k in EDITABLE_PROMPTS and v.strip()})


def list_prompts() -> list[dict]:
    return [{"key": k, "label": label, "default": default_prompt(k),
             "text": prompt_text(k), "custom": k in _prompt_overrides}
            for k, label in EDITABLE_PROMPTS.items()]


def _mode_rules(mode: str) -> str:
    return prompt_text(f"mode.{mode}" if mode in MODE_RULES else "mode.aggressive")


def _with_note(system: str, extra: str) -> str:
    if extra.strip():
        system += f"\n\nADDITIONAL USER INSTRUCTION (overrides tone preferences above):\n{extra.strip()}"
    return system


# --------------------------------------------------------------------------
# JD labelling
#
# The model never writes a JD point. `parsing/jd.py` splits the paste into
# points verbatim, and the call below only attaches a category and the
# keywords already present in each point -- the schema has no text field, so
# a rewrite is not expressible. Labels are a nice-to-have; the points stand
# on their own when no backend is available.
# --------------------------------------------------------------------------

class JDTag(BaseModel):
    id: str = Field(description="The id of the point being labelled, copied exactly.")
    category: Literal["responsibility", "requirement", "preferred", "tooling", "soft_skill"]
    keywords: list[str] = Field(
        description="Substrings of that point naming tools, technologies, methods or "
                    "domain terms, copied character for character from the point.")


class JDTags(BaseModel):
    company: str = Field(description="Hiring company name, or empty string if not stated.")
    role: str = Field(description="Job title, or empty string if not stated.")
    tags: list[JDTag]


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


def _tag_prompt(points: list[dict], text: str) -> str:
    lines = [f"<job_description>\n{text}\n</job_description>", "<points>"]
    lines += [f"{p['id']}: {p['text']}" for p in points]
    lines.append("</points>")
    lines.append(f"Return exactly {len(points)} tag entries, one per id above.")
    return "\n".join(lines)


async def parse_jd(text: str) -> dict:
    """Split a JD verbatim, then have Claude label the points it produced."""
    out = parse_jd_verbatim(text)
    out["source"] = "verbatim"
    points = out["points"]
    if not points:
        return out

    user = _tag_prompt(points, text)
    obs.event("jd.parse_start", **obs.tmeta(text, "jd"), points=len(points),
              lines=text.count("\n") + 1, model=backends.model_label(),
              backend=backends.current_name())
    try:
        backend = _backend()
        with obs.timer() as t:
            res = await backend.complete(
                model=backends.current_model(),
                system=JD_SYSTEM,
                user=user,
                schema=JDTags,
                max_tokens=8000,
                effort="medium",
            )
        parsed = res.parsed
        archived = obs.archive_prompt(
            LOG_DIR, "jd_parse", JD_SYSTEM, user, res.text,
            {"model": _used(res), "backend": backends.current_name(),
             "request_id": res.request_id, **res.meta})
        obs.record_usage("jd_parse", _used(res), res.usage, t.ms, res.request_id,
                         backend=backends.current_name(),
                         points=len(parsed.tags) if parsed else 0,
                         prompt_file=archived)
        if parsed:
            _apply_tags(points, parsed.tags)
            out["company"] = parsed.company.strip() or guess_company(text)
            out["role"] = parsed.role.strip() or guess_role(text)
            out["source"] = f"verbatim, labelled by {_agent()}"
            obs.event("jd.parsed", source="claude", points=len(points),
                      has_company=bool(out["company"]), has_role=bool(out["role"]))
            return out
        obs.warn("jd.untagged", why="no_parse")
        out["warning"] = (f"Kept your job description word for word. {_agent()} could not "
                          "tag the points, so keyword hints are missing.")
    except BackendUnavailable as exc:
        # The points are already final, so this costs labels, not text.
        obs.warn("jd.untagged", why="backend_unavailable")
        out["warning"] = f"{exc} Your points are unchanged; they just have no keyword tags."
    except Exception as exc:  # noqa: BLE001 - labelling is optional, surface why
        obs.record_failure("jd_parse", exc, 0.0)
        obs.warn("jd.untagged", why=type(exc).__name__)
        out["warning"] = (f"{_agent()} could not tag the points ({type(exc).__name__}: {exc}). "
                          "Your job description is unchanged.")
    return out


def _apply_tags(points: list[dict], tags: list[JDTag]) -> None:
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


# --------------------------------------------------------------------------
# Bullet rewriting
# --------------------------------------------------------------------------

class Substitution(BaseModel):
    original: str = Field(description="Wording from the source bullet that was replaced.")
    replacement: str = Field(description="What it became in the rewrite.")
    grounded: bool = Field(
        description="True if the replacement is supported by the original bullet. "
        "False if it introduces a tool, scale or claim the original did not state."
    )
    note: str = Field(description="Short reason for the change.")


class Rewrite(BaseModel):
    rewritten: str = Field(description="The tailored bullet. One bullet, no leading glyph.")
    keywords_used: list[str] = Field(description="JD keywords now present in the bullet.")
    substitutions: list[Substitution]
    defensible: bool = Field(
        description="True if every claim in the rewrite can be defended in an "
        "interview by someone whose real experience is the original bullet."
    )


MODE_RULES = {
    RewriteMode.TRUTHFUL: """MODE: TRUTHFUL REALIGN (most conservative)
- Every fact, metric, number, scale and named tool in the rewrite must appear in
  the original bullet. Introduce NOTHING new.
- You may change: word choice, verb strength, clause order, framing, tone, and
  which existing detail gets emphasis.
- You may adopt a JD keyword ONLY when it is a genuine synonym for something the
  original already says.
- Never add a tool, platform, team size, or metric that is not in the original.""",

    RewriteMode.BRIDGE: """MODE: REALIGN + BRIDGE SYNONYMS
- Preserve every metric and outcome from the original exactly.
- You MAY re-express an existing tool or method using the JD's equivalent term
  when they are functionally the same class of thing (e.g. a hand-rolled
  scheduler described as "DAG-style orchestration").
- Phrase a bridge so it stays literally true of the original work. Prefer
  "X-style" / "equivalent to X" over claiming X outright.
- Record every bridge in `substitutions` with grounded=false.
- Never invent a metric, a team, or a domain the original does not mention.""",

    RewriteMode.AGGRESSIVE: """MODE: AGGRESSIVE KEYWORD ALIGNMENT (maximum ATS overlap)
- Priority is maximising literal overlap with the JD's keywords and phrasing.
- You MAY introduce tools, frameworks and terminology from the mapped JD points
  into the bullet, even when the original does not name them, where it is
  plausible they were part of the work described.
- The underlying accomplishment and every NUMBER must still come from the
  original bullet -- never fabricate or inflate a metric, a scale, a team size,
  a company, or a date. Those are facts, not keywords.
- Every introduced term MUST be recorded in `substitutions` with grounded=false
  so the user can review what they are now claiming.
- Set `defensible` to false if the rewrite asserts hands-on experience the
  original does not support.""",
}

REWRITE_SYSTEM = """You tailor a single resume bullet to specific job-description points.

You are given one bullet from a real resume and the JD points it was mapped to.
Rewrite the bullet so an ATS and a human reviewer both see it as a direct answer
to those JD points.

Align three things:
1. KEYWORDS -- use the JD's exact terminology rather than a synonym.
2. TOOLS -- surface the technologies the JD names, subject to the mode rules.
3. TONE -- match the JD's register (scrappy startup vs. regulated enterprise vs.
   research), and mirror whether it emphasises ownership, scale, speed or rigour.

Hard constraints, regardless of mode:
- Output ONE bullet: a complete, grammatical statement, never a fragment that
  stops mid-clause. No leading bullet glyph. End with a full stop if the
  original bullet does.
- Open with a strong past-tense action verb (present tense only if the original
  described current work).
- Keep every number, percentage, duration and dollar figure from the original
  unchanged. Numbers are facts.
- Stay within +/-15% of the original's character count, and never exceed the
  <max_length> given: that is what the original's lines hold, and a bullet
  that spills onto one more line pushes the whole page down.
- Do not produce a bullet that is merely the JD point restated -- it must
  still describe what this person actually did.
- Never use em dashes."""


def _prompt(bullet: str, section: str, jd_points: list[dict], role: str, company: str,
            max_chars: int = 0) -> str:
    lines = [
        f"<target_role>{role or 'unspecified'} at {company or 'unspecified company'}</target_role>",
        f"<resume_section>{section or 'unspecified'}</resume_section>",
        "<original_bullet>",
        bullet,
        "</original_bullet>",
        f"<original_length>{len(bullet)} characters</original_length>",
    ]
    if max_chars:
        lines.append(f"<max_length>{max_chars} characters</max_length>")
    lines += [
        "<mapped_jd_points>",
    ]
    for p in jd_points:
        kw = ", ".join(p.get("keywords") or []) or "none extracted"
        lines.append(f"- {p['text']}\n  (keywords: {kw})")
    lines.append("</mapped_jd_points>")
    return "\n".join(lines)


def rewrite_request(bullet: str, section: str, jd_points: list[dict], role: str,
                    company: str, mode: str = "aggressive", extra: str = "",
                    max_chars: int = 0) -> tuple[str, str]:
    """(system, user) for one bullet rewrite -- exactly what rewrite_bullet sends,
    and what the Map step's prompt preview shows."""
    system = _with_note(prompt_text("rewrite") + "\n\n" + _mode_rules(mode), extra)
    return system, _prompt(bullet, section, jd_points, role, company, max_chars)


async def rewrite_bullet(
    bullet: str,
    section: str,
    jd_points: list[dict],
    role: str,
    company: str,
    mode: str = "aggressive",
    extra: str = "",
    max_chars: int = 0,
) -> dict:
    system, user = rewrite_request(bullet, section, jd_points, role, company,
                                   mode, extra, max_chars)
    return await _run_rewrite("rewrite", system, user, bullet, jd_points, mode, max_chars)


async def _run_rewrite(op: str, system: str, user: str, bullet: str,
                       jd_points: list[dict], mode: str, max_chars: int) -> dict:
    """Send one bullet's (system, user) pair and check the Rewrite that comes back.

    Shared by realigned and replaced bullets: both fill the slot `bullet` held,
    so both answer to its length and its line budget.
    """
    backend = _backend()
    with obs.timer() as t:
        out = await backend.complete(
            model=backends.current_model(),
            system=system,
            user=user,
            schema=Rewrite,
            max_tokens=4000,
            effort="medium",
        )
    r = out.parsed
    request_id = out.request_id
    archived = obs.archive_prompt(
        LOG_DIR, op, system, user, out.text,
        {"model": _used(out), "backend": backends.current_name(), "mode": mode,
         "request_id": request_id,
         "jd_points": [p.get("id") for p in jd_points], **out.meta})
    if r is None:
        obs.warn("rewrite.unparsable", op=op, request_id=request_id,
                 prompt_file=archived)
        raise RuntimeError(f"{_agent()} returned no parsable rewrite.")

    introduced = [x for x in r.substitutions if not x.grounded]
    delta = (len(r.rewritten) - len(bullet)) / max(len(bullet), 1) * 100
    obs.record_usage(op, _used(out), out.usage, t.ms, request_id,
                     backend=backends.current_name(),
                     mode=mode, points=len(jd_points), prompt_file=archived,
                     **obs.tmeta(bullet, "orig"), **obs.tmeta(r.rewritten, "new"),
                     length_delta_pct=round(delta, 1),
                     keywords=len(r.keywords_used),
                     substitutions=len(r.substitutions),
                     introduced=len(introduced),
                     defensible=r.defensible)
    # The prompt caps length growth at +/-15% so the mirrored layout does not
    # reflow; a breach means the instruction was ignored and the page may grow.
    if abs(delta) > 15:
        obs.warn("rewrite.length_breach", op=op, length_delta_pct=round(delta, 1),
                 request_id=request_id, prompt_file=archived)
    if max_chars and len(r.rewritten) > max_chars:
        obs.warn("rewrite.line_budget_breach", op=op, chars=len(r.rewritten),
                 budget=max_chars, request_id=request_id, prompt_file=archived)
    if not r.defensible:
        obs.warn("rewrite.not_defensible", op=op, introduced=len(introduced),
                 prompt_file=archived)
    return {
        "rewritten": r.rewritten.strip().lstrip("•-–— ").strip(),
        "keywords_used": r.keywords_used,
        "substitutions": [s.model_dump() for s in r.substitutions],
        "defensible": r.defensible,
    }


# --------------------------------------------------------------------------
# Replaced bullets
#
# The user can give up a bullet's slot to a JD point the resume does not cover:
# an AWS bullet made over into one about dashboards, say. The new bullet need
# not describe the same work, so the job's mode rules (which all keep the
# original's accomplishment) do not apply. What it keeps is the slot: the
# original's length and line budget, and the role it sits under. Everything it
# claims is new, and `substitutions` is where that has to show.
# --------------------------------------------------------------------------

REPLACE_SYSTEM = """You write a new resume bullet to take the place of an existing one.

The candidate has chosen to give up this bullet's slot on the page to answer
job-description points their resume does not cover yet. The original bullet is
being REPLACED, not reworded: the new one does not have to be about the same
work, tool or outcome. It has to read, to an ATS and a human reviewer alike, as
a direct answer to the mapped JD points.

What you are given:
- <entry>: the role, school or project the bullet sits under. The new bullet
  must read as work done there.
- <other_bullets>: that entry's other bullets. Do not repeat what they already
  claim, and match their style.
- <original_bullet>: its length is your budget and its style is your model.
  Keep any of its detail that still fits the new subject; drop the rest.
- <mapped_jd_points>: what the new bullet must answer.

Hard constraints:
- Output ONE bullet: a complete, grammatical statement, never a fragment that
  stops mid-clause. No leading bullet glyph. End with a full stop if the
  original bullet does.
- Open with a strong past-tense action verb (present tense only if the original
  described current work).
- Use the JD's exact terminology for tools, methods and domain terms.
- Never invent a number, percentage, scale, team size, employer, product name or
  date. Keep a number from the original only if it still describes the new
  work; otherwise leave numbers out and state the outcome in words.
- Stay within +/-15% of the original's character count, and never exceed the
  <max_length> given: the new bullet has to fit the lines the old one held.
- Do not just restate the JD point. Say what was built, analysed or delivered,
  and for whom or to what end, in terms that fit this role.
- Never use em dashes.

Honesty:
- Every tool, method, deliverable and claim in the new bullet that the original
  does not state goes in `substitutions` with grounded=false. `original` is the
  wording it displaced, or an empty string.
- An ADDITIONAL USER INSTRUCTION may describe what the candidate really did.
  Build on it, and still list what it adds with grounded=false, noted as theirs.
- Set `defensible` to true only if the candidate could defend every claim from
  the original bullet and any instruction they gave. A replacement usually
  cannot be, and saying so is the point."""


def _replace_prompt(bullet: str, section: str, entry: str, siblings: list[str],
                    jd_points: list[dict], role: str, company: str,
                    max_chars: int = 0) -> str:
    lines = [
        f"<target_role>{role or 'unspecified'} at {company or 'unspecified company'}</target_role>",
        f"<resume_section>{section or 'unspecified'}</resume_section>",
        f"<entry>{entry or 'none: the bullet sits directly under the section heading'}</entry>",
        "<other_bullets>",
    ]
    lines += [f"- {s}" for s in siblings] or ["- none"]
    lines += [
        "</other_bullets>",
        "<original_bullet>",
        bullet,
        "</original_bullet>",
        f"<original_length>{len(bullet)} characters</original_length>",
    ]
    if max_chars:
        lines.append(f"<max_length>{max_chars} characters</max_length>")
    lines.append("<mapped_jd_points>")
    for p in jd_points:
        kw = ", ".join(p.get("keywords") or []) or "none extracted"
        lines.append(f"- {p['text']}\n  (keywords: {kw})")
    lines.append("</mapped_jd_points>")
    return "\n".join(lines)


def replace_request(bullet: str, section: str, entry: str, siblings: list[str],
                    jd_points: list[dict], role: str, company: str, extra: str = "",
                    max_chars: int = 0) -> tuple[str, str]:
    """(system, user) for one replaced bullet, as sent and as previewed.
    No mode rules: every mode keeps the original's accomplishment."""
    system = _with_note(prompt_text("replace"), extra)
    return system, _replace_prompt(bullet, section, entry, siblings, jd_points,
                                   role, company, max_chars)


async def replace_bullet(
    bullet: str,
    section: str,
    entry: str,
    siblings: list[str],
    jd_points: list[dict],
    role: str,
    company: str,
    extra: str = "",
    max_chars: int = 0,
) -> dict:
    """A new bullet for the slot `bullet` holds, written for `jd_points`."""
    system, user = replace_request(bullet, section, entry, siblings, jd_points,
                                   role, company, extra, max_chars)
    return await _run_rewrite("replace", system, user, bullet, jd_points,
                              "replace", max_chars)


async def rewrite_many(jobs: list[dict], concurrency: int | None = None) -> list[dict]:
    """Rewrite a batch of bullets concurrently, isolating per-bullet failures.

    The default comes from the backend: HTTP requests parallelise more happily
    than one `claude` subprocess per bullet.
    """
    if concurrency is None:
        try:
            concurrency = backends.get().CONCURRENCY
        except Exception:  # noqa: BLE001 - the per-bullet calls report why
            concurrency = 1
    sem = asyncio.Semaphore(concurrency)
    obs.event("rewrite.batch_start", bullets=len(jobs), concurrency=concurrency,
              backend=backends.current_name(),
              mode=(jobs[0]["args"].get("mode") if jobs else None))

    async def one(job: dict) -> dict:
        fn = {"experience": write_experience_bullet,
              "replace": replace_bullet}.get(job.get("kind"), rewrite_bullet)
        async with sem:
            try:
                res = await fn(**job["args"])
                return {"block_id": job["block_id"], "ok": True, **res}
            except Exception as exc:  # noqa: BLE001
                obs.record_failure("rewrite", exc, 0.0, block=job["block_id"])
                return {
                    "block_id": job["block_id"],
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }

    with obs.timer() as t:
        results = await asyncio.gather(*(one(j) for j in jobs))
    ok = sum(1 for r in results if r.get("ok"))
    obs.event("rewrite.batch_done", bullets=len(results), ok=ok,
              failed=len(results) - ok, ms=t.ms, **obs.totals())
    return results


# --------------------------------------------------------------------------
# New bullets from the experience library
#
# An experience the user stored because it is not on the resume becomes one
# new bullet, plus a suggestion of where on the page it goes. Placement is an
# honesty question as much as a layout one: a side project set under an
# employer's entry reads as work done for that employer.
# --------------------------------------------------------------------------

class NewBullet(BaseModel):
    bullet: str = Field(description="The new bullet. One bullet, no leading glyph.")
    keywords_used: list[str] = Field(description="JD keywords now present in the bullet.")
    substitutions: list[Substitution]
    defensible: bool = Field(
        description="True if every claim in the bullet can be defended in an "
        "interview by someone whose real experience is the description given."
    )
    placement: str = Field(
        description="The id of the entry or section the bullet belongs under, copied "
        "from <placements>, or \"new\" for a section of its own.")
    new_section: str = Field(
        description="When placement is \"new\": its heading, in the resume's own "
        "capitalisation. Otherwise an empty string.")


EXPERIENCE_KINDS = {
    ExperienceKind.PROJECT: "Project",
    ExperienceKind.WORK: "Work experience",
    ExperienceKind.SOFT_SKILL: "Soft skill",
    ExperienceKind.OTHER: "Other",
}

# Where a bullet goes when the model's placement names nothing on the page.
DEFAULT_SECTION = {ExperienceKind.PROJECT: "PROJECTS"}

NEW_BULLET_SYSTEM = """You write ONE new resume bullet from an experience the candidate describes in their
own words, aimed at specific job-description points.

The experience is not on the resume yet, and the candidate's description is the
only source of fact about it. Turn it into a bullet an ATS and a human reviewer
both read as a direct answer to the mapped JD points.

Align three things:
1. KEYWORDS -- use the JD's exact terminology rather than a synonym.
2. TOOLS -- surface the technologies the JD names, subject to the mode rules.
3. TONE -- match the JD's register, and the style of the resume's existing
   bullets in <style_examples>.

Hard constraints, regardless of mode:
- Output ONE bullet: a complete, grammatical statement. No leading bullet glyph.
  End with a full stop if the example bullets do.
- Open with a strong past-tense action verb (present tense only if the
  experience is ongoing).
- Every number, percentage, duration, scale and name must come from the
  description. Never invent a metric, a team size, an employer or a date.
- Do not pad a thin description with detail it does not give. A short bullet is
  fine; a soft skill needs the concrete thing the candidate did to show it.
- Never exceed <max_length>. The bullet is new, so every line it takes pushes
  the rest of the page down.
- Never use em dashes.
- In the mode rules below, "the original bullet" means the candidate's
  description.

Placement, from <placements>:
- An entry id ONLY if the description says the work was done in that role or
  at that school. A side project under an employer's entry reads as work done
  for that employer.
- Otherwise a section whose heading fits it (an existing Projects section, say),
  or "new" with a short heading in the resume's own capitalisation, such as
  PROJECTS, LEADERSHIP or ADDITIONAL EXPERIENCE."""


def _experience_prompt(experience: dict, jd_points: list[dict], role: str,
                       company: str, placements: list[dict], examples: list[str],
                       max_chars: int) -> str:
    kind = EXPERIENCE_KINDS.get(experience.get("kind", ""), "Other")
    lines = [
        f"<target_role>{role or 'unspecified'} at {company or 'unspecified company'}</target_role>",
        f"<experience kind=\"{kind}\">",
    ]
    if experience.get("title"):
        lines.append(f"Title: {experience['title']}")
    lines += [experience["text"], "</experience>"]
    if max_chars:
        lines.append(f"<max_length>{max_chars} characters</max_length>")
    if examples:
        lines.append("<style_examples>")
        lines += [f"- {e}" for e in examples]
        lines.append("</style_examples>")
    lines.append("<placements>")
    for p in placements:
        where = f" in {p['section']}" if p.get("section") else ""
        lines.append(f"{p['id']}: {p['kind']} \"{p['text']}\"{where}")
    lines += ["new: a section of its own", "</placements>", "<mapped_jd_points>"]
    for p in jd_points:
        kw = ", ".join(p.get("keywords") or []) or "none extracted"
        lines.append(f"- {p['text']}\n  (keywords: {kw})")
    lines.append("</mapped_jd_points>")
    return "\n".join(lines)


def experience_request(experience: dict, jd_points: list[dict], role: str,
                       company: str, placements: list[dict], examples: list[str],
                       mode: str = "aggressive", extra: str = "",
                       max_chars: int = 0) -> tuple[str, str]:
    """(system, user) for one new bullet from an experience, as sent."""
    system = _with_note(prompt_text("experience") + "\n\n" + _mode_rules(mode), extra)
    return system, _experience_prompt(experience, jd_points, role, company,
                                      placements, examples, max_chars)


async def write_experience_bullet(
    experience: dict,
    jd_points: list[dict],
    role: str,
    company: str,
    placements: list[dict],
    examples: list[str],
    mode: str = "aggressive",
    extra: str = "",
    max_chars: int = 0,
) -> dict:
    """One new bullet from a stored experience, and where it should go.

    `placement` comes back as a block id from `placements` or "new:<heading>";
    anything else the model names is replaced by a new section, never guessed
    onto an employer.
    """
    backend = _backend()
    system, user = experience_request(experience, jd_points, role, company,
                                      placements, examples, mode, extra, max_chars)
    with obs.timer() as t:
        out = await backend.complete(
            model=backends.current_model(),
            system=system,
            user=user,
            schema=NewBullet,
            max_tokens=4000,
            effort="medium",
        )
    r = out.parsed
    archived = obs.archive_prompt(
        LOG_DIR, "experience_bullet", system, user, out.text,
        {"model": _used(out), "backend": backends.current_name(), "mode": mode,
         "request_id": out.request_id,
         "jd_points": [p.get("id") for p in jd_points], **out.meta})
    if r is None:
        obs.warn("experience_bullet.unparsable", request_id=out.request_id,
                 prompt_file=archived)
        raise RuntimeError(f"{_agent()} returned no parsable bullet.")

    ids = {p["id"] for p in placements}
    chosen = r.placement.strip()
    if chosen not in ids:
        heading = (r.new_section.strip()
                   or DEFAULT_SECTION.get(experience.get("kind", ""), "ADDITIONAL EXPERIENCE"))
        chosen = "new:" + heading[:40]
    bullet = r.bullet.strip().lstrip("•-–— ").strip()
    introduced = [x for x in r.substitutions if not x.grounded]
    obs.record_usage("experience_bullet", _used(out), out.usage, t.ms, out.request_id,
                     backend=backends.current_name(), mode=mode, points=len(jd_points),
                     prompt_file=archived,
                     **obs.tmeta(experience["text"], "exp"), **obs.tmeta(bullet, "new"),
                     keywords=len(r.keywords_used), introduced=len(introduced),
                     defensible=r.defensible,
                     placement="new" if chosen.startswith("new:") else chosen)
    if max_chars and len(bullet) > max_chars:
        obs.warn("rewrite.line_budget_breach", chars=len(bullet), budget=max_chars,
                 request_id=out.request_id, prompt_file=archived)
    return {
        "rewritten": bullet,
        "keywords_used": r.keywords_used,
        "substitutions": [x.model_dump() for x in r.substitutions],
        "defensible": r.defensible,
        "placement": chosen,
    }


# --------------------------------------------------------------------------
# Recruiter outreach
# --------------------------------------------------------------------------

# LinkedIn truncates a connection-request note past this many characters.
LINKEDIN_LIMIT = 300

# Liked messages the user keeps as style references, per kind. Only the newest
# few go into a prompt: past that they cost tokens without adding a new voice.
OUTREACH_EXAMPLE_KINDS = tuple(OutreachKind)
OUTREACH_EXAMPLE_LIMIT = 5


class Outreach(BaseModel):
    linkedin_subject: str = Field(
        description="Subject line for the LinkedIn message, used when it goes "
        "out as an InMail or direct message. Under 60 characters."
    )
    linkedin: str = Field(
        description="The LinkedIn note to the recruiter. Plain text, one short "
        f"paragraph, at most {LINKEDIN_LIMIT} characters including spaces."
    )
    email_subject: str = Field(description="Subject line for the follow-up email.")
    email_body: str = Field(
        description="The email body as plain text. Paragraphs separated by a "
        "blank line, signed off with the candidate's first name."
    )
    highlights_used: list[str] = Field(
        description="The resume highlights these messages lean on, quoted briefly."
    )


OUTREACH_SYSTEM = f"""You write two outreach messages from a job candidate to the
recruiter for one specific role, using the candidate's own tailored resume as the
only source of fact.

1. LINKEDIN NOTE -- goes out attached to a connection request, so it has a hard
   limit of {LINKEDIN_LIMIT} characters including spaces. Aim for 240 to 290.
   Name the role and company, give ONE concrete reason this candidate fits, and
   close by asking to connect. No links, no lists, no signature block.
   Also write a subject line for when the same note is sent as an InMail or a
   direct message instead: under 60 characters, naming the role.

2. FOLLOW-UP EMAIL -- sent a few days after applying, to reiterate interest.
   Subject under 70 characters, naming the role. Body of 110 to 160 words in
   three short paragraphs: that the application is in and why this role
   specifically, the single strongest piece of evidence from the resume, then a
   low-pressure ask about next steps. Sign off with the candidate's first name.

Hard constraints:
- Every claim must come from the resume highlights below. Never invent a metric,
  employer, tool, degree or date, and never imply a referral, a prior
  conversation or an interview that has not happened.
- Reuse the job description's own vocabulary wherever the resume supports it.
- Plain text only: no markdown, no emoji, no em dashes.
- No placeholders like [Company] or [Your Name]. Every detail you need is given;
  where something genuinely is not, write around it rather than leaving a blank.
- Greet the recruiter by name only if a name is given. Otherwise open the
  LinkedIn note with "Hi there" and the email with "Hello".
- Specific and plainly confident. Not effusive: no "I am thrilled", no "I would
  be a perfect fit", no praise for the company's mission.

Liked examples: the message may include <liked_linkedin_examples> and
<liked_email_examples>, messages the candidate liked for other roles. Match
their voice: tone, length, structure, how they open, and how direct the ask is.
Take no facts from them. A name, employer, role, metric, tool or event in an
example is not this candidate's unless the resume highlights also say it. Do
not copy their sentences, and where an example breaks a limit above, the limit
wins."""


def _example_lines(examples: list[dict], kind: str) -> list[str]:
    """One tagged block of liked messages of `kind`, or nothing if there are none."""
    picked = [e for e in examples if e["kind"] == kind][:OUTREACH_EXAMPLE_LIMIT]
    if not picked:
        return []
    lines = [f"<liked_{kind}_examples>"]
    for e in picked:
        lines.append("<example>")
        if e.get("subject", "").strip():
            lines.append(f"Subject: {e['subject'].strip()}")
        lines += [e["body"].strip(), "</example>"]
    lines.append(f"</liked_{kind}_examples>")
    return lines


def _outreach_prompt(candidate: str, recruiter: str, role: str, company: str,
                     highlights: list[str], jd_points: list[dict],
                     examples: list[dict] = ()) -> str:
    lines = [
        f"<candidate_name>{candidate or 'unspecified'}</candidate_name>",
        f"<recruiter_name>{recruiter or 'not known'}</recruiter_name>",
        f"<target_role>{role or 'unspecified'} at {company or 'unspecified company'}</target_role>",
        "<resume_highlights>",
    ]
    lines += [f"- {h}" for h in highlights] or ["- none available"]
    lines += ["</resume_highlights>", "<job_description_points>"]
    for p in jd_points:
        kw = ", ".join(p.get("keywords") or []) or "none extracted"
        lines.append(f"- {p['text']}\n  (keywords: {kw})")
    lines.append("</job_description_points>")
    for kind in OUTREACH_EXAMPLE_KINDS:
        lines += _example_lines(list(examples), kind)
    return "\n".join(lines)


async def draft_outreach(
    candidate: str,
    recruiter: str,
    role: str,
    company: str,
    highlights: list[str],
    jd_points: list[dict],
    extra: str = "",
    examples: list[dict] = (),
) -> dict:
    """One LinkedIn note and one follow-up email for a tailored application.

    `examples` are liked messages (newest first) that set the voice; the prompt
    forbids taking facts from them, since they were written for other roles.
    """
    backend = _backend()
    system = OUTREACH_SYSTEM
    if extra.strip():
        system += f"\n\nADDITIONAL USER INSTRUCTION (overrides tone preferences above):\n{extra.strip()}"

    user = _outreach_prompt(candidate, recruiter, role, company, highlights,
                            jd_points, examples)
    n_ex = {k: min(OUTREACH_EXAMPLE_LIMIT, sum(e["kind"] == k for e in examples))
            for k in OUTREACH_EXAMPLE_KINDS}
    obs.event("outreach.start", has_recruiter=bool(recruiter.strip()),
              highlights=len(highlights), points=len(jd_points),
              linkedin_examples=n_ex["linkedin"], email_examples=n_ex["email"],
              has_note=bool(extra.strip()), model=backends.model_label(),
              backend=backends.current_name())
    with obs.timer() as t:
        out = await backend.complete(
            model=backends.current_model(),
            system=system,
            user=user,
            schema=Outreach,
            max_tokens=4000,
            effort="medium",
        )
    o = out.parsed
    archived = obs.archive_prompt(
        LOG_DIR, "outreach", system, user, out.text,
        {"model": _used(out), "backend": backends.current_name(),
         "request_id": out.request_id,
         **out.meta})
    if o is None:
        obs.warn("outreach.unparsable", request_id=out.request_id,
                 prompt_file=archived)
        raise RuntimeError(f"{_agent()} returned no parsable outreach draft.")

    linkedin = o.linkedin.strip()
    li_subject = o.linkedin_subject.strip()
    subject = o.email_subject.strip()
    body = o.email_body.strip()
    obs.record_usage("outreach", _used(out), out.usage, t.ms, out.request_id,
                     backend=backends.current_name(), prompt_file=archived,
                     linkedin_chars=len(linkedin),
                     **obs.tmeta(linkedin, "li"), **obs.tmeta(body, "email"),
                     subject_chars=len(subject), li_subject_chars=len(li_subject))
    # Over the limit LinkedIn would cut the note off mid-word. Truncating here
    # would do the same, so surface it instead and let the user trim it.
    if len(linkedin) > LINKEDIN_LIMIT:
        obs.warn("outreach.linkedin_too_long", chars=len(linkedin),
                 limit=LINKEDIN_LIMIT, prompt_file=archived)
    return {
        "linkedin": linkedin,
        "linkedin_subject": li_subject,
        "email_subject": subject,
        "email_body": body,
        "highlights_used": o.highlights_used,
    }


# --------------------------------------------------------------------------
# PDF corrections chat
#
# The user looks at the rendered preview and says what is wrong with it. The
# model can only answer in the two currencies the renderer understands: new
# text for a block id (stored as an "edited" rewrite, so it lands in the review
# step with its substitutions like any other) and a handful of page-wide layout
# numbers. Everything else is declined in `reply`.
# --------------------------------------------------------------------------

class BlockEdit(BaseModel):
    block_id: str = Field(description="Id of the block to change, copied exactly from the listing.")
    text: str = Field(
        description="The block's complete new text, no leading bullet glyph. For an "
        "entry, only its left-hand part: the right-aligned column is not editable.")
    substitutions: list[Substitution] = Field(
        description="Everything in the new text that the block's ORIGINAL text does "
        "not support, compared with the original rather than the current wording.")
    defensible: bool = Field(
        description="True if every claim can be defended in an interview by someone "
        "whose real experience is the original text.")


class LayoutChange(BaseModel):
    setting: Literal["font_size", "line_spacing", "block_spacing",
                     "side_margins", "top_bottom_margins"]
    value: float = Field(
        description="The new absolute value. font_size, line_spacing and block_spacing "
        "are percentages of the uploaded resume's own (100 = as uploaded); margins "
        "are in points (72 = one inch).")


class PdfRevision(BaseModel):
    reply: str = Field(
        description="One to three plain sentences to the user: what you changed, or "
        "why the request cannot be done and the nearest thing that can.")
    edits: list[BlockEdit]
    layout: list[LayoutChange]
    justify: Literal["unchanged", "original", "on", "off"]


# setting name -> (layout key, divisor from the model's units to the renderer's)
LAYOUT_SETTINGS = {
    "font_size": ("font", 100.0),
    "line_spacing": ("leading", 100.0),
    "block_spacing": ("spacing", 100.0),
    "side_margins": ("margin_x", 1.0),
    "top_bottom_margins": ("margin_y", 1.0),
}

PDF_CHAT_SYSTEM = """You correct a one-page resume PDF while its owner watches the preview.

The PDF is re-rendered from the resume's parsed blocks after every answer, so you
never touch the PDF itself. You can change exactly two things:
1. The text of individual blocks, by id (`edits`).
2. A few page-wide layout settings (`layout`, `justify`).

Anything else -- a different font, bold or italic on particular words, moving,
reordering, adding or deleting blocks or sections, colours, a second page -- you
cannot do. Say so in `reply` and offer the nearest thing you can do. Never
describe a change you did not return, and return no change at all when the
request is only a question.

Reading the listing:
- Every block shows its id and kind, the lines it takes now and in the uploaded
  original, and roughly how many characters fill one of its lines.
- A tailored block shows the wording the PDF uses now and, under it, the
  original from the uploaded resume.
- The renderer fits the page automatically, tightening spacing and then type
  until it is one page; <fit> says how hard it had to squeeze.

Text edits:
- Change only the blocks the request is about, and return each one in full.
- Unless the user asks for a length change, keep an edited block within the
  lines it takes now. Asked to make things fit, to tighten, or to look like the
  original, bring blocks back to their original line count: at most lines x
  characters-per-line, minus a few characters per line for word wrap. A block
  that runs one or two words onto an extra line is the usual culprit.
- Keep every number, percentage, date and employer. Change a number only to a
  value the user gives you.
- `substitutions` compares the new text with the block's ORIGINAL text: list
  every tool, claim or scale it does not support, with grounded=false,
  including terms an earlier rewrite introduced that you are keeping. Something
  the user just asked you to add is still grounded=false, noted as their request.
- No em dashes.

Layout:
- Set only what the user asked to change, as absolute values; leave the rest
  out. "A bit" means about 10 to 15 percent.
- font_size 70-130, line_spacing 70-160, block_spacing 0-250 (percent of the
  original); margins 18-108 points.
- justify: "on" justifies every wrapped bullet and paragraph, "off" sets them
  ragged-right, "original" follows the uploaded resume, "unchanged" keeps the
  current setting. Use "unchanged" unless alignment was asked about.
- Bigger type or more spacing may not fit one page, in which case the renderer
  shrinks it back. Say so when that is likely."""


def _chat_prompt(blocks: list[dict], layout: dict, fit: dict, geometry: dict,
                 role: str, company: str, jd_points: list[dict],
                 history: list[dict], message: str) -> str:
    def pct(v: float) -> str:
        return f"{round(v * 100)}%"

    def pt(key: str) -> int:
        return round(geometry.get(key) or 0)

    mx, my = layout.get("margin_x"), layout.get("margin_y")
    side = f"{round(mx)}pt" if mx else f"as uploaded ({pt('left')}pt)"
    vert = (f"{round(my)}pt" if my
            else f"as uploaded ({pt('top')}pt top, {pt('bottom')}pt bottom)")
    lines = [
        f"<target_role>{role or 'unspecified'} at {company or 'unspecified company'}</target_role>",
        "<layout>",
        f"font_size {pct(layout['font'])}, line_spacing {pct(layout['leading'])}, "
        f"block_spacing {pct(layout['spacing'])}",
        f"side_margins {side}, top_bottom_margins {vert}",
        f"justify {layout['justify']}",
        "</layout>",
        f"<fit>{fit.get('pages', 1)} page(s); leading at {pct(fit.get('leading_scale', 1))}, "
        f"block spacing at {pct(fit.get('space_scale', 1))}, type at "
        f"{pct(fit.get('font_scale', 1))} of the settings above</fit>",
        "<resume>",
    ]
    for b in blocks:
        head = f"[{b['id']}] {b['kind']}"
        if b.get("section"):
            head += f" in {b['section']}"
        if b.get("new"):
            head += (f" | NEW, written from the candidate's own notes (its original), "
                     f"{b['lines']} line(s) now, ~{b['chars_per_line']} chars/line")
        elif b["kind"] != "section":
            head += (f" | {b['lines']} line(s) now, {b['source_lines']} in the original, "
                     f"~{b['chars_per_line']} chars/line")
        if b.get("right"):
            head += f" | right column, not editable: {b['right']}"
        lines.append(head)
        lines.append(f"  now: {b['text']}")
        if b.get("original") and b["original"] != b["text"]:
            lines.append(f"  original: {b['original']}")
        if b.get("pending"):
            lines.append("  (a tailored version is awaiting review and is not in the PDF)")
    lines.append("</resume>")
    kept = [p for p in jd_points if not p.get("dropped")]
    if kept:
        lines.append("<job_description_points>")
        lines += [f"- {p['text']}" for p in kept]
        lines.append("</job_description_points>")
    if history:
        lines.append("<conversation_so_far>")
        for m in history:
            who = "user" if m["role"] == "user" else "you"
            lines.append(f"{who}: {m['text']}")
        lines.append("</conversation_so_far>")
    lines += ["<request>", message, "</request>"]
    return "\n".join(lines)


async def revise_pdf(
    blocks: list[dict],
    layout: dict,
    fit: dict,
    geometry: dict,
    role: str,
    company: str,
    jd_points: list[dict],
    history: list[dict],
    message: str,
) -> dict:
    """Turn one chat request into block edits and layout changes.

    Edits naming an id that is not in `blocks` are dropped here rather than
    trusted, and layout values come back in the renderer's own units.
    """
    backend = _backend()
    user = _chat_prompt(blocks, layout, fit, geometry, role, company, jd_points,
                        history, message)
    obs.event("pdfchat.start", blocks=len(blocks), turns=len(history),
              **obs.tmeta(message, "msg"), model=backends.model_label(),
              backend=backends.current_name())
    with obs.timer() as t:
        out = await backend.complete(
            model=backends.current_model(),
            system=PDF_CHAT_SYSTEM,
            user=user,
            schema=PdfRevision,
            max_tokens=8000,
            effort="medium",
        )
    rev = out.parsed
    archived = obs.archive_prompt(
        LOG_DIR, "pdf_chat", PDF_CHAT_SYSTEM, user, out.text,
        {"model": _used(out), "backend": backends.current_name(),
         "request_id": out.request_id,
         **out.meta})
    if rev is None:
        obs.warn("pdfchat.unparsable", request_id=out.request_id,
                 prompt_file=archived)
        raise RuntimeError(f"{_agent()} returned no parsable answer.")

    known = {b["id"]: b for b in blocks}
    edits, unknown = [], []
    for e in rev.edits:
        bid = e.block_id.strip()
        text = e.text.strip().lstrip("\u2022\u25cf-\u2013\u2014 ").strip()
        if bid not in known:
            unknown.append(bid)
            continue
        if not text or text == known[bid]["text"]:
            continue
        edits.append({"block_id": bid, "text": text,
                      "substitutions": [x.model_dump() for x in e.substitutions],
                      "defensible": e.defensible})
    changes: dict = {}
    for c in rev.layout:
        key, div = LAYOUT_SETTINGS[c.setting]
        changes[key] = c.value / div
    if rev.justify != "unchanged":
        changes["justify"] = rev.justify

    obs.record_usage("pdf_chat", _used(out), out.usage, t.ms, out.request_id,
                     backend=backends.current_name(), prompt_file=archived, edits=len(edits),
                     layout=sorted(changes) or None,
                     introduced=sum(1 for e in edits for x in e["substitutions"]
                                    if not x["grounded"]))
    if unknown:
        obs.warn("pdfchat.unknown_blocks", ids=unknown, prompt_file=archived)
    return {"reply": rev.reply.strip(), "edits": edits, "layout": changes}

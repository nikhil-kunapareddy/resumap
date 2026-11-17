"""The instructions a user can edit, and the store that applies their edits.

The Map step's Prompt... dialog edits these: the rewrite, experience and
replace instructions and the three mode rules. Code reads them through a
``PromptStore``, never the constants, so an edit reaches every call.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ..domain import RewriteMode

# --------------------------------------------------------------------------
# User-editable prompts
#
# The Map step shows the instructions Transform sends and lets the user
# rewrite them. Only instruction text is editable: the response schema is
# enforced by structured output whatever the prompt says, so an edit changes
# wording and rules but cannot stop the reply being read back. The settings
# service keeps the edits in the settings table and hands them to a PromptStore.
# --------------------------------------------------------------------------

EDITABLE_PROMPTS = {
    "rewrite": "Resume bullets",
    "experience": "Experiences",
    "replace": "Replaced bullets",
    "mode.aggressive": "Mode: aggressive keyword alignment",
    "mode.bridge": "Mode: realign + bridge synonyms",
    "mode.truthful": "Mode: truthful realign",
}


MODE_RULES: dict[str, str] = {
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


def with_note(system: str, extra: str) -> str:
    if extra.strip():
        system += (
            f"\n\nADDITIONAL USER INSTRUCTION (overrides tone preferences above):\n{extra.strip()}"
        )
    return system


def target_role(role: str, company: str) -> str:
    """The <target_role> line every request leads with."""
    return (
        f"<target_role>{role or 'unspecified'} at {company or 'unspecified company'}</target_role>"
    )


def jd_point_lines(points: Sequence[dict]) -> list[str]:
    """One entry per JD point: its text, then the keywords tagged on it."""
    lines = []
    for p in points:
        kw = ", ".join(p.get("keywords") or []) or "none extracted"
        lines.append(f"- {p['text']}\n  (keywords: {kw})")
    return lines


class PromptStore:
    """The instructions in force: each editable prompt's default, or the user's edit of it."""

    def __init__(self, overrides: Mapping[str, str] | None = None) -> None:
        self._overrides: dict[str, str] = {}
        self.set_overrides(overrides or {})

    @staticmethod
    def default(key: str) -> str:
        if key == "rewrite":
            return REWRITE_SYSTEM
        if key == "experience":
            return NEW_BULLET_SYSTEM
        if key == "replace":
            return REPLACE_SYSTEM
        if key.startswith("mode.") and key[5:] in MODE_RULES:
            return MODE_RULES[key[5:]]
        raise KeyError(key)

    def text(self, key: str) -> str:
        """The instructions in force for `key`: the user's edit, else the default."""
        return self._overrides.get(key) or self.default(key)

    def set_overrides(self, overrides: Mapping[str, str]) -> None:
        """Replace every edit at once; unknown keys and blank text are ignored."""
        self._overrides = {
            k: v for k, v in overrides.items() if k in EDITABLE_PROMPTS and v.strip()
        }

    def is_custom(self, key: str) -> bool:
        return key in self._overrides

    def describe(self) -> list[dict]:
        return [
            {
                "key": k,
                "label": label,
                "default": self.default(k),
                "text": self.text(k),
                "custom": self.is_custom(k),
            }
            for k, label in EDITABLE_PROMPTS.items()
        ]

    def mode_rules(self, mode: str) -> str:
        return self.text(f"mode.{mode}" if mode in MODE_RULES else "mode.aggressive")

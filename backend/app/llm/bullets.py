"""The messages that ask for a bullet: realigned, replaced, or new from an experience.

Each ``*_request`` builds the exact (system, user) pair, for the call and for
the Map step's preview alike, so the preview cannot drift from what is sent.
"""
from __future__ import annotations

from ..domain import ExperienceKind
from .prompts import PromptStore, with_note

# --------------------------------------------------------------------------
# Bullet rewriting
# --------------------------------------------------------------------------

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



def rewrite_request(prompts: PromptStore, bullet: str, section: str, jd_points: list[dict], role: str,
                    company: str, mode: str = "aggressive", extra: str = "",
                    max_chars: int = 0) -> tuple[str, str]:
    """(system, user) for one bullet rewrite -- exactly what rewrite_bullet sends,
    and what the Map step's prompt preview shows."""
    system = with_note(prompts.text("rewrite") + "\n\n" + prompts.mode_rules(mode), extra)
    return system, _prompt(bullet, section, jd_points, role, company, max_chars)



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



def replace_request(prompts: PromptStore, bullet: str, section: str, entry: str, siblings: list[str],
                    jd_points: list[dict], role: str, company: str, extra: str = "",
                    max_chars: int = 0) -> tuple[str, str]:
    """(system, user) for one replaced bullet, as sent and as previewed.
    No mode rules: every mode keeps the original's accomplishment."""
    system = with_note(prompts.text("replace"), extra)
    return system, _replace_prompt(bullet, section, entry, siblings, jd_points,
                                   role, company, max_chars)



# --------------------------------------------------------------------------
# New bullets from the experience library
#
# An experience the user stored because it is not on the resume becomes one
# new bullet, plus a suggestion of where on the page it goes. Placement is an
# honesty question as much as a layout one: a side project set under an
# employer's entry reads as work done for that employer.
# --------------------------------------------------------------------------

EXPERIENCE_KINDS = {
    ExperienceKind.PROJECT: "Project",
    ExperienceKind.WORK: "Work experience",
    ExperienceKind.SOFT_SKILL: "Soft skill",
    ExperienceKind.OTHER: "Other",
}

# Where a bullet goes when the model's placement names nothing on the page.
DEFAULT_SECTION = {ExperienceKind.PROJECT: "PROJECTS"}


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



def experience_request(prompts: PromptStore, experience: dict, jd_points: list[dict], role: str,
                       company: str, placements: list[dict], examples: list[str],
                       mode: str = "aggressive", extra: str = "",
                       max_chars: int = 0) -> tuple[str, str]:
    """(system, user) for one new bullet from an experience, as sent."""
    system = with_note(prompts.text("experience") + "\n\n" + prompts.mode_rules(mode), extra)
    return system, _experience_prompt(experience, jd_points, role, company,
                                      placements, examples, max_chars)


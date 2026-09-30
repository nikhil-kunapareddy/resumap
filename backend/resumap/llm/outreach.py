"""The messages that ask for recruiter outreach."""

from __future__ import annotations

from collections.abc import Sequence

from ..domain import OutreachKind

# --------------------------------------------------------------------------
# Recruiter outreach
# --------------------------------------------------------------------------

# LinkedIn truncates a connection-request note past this many characters.
LINKEDIN_LIMIT = 300

# Liked messages the user keeps as style references, per kind. Only the newest
# few go into a prompt: past that they cost tokens without adding a new voice.
OUTREACH_EXAMPLE_KINDS = tuple(OutreachKind)
OUTREACH_EXAMPLE_LIMIT = 5


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


def outreach_prompt(
    candidate: str,
    recruiter: str,
    role: str,
    company: str,
    highlights: list[str],
    jd_points: list[dict],
    examples: Sequence[dict] = (),
) -> str:
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

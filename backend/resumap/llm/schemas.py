"""What each model call must answer with.

Every call names one of these as its response schema, and the backend returns
an instance of it or nothing. The schema is the only guarantee: the prompts
ask for lengths and substitutions, but the gateway still checks them itself.
Changing a field or its description changes what is sent to the model.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .outreach import LINKEDIN_LIMIT


class Ping(BaseModel):
    ok: bool = Field(description="Always true.")
    model: str = Field(
        description="The name of the model answering, if known, else an empty string."
    )


class JDTag(BaseModel):
    id: str = Field(description="The id of the point being labelled, copied exactly.")
    category: Literal["responsibility", "requirement", "preferred", "tooling", "soft_skill"]
    keywords: list[str] = Field(
        description="Substrings of that point naming tools, technologies, methods or "
        "domain terms, copied character for character from the point."
    )


class JDTags(BaseModel):
    company: str = Field(description="Hiring company name, or empty string if not stated.")
    role: str = Field(description="Job title, or empty string if not stated.")
    tags: list[JDTag]


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
        'from <placements>, or "new" for a section of its own.'
    )
    new_section: str = Field(
        description='When placement is "new": its heading, in the resume\'s own '
        "capitalisation. Otherwise an empty string."
    )


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


class BlockEdit(BaseModel):
    block_id: str = Field(description="Id of the block to change, copied exactly from the listing.")
    text: str = Field(
        description="The block's complete new text, no leading bullet glyph. For an "
        "entry, only its left-hand part: the right-aligned column is not editable."
    )
    substitutions: list[Substitution] = Field(
        description="Everything in the new text that the block's ORIGINAL text does "
        "not support, compared with the original rather than the current wording."
    )
    defensible: bool = Field(
        description="True if every claim can be defended in an interview by someone "
        "whose real experience is the original text."
    )


class LayoutChange(BaseModel):
    setting: Literal[
        "font_size", "line_spacing", "block_spacing", "side_margins", "top_bottom_margins"
    ]
    value: float = Field(
        description="The new absolute value. font_size, line_spacing and block_spacing "
        "are percentages of the uploaded resume's own (100 = as uploaded); margins "
        "are in points (72 = one inch)."
    )


class PdfRevision(BaseModel):
    reply: str = Field(
        description="One to three plain sentences to the user: what you changed, or "
        "why the request cannot be done and the nearest thing that can."
    )
    edits: list[BlockEdit]
    layout: list[LayoutChange]
    justify: Literal["unchanged", "original", "on", "off"]

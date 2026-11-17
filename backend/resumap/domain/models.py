"""The shapes Resumap stores and reasons about.

Two kinds of model live here:

* **Entities** (``Resume``, ``Job``, ``Rewrite``, ``Experience`` ...) mirror
  rows the app writes. They are strict: an unexpected field is an error,
  because it can only mean the table and the model have drifted apart.
* **Documents** (``ResumeDoc``, ``Block``, ``Run`` and the ``*Meta`` blobs)
  are JSON persisted whole. They are tolerant readers: a document written by
  an older or newer version still loads, and keys this version does not know
  pass through untouched instead of being dropped on the next save.

The storage layer hands these models out and the services work with them. The
parser, renderer and prompt builders still take the plain-dict form the
models serialise to (``ResumeDoc.as_dict()`` for the resume).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .enums import (
    BlockKind,
    ChatRole,
    ExperienceKind,
    JDCategory,
    Justify,
    OutreachKind,
    RewriteMode,
    RewriteStatus,
)


class Entity(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Document(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


# -- the parsed resume ------------------------------------------------------


class Run(Document):
    """A stretch of one line in a single style, possibly a link."""

    text: str
    bold: bool = False
    italic: bool = False
    uri: str = ""
    color: str = ""
    underline: bool = False


class LineGeometry(Document):
    """Where a block sat on the source page, in points. Read by the renderer only."""

    x0: float
    x1: float
    text_x0: float
    top: float
    bottom: float
    size: float
    bold: bool
    italic: bool
    font: str
    page: int


class Block(Document):
    """One classified piece of a resume, as the browser sees it.

    ``id`` ("bul005", "sec003") is the join key for mappings, rewrites and
    export overrides, so it is stable for as long as the resume is.
    """

    id: str
    kind: BlockKind
    section: str = ""
    text: str
    left: str = ""  # an entry's two columns, when it has a right-aligned one
    right: str = ""
    bullet_char: str = "•"
    rule_below: bool = False
    mappable: bool = False
    reason: str | None = Field(
        default=None, description="Why the block is not a mappable point; null for one that is."
    )
    truncated: bool = False
    justified: bool = False
    runs: list[Run] = Field(default_factory=list)
    right_runs: list[Run] = Field(default_factory=list)


class StoredBlock(Block):
    """A block as persisted, with the source geometry the renderer replays."""

    line: LineGeometry = Field(alias="_line")


class PageGeometry(Document):
    width: float
    height: float
    left: float
    right: float
    top: float
    bottom: float


class ResumeDoc(Document):
    """The whole parsed resume, stored as one JSON blob."""

    name: str = ""
    body_size: float
    leading: float
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    geometry: PageGeometry
    blocks: list[StoredBlock]
    sections: list[str] = Field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """The blob exactly as stored: the form the parser writes and the renderer reads."""
        return self.model_dump(by_alias=True, mode="json")


class Resume(Entity):
    id: int
    label: str
    filename: str
    stored_path: str
    person_name: str = ""
    created_at: float
    doc: ResumeDoc


class ResumeSummary(Entity):
    """A resume in the list view, without its document."""

    id: int
    label: str
    filename: str
    person_name: str
    created_at: float
    job_count: int


# -- jobs and what hangs off them -------------------------------------------


class JDPoint(Document):
    """One line of a job description, verbatim. Only its labels come from a model."""

    id: str
    text: str
    category: JDCategory = JDCategory.REQUIREMENT
    keywords: list[str] = Field(default_factory=list)
    dropped: bool = False


class Job(Entity):
    id: int
    resume_id: int
    company: str = ""
    role: str = ""
    raw_text: str
    mode: RewriteMode = RewriteMode.AGGRESSIVE
    created_at: float
    points: list[JDPoint]

    def summary(self) -> JobSummary:
        return JobSummary.model_validate(self.model_dump(include=set(JobSummary.model_fields)))


class JobSummary(Entity):
    """A job in the list view, without its posting and points."""

    id: int
    resume_id: int
    company: str
    role: str
    mode: RewriteMode
    created_at: float


class Substitution(Document):
    """A change a rewrite made. ``grounded=False`` marks a claim the original did not make."""

    original: str
    replacement: str
    grounded: bool
    note: str = ""


class RewriteMeta(Document):
    keywords_used: list[str] = Field(default_factory=list)
    substitutions: list[Substitution] = Field(default_factory=list)
    defensible: bool | None = None
    # Bullets written from experiences: an entry id, a section id or "new:<heading>".
    placement: str | None = None
    placement_user: bool = False
    replaced: bool = False
    source: str | None = Field(
        default=None, description='"chat" when the corrections chat wrote it.'
    )


class Rewrite(Entity):
    """A tailored version of one block (or experience), and its review state."""

    job_id: int
    block_id: str
    original: str
    rewritten: str
    status: RewriteStatus
    meta: RewriteMeta = Field(default_factory=RewriteMeta)
    updated_at: float

    @property
    def in_export(self) -> bool:
        """Whether this wording replaces the source's in the exported PDF."""
        return self.status.included and bool(self.rewritten.strip())


class Layout(Entity):
    """Page-wide adjustments on top of the mirrored layout (see pdfgen.clean_layout).

    Font, leading and spacing multiply the source's own values; margins
    replace them, in points.
    """

    font: float = 1.0
    leading: float = 1.0
    spacing: float = 1.0
    margin_x: float | None = None
    margin_y: float | None = None
    justify: Justify = Justify.ORIGINAL


class ChatMessage(Entity):
    id: int
    job_id: int
    role: ChatRole
    text: str
    # An assistant turn records what it changed and the state it replaced.
    meta: dict[str, Any] = Field(default_factory=dict)
    created_at: float


class OutreachMeta(Document):
    highlights_used: list[str] = Field(default_factory=list)
    linkedin_limit: int | None = None


class OutreachDraft(Entity):
    job_id: int
    recruiter: str = ""
    linkedin: str = ""
    linkedin_subject: str = ""
    email_subject: str = ""
    email_body: str = ""
    meta: OutreachMeta = Field(default_factory=OutreachMeta)
    updated_at: float


# -- global libraries -------------------------------------------------------


class Experience(Entity):
    """Something that is not on the resume, stored once and offered to every job."""

    id: int
    kind: ExperienceKind
    title: str = ""
    text: str
    created_at: float
    key: str = Field(description='Its mapping and rewrite key, e.g. "exp012".')


class OutreachExample(Entity):
    """A liked message, sent with every draft as a style reference only."""

    id: int
    kind: OutreachKind
    subject: str = ""
    body: str
    created_at: float

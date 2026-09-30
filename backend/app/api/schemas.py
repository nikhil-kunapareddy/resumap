"""Request bodies and response envelopes for every JSON route.

Every route declares one of these as its response, so ``/openapi.json``
describes the whole API and a client can generate its types from it.
Envelopes are strict (``extra="forbid"``): a handler that returns a field
the contract does not name fails its request, and the test suite, rather
than shipping it undocumented. Bodies ignore unknown fields, as before.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..domain import (Block, BoxMode, ChatRole, ExperienceKind, JDPoint, Layout, OutreachDraft,
                      OutreachKind, Rewrite, RewriteMode, RewriteStatus, Substitution)


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Ok(Envelope):
    ok: bool = True


class ErrorOut(Envelope):
    detail: str
    errors: list[dict[str, Any]] | None = Field(
        default=None, description="Per-field validation errors, on a 422 only.")


# -- request bodies ---------------------------------------------------------

class JobCreate(BaseModel):
    resume_id: int
    text: str
    mode: RewriteMode = RewriteMode.AGGRESSIVE


class JobPatch(BaseModel):
    company: str | None = None
    role: str | None = None
    mode: RewriteMode | None = None


class PointsKeep(BaseModel):
    keep: list[str]


class MappingBody(BaseModel):
    block_id: str
    point_ids: list[str]


class BoxModeBody(BaseModel):
    block_id: str
    mode: BoxMode = BoxMode.INHERIT


class TransformBody(BaseModel):
    block_ids: list[str] | None = Field(
        default=None, description="Re-roll only these boxes; every mapped box when omitted.")
    extra: str = Field(default="", description="A note for the model, e.g. a tone request.")


class RewritePatch(BaseModel):
    text: str | None = None
    status: RewriteStatus | None = None
    # Where a bullet written from an experience goes: see pdfgen.insert_blocks.
    placement: str | None = None


class ExperienceCreate(BaseModel):
    kind: ExperienceKind = ExperienceKind.PROJECT
    title: str = ""
    text: str


class OutreachBody(BaseModel):
    recruiter: str = ""
    extra: str = ""


class OutreachPatch(BaseModel):
    recruiter: str | None = None
    linkedin: str | None = None
    linkedin_subject: str | None = None
    email_subject: str | None = None
    email_body: str | None = None


class OutreachExampleCreate(BaseModel):
    kind: OutreachKind
    subject: str = ""
    body: str


class SettingsBody(BaseModel):
    first_name: str


class ChatBody(BaseModel):
    message: str


class BackendBody(BaseModel):
    backend: str
    # Blank means the backend's default: RESUMAP_MODEL for the Anthropic ones,
    # the CLI's own configured model for Codex and Pi.
    model: str = ""


class ModelBody(BaseModel):
    model: str = ""


class PromptBody(BaseModel):
    text: str


# -- status, settings and prompts -------------------------------------------

class StatusOut(Envelope):
    backend: str
    label: str
    ready: bool
    detail: str | None = Field(description="Why the backend cannot run; null when it can.")
    model: str
    first_name: str


class BackendInfo(Envelope):
    name: str
    label: str
    ready: bool
    detail: str | None
    default_model: str
    model: str
    selected: bool


class BackendsOut(Envelope):
    selected: str
    backends: list[BackendInfo]


class BackendTestOut(Envelope):
    ok: bool
    detail: str
    ms: int | None = None


class SettingsOut(Envelope):
    first_name: str


class PromptInfo(Envelope):
    key: str
    label: str
    default: str
    text: str
    custom: bool


class PromptsOut(Envelope):
    prompts: list[PromptInfo]


# -- resumes and experiences ------------------------------------------------

class ResumeSummary(Envelope):
    id: int
    label: str
    filename: str
    person_name: str
    created_at: float
    job_count: int


class ResumeOut(Envelope):
    """A parsed resume without the source geometry only the renderer needs."""

    id: int
    label: str
    filename: str
    name: str
    sections: list[str]
    diagnostics: dict[str, Any]
    blocks: list[Block]


class ExperienceDeleted(Envelope):
    ok: bool = True
    mappings: int
    rewrites: int


# -- jobs, mapping and transform --------------------------------------------

class JobSummary(Envelope):
    id: int
    resume_id: int
    company: str
    role: str
    mode: RewriteMode
    created_at: float


class JobCreated(Envelope):
    id: int
    company: str
    role: str
    points: list[JDPoint]
    mode: RewriteMode
    source: str = Field(description='"verbatim", or "verbatim, labelled by <backend>".')
    warning: str | None


class JobDetail(Envelope):
    id: int
    resume_id: int
    company: str
    role: str
    mode: RewriteMode
    raw: str
    points: list[JDPoint]
    mappings: dict[str, list[str]]
    box_modes: dict[str, BoxMode]
    rewrites: dict[str, Rewrite]
    outreach: OutreachDraft | None
    resume: ResumeOut | None


class PointsKept(Envelope):
    ok: bool = True
    points: list[JDPoint]
    dropped: list[str]
    unwired: int


class MappingSaved(Envelope):
    ok: bool = True
    block_id: str
    point_ids: list[str]


class BoxModeSaved(Envelope):
    ok: bool = True
    block_id: str
    mode: BoxMode


class TransformResult(Envelope):
    """One box's outcome. A failed box carries ``error`` and nothing else."""

    block_id: str
    ok: bool
    error: str | None = None
    rewritten: str | None = None
    keywords_used: list[str] | None = None
    substitutions: list[Substitution] | None = None
    defensible: bool | None = None
    placement: str | None = None


class TransformOut(Envelope):
    results: list[TransformResult]
    rewrites: dict[str, Rewrite]


class PromptPreview(Envelope):
    block_id: str
    kind: Literal["rewrite", "experience", "replace"]
    mode: RewriteMode
    system: str
    user: str


# -- export -----------------------------------------------------------------

class SelfCheck(Envelope):
    """report.verify_pdf's findings. ``ok`` is null when the check itself failed."""

    ok: bool | None
    pages: int | None = None
    links: int | None = None
    overflow_rows: int | None = None
    words: int | None = None
    has_spaces: bool | None = None
    dingbat_font: bool | None = None
    source_links: int | None = None
    similarity_pct: float | None = None
    problems: list[str] = Field(default_factory=list)
    error: str | None = None


class ExportOut(Envelope):
    check: SelfCheck
    filename: str
    url: str
    applied: int
    total: int
    pages: int
    font_scale: float
    cramped: bool


class FitOut(Envelope):
    pages: int
    leading_scale: float
    space_scale: float
    font_scale: float
    compressed: bool
    cramped: bool
    layout: Layout


# -- corrections chat -------------------------------------------------------

class ChatMetaOut(BaseModel):
    """What an assistant turn changed. The undo snapshot is never sent."""

    model_config = ConfigDict(extra="allow")

    edits: list[str] = Field(default_factory=list)
    layout: dict[str, Any] = Field(default_factory=dict)
    undone: bool = False


class ChatMessageOut(Envelope):
    id: int
    role: ChatRole
    text: str
    meta: ChatMetaOut
    can_undo: bool


class ChatState(Envelope):
    messages: list[ChatMessageOut]
    rewrites: dict[str, Rewrite]
    layout: Layout


# -- outreach ---------------------------------------------------------------

class OutreachOut(Envelope):
    outreach: OutreachDraft | None


# -- diagnostics ------------------------------------------------------------

class UsageOut(Envelope):
    model: str
    backend: str
    calls: int
    failures: int
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    cost_usd: float
    by_op: dict[str, dict[str, Any]]


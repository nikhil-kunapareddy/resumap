"""What each service returns: the read models the API declares as its responses.

Every JSON route returns one of these, so ``/openapi.json`` describes the
whole API and a client can generate its types from it. They are strict
(``extra="forbid"``): a service that returns a field the contract does not
name fails its request, and the test suite, rather than shipping it
undocumented. Optional fields are always present, as null or a default.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..domain import (
    Block,
    BoxMode,
    ChatRole,
    JDPoint,
    Layout,
    OutreachDraft,
    Rewrite,
    RewriteMode,
    Substitution,
)


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Ok(Envelope):
    ok: bool = True


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


PreviewKind = Literal["rewrite", "experience", "replace"]


class PromptPreview(Envelope):
    block_id: str
    kind: PreviewKind
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


"""What the HTTP layer accepts: request bodies, and the one error body it sends.

Response models belong to the services that produce them (services/results.py);
the routers only declare them. Bodies ignore unknown fields.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..domain import BoxMode, ExperienceKind, OutreachKind, RewriteMode, RewriteStatus


class ErrorOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

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

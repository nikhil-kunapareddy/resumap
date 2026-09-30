"""Resumap's domain: the vocabularies and shapes every other layer agrees on.

Nothing here imports from the rest of the app. HTTP, storage, the model
backends and the renderer depend on this package, never the other way round.
"""
from .enums import (
                    INCLUDED_STATUSES,
                    BlockKind,
                    BoxMode,
                    ChatRole,
                    ExperienceKind,
                    JDCategory,
                    Justify,
                    OutreachKind,
                    RewriteMode,
                    RewriteStatus,
)
from .keys import experience_id, experience_key
from .models import (
                    Block,
                    ChatMessage,
                    Experience,
                    JDPoint,
                    Job,
                    JobSummary,
                    Layout,
                    LineGeometry,
                    OutreachDraft,
                    OutreachExample,
                    OutreachMeta,
                    PageGeometry,
                    Resume,
                    ResumeDoc,
                    ResumeSummary,
                    Rewrite,
                    RewriteMeta,
                    Run,
                    StoredBlock,
                    Substitution,
)

__all__ = [
    "INCLUDED_STATUSES", "BlockKind", "BoxMode", "ChatRole", "ExperienceKind", "JDCategory",
    "Justify", "OutreachKind", "RewriteMode", "RewriteStatus",
    "experience_id", "experience_key",
    "Block", "ChatMessage", "Experience", "JDPoint", "Job", "JobSummary", "Layout",
    "LineGeometry", "OutreachDraft", "OutreachExample", "OutreachMeta", "PageGeometry",
    "Resume", "ResumeDoc", "ResumeSummary", "Rewrite", "RewriteMeta", "Run", "StoredBlock",
    "Substitution",
]

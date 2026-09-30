"""Diagnostics: the parse report and token usage."""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from ... import obs
from ...services.results import UsageOut
from ..deps import ServicesDep

router = APIRouter(prefix="/api/debug", tags=["diagnostics"])


@router.get("/parse/{resume_id}", response_class=PlainTextResponse)
def debug_parse(resume_id: int, services: ServicesDep) -> str:
    """The block-classification table for a stored resume."""
    return services.resumes.parse_report(resume_id)


@router.get("/usage")
def debug_usage(services: ServicesDep) -> UsageOut:
    """Token and cost totals for this process, broken down by operation."""
    registry = services.gateway.registry
    return UsageOut(model=registry.model_label(), backend=registry.current_name(),
                    **obs.totals())

"""Transform and review: the rewrites of a job's mapped boxes."""
from __future__ import annotations

from fastapi import APIRouter

from ...domain import Rewrite
from ...services.results import PromptPreview, TransformOut
from ..deps import ServicesDep
from ..schemas import RewritePatch, TransformBody

router = APIRouter(prefix="/api/jobs/{job_id}", tags=["rewrites"])


@router.post("/transform")
async def transform(job_id: int, body: TransformBody, services: ServicesDep) -> TransformOut:
    return await services.rewrites.transform(job_id, body.block_ids, body.extra)


@router.get("/prompt")
def prompt_preview(job_id: int, block_id: str, services: ServicesDep) -> PromptPreview:
    """The exact system prompt and message Transform would send for one box."""
    return services.rewrites.preview(job_id, block_id)


@router.patch("/rewrites/{block_id}")
def patch_rewrite(job_id: int, block_id: str, body: RewritePatch,
                  services: ServicesDep) -> Rewrite:
    return services.rewrites.edit(job_id, block_id, text=body.text, status=body.status,
                                  placement=body.placement)


@router.post("/accept-all")
def accept_all(job_id: int, services: ServicesDep) -> dict[str, Rewrite]:
    return services.rewrites.accept_all(job_id)

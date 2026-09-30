"""Jobs: the pasted posting, its points, and the Map step."""

from __future__ import annotations

from fastapi import APIRouter

from ...domain import JobSummary
from ...services.results import BoxModeSaved, JobCreated, JobDetail, MappingSaved, Ok, PointsKept
from ..deps import ServicesDep
from ..schemas import BoxModeBody, JobCreate, JobPatch, MappingBody, PointsKeep

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("")
def jobs(services: ServicesDep, resume_id: int | None = None) -> list[JobSummary]:
    return services.jobs.all(resume_id)


@router.post("")
async def create_job(body: JobCreate, services: ServicesDep) -> JobCreated:
    return await services.jobs.create(body.resume_id, body.text, body.mode)


@router.get("/{job_id}")
def job_detail(job_id: int, services: ServicesDep) -> JobDetail:
    return services.jobs.detail(job_id)


@router.patch("/{job_id}")
def patch_job(job_id: int, body: JobPatch, services: ServicesDep) -> JobSummary:
    """Change a job's company, role or mode; returns the job as it now stands."""
    return services.jobs.update(job_id, company=body.company, role=body.role, mode=body.mode)


@router.delete("/{job_id}")
def job_delete(job_id: int, services: ServicesDep) -> Ok:
    return services.jobs.delete(job_id)


@router.put("/{job_id}/points")
def set_points(job_id: int, body: PointsKeep, services: ServicesDep) -> PointsKept:
    """Choose which parsed points to carry into mapping."""
    return services.jobs.keep_points(job_id, body.keep)


@router.post("/{job_id}/mappings")
def save_mapping(job_id: int, body: MappingBody, services: ServicesDep) -> MappingSaved:
    return services.jobs.map_box(job_id, body.block_id, body.point_ids)


@router.post("/{job_id}/box-modes")
def save_box_mode(job_id: int, body: BoxModeBody, services: ServicesDep) -> BoxModeSaved:
    """Set how one resume box is transformed."""
    return services.jobs.set_box_mode(job_id, body.block_id, body.mode)

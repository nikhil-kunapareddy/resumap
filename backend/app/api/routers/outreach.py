"""Recruiter outreach, and the liked messages that set its voice."""
from __future__ import annotations

from fastapi import APIRouter

from ...domain import OutreachExample
from ...services.results import Ok, OutreachOut
from ..deps import ServicesDep
from ..schemas import OutreachBody, OutreachExampleCreate, OutreachPatch

router = APIRouter(prefix="/api", tags=["outreach"])


@router.get("/jobs/{job_id}/outreach")
def get_outreach(job_id: int, services: ServicesDep) -> OutreachOut:
    return services.outreach.get(job_id)


@router.post("/jobs/{job_id}/outreach")
async def make_outreach(job_id: int, body: OutreachBody, services: ServicesDep) -> OutreachOut:
    return await services.outreach.draft(job_id, body.recruiter, body.extra)


@router.patch("/jobs/{job_id}/outreach")
def patch_outreach(job_id: int, body: OutreachPatch, services: ServicesDep) -> OutreachOut:
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    return services.outreach.edit(job_id, fields)


@router.get("/outreach-examples")
def outreach_examples(services: ServicesDep) -> list[OutreachExample]:
    return services.outreach.examples()


@router.post("/outreach-examples")
def add_outreach_example(body: OutreachExampleCreate, services: ServicesDep) -> OutreachExample:
    return services.outreach.add_example(body.kind, body.subject, body.body)


@router.delete("/outreach-examples/{example_id}")
def delete_outreach_example(example_id: int, services: ServicesDep) -> Ok:
    return services.outreach.delete_example(example_id)

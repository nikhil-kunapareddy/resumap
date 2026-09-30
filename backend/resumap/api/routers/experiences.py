"""The experience library: things that are not on the resume."""
from __future__ import annotations

from fastapi import APIRouter

from ...domain import Experience
from ...services.results import ExperienceDeleted
from ..deps import ServicesDep
from ..schemas import ExperienceCreate

router = APIRouter(prefix="/api/experiences", tags=["experiences"])


@router.get("")
def experiences(services: ServicesDep) -> list[Experience]:
    return services.experiences.all()


@router.post("")
def add_experience(body: ExperienceCreate, services: ServicesDep) -> Experience:
    return services.experiences.add(body.kind, body.title, body.text)


@router.delete("/{experience_id}")
def delete_experience(experience_id: int, services: ServicesDep) -> ExperienceDeleted:
    """Delete an experience and unpick it everywhere it was mapped or written up."""
    return services.experiences.delete(experience_id)

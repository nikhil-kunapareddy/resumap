"""The corrections chat on the Export step."""
from __future__ import annotations

from fastapi import APIRouter

from ...services.results import ChatState
from ..deps import ServicesDep
from ..schemas import ChatBody

router = APIRouter(prefix="/api/jobs/{job_id}/chat", tags=["corrections"])


@router.get("")
def get_chat(job_id: int, services: ServicesDep) -> ChatState:
    return services.corrections.state(job_id)


@router.post("")
async def chat(job_id: int, body: ChatBody, services: ServicesDep) -> ChatState:
    """Apply one correction request to the PDF: block edits and layout changes."""
    return await services.corrections.apply(job_id, body.message)


@router.post("/{message_id}/undo")
def undo_chat(job_id: int, message_id: int, services: ServicesDep) -> ChatState:
    """Put back what the latest applied correction replaced."""
    return services.corrections.undo(job_id, message_id)

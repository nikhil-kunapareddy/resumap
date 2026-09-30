"""Uploading and reading resumes."""
from __future__ import annotations

from fastapi import APIRouter, File, Form, UploadFile

from ...domain import ResumeSummary
from ...services.results import Ok, ResumeOut
from ..deps import ServicesDep

router = APIRouter(prefix="/api/resumes", tags=["resumes"])


@router.get("")
def resumes(services: ServicesDep) -> list[ResumeSummary]:
    return services.resumes.all()


@router.post("")
def upload_resume(services: ServicesDep, file: UploadFile = File(...),
                  label: str = Form("")) -> ResumeOut:
    return services.resumes.upload(file.filename, file.file, label)


@router.get("/{resume_id}")
def resume_detail(resume_id: int, services: ServicesDep) -> ResumeOut:
    return services.resumes.get(resume_id)


@router.delete("/{resume_id}")
def resume_delete(resume_id: int, services: ServicesDep) -> Ok:
    return services.resumes.delete(resume_id)

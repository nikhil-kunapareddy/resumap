"""The rendered PDF: preview, export, fit and download."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import FileResponse

from ...services.results import ExportOut, FitOut
from ..deps import ServicesDep

router = APIRouter(prefix="/api", tags=["export"])


@router.get("/jobs/{job_id}/preview.pdf")
def preview(job_id: int, services: ServicesDep) -> FileResponse:
    return FileResponse(
        services.exports.preview(job_id),
        media_type="application/pdf",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/jobs/{job_id}/export")
def export(job_id: int, services: ServicesDep) -> ExportOut:
    return services.exports.export(job_id)


@router.get("/jobs/{job_id}/fit")
def fit_info(job_id: int, services: ServicesDep) -> FitOut:
    """How hard the resume had to be squeezed to reach a single page."""
    return services.exports.fit(job_id)


@router.get("/download/{filename}")
def download(filename: str, services: ServicesDep) -> FileResponse:
    path = services.exports.download(filename)
    return FileResponse(path, media_type="application/pdf", filename=path.name)

"""Rendering a job's tailored resume to one PDF page, and checking it."""

from __future__ import annotations

from pathlib import Path

from .. import obs
from ..config import Paths
from ..rendering.pdfgen import export_filename, render_resume
from ..rendering.selfcheck import verify_pdf
from ..storage import Store
from .base import Service
from .documents import overrides, render_doc
from .errors import NotFound
from .results import ExportOut, FitOut, SelfCheck
from .settings import SettingsService


class ExportService(Service):
    def __init__(self, store: Store, paths: Paths, settings: SettingsService) -> None:
        super().__init__(store)
        self.paths = paths
        self.settings = settings

    def build(self, job_id: int, preview: bool) -> tuple[Path, str, dict]:
        """Render the job's PDF: the original document, only accepted and edited
        wording applied. Returns the file, its download name and the fit."""
        job = self._job(job_id)
        resume = self._resume(job.resume_id)

        rewrites = self.store.rewrites.for_job(job_id)
        name = export_filename(self.settings.first_name(), job.company or "company")
        self.paths.ensure()
        out = self.paths.preview(job_id) if preview else self.paths.exports / name
        fit = render_resume(
            render_doc(resume.doc.as_dict(), rewrites),
            str(out),
            overrides(rewrites),
            layout=self.store.layouts.get(job_id),
        )
        return out, name, fit

    def preview(self, job_id: int) -> Path:
        out, _name, _fit = self.build(job_id, preview=True)
        return out

    def export(self, job_id: int) -> ExportOut:
        out, name, fit = self.build(job_id, preview=False)
        rewrites = self.store.rewrites.for_job(job_id)
        job = self.store.jobs.get(job_id)
        resume = self.store.resumes.get(job.resume_id) if job else None
        check = verify_pdf(
            out, resume.doc.as_dict() if resume else {}, resume.stored_path if resume else None
        )
        applied = sum(1 for rw in rewrites.values() if rw.status.included)
        obs.event(
            "export.done",
            job_id=job_id,
            filename=name,
            bytes=out.stat().st_size,
            pages=fit["pages"],
            font_scale=fit["font_scale"],
            cramped=fit["cramped"],
            applied=applied,
            selfcheck_ok=check.get("ok"),
            problems=check.get("problems") or None,
        )
        return ExportOut(
            check=SelfCheck(**{k: v for k, v in check.items() if k != "fonts"}),
            filename=name,
            url=f"/api/download/{name}",
            applied=applied,
            total=len(rewrites),
            pages=fit["pages"],
            font_scale=fit["font_scale"],
            cramped=fit["cramped"],
        )

    def fit(self, job_id: int) -> FitOut:
        """How hard the resume had to be squeezed to reach a single page."""
        _out, _name, fit = self.build(job_id, preview=True)
        return FitOut(**{k: v for k, v in fit.items() if k not in ("path", "blocks")})

    def download(self, filename: str) -> Path:
        """An export by name. Only the name is used, so no path can reach outside."""
        path = self.paths.exports / Path(filename).name
        if not path.exists():
            raise NotFound("Export not found. Generate it again.")
        return path

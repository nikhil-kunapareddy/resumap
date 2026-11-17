"""Rendering a job's tailored resume to one PDF page, and checking it."""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from .. import obs
from ..config import Paths
from ..domain import Job, Resume, Rewrite
from ..rendering.pdfgen import export_filename, render_resume
from ..rendering.selfcheck import verify_pdf
from ..storage import Store
from .base import Service
from .documents import overrides, render_doc
from .errors import NotFound
from .results import ExportOut, FitOut, SelfCheck
from .settings import SettingsService


class Rendered(NamedTuple):
    """One render of a job's tailored resume, and what it was made from."""

    resume: Resume
    rewrites: dict[str, Rewrite]
    doc: dict  # as rendered: the original, with accepted experience bullets spliced in
    fit: dict


class ExportService(Service):
    def __init__(self, store: Store, paths: Paths, settings: SettingsService) -> None:
        super().__init__(store)
        self.paths = paths
        self.settings = settings

    def _render(self, job: Job, out: Path | None) -> Rendered:
        """Render the job's PDF to `out`, or only lay it out with None: the
        original document, with only accepted and edited wording applied."""
        resume = self._resume(job.resume_id)
        rewrites = self.store.rewrites.for_job(job.id)
        doc = render_doc(resume.doc.as_dict(), rewrites)
        if out is not None:
            self.paths.ensure()
        fit = render_resume(
            doc,
            None if out is None else str(out),
            overrides(rewrites),
            layout=self.store.layouts.get(job.id),
        )
        return Rendered(resume, rewrites, doc, fit)

    def measure(self, job: Job) -> Rendered:
        """How the page lays out, without writing a PDF."""
        return self._render(job, None)

    def preview(self, job_id: int) -> Path:
        out = self.paths.preview(job_id)
        self._render(self._job(job_id), out)
        return out

    def export(self, job_id: int) -> ExportOut:
        job = self._job(job_id)
        name = export_filename(self.settings.first_name(), job.company or "company")
        out = self.paths.exports / name
        r = self._render(job, out)
        fit = r.fit
        check = verify_pdf(out, r.resume.doc.as_dict(), r.resume.stored_path, layout=fit["layout"])
        applied = sum(1 for rw in r.rewrites.values() if rw.status.included)
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
            total=len(r.rewrites),
            pages=fit["pages"],
            font_scale=fit["font_scale"],
            cramped=fit["cramped"],
        )

    def fit(self, job_id: int) -> FitOut:
        """How hard the resume had to be squeezed to reach a single page.

        Measured, not written: the preview PDF the browser may be reading at
        the same moment is left alone.
        """
        fit = self.measure(self._job(job_id)).fit
        return FitOut(**{k: v for k, v in fit.items() if k not in ("path", "blocks")})

    def download(self, filename: str) -> Path:
        """An export by name. Only the name is used, so no path can reach outside."""
        path = self.paths.exports / Path(filename).name
        if not path.exists():
            raise NotFound("Export not found. Generate it again.")
        return path

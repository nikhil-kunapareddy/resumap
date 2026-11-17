"""Uploading, parsing and storing resumes, and the experience library beside them."""

from __future__ import annotations

import re
import secrets
import shutil
import time
from pathlib import Path
from typing import BinaryIO

from .. import obs
from ..config import Paths
from ..domain import Block, Experience, ExperienceKind, Resume, ResumeSummary, experience_key
from ..parsing.report import parse_report, write_parse_report
from ..parsing.resume import parse_resume
from ..storage import Store
from .base import Service
from .errors import Invalid, NotFound
from .results import ExperienceDeleted, Ok, ResumeOut

ALLOWED_EXT = {".pdf", ".docx", ".txt", ".md"}


def resume_out(resume: Resume) -> ResumeOut:
    """A stored resume for the browser, without the geometry only the renderer reads."""
    doc = resume.doc
    return ResumeOut(
        id=resume.id,
        label=resume.label,
        filename=resume.filename,
        diagnostics=doc.diagnostics,
        name=doc.name,
        sections=doc.sections,
        blocks=[Block.model_validate(b.model_dump(exclude={"line"})) for b in doc.blocks],
    )


class ResumeService(Service):
    def __init__(self, store: Store, paths: Paths) -> None:
        super().__init__(store)
        self.paths = paths

    def all(self) -> list[ResumeSummary]:
        return self.store.resumes.all()

    def get(self, resume_id: int) -> ResumeOut:
        return resume_out(self._resume(resume_id))

    def upload(self, filename: str | None, content: BinaryIO, label: str) -> ResumeOut:
        ext = Path(filename or "").suffix.lower()
        if ext not in ALLOWED_EXT:
            raise Invalid(f"Unsupported file type '{ext}'. Use PDF, DOCX, TXT or MD.")

        self.paths.ensure()
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", Path(filename or "resume").name)
        # The random part keeps two uploads of one name in the same second apart.
        stored = self.paths.uploads / f"{int(time.time())}_{secrets.token_hex(4)}_{safe}"
        with stored.open("wb") as out:
            shutil.copyfileobj(content, out)

        obs.event("upload.received", filename_ext=ext, bytes=stored.stat().st_size)
        try:
            with obs.timer() as t:
                doc = parse_resume(str(stored))
            obs.event("parse.ok", ms=t.ms, blocks=len(doc["blocks"]))
        except Exception as exc:  # noqa: BLE001
            stored.unlink(missing_ok=True)
            obs.error("parse.failed", exc=exc, ext=ext)
            raise Invalid(f"Could not parse that resume: {type(exc).__name__}: {exc}") from exc

        if not doc["blocks"]:
            stored.unlink(missing_ok=True)
            raise Invalid("No text found in that file. If it is a scanned PDF, it needs OCR first.")

        resume_label = label.strip() or Path(filename or "resume").stem
        resume = self.store.resumes.add(
            label=resume_label,
            filename=filename or safe,
            stored_path=str(stored),
            person=doc.get("name", ""),
            doc=doc,
        )
        written = write_parse_report(self.paths.logs, resume.id, resume_label, doc)
        obs.event(
            "resume.stored",
            resume_id=resume.id,
            points=sum(1 for b in doc["blocks"] if b["mappable"]),
            report=str(written.name) if written else None,
        )
        return resume_out(resume)

    def delete(self, resume_id: int) -> Ok:
        """Delete a resume, its jobs (by cascade) and every file they left behind.

        The rows go first, so a delete that fails leaves a resume that still
        renders rather than one whose source file is gone.
        """
        resume = self._resume(resume_id)
        job_ids = [j.id for j in self.store.jobs.all(resume_id)]
        self.store.resumes.delete(resume_id)
        for path in (
            Path(resume.stored_path),
            self.paths.parse_report(resume_id),
            *(self.paths.preview(j) for j in job_ids),
        ):
            path.unlink(missing_ok=True)
        return Ok()

    def parse_report(self, resume_id: int) -> str:
        """The block-classification table for a stored resume."""
        resume = self._resume(resume_id)
        return parse_report(resume_id, resume.label, resume.doc.as_dict())


class ExperienceService(Service):
    """Things that are not on the resume, offered to every job."""

    def all(self) -> list[Experience]:
        return self.store.experiences.all()

    def add(self, kind: ExperienceKind, title: str, text: str) -> Experience:
        text = text.strip()
        if len(text) < 15:
            raise Invalid(
                "Describe the experience in at least a sentence: "
                "what you did, with what, and what came of it."
            )
        exp = self.store.experiences.add(kind=kind, title=title.strip()[:80], text=text)
        obs.event(
            "experience.added",
            id=exp.id,
            kind=kind,
            has_title=bool(title.strip()),
            **obs.tmeta(text, "exp"),
        )
        return exp

    def delete(self, experience_id: int) -> ExperienceDeleted:
        """Delete an experience and unpick it everywhere it was mapped or written up.

        Its tailored bullets go too: otherwise the next export would still splice
        in a bullet written from something the user has taken back.
        """
        if self.store.experiences.get(experience_id) is None:
            raise NotFound("Experience not found")
        key = experience_key(experience_id)
        with self.store.transaction():
            mappings = self.store.mappings.delete_key(key)
            rewrites = self.store.rewrites.delete_key(key)
            self.store.experiences.delete(experience_id)
        obs.event("experience.deleted", id=experience_id, mappings=mappings, rewrites=rewrites)
        return ExperienceDeleted(mappings=mappings, rewrites=rewrites)

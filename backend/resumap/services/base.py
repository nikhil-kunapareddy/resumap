"""What every service starts from: the Store, and the lookups that 404."""

from __future__ import annotations

from typing import Any

from ..domain import Job, Resume, Rewrite
from ..storage import Store
from .errors import NotFound


class Service:
    def __init__(self, store: Store) -> None:
        self.store = store

    def _job(self, job_id: int) -> Job:
        job = self.store.jobs.get(job_id)
        if job is None:
            raise NotFound("Job not found")
        return job

    def _resume(self, resume_id: int) -> Resume:
        resume = self.store.resumes.get(resume_id)
        if resume is None:
            raise NotFound("Resume not found")
        return resume

    def _rewrite(self, job_id: int, block_id: str) -> Rewrite:
        rw = self.store.rewrites.get(job_id, block_id)
        if rw is None:
            raise NotFound("No rewrite for that block yet.")
        return rw


def stored_meta(rw: Rewrite) -> dict[str, Any]:
    """A rewrite's meta as stored, ready to merge changes into."""
    return rw.meta.model_dump(mode="json", exclude_unset=True)

"""What every service starts from: the Store, the lookups that 404, and for the
services that need a model, the gateway and the check that one can run."""

from __future__ import annotations

from typing import Any

from ..domain import Job, Resume, Rewrite
from ..llm import ModelGateway
from ..storage import Store
from .errors import NotFound, Unavailable


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

    def _job_and_resume(self, job_id: int) -> tuple[Job, Resume]:
        job = self._job(job_id)
        return job, self._resume(job.resume_id)

    def _rewrite(self, job_id: int, block_id: str) -> Rewrite:
        rw = self.store.rewrites.get(job_id, block_id)
        if rw is None:
            raise NotFound("No rewrite for that block yet.")
        return rw


class ModelService(Service):
    """A service whose work goes through the model gateway."""

    def __init__(self, store: Store, gateway: ModelGateway) -> None:
        super().__init__(store)
        self.gateway = gateway

    async def _require_backend(self) -> None:
        """Refuse with 503 when the live backend cannot run."""
        why = await self.gateway.astatus()
        if why:
            raise Unavailable(why)


def stored_meta(rw: Rewrite) -> dict[str, Any]:
    """A rewrite's meta as stored, ready to merge changes into."""
    return rw.meta.model_dump(mode="json", exclude_unset=True)

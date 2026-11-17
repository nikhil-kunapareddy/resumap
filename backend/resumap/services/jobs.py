"""Jobs: a pasted posting, its verbatim points, and which boxes answer which point."""

from __future__ import annotations

from .. import obs
from ..domain import BoxMode, JobSummary, RewriteMode
from ..llm import ModelGateway
from ..storage import Store
from .base import Service
from .errors import Invalid
from .results import BoxModeSaved, JobCreated, JobDetail, MappingSaved, Ok, PointsKept
from .resumes import resume_out


class JobService(Service):
    def __init__(self, store: Store, gateway: ModelGateway) -> None:
        super().__init__(store)
        self.gateway = gateway

    def all(self, resume_id: int | None = None) -> list[JobSummary]:
        return self.store.jobs.all(resume_id)

    async def create(self, resume_id: int, text: str, mode: RewriteMode) -> JobCreated:
        self._resume(resume_id)
        if len(text.strip()) < 40:
            raise Invalid("That job description looks too short to parse.")

        # Never raises for a missing backend: the points come from parsing/jd.py
        # and only their labels need a model.
        parsed = await self.gateway.parse_jd(text)

        if not parsed["points"]:
            raise Invalid("Could not find any requirements in that job description.")

        job = self.store.jobs.add(
            resume_id=resume_id,
            company=parsed["company"],
            role=parsed["role"],
            raw=text,
            points=parsed["points"],
            mode=mode,
        )
        return JobCreated(
            id=job.id,
            company=parsed["company"],
            role=parsed["role"],
            points=parsed["points"],
            mode=mode,
            source=parsed["source"],
            warning=parsed.get("warning"),
        )

    def detail(self, job_id: int) -> JobDetail:
        job = self._job(job_id)
        resume = self.store.resumes.get(job.resume_id)
        return JobDetail(
            id=job.id,
            resume_id=job.resume_id,
            company=job.company,
            role=job.role,
            mode=job.mode,
            raw=job.raw_text,
            points=job.points,
            mappings=self.store.mappings.for_job(job_id),
            box_modes=self.store.mappings.box_modes(job_id),
            rewrites=self.store.rewrites.for_job(job_id),
            outreach=self.store.outreach.get(job_id),
            resume=resume_out(resume) if resume else None,
        )

    def update(
        self,
        job_id: int,
        *,
        company: str | None = None,
        role: str | None = None,
        mode: RewriteMode | None = None,
    ) -> JobSummary:
        """Change a job's company, role or mode; returns the job as it now stands."""
        self._job(job_id)
        self.store.jobs.update(job_id, company=company, role=role, mode=mode)
        return self._job(job_id).summary()

    def delete(self, job_id: int) -> Ok:
        self.store.jobs.delete(job_id)
        return Ok()

    def keep_points(self, job_id: int, keep_ids: list[str]) -> PointsKept:
        """Choose which parsed points to carry into mapping.

        Dropping is reversible: the point keeps its text and its id and is only
        flagged, so the UI can put it back without re-parsing the JD.
        """
        job = self._job(job_id)
        points = job.points
        keep = set(keep_ids)
        unknown = sorted(keep - {p.id for p in points})
        if unknown:
            raise Invalid(f"Unknown JD point(s): {', '.join(unknown)}")
        if not keep:
            raise Invalid("Keep at least one JD point to map against.")

        for p in points:
            p.dropped = p.id not in keep
        # A dropped point cannot stay wired to a resume box, or transform would
        # rewrite bullets against requirements the user has already discarded.
        gone = {p.id for p in points if p.dropped}
        unwired = 0
        with self.store.transaction():
            self.store.jobs.set_points(job_id, points)
            for block_id, ids in self.store.mappings.for_job(job_id).items():
                left = [i for i in ids if i not in gone]
                if len(left) != len(ids):
                    self.store.mappings.map_box(job_id, block_id, left)
                    unwired += len(ids) - len(left)
        obs.event(
            "jd.points_kept", job_id=job_id, kept=len(keep), dropped=len(gone), unwired=unwired
        )
        return PointsKept(points=points, dropped=sorted(gone), unwired=unwired)

    def map_box(self, job_id: int, block_id: str, point_ids: list[str]) -> MappingSaved:
        job = self._job(job_id)
        valid = {p.id for p in job.points if not p.dropped}
        unknown = [p for p in point_ids if p not in valid]
        if unknown:
            obs.warn("map.unknown_points", job_id=job_id, ids=unknown)
            raise Invalid(f"Unknown JD point(s): {', '.join(unknown)}")
        before = self.store.mappings.for_job(job_id).get(block_id, [])
        self.store.mappings.map_box(job_id, block_id, point_ids)
        obs.event(
            "map.set",
            job_id=job_id,
            block=block_id,
            points=point_ids,
            added=len(set(point_ids) - set(before)),
            removed=len(set(before) - set(point_ids)),
        )
        return MappingSaved(block_id=block_id, point_ids=point_ids)

    def set_box_mode(self, job_id: int, block_id: str, mode: BoxMode) -> BoxModeSaved:
        """Set how one resume box is transformed. Experiences are always new bullets,
        so only a parsed block can be replaced."""
        job = self._job(job_id)
        resume = self.store.resumes.get(job.resume_id)
        if not resume or not any(b.id == block_id for b in resume.doc.blocks):
            raise Invalid(f"'{block_id}' is not a box on this resume.")
        self.store.mappings.set_box_mode(job_id, block_id, mode)
        obs.event("map.box_mode", job_id=job_id, block=block_id, mode=mode or "job")
        return BoxModeSaved(block_id=block_id, mode=mode)

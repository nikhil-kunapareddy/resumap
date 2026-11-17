"""Tailoring: turning mapped boxes into rewrites, and reviewing them."""

from __future__ import annotations

from .. import obs
from ..domain import Job, Rewrite, RewriteStatus
from ..llm import ModelGateway
from ..rendering.pdfgen import line_budget, new_bullet_budget
from ..storage import Store
from .base import Service, stored_meta
from .documents import entry_context, placements
from .errors import Invalid, Unavailable
from .results import PreviewKind, PromptPreview, TransformOut, TransformResult


class RewriteService(Service):
    def __init__(self, store: Store, gateway: ModelGateway) -> None:
        super().__init__(store)
        self.gateway = gateway

    async def transform(self, job_id: int, block_ids: list[str] | None, extra: str) -> TransformOut:
        """Rewrite every mapped box, or only `block_ids` for a re-roll.

        A box that fails is reported in `results` and keeps whatever rewrite it
        had; the rest are saved together.
        """
        job = self._job(job_id)
        resume = self._resume(job.resume_id)
        why = await self.gateway.astatus()
        if why:
            raise Unavailable(why)

        doc = resume.doc.as_dict()
        blocks = {b["id"]: b for b in doc["blocks"]}
        exps = {e.key: e for e in self.store.experiences.all()}
        old = self.store.rewrites.for_job(job_id)
        jobs_to_run = self._calls(job, doc, block_ids, extra)
        replaced = {x["block_id"] for x in jobs_to_run if x.get("kind") == "replace"}

        if not jobs_to_run:
            raise Invalid("Nothing to transform. Map at least one resume box to a JD point first.")

        obs.event(
            "transform.start",
            job_id=job_id,
            mode=job.mode,
            bullets=len(jobs_to_run),
            replaced=len(replaced),
            reroll=bool(block_ids),
            has_note=bool(extra.strip()),
        )
        results = await self.gateway.rewrite_many(jobs_to_run)
        with self.store.transaction():
            for res in results:
                bid = res["block_id"]
                if not res.get("ok"):
                    continue
                meta = {
                    "keywords_used": res["keywords_used"],
                    "substitutions": res["substitutions"],
                    "defensible": res["defensible"],
                }
                if bid in exps:
                    # A re-roll re-suggests a spot, but one the user picked stays put.
                    prev = old[bid].meta if bid in old else None
                    picked = bool(prev and prev.placement_user)
                    meta["placement"] = prev.placement if prev and picked else res["placement"]
                    meta["placement_user"] = picked
                    original = exps[bid].text
                else:
                    original = blocks[bid]["text"]
                    if bid in replaced:
                        meta["replaced"] = True
                self.store.rewrites.save(
                    job_id,
                    bid,
                    original=original,
                    rewritten=res["rewritten"],
                    status=RewriteStatus.PENDING,
                    meta=meta,
                )
        return TransformOut(
            results=[TransformResult(**r) for r in results],
            rewrites=self.store.rewrites.for_job(job_id),
        )

    def preview(self, job_id: int, block_id: str) -> PromptPreview:
        """The exact system prompt and message Transform would send for one box."""
        job = self._job(job_id)
        resume = self._resume(job.resume_id)
        calls = self._calls(job, resume.doc.as_dict(), [block_id], "")
        if not calls:
            raise Invalid("That box is not mapped to any JD point yet.")
        call = calls[0]
        kind: PreviewKind = call.get("kind") or "rewrite"
        system, user = self.gateway.request(kind, call["args"])
        return PromptPreview(block_id=block_id, kind=kind, mode=job.mode, system=system, user=user)

    def _calls(self, job: Job, doc: dict, block_ids: list[str] | None, extra: str) -> list[dict]:
        """One call per mapped box: the arguments its rewrite will get.

        Shared by transform and the prompt preview, so the preview is exactly what
        goes out rather than a second guess at it.
        """
        mappings = self.store.mappings.for_job(job.id)
        blocks = {b["id"]: b for b in doc["blocks"]}
        by_point = {p.id: p.model_dump(mode="json") for p in job.points}
        layout = self.store.layouts.get(job.id)
        budget = line_budget(doc, layout)
        exps = {e.key: e.model_dump(mode="json") for e in self.store.experiences.all()}
        box_modes = self.store.mappings.box_modes(job.id)
        out = []
        for bid in block_ids or list(mappings.keys()):
            pts = [by_point[p] for p in mappings.get(bid, []) if p in by_point]
            if not pts:
                continue
            if bid in exps:
                out.append(
                    {
                        "block_id": bid,
                        "kind": "experience",
                        "args": {
                            "experience": exps[bid],
                            "jd_points": pts,
                            "role": job.role,
                            "company": job.company,
                            "placements": placements(doc),
                            "examples": [b["text"] for b in doc["blocks"] if b["mappable"]][:3],
                            "mode": job.mode,
                            "extra": extra,
                            "max_chars": new_bullet_budget(doc, layout),
                        },
                    }
                )
            elif bid in blocks and box_modes.get(bid) == "replace":
                blk = blocks[bid]
                entry, siblings = entry_context(doc, bid)
                out.append(
                    {
                        "block_id": bid,
                        "kind": "replace",
                        "args": {
                            "bullet": blk["text"],
                            "section": blk.get("section", ""),
                            "entry": entry,
                            "siblings": siblings,
                            "jd_points": pts,
                            "role": job.role,
                            "company": job.company,
                            "extra": extra,
                            "max_chars": budget.get(bid, 0),
                        },
                    }
                )
            elif bid in blocks:
                blk = blocks[bid]
                out.append(
                    {
                        "block_id": bid,
                        "args": {
                            "bullet": blk["text"],
                            "section": blk.get("section", ""),
                            "jd_points": pts,
                            "role": job.role,
                            "company": job.company,
                            "mode": job.mode,
                            "extra": extra,
                            "max_chars": budget.get(bid, 0),
                        },
                    }
                )
        return out

    def edit(
        self,
        job_id: int,
        block_id: str,
        *,
        text: str | None = None,
        status: RewriteStatus | None = None,
        placement: str | None = None,
    ) -> Rewrite:
        """Review one rewrite: re-place it, hand-edit it, or change its status."""
        existing = self._rewrite(job_id, block_id)
        meta = stored_meta(existing)
        with self.store.transaction():
            if placement is not None:
                job = self.store.jobs.get(job_id)
                resume = self.store.resumes.get(job.resume_id) if job else None
                place = placement.strip()
                anchors = {p["id"] for p in placements(resume.doc.as_dict())} if resume else set()
                if not (
                    place in anchors
                    or (place.startswith("new:") and 0 < len(place[4:].strip()) <= 40)
                ):
                    raise Invalid(f"Can't place a bullet at '{place}'.")
                obs.event(
                    "review.placement",
                    job_id=job_id,
                    block=block_id,
                    placement="new" if place.startswith("new:") else place,
                )
                meta = {**meta, "placement": place, "placement_user": True}
                self.store.rewrites.save(
                    job_id,
                    block_id,
                    original=existing.original,
                    rewritten=existing.rewritten,
                    status=existing.status,
                    meta=meta,
                )
            if text is not None:
                obs.event(
                    "review.edit",
                    job_id=job_id,
                    block=block_id,
                    from_status=existing.status,
                    to_status=status or "edited",
                    **obs.tmeta(text, "edited"),
                )
                self.store.rewrites.save(
                    job_id,
                    block_id,
                    original=existing.original,
                    rewritten=text.strip(),
                    status=status or RewriteStatus.EDITED,
                    meta=meta,
                )
            elif status is not None:
                obs.event(
                    "review.status",
                    job_id=job_id,
                    block=block_id,
                    from_status=existing.status,
                    to_status=status,
                )
                self.store.rewrites.set_status(job_id, block_id, status)
        return self._rewrite(job_id, block_id)

    def accept_all(self, job_id: int) -> dict[str, Rewrite]:
        rws = self.store.rewrites.for_job(job_id)
        pending = [bid for bid, rw in rws.items() if rw.status == RewriteStatus.PENDING]
        with self.store.transaction():
            for bid in pending:
                self.store.rewrites.set_status(job_id, bid, RewriteStatus.ACCEPTED)
        obs.event("review.accept_all", job_id=job_id, accepted=len(pending))
        return self.store.rewrites.for_job(job_id)

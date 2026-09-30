"""Recruiter outreach: the LinkedIn note and follow-up email, and liked examples."""

from __future__ import annotations

from .. import obs
from ..domain import OutreachExample, OutreachKind, Rewrite
from ..llm import BackendUnavailable, ModelGateway
from ..llm.outreach import LINKEDIN_LIMIT
from ..storage import Store
from .base import Service
from .documents import render_doc
from .errors import Invalid, NotFound, Unavailable, UpstreamFailed
from .results import Ok, OutreachOut

HIGHLIGHT_LIMIT = 8

# Liked messages, shared by every job: the newest few of each kind go into each
# draft as style references (llm.outreach.OUTREACH_EXAMPLE_LIMIT).
EXAMPLE_MAX_CHARS = 4000


def highlights(doc: dict, rewrites: dict[str, Rewrite], mappings: dict) -> list[str]:
    """The lines the outreach may quote: what the exported resume will say.

    Preference order per point: the tailored wording that is actually going into
    the PDF, then anything mapped to the JD but not accepted, then -- only if
    neither exists -- the resume's own strongest points, so a draft is still
    possible before the review step is finished.
    """
    picked: list[str] = []
    spare: list[str] = []
    for b in doc.get("blocks", []):
        if not b.get("mappable"):
            continue
        rw = rewrites.get(b["id"])
        if rw is not None and rw.in_export:
            picked.append(rw.rewritten.strip())
        elif mappings.get(b["id"]):
            picked.append(b["text"])
        else:
            spare.append(b["text"])
    return (picked or spare)[:HIGHLIGHT_LIMIT]


class OutreachService(Service):
    def __init__(self, store: Store, gateway: ModelGateway) -> None:
        super().__init__(store)
        self.gateway = gateway

    def get(self, job_id: int) -> OutreachOut:
        self._job(job_id)
        return OutreachOut(outreach=self.store.outreach.get(job_id))

    async def draft(self, job_id: int, recruiter: str, extra: str) -> OutreachOut:
        job = self._job(job_id)
        resume = self._resume(job.resume_id)
        why = self.gateway.status()
        if why:
            raise Unavailable(why)

        rewrites = self.store.rewrites.for_job(job_id)
        quoted = highlights(
            render_doc(resume.doc.as_dict(), rewrites),
            rewrites,
            self.store.mappings.for_job(job_id),
        )
        if not quoted:
            raise Invalid("No resume points to write from. Map and transform some boxes first.")

        try:
            draft = await self.gateway.draft_outreach(
                candidate=resume.doc.name or resume.person_name,
                recruiter=recruiter.strip(),
                role=job.role,
                company=job.company,
                highlights=quoted,
                jd_points=[p.model_dump(mode="json") for p in job.points],
                extra=extra,
                examples=[e.model_dump(mode="json") for e in self.store.examples.all()],
            )
        except BackendUnavailable as exc:
            raise Unavailable(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - the draft is optional, say why it failed
            obs.record_failure("outreach", exc, 0.0, job_id=job_id)
            raise UpstreamFailed(
                f"Could not draft the outreach: {type(exc).__name__}: {exc}"
            ) from exc

        meta = {
            "highlights_used": draft["highlights_used"],
            "linkedin_limit": LINKEDIN_LIMIT,
        }
        self.store.outreach.save(
            job_id,
            recruiter=recruiter.strip(),
            linkedin=draft["linkedin"],
            linkedin_subject=draft["linkedin_subject"],
            email_subject=draft["email_subject"],
            email_body=draft["email_body"],
            meta=meta,
        )
        obs.event(
            "outreach.done",
            job_id=job_id,
            highlights=len(quoted),
            linkedin_chars=len(draft["linkedin"]),
            over_limit=len(draft["linkedin"]) > LINKEDIN_LIMIT,
        )
        return OutreachOut(outreach=self.store.outreach.get(job_id))

    def edit(self, job_id: int, fields: dict[str, str]) -> OutreachOut:
        """Save hand edits to a draft; `fields` holds only what changed."""
        existing = self.store.outreach.get(job_id)
        if existing is None:
            raise NotFound("Nothing drafted for this job yet.")
        merged = existing.model_copy(update=fields)
        self.store.outreach.save(
            job_id,
            recruiter=merged.recruiter,
            linkedin=merged.linkedin.strip(),
            linkedin_subject=merged.linkedin_subject.strip(),
            email_subject=merged.email_subject.strip(),
            email_body=merged.email_body.strip(),
            meta=existing.meta,
        )
        obs.event("outreach.edited", job_id=job_id, fields=sorted(fields))
        return OutreachOut(outreach=self.store.outreach.get(job_id))

    # -- liked examples -----------------------------------------------------

    def examples(self) -> list[OutreachExample]:
        return self.store.examples.all()

    def add_example(self, kind: OutreachKind, subject: str, body: str) -> OutreachExample:
        text = body.strip()
        if len(text) < 20:
            raise Invalid("Paste the whole message you liked, not just a line of it.")
        if len(text) > EXAMPLE_MAX_CHARS:
            raise Invalid(
                f"That example is {len(text)} characters. Keep it under "
                f"{EXAMPLE_MAX_CHARS}: one message, not a thread."
            )
        example = self.store.examples.add(kind=kind, subject=subject.strip()[:200], body=text)
        obs.event(
            "outreach_example.added",
            id=example.id,
            kind=kind,
            has_subject=bool(subject.strip()),
            **obs.tmeta(text, "ex"),
        )
        return example

    def delete_example(self, example_id: int) -> Ok:
        if not self.store.examples.delete(example_id):
            raise NotFound("Example not found")
        obs.event("outreach_example.deleted", id=example_id)
        return Ok()

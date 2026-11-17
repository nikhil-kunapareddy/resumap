"""Every model call Resumap makes, through whichever backend is live.

The gateway owns what the rest of the app should not have to know: which
backend and model are live (its ``BackendRegistry``), which instructions are
in force (its ``PromptStore``), and the bookkeeping every call gets -- timing,
the prompt archive, usage and cost, and checking the reply against the rules
the app depends on, whatever the prompt asked for. Services call its methods;
nothing else reaches a backend.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .. import obs
from ..parsing.jd import guess_company, guess_role, parse_jd_verbatim
from . import bullets, corrections, outreach, tagging
from .backends import Backend, BackendRegistry, Completion
from .prompts import PromptStore, with_note
from .schemas import JDTags, NewBullet, Outreach, PdfRevision, Ping, Rewrite


class BackendUnavailable(RuntimeError):
    """The selected backend cannot run: no API key, or its CLI is missing or signed out."""


# The request builders behind each kind of transform job, for the preview.
REQUESTS: dict[str, Callable[..., tuple[str, str]]] = {
    "rewrite": bullets.rewrite_request,
    "replace": bullets.replace_request,
    "experience": bullets.experience_request,
}

# What a model sometimes leads a bullet with; the renderer draws its own.
_BULLET_GLYPHS = "•●-–— "


def clean_bullet(text: str) -> str:
    """Model-written bullet text without a leading bullet glyph."""
    return text.strip().lstrip(_BULLET_GLYPHS).strip()


@dataclass
class _Reply:
    """One answered call, and what its bookkeeping needs."""

    op: str
    out: Completion
    ms: float
    backend: str
    agent: str
    model: str
    archived: str | None

    def record(self, **fields: Any) -> None:
        """Log the call's tokens, cost and latency, with `fields` describing the answer."""
        obs.record_usage(
            self.op,
            self.model,
            self.out.usage,
            self.ms,
            self.out.request_id,
            backend=self.backend,
            prompt_file=self.archived,
            **fields,
        )

    def parsed(self, what: str) -> Any:
        """The validated answer. A reply that did not validate still cost its
        tokens, so it is recorded as a call and as a failure before raising."""
        if self.out.parsed is not None:
            return self.out.parsed
        self.record(parsed=False)
        err = RuntimeError(f"{self.agent} returned no parsable {what}.")
        obs.record_failure(
            self.op,
            err,
            self.ms,
            self.out.request_id,
            backend=self.backend,
            prompt_file=self.archived,
        )
        raise err


class ModelGateway:
    def __init__(self, registry: BackendRegistry, prompts: PromptStore, log_dir: Path) -> None:
        self.registry = registry
        self.prompts = prompts
        self.log_dir = log_dir

    def status(self, name: str | None = None) -> str | None:
        """Why the live backend (or `name`) cannot run, or None if it is ready.

        A CLI backend may run a subprocess to answer, so async code should
        await `astatus()` instead.
        """
        try:
            return self.registry.get(name).unavailable()
        except Exception as exc:  # noqa: BLE001 - an unknown RESUMAP_BACKEND value
            return str(exc)

    async def astatus(self, name: str | None = None) -> str | None:
        """`status()`, run off the event loop."""
        return await asyncio.to_thread(self.status, name)

    def agent(self) -> str:
        """What to call the live backend in a message to the user."""
        try:
            return self.registry.get().label
        except Exception:  # noqa: BLE001
            return "The model"

    def request(self, kind: str, args: dict) -> tuple[str, str]:
        """The exact (system, user) pair a transform job of `kind` sends."""
        return REQUESTS[kind](self.prompts, **args)

    async def _call(
        self,
        op: str,
        system: str,
        user: str,
        schema: type[BaseModel],
        *,
        max_tokens: int = 4000,
        archive: dict[str, Any] | None = None,
    ) -> _Reply:
        """One request through the live backend, with the bookkeeping every call gets.

        The backend and model are read once, before the call, so switching the
        backend mid-batch cannot mislabel this call's archive or usage. A call
        that raises is recorded as failed, with how long it ran, and re-raised.
        """
        name = self.registry.current_name()
        model = self.registry.current_model()
        label = self.registry.model_label()
        why = await self.astatus(name)
        if why:
            raise BackendUnavailable(why)
        backend: Backend = self.registry.get(name)
        t = obs.timer()
        try:
            with t:
                out = await backend.complete(
                    model=model,
                    system=system,
                    user=user,
                    schema=schema,
                    max_tokens=max_tokens,
                    effort="medium",
                )
        except Exception as exc:
            obs.record_failure(op, exc, t.ms, backend=name)
            raise
        used = out.meta.get("model") or label
        archived = obs.archive_prompt(
            self.log_dir,
            op,
            system,
            user,
            out.text,
            {
                "model": used,
                "backend": name,
                "request_id": out.request_id,
                **(archive or {}),
                **out.meta,
            },
        )
        return _Reply(op, out, t.ms, name, backend.label, used, archived)

    # ----------------------------------------------------------------------
    # Connection test
    #
    # A CLI can report itself signed in and still fail every call (an expired
    # token that no longer refreshes), so the settings panel's Test button makes
    # one real, tiny structured call rather than trusting a status command.
    # ----------------------------------------------------------------------

    async def ping(self, name: str, model: str | None = None) -> dict:
        """One minimal structured call through backend `name`: what came back, or why not."""
        b = self.registry.get(name)
        model = model or self.registry.current_model(name)
        why = await asyncio.to_thread(b.unavailable)
        if why:
            return {"ok": False, "detail": why}
        try:
            with obs.timer() as t:
                res = await b.complete(
                    model=model,
                    system="You are a connectivity check. Reply with ok=true.",
                    user="Reply with ok=true and your model name.",
                    schema=Ping,
                    max_tokens=200,
                    effort="low",
                )
        except Exception as exc:  # noqa: BLE001 - the error is the answer here
            obs.warn("backend.test_failed", backend=name, error=type(exc).__name__)
            return {"ok": False, "detail": str(exc)[:400]}
        used = res.meta.get("model") or model or f"{name} default"
        obs.record_usage("backend_test", used, res.usage, t.ms, res.request_id, backend=name)
        if res.parsed is None:
            return {
                "ok": False,
                "detail": f"{b.label} answered, but not in the requested JSON shape.",
            }
        reported = res.parsed.model.strip()
        return {
            "ok": True,
            "ms": round(t.ms),
            "detail": f"{b.label} answered in {t.ms / 1000:.1f}s"
            + (f" ({reported})" if reported else "")
            + ".",
        }

    # ----------------------------------------------------------------------
    # JD labelling
    #
    # The model never writes a JD point: it only tags the points parsing/jd.py
    # split out, and a failure here costs the tags, never the points.
    # ----------------------------------------------------------------------

    async def parse_jd(self, text: str) -> dict:
        """Split a JD verbatim, then have the model label the points it produced."""
        out = parse_jd_verbatim(text)
        out["source"] = "verbatim"
        points = out["points"]
        if not points:
            return out

        user = tagging.tag_prompt(points, text)
        obs.event(
            "jd.parse_start",
            **obs.tmeta(text, "jd"),
            points=len(points),
            lines=text.count("\n") + 1,
            model=self.registry.model_label(),
            backend=self.registry.current_name(),
        )
        try:
            reply = await self._call("jd_parse", tagging.JD_SYSTEM, user, JDTags, max_tokens=8000)
            parsed = reply.out.parsed
            reply.record(points=len(parsed.tags) if parsed else 0)
            if parsed:
                tagging.apply_tags(points, parsed.tags)
                out["company"] = parsed.company.strip() or guess_company(text)
                out["role"] = parsed.role.strip() or guess_role(text)
                out["source"] = f"verbatim, labelled by {reply.agent}"
                obs.event(
                    "jd.parsed",
                    source="claude",
                    points=len(points),
                    has_company=bool(out["company"]),
                    has_role=bool(out["role"]),
                )
                return out
            obs.warn("jd.untagged", why="no_parse")
            out["warning"] = (
                f"Kept your job description word for word. {reply.agent} "
                "could not tag the points, so keyword hints are missing."
            )
        except BackendUnavailable as exc:
            # The points are already final, so this costs labels, not text.
            obs.warn("jd.untagged", why="backend_unavailable")
            out["warning"] = f"{exc} Your points are unchanged; they just have no keyword tags."
        except Exception as exc:  # noqa: BLE001 - labelling is optional, surface why
            obs.warn("jd.untagged", why=type(exc).__name__)
            out["warning"] = (
                f"{self.agent()} could not tag the points "
                f"({type(exc).__name__}: {exc}). Your job description is unchanged."
            )
        return out

    # ----------------------------------------------------------------------
    # Bullets: realigned, replaced and new
    # ----------------------------------------------------------------------

    async def rewrite_bullet(
        self,
        bullet: str,
        section: str,
        jd_points: list[dict],
        role: str,
        company: str,
        mode: str = "aggressive",
        extra: str = "",
        max_chars: int = 0,
    ) -> dict:
        system, user = bullets.rewrite_request(
            self.prompts, bullet, section, jd_points, role, company, mode, extra, max_chars
        )
        return await self._run_rewrite("rewrite", system, user, bullet, jd_points, mode, max_chars)

    async def _run_rewrite(
        self,
        op: str,
        system: str,
        user: str,
        bullet: str,
        jd_points: list[dict],
        mode: str,
        max_chars: int,
    ) -> dict:
        """Send one bullet's (system, user) pair and check the Rewrite that comes back.

        Shared by realigned and replaced bullets: both fill the slot `bullet` held,
        so both answer to its length and its line budget.
        """
        reply = await self._call(
            op,
            system,
            user,
            Rewrite,
            archive={"mode": mode, "jd_points": [p.get("id") for p in jd_points]},
        )
        r = reply.parsed("rewrite")
        request_id = reply.out.request_id

        introduced = [x for x in r.substitutions if not x.grounded]
        delta = (len(r.rewritten) - len(bullet)) / max(len(bullet), 1) * 100
        reply.record(
            mode=mode,
            points=len(jd_points),
            **obs.tmeta(bullet, "orig"),
            **obs.tmeta(r.rewritten, "new"),
            length_delta_pct=round(delta, 1),
            keywords=len(r.keywords_used),
            substitutions=len(r.substitutions),
            introduced=len(introduced),
            defensible=r.defensible,
        )
        # The prompt caps length growth at +/-15% so the mirrored layout does not
        # reflow; a breach means the instruction was ignored and the page may grow.
        if abs(delta) > 15:
            obs.warn(
                "rewrite.length_breach",
                op=op,
                length_delta_pct=round(delta, 1),
                request_id=request_id,
                prompt_file=reply.archived,
            )
        if max_chars and len(r.rewritten) > max_chars:
            obs.warn(
                "rewrite.line_budget_breach",
                op=op,
                chars=len(r.rewritten),
                budget=max_chars,
                request_id=request_id,
                prompt_file=reply.archived,
            )
        if not r.defensible:
            obs.warn(
                "rewrite.not_defensible",
                op=op,
                introduced=len(introduced),
                prompt_file=reply.archived,
            )
        return {
            "rewritten": clean_bullet(r.rewritten),
            "keywords_used": r.keywords_used,
            "substitutions": [s.model_dump() for s in r.substitutions],
            "defensible": r.defensible,
        }

    async def replace_bullet(
        self,
        bullet: str,
        section: str,
        entry: str,
        siblings: list[str],
        jd_points: list[dict],
        role: str,
        company: str,
        extra: str = "",
        max_chars: int = 0,
    ) -> dict:
        """A new bullet for the slot `bullet` holds, written for `jd_points`."""
        system, user = bullets.replace_request(
            self.prompts,
            bullet,
            section,
            entry,
            siblings,
            jd_points,
            role,
            company,
            extra,
            max_chars,
        )
        return await self._run_rewrite(
            "replace", system, user, bullet, jd_points, "replace", max_chars
        )

    async def rewrite_many(self, jobs: list[dict], concurrency: int | None = None) -> list[dict]:
        """Rewrite a batch of bullets concurrently, isolating per-bullet failures.

        The default comes from the backend: HTTP requests parallelise more happily
        than one `claude` subprocess per bullet.
        """
        if concurrency is None:
            try:
                concurrency = self.registry.get().concurrency
            except Exception:  # noqa: BLE001 - the per-bullet calls report why
                concurrency = 1
        sem = asyncio.Semaphore(concurrency)
        obs.event(
            "rewrite.batch_start",
            bullets=len(jobs),
            concurrency=concurrency,
            backend=self.registry.current_name(),
            mode=(jobs[0]["args"].get("mode") if jobs else None),
        )

        calls: dict[str, Callable[..., Awaitable[dict]]] = {
            "rewrite": self.rewrite_bullet,
            "experience": self.write_experience_bullet,
            "replace": self.replace_bullet,
        }

        async def one(job: dict) -> dict:
            kind = job.get("kind") or "rewrite"
            async with sem:
                try:
                    res = await calls.get(kind, self.rewrite_bullet)(**job["args"])
                    return {"block_id": job["block_id"], "ok": True, **res}
                except Exception as exc:  # noqa: BLE001 - one bullet's failure is its own
                    # A failed model call is already recorded by _call; this
                    # line ties it, or a refusal before any call, to its bullet.
                    obs.warn(
                        "rewrite.failed", kind=kind, block=job["block_id"], error=type(exc).__name__
                    )
                    return {
                        "block_id": job["block_id"],
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }

        with obs.timer() as t:
            results = await asyncio.gather(*(one(j) for j in jobs))
        ok = sum(1 for r in results if r.get("ok"))
        obs.event(
            "rewrite.batch_done",
            bullets=len(results),
            ok=ok,
            failed=len(results) - ok,
            ms=t.ms,
            **obs.totals(),
        )
        return results

    async def write_experience_bullet(
        self,
        experience: dict,
        jd_points: list[dict],
        role: str,
        company: str,
        placements: list[dict],
        examples: list[str],
        mode: str = "aggressive",
        extra: str = "",
        max_chars: int = 0,
    ) -> dict:
        """One new bullet from a stored experience, and where it should go.

        `placement` comes back as a block id from `placements` or "new:<heading>";
        anything else the model names is replaced by a new section, never guessed
        onto an employer.
        """
        system, user = bullets.experience_request(
            self.prompts,
            experience,
            jd_points,
            role,
            company,
            placements,
            examples,
            mode,
            extra,
            max_chars,
        )
        reply = await self._call(
            "experience_bullet",
            system,
            user,
            NewBullet,
            archive={"mode": mode, "jd_points": [p.get("id") for p in jd_points]},
        )
        r = reply.parsed("bullet")

        ids = {p["id"] for p in placements}
        chosen = r.placement.strip()
        if chosen not in ids:
            heading = r.new_section.strip() or bullets.DEFAULT_SECTION.get(
                experience.get("kind", ""), "ADDITIONAL EXPERIENCE"
            )
            chosen = "new:" + heading[:40]
        bullet = clean_bullet(r.bullet)
        introduced = [x for x in r.substitutions if not x.grounded]
        reply.record(
            mode=mode,
            points=len(jd_points),
            **obs.tmeta(experience["text"], "exp"),
            **obs.tmeta(bullet, "new"),
            keywords=len(r.keywords_used),
            introduced=len(introduced),
            defensible=r.defensible,
            placement="new" if chosen.startswith("new:") else chosen,
        )
        if max_chars and len(bullet) > max_chars:
            obs.warn(
                "rewrite.line_budget_breach",
                chars=len(bullet),
                budget=max_chars,
                request_id=reply.out.request_id,
                prompt_file=reply.archived,
            )
        return {
            "rewritten": bullet,
            "keywords_used": r.keywords_used,
            "substitutions": [x.model_dump() for x in r.substitutions],
            "defensible": r.defensible,
            "placement": chosen,
        }

    # ----------------------------------------------------------------------
    # Recruiter outreach
    # ----------------------------------------------------------------------

    async def draft_outreach(
        self,
        candidate: str,
        recruiter: str,
        role: str,
        company: str,
        highlights: list[str],
        jd_points: list[dict],
        extra: str = "",
        examples: Sequence[dict] = (),
    ) -> dict:
        """One LinkedIn note and one follow-up email for a tailored application.

        `examples` are liked messages (newest first) that set the voice; the prompt
        forbids taking facts from them, since they were written for other roles.
        """
        system = with_note(outreach.OUTREACH_SYSTEM, extra)
        user = outreach.outreach_prompt(
            candidate, recruiter, role, company, highlights, jd_points, examples
        )
        n_ex: dict[str, int] = {
            k: min(outreach.OUTREACH_EXAMPLE_LIMIT, sum(e["kind"] == k for e in examples))
            for k in outreach.OUTREACH_EXAMPLE_KINDS
        }
        obs.event(
            "outreach.start",
            has_recruiter=bool(recruiter.strip()),
            highlights=len(highlights),
            points=len(jd_points),
            linkedin_examples=n_ex["linkedin"],
            email_examples=n_ex["email"],
            has_note=bool(extra.strip()),
            model=self.registry.model_label(),
            backend=self.registry.current_name(),
        )
        reply = await self._call("outreach", system, user, Outreach)
        o = reply.parsed("outreach draft")

        linkedin = o.linkedin.strip()
        li_subject = o.linkedin_subject.strip()
        subject = o.email_subject.strip()
        body = o.email_body.strip()
        reply.record(
            linkedin_chars=len(linkedin),
            **obs.tmeta(linkedin, "li"),
            **obs.tmeta(body, "email"),
            subject_chars=len(subject),
            li_subject_chars=len(li_subject),
        )
        # Over the limit LinkedIn would cut the note off mid-word. Truncating here
        # would do the same, so surface it instead and let the user trim it.
        if len(linkedin) > outreach.LINKEDIN_LIMIT:
            obs.warn(
                "outreach.linkedin_too_long",
                chars=len(linkedin),
                limit=outreach.LINKEDIN_LIMIT,
                prompt_file=reply.archived,
            )
        return {
            "linkedin": linkedin,
            "linkedin_subject": li_subject,
            "email_subject": subject,
            "email_body": body,
            "highlights_used": o.highlights_used,
        }

    # ----------------------------------------------------------------------
    # PDF corrections chat
    # ----------------------------------------------------------------------

    async def revise_pdf(
        self,
        blocks: list[dict],
        layout: dict,
        fit: dict,
        geometry: dict,
        role: str,
        company: str,
        jd_points: list[dict],
        history: list[dict],
        message: str,
    ) -> dict:
        """Turn one chat request into block edits and layout changes.

        Edits naming an id that is not in `blocks` are dropped here rather than
        trusted, and layout values come back in the renderer's own units.
        """
        user = corrections.chat_prompt(
            blocks, layout, fit, geometry, role, company, jd_points, history, message
        )
        obs.event(
            "pdfchat.start",
            blocks=len(blocks),
            turns=len(history),
            **obs.tmeta(message, "msg"),
            model=self.registry.model_label(),
            backend=self.registry.current_name(),
        )
        reply = await self._call(
            "pdf_chat", corrections.PDF_CHAT_SYSTEM, user, PdfRevision, max_tokens=8000
        )
        rev = reply.parsed("answer")

        known = {b["id"]: b for b in blocks}
        edits, unknown = [], []
        for e in rev.edits:
            bid = e.block_id.strip()
            text = clean_bullet(e.text)
            if bid not in known:
                unknown.append(bid)
                continue
            if not text or text == known[bid]["text"]:
                continue
            edits.append(
                {
                    "block_id": bid,
                    "text": text,
                    "substitutions": [x.model_dump() for x in e.substitutions],
                    "defensible": e.defensible,
                }
            )
        changes: dict = {}
        for c in rev.layout:
            key, div = corrections.LAYOUT_SETTINGS[c.setting]
            changes[key] = c.value / div
        if rev.justify != "unchanged":
            changes["justify"] = rev.justify

        reply.record(
            edits=len(edits),
            layout=sorted(changes) or None,
            introduced=sum(1 for e in edits for x in e["substitutions"] if not x["grounded"]),
        )
        if unknown:
            obs.warn("pdfchat.unknown_blocks", ids=unknown, prompt_file=reply.archived)
        return {"reply": rev.reply.strip(), "edits": edits, "layout": changes}

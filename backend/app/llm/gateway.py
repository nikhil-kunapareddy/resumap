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
from pathlib import Path

from .. import obs
from ..parsing.jd import guess_company, guess_role, parse_jd_verbatim
from . import bullets, corrections, outreach, tagging
from .backends import Backend, BackendRegistry, Completion
from .prompts import PromptStore
from .schemas import JDTags, NewBullet, Outreach, PdfRevision, Ping, Rewrite


class BackendUnavailable(RuntimeError):
    """The selected backend cannot run: no API key, or its CLI is missing or signed out."""


# The request builders behind each kind of transform job, for the preview.
REQUESTS = {"rewrite": bullets.rewrite_request, "replace": bullets.replace_request,
            "experience": bullets.experience_request}


class ModelGateway:
    def __init__(self, registry: BackendRegistry, prompts: PromptStore, log_dir: Path) -> None:
        self.registry = registry
        self.prompts = prompts
        self.log_dir = log_dir

    def status(self) -> str | None:
        """Why the live backend cannot run, or None if it is ready."""
        try:
            return self.registry.get().unavailable()
        except Exception as exc:  # noqa: BLE001 - an unknown RESUMAP_BACKEND value
            return str(exc)

    def agent(self) -> str:
        """What to call the live backend in a message to the user."""
        try:
            return self.registry.get().label
        except Exception:  # noqa: BLE001
            return "The model"

    def _used(self, res: Completion) -> str:
        """The model that answered, as the backend reports it, for logs and costs."""
        return (res.meta or {}).get("model") or self.registry.model_label()

    def _backend(self) -> Backend:
        why = self.status()
        if why:
            raise BackendUnavailable(why)
        return self.registry.get()

    def request(self, kind: str, args: dict) -> tuple[str, str]:
        """The exact (system, user) pair a transform job of `kind` sends."""
        return REQUESTS[kind](self.prompts, **args)

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
        why = b.unavailable()
        if why:
            return {"ok": False, "detail": why}
        try:
            with obs.timer() as t:
                res = await b.complete(model=model,
                                       system="You are a connectivity check. Reply with ok=true.",
                                       user="Reply with ok=true and your model name.",
                                       schema=Ping, max_tokens=200, effort="low")
        except Exception as exc:  # noqa: BLE001 - the error is the answer here
            obs.warn("backend.test_failed", backend=name, error=type(exc).__name__)
            return {"ok": False, "detail": str(exc)[:400]}
        used = (res.meta or {}).get("model") or model or f"{name} default"
        obs.record_usage("backend_test", used, res.usage, t.ms, res.request_id, backend=name)
        if res.parsed is None:
            return {"ok": False,
                    "detail": f"{b.label} answered, but not in the requested JSON shape."}
        reported = res.parsed.model.strip()
        return {"ok": True, "ms": round(t.ms),
                "detail": f"{b.label} answered in {t.ms / 1000:.1f}s"
                          + (f" ({reported})" if reported else "") + "."}

    # ----------------------------------------------------------------------
    # JD labelling
    #
    # The model never writes a JD point: it only tags the points parsing/jd.py
    # split out, and a failure here costs the tags, never the points.
    # ----------------------------------------------------------------------

    async def parse_jd(self, text: str) -> dict:
        """Split a JD verbatim, then have Claude label the points it produced."""
        out = parse_jd_verbatim(text)
        out["source"] = "verbatim"
        points = out["points"]
        if not points:
            return out

        user = tagging.tag_prompt(points, text)
        obs.event("jd.parse_start", **obs.tmeta(text, "jd"), points=len(points),
                  lines=text.count("\n") + 1, model=self.registry.model_label(),
                  backend=self.registry.current_name())
        try:
            backend = self._backend()
            with obs.timer() as t:
                res = await backend.complete(
                    model=self.registry.current_model(),
                    system=tagging.JD_SYSTEM,
                    user=user,
                    schema=JDTags,
                    max_tokens=8000,
                    effort="medium",
                )
            parsed = res.parsed
            archived = obs.archive_prompt(
                self.log_dir, "jd_parse", tagging.JD_SYSTEM, user, res.text,
                {"model": self._used(res), "backend": self.registry.current_name(),
                 "request_id": res.request_id, **res.meta})
            obs.record_usage("jd_parse", self._used(res), res.usage, t.ms, res.request_id,
                             backend=self.registry.current_name(),
                             points=len(parsed.tags) if parsed else 0,
                             prompt_file=archived)
            if parsed:
                tagging.apply_tags(points, parsed.tags)
                out["company"] = parsed.company.strip() or guess_company(text)
                out["role"] = parsed.role.strip() or guess_role(text)
                out["source"] = f"verbatim, labelled by {self.agent()}"
                obs.event("jd.parsed", source="claude", points=len(points),
                          has_company=bool(out["company"]), has_role=bool(out["role"]))
                return out
            obs.warn("jd.untagged", why="no_parse")
            out["warning"] = (f"Kept your job description word for word. {self.agent()} "
                              "could not tag the points, so keyword hints are missing.")
        except BackendUnavailable as exc:
            # The points are already final, so this costs labels, not text.
            obs.warn("jd.untagged", why="backend_unavailable")
            out["warning"] = f"{exc} Your points are unchanged; they just have no keyword tags."
        except Exception as exc:  # noqa: BLE001 - labelling is optional, surface why
            obs.record_failure("jd_parse", exc, 0.0)
            obs.warn("jd.untagged", why=type(exc).__name__)
            out["warning"] = (f"{self.agent()} could not tag the points "
                              f"({type(exc).__name__}: {exc}). Your job description is unchanged.")
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
        system, user = bullets.rewrite_request(self.prompts, bullet, section, jd_points, role,
                                               company, mode, extra, max_chars)
        return await self._run_rewrite("rewrite", system, user, bullet, jd_points, mode, max_chars)

    async def _run_rewrite(self, op: str, system: str, user: str, bullet: str,
                           jd_points: list[dict], mode: str, max_chars: int) -> dict:
        """Send one bullet's (system, user) pair and check the Rewrite that comes back.

        Shared by realigned and replaced bullets: both fill the slot `bullet` held,
        so both answer to its length and its line budget.
        """
        backend = self._backend()
        with obs.timer() as t:
            out = await backend.complete(
                model=self.registry.current_model(),
                system=system,
                user=user,
                schema=Rewrite,
                max_tokens=4000,
                effort="medium",
            )
        r = out.parsed
        request_id = out.request_id
        archived = obs.archive_prompt(
            self.log_dir, op, system, user, out.text,
            {"model": self._used(out), "backend": self.registry.current_name(), "mode": mode,
             "request_id": request_id,
             "jd_points": [p.get("id") for p in jd_points], **out.meta})
        if r is None:
            obs.warn("rewrite.unparsable", op=op, request_id=request_id,
                     prompt_file=archived)
            raise RuntimeError(f"{self.agent()} returned no parsable rewrite.")

        introduced = [x for x in r.substitutions if not x.grounded]
        delta = (len(r.rewritten) - len(bullet)) / max(len(bullet), 1) * 100
        obs.record_usage(op, self._used(out), out.usage, t.ms, request_id,
                         backend=self.registry.current_name(),
                         mode=mode, points=len(jd_points), prompt_file=archived,
                         **obs.tmeta(bullet, "orig"), **obs.tmeta(r.rewritten, "new"),
                         length_delta_pct=round(delta, 1),
                         keywords=len(r.keywords_used),
                         substitutions=len(r.substitutions),
                         introduced=len(introduced),
                         defensible=r.defensible)
        # The prompt caps length growth at +/-15% so the mirrored layout does not
        # reflow; a breach means the instruction was ignored and the page may grow.
        if abs(delta) > 15:
            obs.warn("rewrite.length_breach", op=op, length_delta_pct=round(delta, 1),
                     request_id=request_id, prompt_file=archived)
        if max_chars and len(r.rewritten) > max_chars:
            obs.warn("rewrite.line_budget_breach", op=op, chars=len(r.rewritten),
                     budget=max_chars, request_id=request_id, prompt_file=archived)
        if not r.defensible:
            obs.warn("rewrite.not_defensible", op=op, introduced=len(introduced),
                     prompt_file=archived)
        return {
            "rewritten": r.rewritten.strip().lstrip("•-–— ").strip(),
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
        system, user = bullets.replace_request(self.prompts, bullet, section, entry, siblings,
                                               jd_points, role, company, extra, max_chars)
        return await self._run_rewrite("replace", system, user, bullet, jd_points,
                                       "replace", max_chars)

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
        obs.event("rewrite.batch_start", bullets=len(jobs), concurrency=concurrency,
                  backend=self.registry.current_name(),
                  mode=(jobs[0]["args"].get("mode") if jobs else None))

        async def one(job: dict) -> dict:
            fn = {"experience": self.write_experience_bullet,
                  "replace": self.replace_bullet}.get(job.get("kind"), self.rewrite_bullet)
            async with sem:
                try:
                    res = await fn(**job["args"])
                    return {"block_id": job["block_id"], "ok": True, **res}
                except Exception as exc:  # noqa: BLE001
                    obs.record_failure("rewrite", exc, 0.0, block=job["block_id"])
                    return {
                        "block_id": job["block_id"],
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }

        with obs.timer() as t:
            results = await asyncio.gather(*(one(j) for j in jobs))
        ok = sum(1 for r in results if r.get("ok"))
        obs.event("rewrite.batch_done", bullets=len(results), ok=ok,
                  failed=len(results) - ok, ms=t.ms, **obs.totals())
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
        backend = self._backend()
        system, user = bullets.experience_request(self.prompts, experience, jd_points, role,
                                                  company, placements, examples, mode, extra,
                                                  max_chars)
        with obs.timer() as t:
            out = await backend.complete(
                model=self.registry.current_model(),
                system=system,
                user=user,
                schema=NewBullet,
                max_tokens=4000,
                effort="medium",
            )
        r = out.parsed
        archived = obs.archive_prompt(
            self.log_dir, "experience_bullet", system, user, out.text,
            {"model": self._used(out), "backend": self.registry.current_name(), "mode": mode,
             "request_id": out.request_id,
             "jd_points": [p.get("id") for p in jd_points], **out.meta})
        if r is None:
            obs.warn("experience_bullet.unparsable", request_id=out.request_id,
                     prompt_file=archived)
            raise RuntimeError(f"{self.agent()} returned no parsable bullet.")

        ids = {p["id"] for p in placements}
        chosen = r.placement.strip()
        if chosen not in ids:
            heading = (r.new_section.strip()
                       or bullets.DEFAULT_SECTION.get(experience.get("kind", ""),
                                                      "ADDITIONAL EXPERIENCE"))
            chosen = "new:" + heading[:40]
        bullet = r.bullet.strip().lstrip("•-–— ").strip()
        introduced = [x for x in r.substitutions if not x.grounded]
        obs.record_usage("experience_bullet", self._used(out), out.usage, t.ms, out.request_id,
                         backend=self.registry.current_name(), mode=mode,
                         points=len(jd_points),
                         prompt_file=archived,
                         **obs.tmeta(experience["text"], "exp"), **obs.tmeta(bullet, "new"),
                         keywords=len(r.keywords_used), introduced=len(introduced),
                         defensible=r.defensible,
                         placement="new" if chosen.startswith("new:") else chosen)
        if max_chars and len(bullet) > max_chars:
            obs.warn("rewrite.line_budget_breach", chars=len(bullet), budget=max_chars,
                     request_id=out.request_id, prompt_file=archived)
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
        examples: list[dict] = (),
    ) -> dict:
        """One LinkedIn note and one follow-up email for a tailored application.

        `examples` are liked messages (newest first) that set the voice; the prompt
        forbids taking facts from them, since they were written for other roles.
        """
        backend = self._backend()
        system = outreach.OUTREACH_SYSTEM
        if extra.strip():
            system += f"\n\nADDITIONAL USER INSTRUCTION (overrides tone preferences above):\n{extra.strip()}"

        user = outreach.outreach_prompt(candidate, recruiter, role, company, highlights,
                                        jd_points, examples)
        n_ex = {k: min(outreach.OUTREACH_EXAMPLE_LIMIT, sum(e["kind"] == k for e in examples))
                for k in outreach.OUTREACH_EXAMPLE_KINDS}
        obs.event("outreach.start", has_recruiter=bool(recruiter.strip()),
                  highlights=len(highlights), points=len(jd_points),
                  linkedin_examples=n_ex["linkedin"], email_examples=n_ex["email"],
                  has_note=bool(extra.strip()), model=self.registry.model_label(),
                  backend=self.registry.current_name())
        with obs.timer() as t:
            out = await backend.complete(
                model=self.registry.current_model(),
                system=system,
                user=user,
                schema=Outreach,
                max_tokens=4000,
                effort="medium",
            )
        o = out.parsed
        archived = obs.archive_prompt(
            self.log_dir, "outreach", system, user, out.text,
            {"model": self._used(out), "backend": self.registry.current_name(),
             "request_id": out.request_id,
             **out.meta})
        if o is None:
            obs.warn("outreach.unparsable", request_id=out.request_id,
                     prompt_file=archived)
            raise RuntimeError(f"{self.agent()} returned no parsable outreach draft.")

        linkedin = o.linkedin.strip()
        li_subject = o.linkedin_subject.strip()
        subject = o.email_subject.strip()
        body = o.email_body.strip()
        obs.record_usage("outreach", self._used(out), out.usage, t.ms, out.request_id,
                         backend=self.registry.current_name(), prompt_file=archived,
                         linkedin_chars=len(linkedin),
                         **obs.tmeta(linkedin, "li"), **obs.tmeta(body, "email"),
                         subject_chars=len(subject), li_subject_chars=len(li_subject))
        # Over the limit LinkedIn would cut the note off mid-word. Truncating here
        # would do the same, so surface it instead and let the user trim it.
        if len(linkedin) > outreach.LINKEDIN_LIMIT:
            obs.warn("outreach.linkedin_too_long", chars=len(linkedin),
                     limit=outreach.LINKEDIN_LIMIT, prompt_file=archived)
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
        backend = self._backend()
        user = corrections.chat_prompt(blocks, layout, fit, geometry, role, company, jd_points,
                                       history, message)
        obs.event("pdfchat.start", blocks=len(blocks), turns=len(history),
                  **obs.tmeta(message, "msg"), model=self.registry.model_label(),
                  backend=self.registry.current_name())
        with obs.timer() as t:
            out = await backend.complete(
                model=self.registry.current_model(),
                system=corrections.PDF_CHAT_SYSTEM,
                user=user,
                schema=PdfRevision,
                max_tokens=8000,
                effort="medium",
            )
        rev = out.parsed
        archived = obs.archive_prompt(
            self.log_dir, "pdf_chat", corrections.PDF_CHAT_SYSTEM, user, out.text,
            {"model": self._used(out), "backend": self.registry.current_name(),
             "request_id": out.request_id,
             **out.meta})
        if rev is None:
            obs.warn("pdfchat.unparsable", request_id=out.request_id,
                     prompt_file=archived)
            raise RuntimeError(f"{self.agent()} returned no parsable answer.")

        known = {b["id"]: b for b in blocks}
        edits, unknown = [], []
        for e in rev.edits:
            bid = e.block_id.strip()
            text = e.text.strip().lstrip("\u2022\u25cf-\u2013\u2014 ").strip()
            if bid not in known:
                unknown.append(bid)
                continue
            if not text or text == known[bid]["text"]:
                continue
            edits.append({"block_id": bid, "text": text,
                          "substitutions": [x.model_dump() for x in e.substitutions],
                          "defensible": e.defensible})
        changes: dict = {}
        for c in rev.layout:
            key, div = corrections.LAYOUT_SETTINGS[c.setting]
            changes[key] = c.value / div
        if rev.justify != "unchanged":
            changes["justify"] = rev.justify

        obs.record_usage("pdf_chat", self._used(out), out.usage, t.ms, out.request_id,
                         backend=self.registry.current_name(), prompt_file=archived,
                         edits=len(edits),
                         layout=sorted(changes) or None,
                         introduced=sum(1 for e in edits for x in e["substitutions"]
                                        if not x["grounded"]))
        if unknown:
            obs.warn("pdfchat.unknown_blocks", ids=unknown, prompt_file=archived)
        return {"reply": rev.reply.strip(), "edits": edits, "layout": changes}

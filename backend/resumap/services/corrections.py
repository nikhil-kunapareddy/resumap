"""The corrections chat: block edits and layout changes asked for on the preview."""
from __future__ import annotations

from typing import Any

from .. import obs
from ..domain import ChatMessage, ChatRole, Rewrite, RewriteStatus
from ..llm import BackendUnavailable, ModelGateway
from ..rendering.pdfgen import clean_layout
from ..storage import Store
from .base import Service, stored_meta
from .documents import left_text, render_doc
from .errors import Conflict, Invalid, NotFound, Unavailable, UpstreamFailed
from .exports import ExportService
from .results import ChatMessageOut, ChatState

CHAT_HISTORY = 12


def chat_blocks(doc: dict, rewrites: dict[str, Rewrite], stats: dict) -> list[dict]:
    """Every block as the model sees it: rendered wording, original, line counts.

    `doc` is the render doc, so bullets written from experiences are listed
    with the user's own notes as their original. Headings spliced in for them
    are left out: they are named by the bullet's placement, not edited.
    """
    out = []
    for b in doc.get("blocks", []):
        if b.get("inserted") and b["kind"] == "section":
            continue
        rw = rewrites.get(b["id"])
        st = stats.get(b["id"]) or {}
        new = bool(b.get("inserted"))
        out.append({
            "id": b["id"], "kind": b["kind"], "section": b.get("section", ""),
            "text": rw.rewritten if rw is not None and rw.in_export else left_text(b),
            "original": rw.original if new and rw is not None else left_text(b),
            "new": new, "right": b.get("right", ""),
            "pending": rw is not None and rw.status == RewriteStatus.PENDING,
            "lines": st.get("lines", 1), "source_lines": st.get("source_lines", 1),
            "chars_per_line": st.get("chars_per_line", 0),
        })
    return out


def _applied(m: ChatMessage) -> bool:
    return m.role == ChatRole.ASSISTANT and not m.meta.get("undone") and bool(
        m.meta.get("edits") or m.meta.get("layout"))


class CorrectionService(Service):
    def __init__(self, store: Store, gateway: ModelGateway, exports: ExportService) -> None:
        super().__init__(store)
        self.gateway = gateway
        self.exports = exports

    def state(self, job_id: int) -> ChatState:
        """Messages without their undo snapshots, the latest applied one undoable."""
        self._job(job_id)
        return self._state(job_id)

    def _state(self, job_id: int) -> ChatState:
        msgs = self.store.chat.for_job(job_id)
        last = next((m.id for m in reversed(msgs) if _applied(m)), None)
        return ChatState(
            messages=[
                ChatMessageOut(id=m.id, role=m.role, text=m.text,
                               meta={k: v for k, v in m.meta.items() if k != "before"},
                               can_undo=m.id == last)
                for m in msgs
            ],
            rewrites=self.store.rewrites.for_job(job_id),
            layout=clean_layout(self.store.layouts.get(job_id)),
        )

    async def apply(self, job_id: int, message: str) -> ChatState:
        """Apply one correction request to the PDF: block edits and layout changes.

        Edits are saved as "edited" rewrites, so they are in the PDF at once and in
        the review step with their substitutions, like a hand edit. The state each
        one replaced is kept on the reply, which is what Undo restores.
        """
        job = self._job(job_id)
        resume = self._resume(job.resume_id)
        why = self.gateway.status()
        if why:
            raise Unavailable(why)
        message = message.strip()
        if not message:
            raise Invalid("Type what you want changed first.")

        _out, _name, fit = self.exports.build(job_id, preview=True)
        rewrites = self.store.rewrites.for_job(job_id)
        doc = render_doc(resume.doc.as_dict(), rewrites)
        layout = fit["layout"]
        history = self.store.chat.for_job(job_id)
        try:
            rev = await self.gateway.revise_pdf(
                blocks=chat_blocks(doc, rewrites, fit["blocks"]),
                layout=layout, fit=fit, geometry=doc.get("geometry") or {},
                role=job.role, company=job.company,
                jd_points=[p.model_dump(mode="json") for p in job.points],
                history=[m.model_dump(mode="json") for m in history[-CHAT_HISTORY:]],
                message=message,
            )
        except BackendUnavailable as exc:
            raise Unavailable(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - say why, keep the page as it was
            obs.record_failure("pdf_chat", exc, 0.0, job_id=job_id)
            raise UpstreamFailed(f"Could not apply that: {type(exc).__name__}: {exc}") from exc

        blocks = {b["id"]: b for b in doc.get("blocks", [])}
        before: dict[str, dict[str, Any] | None] = {}
        new_layout = clean_layout({**layout, **rev["layout"]})
        changed = {k: v for k, v in new_layout.items() if v != layout.get(k)}
        with self.store.transaction():
            for e in rev["edits"]:
                bid = e["block_id"]
                old = rewrites.get(bid)
                before[bid] = old.model_dump(mode="json", exclude_unset=True) if old else None
                meta = {**(stored_meta(old) if old else {}),
                        "substitutions": e["substitutions"],
                        "defensible": e["defensible"], "source": "chat"}
                self.store.rewrites.save(job_id, bid,
                                         original=old.original if old else left_text(blocks[bid]),
                                         rewritten=e["text"], status=RewriteStatus.EDITED,
                                         meta=meta)
            if changed:
                self.store.layouts.set(job_id, new_layout)
            self.store.chat.add(job_id, ChatRole.USER, message, {})
            self.store.chat.add(job_id, ChatRole.ASSISTANT, rev["reply"] or "Done.", {
                "edits": [e["block_id"] for e in rev["edits"]],
                "layout": changed,
                "before": {"rewrites": before, "layout": layout},
            })
        obs.event("pdfchat.applied", job_id=job_id, edits=len(rev["edits"]),
                  layout=sorted(changed) or None)
        return self._state(job_id)

    def undo(self, job_id: int, message_id: int) -> ChatState:
        """Put back what one correction replaced. Only the latest applied one, so an
        undo can never silently revert a later change to the same block."""
        msgs = self.store.chat.for_job(job_id)
        target = next((m for m in msgs if m.id == message_id), None)
        if target is None or target.role != ChatRole.ASSISTANT:
            raise NotFound("No such correction.")
        if not _applied(target):
            raise Invalid("That correction changed nothing, or is already undone.")
        if any(_applied(m) for m in msgs if m.id > message_id):
            raise Conflict("Undo the later corrections first.")

        before = target.meta.get("before") or {}
        with self.store.transaction():
            for bid, row in (before.get("rewrites") or {}).items():
                if row is None:
                    self.store.rewrites.delete(job_id, bid)
                else:
                    self.store.rewrites.save(job_id, bid, original=row["original"],
                                             rewritten=row["rewritten"],
                                             status=RewriteStatus(row["status"]),
                                             meta=row["meta"])
            self.store.layouts.set(job_id, before.get("layout") or {})
            self.store.chat.set_meta(message_id, {**target.meta, "undone": True})
        obs.event("pdfchat.undone", job_id=job_id, message=message_id,
                  edits=len(before.get("rewrites") or {}))
        return self._state(job_id)

"""Reading and assembling the resume document: pure functions over its dict form.

The parser writes the document as a dict and the renderer reads one, so the
helpers that sit between them take and return that form too
(``ResumeDoc.as_dict()``). None of them touch storage.
"""
from __future__ import annotations

from ..domain import Rewrite, experience_id
from ..rendering.pdfgen import insert_blocks


def left_text(blk: dict) -> str:
    """A block's editable text: an entry's right-aligned column is not part of it."""
    return (blk.get("left") or blk["text"]) if blk.get("right") else blk["text"]


def placements(doc: dict) -> list[dict]:
    """The entries and sections a new bullet can be placed under."""
    out = []
    for b in doc.get("blocks", []):
        if b["kind"] not in ("entry", "section"):
            continue
        text = left_text(b) + (f" ({b['right']})" if b.get("right") else "")
        out.append({"id": b["id"], "kind": b["kind"], "text": text,
                    "section": b.get("section", "")})
    return out


def entry_context(doc: dict, block_id: str) -> tuple[str, list[str]]:
    """The entry a bullet sits under, and that entry's other points.

    A replaced bullet has nothing of its own to stand on, so these are what
    keep it plausible for the role and stop it repeating its neighbours.
    """
    entry, group, found = "", [], False
    for b in doc.get("blocks", []):
        if b["kind"] in ("entry", "section"):
            if found:
                break
            entry = (left_text(b) + (f" ({b['right']})" if b.get("right") else "")
                     if b["kind"] == "entry" else "")
            group = []
        elif b["id"] == block_id:
            found = True
        elif b.get("mappable"):
            group.append(b["text"])
    return (entry, group) if found else ("", [])


def render_doc(doc: dict, rewrites: dict[str, Rewrite]) -> dict:
    """The parsed resume plus the bullets written from experiences that are going in.

    Export still does not rewrite the stored document: this is a copy, rebuilt
    on every render, so dropping or re-placing a bullet needs no clean-up.
    """
    inserts = sorted(
        ({"id": bid, "text": rw.rewritten.strip(), "placement": rw.meta.placement or ""}
         for bid, rw in rewrites.items()
         if experience_id(bid) is not None and rw.in_export),
        key=lambda x: experience_id(x["id"]) or 0)
    return insert_blocks(doc, inserts)


def overrides(rewrites: dict[str, Rewrite]) -> dict[str, str]:
    """{block_id: text} for every rewrite that goes into the PDF."""
    return {bid: rw.rewritten for bid, rw in rewrites.items() if rw.in_export}

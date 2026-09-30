"""The parse report: every block, its kind and flags, and the rule that excluded it."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .. import obs


def _text_allowed() -> bool:
    return os.getenv("RESUMAP_LOG_TEXT", "0").strip() not in ("", "0", "false", "no")


def parse_report(resume_id: int, label: str, doc: dict[str, Any]) -> str:
    """The block-classification table, with the reason for every rejection.

    This is the view that makes a misclassification obvious: which blocks became
    points, and for the rest, exactly which rule excluded them.
    """
    d = doc.get("diagnostics") or {}
    g = d.get("geometry") or {}

    def linked(b: dict[str, Any]) -> int:
        """How many of a block's runs carry a hyperlink."""
        return sum(1 for r in (b.get("runs") or []) + (b.get("right_runs") or []) if r.get("uri"))

    blocks = doc.get("blocks") or []
    out: list[str] = [
        f"PARSE REPORT  resume={resume_id}  label={label}",
        "=" * 78,
        f"pages {d.get('pages')}   lines {d.get('lines')}   "
        f"blocks {d.get('blocks')}   points {d.get('points')}",
        f"body size {d.get('body_size')}pt   leading {d.get('leading')}pt   "
        f"margins L{g.get('left')} R{g.get('right')} T{g.get('top')} B{g.get('bottom')}",
        f"tab stops {d.get('tab_stops')}   justified lines {d.get('justified_lines')}   "
        f"rules {d.get('rules')}   links {sum(linked(b) for b in blocks)}",
        f"merges {d.get('merges')} {d.get('merge_reasons')}   "
        f"label-guard blocks {d.get('label_guard_blocks')}   "
        f"truncated {d.get('truncated')}",
        f"fonts {', '.join(d.get('fonts') or [])}",
        "",
        f"{'PT':<3}{'ID':<8}{'KIND':<9}{'SECTION':<24}{'LEN':>5}  FLAGS  WHY NOT A POINT",
        "-" * 78,
    ]
    for b in blocks:
        flags = "".join(
            [
                "R" if b.get("right") else "-",
                "J" if b.get("justified") else "-",
                "T" if b.get("truncated") else "-",
                "L" if linked(b) else "-",
            ]
        )
        reason = "" if b.get("mappable") else (b.get("reason") or "?")
        out.append(
            f"{'->' if b.get('mappable') else '':<3}"
            f"{b.get('id', ''):<8}{b.get('kind', ''):<9}"
            f"{(b.get('section') or '-')[:23]:<24}"
            f"{len(b.get('text') or ''):>5}  {flags}   {reason}"
        )
        if _text_allowed():
            out.append(f"       {obs.scrub(b.get('text') or '')[:70]}")

    out += ["", "counts by reason:"]
    for k, v in sorted((d.get("reasons") or {}).items(), key=lambda kv: -kv[1]):
        out.append(f"  {k:<24}{v}")
    return "\n".join(out) + "\n"


def write_parse_report(
    log_dir: Path, resume_id: int, label: str, doc: dict[str, Any]
) -> Path | None:
    try:
        directory = log_dir / "parse"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{resume_id}.txt"
        path.write_text(parse_report(resume_id, label, doc), encoding="utf-8")
        return path
    except Exception:  # noqa: BLE001 - reporting must not break an upload
        return None

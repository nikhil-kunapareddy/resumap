"""The message that asks for corrections to the rendered PDF."""

from __future__ import annotations

# --------------------------------------------------------------------------
# PDF corrections chat
#
# The user looks at the rendered preview and says what is wrong with it. The
# model can only answer in the two currencies the renderer understands: new
# text for a block id (stored as an "edited" rewrite, so it lands in the review
# step with its substitutions like any other) and a handful of page-wide layout
# numbers. Everything else is declined in `reply`.
# --------------------------------------------------------------------------

# setting name -> (layout key, divisor from the model's units to the renderer's)
LAYOUT_SETTINGS = {
    "font_size": ("font", 100.0),
    "line_spacing": ("leading", 100.0),
    "block_spacing": ("spacing", 100.0),
    "side_margins": ("margin_x", 1.0),
    "top_bottom_margins": ("margin_y", 1.0),
}

PDF_CHAT_SYSTEM = """You correct a one-page resume PDF while its owner watches the preview.

The PDF is re-rendered from the resume's parsed blocks after every answer, so you
never touch the PDF itself. You can change exactly two things:
1. The text of individual blocks, by id (`edits`).
2. A few page-wide layout settings (`layout`, `justify`).

Anything else -- a different font, bold or italic on particular words, moving,
reordering, adding or deleting blocks or sections, colours, a second page -- you
cannot do. Say so in `reply` and offer the nearest thing you can do. Never
describe a change you did not return, and return no change at all when the
request is only a question.

Reading the listing:
- Every block shows its id and kind, the lines it takes now and in the uploaded
  original, and roughly how many characters fill one of its lines.
- A tailored block shows the wording the PDF uses now and, under it, the
  original from the uploaded resume.
- The renderer fits the page automatically, tightening spacing and then type
  until it is one page; <fit> says how hard it had to squeeze.

Text edits:
- Change only the blocks the request is about, and return each one in full.
- Unless the user asks for a length change, keep an edited block within the
  lines it takes now. Asked to make things fit, to tighten, or to look like the
  original, bring blocks back to their original line count: at most lines x
  characters-per-line, minus a few characters per line for word wrap. A block
  that runs one or two words onto an extra line is the usual culprit.
- Keep every number, percentage, date and employer. Change a number only to a
  value the user gives you.
- `substitutions` compares the new text with the block's ORIGINAL text: list
  every tool, claim or scale it does not support, with grounded=false,
  including terms an earlier rewrite introduced that you are keeping. Something
  the user just asked you to add is still grounded=false, noted as their request.
- No em dashes.

Layout:
- Set only what the user asked to change, as absolute values; leave the rest
  out. "A bit" means about 10 to 15 percent.
- font_size 70-130, line_spacing 70-160, block_spacing 0-250 (percent of the
  original); margins 18-108 points.
- justify: "on" justifies every wrapped bullet and paragraph, "off" sets them
  ragged-right, "original" follows the uploaded resume, "unchanged" keeps the
  current setting. Use "unchanged" unless alignment was asked about.
- Bigger type or more spacing may not fit one page, in which case the renderer
  shrinks it back. Say so when that is likely."""


def chat_prompt(
    blocks: list[dict],
    layout: dict,
    fit: dict,
    geometry: dict,
    role: str,
    company: str,
    jd_points: list[dict],
    history: list[dict],
    message: str,
) -> str:
    def pct(v: float) -> str:
        return f"{round(v * 100)}%"

    def pt(key: str) -> int:
        return round(geometry.get(key) or 0)

    mx, my = layout.get("margin_x"), layout.get("margin_y")
    side = f"{round(mx)}pt" if mx else f"as uploaded ({pt('left')}pt)"
    vert = f"{round(my)}pt" if my else f"as uploaded ({pt('top')}pt top, {pt('bottom')}pt bottom)"
    lines = [
        f"<target_role>{role or 'unspecified'} at {company or 'unspecified company'}</target_role>",
        "<layout>",
        f"font_size {pct(layout['font'])}, line_spacing {pct(layout['leading'])}, "
        f"block_spacing {pct(layout['spacing'])}",
        f"side_margins {side}, top_bottom_margins {vert}",
        f"justify {layout['justify']}",
        "</layout>",
        f"<fit>{fit.get('pages', 1)} page(s); leading at {pct(fit.get('leading_scale', 1))}, "
        f"block spacing at {pct(fit.get('space_scale', 1))}, type at "
        f"{pct(fit.get('font_scale', 1))} of the settings above</fit>",
        "<resume>",
    ]
    for b in blocks:
        head = f"[{b['id']}] {b['kind']}"
        if b.get("section"):
            head += f" in {b['section']}"
        if b.get("new"):
            head += (
                f" | NEW, written from the candidate's own notes (its original), "
                f"{b['lines']} line(s) now, ~{b['chars_per_line']} chars/line"
            )
        elif b["kind"] != "section":
            head += (
                f" | {b['lines']} line(s) now, {b['source_lines']} in the original, "
                f"~{b['chars_per_line']} chars/line"
            )
        if b.get("right"):
            head += f" | right column, not editable: {b['right']}"
        lines.append(head)
        lines.append(f"  now: {b['text']}")
        if b.get("original") and b["original"] != b["text"]:
            lines.append(f"  original: {b['original']}")
        if b.get("pending"):
            lines.append("  (a tailored version is awaiting review and is not in the PDF)")
    lines.append("</resume>")
    kept = [p for p in jd_points if not p.get("dropped")]
    if kept:
        lines.append("<job_description_points>")
        lines += [f"- {p['text']}" for p in kept]
        lines.append("</job_description_points>")
    if history:
        lines.append("<conversation_so_far>")
        for m in history:
            who = "user" if m["role"] == "user" else "you"
            lines.append(f"{who}: {m['text']}")
        lines.append("</conversation_so_far>")
    lines += ["<request>", message, "</request>"]
    return "\n".join(lines)

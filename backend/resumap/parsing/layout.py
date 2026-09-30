"""Extract a style-aware line model from a PDF.

The goal is not perfect fidelity -- it is capturing enough of the original
resume's typography (fonts, sizes, weights, indents, tab stops, rules) that
the regenerated PDF reads as the same document.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

BULLET_GLYPHS = "•◦▪▫‣·●■□-–—*o"
BULLET_RE = re.compile(rf"^\s*([{re.escape(BULLET_GLYPHS)}])\s+")
CID_RE = re.compile(r"^\(cid:\d+\)$")


@dataclass
class Run:
    """A stretch of one line sharing a single style.

    Needed because resumes mix weights inside a line: "**Senior Engineer**, ZF
    Group" or "**Languages:** Python, SQL".

    A run is also the smallest unit that can carry a hyperlink, because a link's
    anchor text ("LinkedIn") is a span inside a line, not a line of its own. The
    URL lives in the PDF's annotation layer, not in the text, so it is invisible
    to character extraction and has to be read separately and re-attached here.
    `color` is captured only for linked or underlined runs -- it is what makes a
    link *look* like a link, and re-rendering it black is the other half of
    losing it. An underline is a separate drawn stroke, not a font property, so
    it too is matched back onto the characters above it; a typed-out email is
    often styled as a link with no annotation behind it at all.
    """

    text: str
    bold: bool
    italic: bool
    uri: str = ""
    color: str = ""
    underline: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Line:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    size: float
    font: str
    bold: bool
    italic: bool
    page: int
    # A wide horizontal gap inside a line usually means a tab stop: a role on
    # the left and a date range flushed right. Keeping the halves separate lets
    # us re-render that alignment instead of collapsing it to one long string.
    left: str = ""
    right: str = ""
    gap: float = 0.0
    text_x0: float = 0.0
    runs: list[Run] = field(default_factory=list)
    right_runs: list[Run] = field(default_factory=list)
    # The source line had its inter-word spaces stretched, i.e. it was
    # justified to both margins.
    stretched: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Rule:
    """A horizontal divider line, usually under a section heading."""

    top: float
    x0: float
    x1: float
    page: int


@dataclass
class PageGeometry:
    width: float = 612.0
    height: float = 792.0
    left: float = 54.0
    right: float = 54.0
    top: float = 54.0
    bottom: float = 54.0


@dataclass
class ExtractedDoc:
    lines: list[Line] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    geometry: PageGeometry = field(default_factory=PageGeometry)


def _is_bold(fontname: str) -> bool:
    f = fontname.lower()
    return any(t in f for t in ("bold", "black", "heavy", "semib", "demib"))


def _is_italic(fontname: str) -> bool:
    f = fontname.lower()
    return "italic" in f or "oblique" in f


def _dominant(values: list[Any]) -> Any:
    if not values:
        return None
    counts: dict[Any, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _rgb_hex(color: Any) -> str:
    """Normalise a pdfminer colour to "#rrggbb", or "" for plain black.

    The value is a tuple whose length names the space: 1 is grey, 3 is RGB, 4 is
    CMYK. Black is returned as "" so an ordinary run carries no colour at all and
    renders exactly as it did before.
    """
    try:
        vals = tuple(float(v) for v in color)
    except (TypeError, ValueError):
        return ""
    if len(vals) == 1:
        r = g = b = vals[0]
    elif len(vals) == 3:
        r, g, b = vals
    elif len(vals) == 4:
        c, m, y, k = vals
        r, g, b = (1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k)
    else:
        return ""
    if max(r, g, b) <= 0.04:
        return ""
    return "#" + "".join(f"{max(0, min(255, round(v * 255))):02x}" for v in (r, g, b))


def _link_at(ch: dict, links: list[dict]) -> str:
    """The URI of the annotation covering this character, if any.

    Matched on the glyph's centre rather than its box: annotation rectangles are
    drawn by hand in the source document and routinely clip a descender or miss
    the last glyph's side bearing by a fraction of a point.
    """
    cx = (ch["x0"] + ch["x1"]) / 2
    cy = (ch["top"] + ch["bottom"]) / 2
    for lk in links:
        if lk["x0"] - 1.0 <= cx <= lk["x1"] + 1.0 and lk["top"] - 1.5 <= cy <= lk["bottom"] + 1.5:
            return lk["uri"]
    return ""


def _underlined(ch: dict, strokes: list[dict]) -> bool:
    """True when a short horizontal stroke runs just under this glyph."""
    cx = (ch["x0"] + ch["x1"]) / 2
    for s in strokes:
        if (
            s["x0"] - 0.5 <= cx <= s["x1"] + 0.5
            and ch["bottom"] - ch["size"] * 0.4 <= s["top"] <= ch["bottom"] + 1.5
        ):
            return True
    return False


def _dominant_link(chars: list[dict]) -> tuple[str, str, bool]:
    """The URI, colour and underline a whole word takes, by majority of its characters.

    Deciding per word rather than per character keeps the text intact: a word is
    never split to honour an annotation that covers only part of it, which would
    insert a space the source never had.
    """
    uris = [c.get("uri", "") for c in chars]
    uri = _dominant([u for u in uris if u]) or ""
    if uri and sum(1 for u in uris if u == uri) * 2 < len(uris):
        uri = ""
    under = sum(1 for c in chars if c.get("underline")) * 2 > len(chars)
    if not uri and not under:
        return "", "", False
    return uri, _dominant([_rgb_hex(c.get("non_stroking_color")) for c in chars]) or "", under


def _dominant_style(chars: list[dict]) -> tuple[bool, bool, str, float]:
    fonts = [c.get("fontname", "") for c in chars]
    bold = sum(_is_bold(f) for f in fonts) > len(fonts) / 2
    italic = sum(_is_italic(f) for f in fonts) > len(fonts) / 2
    return (
        bold,
        italic,
        _dominant(fonts) or "Helvetica",
        _dominant([round(c["size"], 1) for c in chars]) or 10.0,
    )


def _chunk_row(row: list[dict]) -> tuple[list[list[dict]], list[float]]:
    """Split a line into word chunks, measuring each whitespace run's width.

    The width is measured geometrically -- from the end of the last glyph to the
    start of the next -- because a PDF may right-align text either by leaving a
    positional gap or by emitting dozens of literal space glyphs. Counting
    spaces would miss the first case; measuring gaps between adjacent glyphs
    would miss the second.
    """
    chunks: list[list[dict]] = []
    gaps: list[float] = []
    cur: list[dict] = []
    i = 0
    while i < len(row):
        ch = row[i]
        if ch["text"].isspace():
            j = i
            while j < len(row) and row[j]["text"].isspace():
                j += 1
            if cur and j < len(row):
                width = max(0.0, row[j]["x0"] - cur[-1]["x1"])
                chunks.append(cur)
                gaps.append(width)
                cur = []
            i = j
            continue
        # A word break may also be pure positioning with no space glyph at all:
        # a bullet drawn at one x and its text at another. Splitting only on
        # whitespace characters glues those together ("<bullet>Rebuilt the...").
        if cur:
            gap = ch["x0"] - cur[-1]["x1"]
            if gap > max(ch["size"], cur[-1]["size"]) * 0.25:
                chunks.append(cur)
                gaps.append(gap)
                cur = []
        cur.append(ch)
        i += 1
    if cur:
        chunks.append(cur)
    return chunks, gaps


def _runs_from_chunks(chunks: list[list[dict]]) -> list[Run]:
    """Collapse adjacent same-style chunks into styled runs."""
    runs: list[Run] = []
    for chunk in chunks:
        bold, italic, _f, _s = _dominant_style(chunk)
        uri, color, under = _dominant_link(chunk)
        text = "".join(c["text"] for c in chunk)
        if (
            runs
            and runs[-1].bold == bold
            and runs[-1].italic == italic
            and runs[-1].uri == uri
            and runs[-1].color == color
            and runs[-1].underline == under
        ):
            runs[-1].text += " " + text
        else:
            runs.append(
                Run(text=text, bold=bold, italic=italic, uri=uri, color=color, underline=under)
            )
    return runs


def _group_chars_to_lines(
    chars: list[dict],
    page_no: int,
    links: list[dict] | None = None,
    strokes: list[dict] | None = None,
) -> list[Line]:
    """Cluster characters into visual lines by their vertical midpoint."""
    if not chars:
        return []

    if links:
        chars = [{**c, "uri": _link_at(c, links)} for c in chars]
    if strokes:
        chars = [{**c, "underline": _underlined(c, strokes)} for c in chars]

    rows: list[list[dict]] = []
    for ch in sorted(chars, key=lambda c: (round(c["top"], 1), c["x0"])):
        placed = False
        for row in rows:
            # Same line if the baselines overlap within half an x-height.
            tol = max(2.0, min(ch["size"], row[0]["size"]) * 0.45)
            if abs(ch["top"] - row[0]["top"]) <= tol:
                row.append(ch)
                placed = True
                break
        if not placed:
            rows.append([ch])

    lines: list[Line] = []
    for row in rows:
        row.sort(key=lambda c: c["x0"])
        # A glyph with no unicode mapping renders as "(cid:NNN)". At the start of
        # a line that is essentially always the bullet dingbat; mid-line it is
        # noise we are better off dropping than printing literally.
        cleaned = []
        for i, ch in enumerate(row):
            if CID_RE.match(ch["text"]):
                if i == 0:
                    ch = {**ch, "text": "\u2022"}
                else:
                    continue
            cleaned.append(ch)
        row = cleaned
        if not row:
            continue

        chunks, gaps = _chunk_row(row)
        if not chunks:
            continue
        bold, italic, font, size = _dominant_style(row)
        space_w = max(size * 0.25, 1.0)

        first_text = "".join(c["text"] for c in chunks[0])
        leading_bullet = bool(BULLET_RE.match(first_text + " "))

        # Widest whitespace run with real content on both sides is a tab stop.
        tab_idx, tab_w = -1, 0.0
        for k, w in enumerate(gaps):
            if leading_bullet and k == 0:
                continue  # the space after a bullet glyph is not a tab stop
            if w > tab_w:
                tab_idx, tab_w = k, w
        if tab_w < size * 2.0:
            tab_idx = -1

        # Any other oversized gap means the line was justified.
        stretched = any(
            w > space_w * 1.9
            for k, w in enumerate(gaps)
            if k != tab_idx and not (leading_bullet and k == 0)
        )

        if tab_idx >= 0:
            left_chunks, right_chunks = chunks[: tab_idx + 1], chunks[tab_idx + 1 :]
        else:
            left_chunks, right_chunks = chunks, []

        def join(cs: list[list[dict]]) -> str:
            return " ".join("".join(c["text"] for c in chunk) for chunk in cs)

        left_txt = join(left_chunks)
        right_txt = join(right_chunks)
        full = (left_txt + " " + right_txt).strip() if right_txt else left_txt
        if not full.strip():
            continue

        # x-position of the first character after a leading bullet glyph.
        text_x0 = float(row[0]["x0"])
        if leading_bullet and len(chunks) > 1:
            text_x0 = float(chunks[1][0]["x0"])

        lines.append(
            Line(
                text=full.strip(),
                x0=min(c["x0"] for c in row),
                x1=max(c["x1"] for c in row if not c["text"].isspace()),
                top=min(c["top"] for c in row),
                bottom=max(c["bottom"] for c in row),
                size=float(size),
                font=font,
                bold=bold,
                italic=italic,
                page=page_no,
                left=left_txt if right_txt else "",
                right=right_txt,
                gap=tab_w,
                text_x0=text_x0,
                runs=_runs_from_chunks(left_chunks),
                right_runs=_runs_from_chunks(right_chunks),
                stretched=stretched,
            )
        )

    lines.sort(key=lambda ln: (ln.page, ln.top, ln.x0))
    return lines


def extract_pdf(path: str) -> ExtractedDoc:
    import pdfplumber

    doc = ExtractedDoc()
    all_lines: list[Line] = []

    with pdfplumber.open(path) as pdf:
        if not pdf.pages:
            return doc
        first = pdf.pages[0]
        doc.geometry.width = float(first.width)
        doc.geometry.height = float(first.height)

        for i, page in enumerate(pdf.pages):
            # Hyperlinks live in the page's annotation layer, entirely outside
            # the character stream, so they have to be read and re-attached
            # explicitly -- extracting text alone silently drops every one.
            links = [
                {
                    "uri": h["uri"],
                    "x0": float(h["x0"]),
                    "x1": float(h["x1"]),
                    "top": float(h["top"]),
                    "bottom": float(h["bottom"]),
                }
                for h in (page.hyperlinks or [])
                if h.get("uri")
            ]
            # Underlines: strokes too short to be a section rule.
            strokes = [
                {"x0": float(r["x0"]), "x1": float(r["x1"]), "top": float(r["top"])}
                for r in list(page.lines) + list(page.rects)
                if abs(r["bottom"] - r["top"]) <= 1.5
                and 2.0 < abs(r["x1"] - r["x0"]) <= page.width * 0.30
            ]
            all_lines.extend(_group_chars_to_lines(page.chars, i, links, strokes))

            # Horizontal rules show up either as thin rects or as lines.
            for r in page.rects:
                h = abs(r["y1"] - r["y0"])
                w = abs(r["x1"] - r["x0"])
                if h <= 2.5 and w > page.width * 0.30:
                    doc.rules.append(
                        Rule(top=float(r["top"]), x0=float(r["x0"]), x1=float(r["x1"]), page=i)
                    )
            for ln in page.lines:
                if abs(ln["y1"] - ln["y0"]) <= 2.5 and abs(ln["x1"] - ln["x0"]) > page.width * 0.30:
                    doc.rules.append(
                        Rule(top=float(ln["top"]), x0=float(ln["x0"]), x1=float(ln["x1"]), page=i)
                    )

    doc.lines = all_lines

    if all_lines:
        g = doc.geometry
        g.left = max(18.0, min(ln.x0 for ln in all_lines))
        g.right = max(18.0, g.width - max(ln.x1 for ln in all_lines))
        g.top = max(18.0, min(ln.top for ln in all_lines if ln.page == 0))
        # Measure the bottom margin too. Assuming a conventional one throws away
        # usable lines: this resume runs its content to 30pt from the foot, and a
        # hardcoded 54pt pushed the last skills lines onto a second page.
        g.bottom = max(18.0, g.height - max(ln.bottom for ln in all_lines))
    return doc


def _docx_runs(para: Any) -> list[Run]:
    """Styled runs for a DOCX paragraph, with hyperlink anchors kept linked.

    A DOCX hyperlink is a `w:hyperlink` wrapper around its runs, so `para.runs`
    steps straight past it. Walking the paragraph's inner content instead is the
    only way to see which runs sit inside a link and what it points at.
    """
    from docx.text.hyperlink import Hyperlink

    runs: list[Run] = []

    def add(text: str, bold: bool, italic: bool, uri: str) -> None:
        if not text.strip():
            return
        if runs and runs[-1].bold == bold and runs[-1].italic == italic and runs[-1].uri == uri:
            runs[-1].text += text
        else:
            runs.append(Run(text=text, bold=bold, italic=italic, uri=uri))

    for item in para.iter_inner_content():
        if isinstance(item, Hyperlink):
            uri = item.address or ""
            if uri and item.fragment:
                uri = f"{uri}#{item.fragment}"
            for r in item.runs:
                add(r.text, bool(r.bold), bool(r.italic), uri)
        else:
            add(item.text, bool(item.bold), bool(item.italic), "")
    return runs


def extract_docx(path: str) -> ExtractedDoc:
    """DOCX carries far less geometry, so we synthesize sane defaults."""
    import docx

    d = docx.Document(path)
    doc = ExtractedDoc()
    y = 54.0
    for para in d.paragraphs:
        text = para.text.strip()
        if not text:
            y += 6
            continue
        runs = [r for r in para.runs if r.text.strip()]
        bold = bool(runs) and sum(bool(r.bold) for r in runs) > len(runs) / 2
        italic = bool(runs) and sum(bool(r.italic) for r in runs) > len(runs) / 2
        size = 10.5
        for r in runs:
            if r.font.size is not None:
                size = r.font.size.pt
                break
        style = ((para.style.name if para.style else "") or "").lower()
        if "heading 1" in style or "title" in style:
            size, bold = 16.0, True
        elif "heading" in style:
            size, bold = 11.5, True
        indent = 54.0
        if para.paragraph_format.left_indent is not None:
            indent += para.paragraph_format.left_indent.pt
        styled = _docx_runs(para)
        if "list" in style and not BULLET_RE.match(text):
            text = "• " + text
            if styled:
                styled.insert(0, Run(text="• ", bold=False, italic=False))
        doc.lines.append(
            Line(
                text=text,
                x0=indent,
                x1=558.0,
                top=y,
                bottom=y + size * 1.35,
                size=size,
                font="Helvetica-Bold" if bold else "Helvetica",
                bold=bold,
                italic=italic,
                page=0,
                text_x0=indent,
                runs=styled,
            )
        )
        y += size * 1.7
    return doc


def extract_txt(path: str) -> ExtractedDoc:
    doc = ExtractedDoc()
    y = 54.0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            text = raw.rstrip()
            if not text.strip():
                y += 6
                continue
            stripped = text.lstrip()
            indent = 54.0 + (len(text) - len(stripped)) * 3.0
            # ALL-CAPS short lines are almost always section headings.
            is_head = stripped.isupper() and len(stripped) < 40
            size = 11.5 if is_head else 10.5
            doc.lines.append(
                Line(
                    text=stripped,
                    x0=indent,
                    x1=558.0,
                    top=y,
                    bottom=y + size * 1.35,
                    size=size,
                    font="Helvetica",
                    bold=is_head,
                    italic=False,
                    page=0,
                    text_x0=indent,
                )
            )
            y += size * 1.7
    return doc


def extract(path: str) -> ExtractedDoc:
    low = path.lower()
    if low.endswith(".pdf"):
        return extract_pdf(path)
    if low.endswith(".docx"):
        return extract_docx(path)
    return extract_txt(path)

"""Re-render a parsed resume, mirroring the original document's layout.

Text lengths change when bullets are rewritten, so absolute positions cannot be
replayed. Instead we reflow top-to-bottom while preserving the things that make
a resume look like itself: page size, margins, font family, sizes, weights
(including weight changes inside a line), indents, bullet glyphs, right-aligned
tab stops, section rules, justification and vertical rhythm.

A resume is always emitted as a single page. If the faithful layout overflows,
leading and inter-block spacing are tightened -- and only then the font scaled --
until it fits.
"""

from __future__ import annotations

import difflib
import io
import re
from datetime import date
from pathlib import Path
from typing import NamedTuple

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfgen import canvas

from .. import obs
from ..parsing.resume import HARD_EXCLUDE_HINTS, section_has

SERIF_HINTS = (
    "times",
    "serif",
    "roman",
    "georgia",
    "garamond",
    "cambria",
    "minion",
    "book",
    "palatino",
    "century",
)
MONO_HINTS = ("courier", "mono", "consolas")

SAFE_BULLET = "\u2022"


class Word(NamedTuple):
    """One word plus everything needed to draw it: style, link, colour."""

    text: str
    font: str
    uri: str = ""
    color: str = ""
    underline: bool = False


# A bare URL that the source wrote out as plain text carries no annotation, so
# there is nothing to preserve -- but it is still a link the reader expects to
# click. Without a scheme or a "www.", both an alphabetic last label and a path
# are required, which is what keeps ordinary dotted words ("socket.io",
# "Node.js") and a GPA ("3.9/4.0") from being turned into links.
AUTOLINK_RE = re.compile(r"^(?:https?://|www\.)\S+$|^[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}/\S+$", re.I)
# A typed-out email is a link too; the source's own mail client would open it.
EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}$", re.I)
# Trailing sentence punctuation is part of the prose, not of the URL.
_URL_TRIM = ".,;:)]}>\u2019\"'"


def _autolink(word: str) -> str:
    """The URL a plain-text word points at, or "" if it is not one."""
    candidate = word.rstrip(_URL_TRIM)
    if EMAIL_RE.match(candidate):
        return "mailto:" + candidate
    if not AUTOLINK_RE.match(candidate):
        return ""
    if candidate.lower().startswith(("http://", "https://")):
        return candidate
    return "https://" + candidate


def _rgb(color: str) -> tuple[float, float, float] | None:
    """Turn "#rrggbb" into ReportLab's 0..1 triple."""
    if not color or not color.startswith("#") or len(color) != 7:
        return None
    try:
        return tuple(int(color[i : i + 2], 16) / 255.0 for i in (1, 3, 5))  # type: ignore[return-value]
    except ValueError:
        return None


# A round bullet is not in the base-14 Type1 fonts: ReportLab silently
# substitutes ZapfDingbats, which extracts as a stray "l" at the head of every
# bullet -- exactly what an ATS reads. Vera ships with ReportLab, so it is
# always available, and it carries a real U+2022 that extracts as itself.
# (The source resumes do the same thing: Times body text, Arial bullet.)
_BULLET_FONT: str | None = None


def _bullet_font() -> str:
    """The registered Vera, or Times-Roman if it cannot be loaded.

    Resolved once either way: a missing font stays missing, and retrying it
    cost a failed font search for every bullet of every fit probe.
    """
    global _BULLET_FONT
    if _BULLET_FONT is None:
        try:
            from reportlab.pdfbase.ttfonts import TTFont

            pdfmetrics.registerFont(TTFont("ResumapBullet", "Vera.ttf"))
            _BULLET_FONT = "ResumapBullet"
        except Exception as exc:  # noqa: BLE001 - fall back to a WinAnsi glyph
            _BULLET_FONT = "Times-Roman"
            obs.warn("render.bullet_font_missing", error=type(exc).__name__, using=_BULLET_FONT)
    return _BULLET_FONT


# Vera has no U+25CF, so a source drawn with a heavy "●" (Word's default
# list bullet, set in Arial) gets Vera's "•" enlarged to the same painted
# size instead: its dot is 0.28em wide and centred 0.38em up, against 0.42em
# and 0.28em for Arial's. (scale, dx, dy) are in multiples of the text size.
HEAVY_BULLETS = "●■▪⚫"
HEAVY_BULLET_FIT = (1.49, -0.13, -0.283)

# Vertical room a section rule takes, as a multiple of the text size: a gap
# above it and a smaller one below.
RULE_ABOVE, RULE_BELOW = 0.26, 0.16
RULE_SPACE = RULE_ABOVE + RULE_BELOW

# How far the spaces of a line may be squeezed, as a share of a normal space,
# before a word is pushed to the next line. The base-14 Times is a hair wider
# than Times New Roman, so a line the source fitted exactly would otherwise
# wrap one word over and grow the page by a line.
MAX_SQUEEZE = 0.08

FAMILIES = {
    "Helvetica": ("Helvetica", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica-BoldOblique"),
    "Times": ("Times-Roman", "Times-Bold", "Times-Italic", "Times-BoldItalic"),
    "Courier": ("Courier", "Courier-Bold", "Courier-Oblique", "Courier-BoldOblique"),
}

# Progressively tighter layouts: (leading, inter-block spacing, font size).
# Spacing is surrendered well before type size, because whitespace is far less
# noticeable than a shrunken font.
# Never shrink type past this: below it a resume stops being readable, and the
# honest answer is that the content will not fit on one page.
MIN_FONT_SCALE = 0.62

FIT_LADDER = (
    (1.00, 1.00, 1.00),
    (0.99, 0.85, 1.00),
    (0.97, 0.70, 1.00),
    (0.95, 0.55, 1.00),
    (0.93, 0.45, 1.00),
    (0.92, 0.38, 0.98),
    (0.90, 0.32, 0.96),
    (0.89, 0.26, 0.94),
    (0.87, 0.22, 0.92),
    (0.86, 0.18, 0.90),
    (0.85, 0.15, 0.88),
    (0.84, 0.12, 0.86),
)


def _family(fontname: str) -> str:
    n = (fontname or "").lower()
    if any(h in n for h in MONO_HINTS):
        return "Courier"
    # "sans-serif" contains "serif", so rule it out first.
    if "sans" in n or "grotesk" in n:
        return "Helvetica"
    if any(h in n for h in SERIF_HINTS):
        return "Times"
    return "Helvetica"


def _font(fontname: str, bold: bool, italic: bool) -> str:
    reg, b, i, bi = FAMILIES[_family(fontname)]
    if bold and italic:
        return bi
    if bold:
        return b
    if italic:
        return i
    return reg


def slugify(value: str, fallback: str = "company") -> str:
    """Lower-case letters and digits only, or `fallback` if nothing is left."""
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower()) or fallback


def export_filename(first_name: str, company: str, when: date | None = None) -> str:
    when = when or date.today()
    return f"{slugify(first_name, 'resume')}_{slugify(company)}_{when.strftime('%m%d')}.pdf"


# The adjustments a user can ask for on top of the mirrored layout (see
# llm/gateway.ModelGateway.revise_pdf). Font, leading and spacing multiply the
# source's own values; margins replace them, in points; justify overrides the
# alignment read from the source. The fit ladder still applies on top of all
# of it.
LAYOUT_DEFAULTS = {
    "font": 1.0,
    "leading": 1.0,
    "spacing": 1.0,
    "margin_x": None,
    "margin_y": None,
    "justify": "original",
}
LAYOUT_LIMITS = {
    "font": (0.7, 1.3),
    "leading": (0.7, 1.6),
    "spacing": (0.0, 2.5),
    "margin_x": (18.0, 108.0),
    "margin_y": (18.0, 108.0),
}


def clean_layout(layout: dict | None) -> dict:
    """Clamp a stored layout to values the renderer can honour."""
    out = dict(LAYOUT_DEFAULTS)
    for k, v in (layout or {}).items():
        if k == "justify":
            if v in ("original", "on", "off"):
                out[k] = v
        elif k in LAYOUT_LIMITS and v is not None:
            lo, hi = LAYOUT_LIMITS[k]
            try:
                out[k] = max(lo, min(hi, float(v)))
            except (TypeError, ValueError):
                pass
    return out


def gap_extra(prev: dict, cur: dict, doc_leading: float, body: float) -> float:
    """The block spacing the source put between two consecutive blocks, in points.

    The raw gap includes the ordinary gap between any two lines, which the
    leading already pays for, and the room around a section rule, which the
    rule pays for. Counting either again made a faithful re-render of an
    untouched one-page resume 12% taller than the original, so every export had
    to be squeezed to fit.
    """
    pl = prev.get("_line") or {}
    cl = cur.get("_line") or {}
    gap = float(cl.get("top", 0)) - float(pl.get("bottom", 0))
    psize = float(pl.get("size", body))
    normal = max(0.0, (doc_leading or psize * 1.2) - psize)
    return gap - normal - (psize * RULE_SPACE if prev.get("rule_below") else 0.0)


class ResumeRenderer:
    def __init__(
        self,
        doc: dict,
        leading_scale: float = 1.0,
        space_scale: float = 1.0,
        font_scale: float = 1.0,
        layout: dict | None = None,
    ):
        lay = clean_layout(layout)
        g = doc.get("geometry") or {}
        self.width = float(g.get("width") or 612.0)
        self.height = float(g.get("height") or 792.0)
        # Indents and centring are read against the source's own margin, so a
        # changed margin moves the whole column instead of eating the indents.
        self.src_ml = float(g.get("left") or 54.0)
        self.ml = lay["margin_x"] or self.src_ml
        self.mr = lay["margin_x"] or float(g.get("right") or 54.0)
        self.mt = lay["margin_y"] or float(g.get("top") or 54.0)
        self.mb = lay["margin_y"] or float(g.get("bottom") or 40.0)
        self.body = float(doc.get("body_size") or 10.5)
        # Replay the source document's own line spacing where we measured it.
        self.doc_leading = float(doc.get("leading") or 0.0)
        self.blocks = doc.get("blocks") or []
        self.right_edge = self.width - self.mr
        # Where the source's text column ended: a wrapped line that reached it
        # was justified, whatever the per-line stretch detector concluded.
        self.src_right = max(
            (float((b.get("_line") or {}).get("x1", 0)) for b in self.blocks),
            default=self.right_edge,
        )
        # Documents parsed before underlines were recorded carry no flag at
        # all; for those, a coloured link is the best evidence of one.
        self.legacy_underline = not any(
            "underline" in r for b in self.blocks for r in (b.get("runs") or [])
        )
        self.leading_scale = leading_scale * lay["leading"]
        self.space_scale = space_scale * lay["spacing"]
        self.font_scale = font_scale * lay["font"]
        self.justify_mode = lay["justify"]
        self.pages = 1
        # Total vertical space the content wants, ignoring page breaks. Lets a
        # caller compute the scale needed to fit rather than search for it.
        self.content_height = 0.0
        # Per block: lines drawn and roughly how many characters fill one.
        self.stats: dict[str, dict] = {}

    # -- geometry helpers ---------------------------------------------------

    def _size(self, blk: dict) -> float:
        raw = float((blk.get("_line") or {}).get("size", self.body))
        return max(6.0, raw * self.font_scale)

    def _indent(self, blk: dict) -> float:
        x0 = float((blk.get("_line") or {}).get("x0", self.src_ml))
        return max(0.0, min(x0 - self.src_ml, (self.width - self.ml - self.mr) * 0.5))

    def _align(self, blk: dict) -> str:
        ln = blk.get("_line") or {}
        x0 = float(ln.get("x0", self.src_ml))
        x1 = float(ln.get("x1", self.right_edge))
        centre = (x0 + x1) / 2
        if abs(centre - self.width / 2) < 20 and (x0 - self.src_ml) > 15:
            return "center"
        return "left"

    def _justified(self, blk: dict) -> bool:
        """Whether a bullet or paragraph was set flush to both margins.

        The parser's stretch test only fires on visibly wide gaps, so a
        justified line that needed little stretch reads as ragged and the
        export came out with a ragged summary and skills block. Geometry is
        the surer sign: a wrapped block whose widest line reaches the source's
        text column edge was justified.
        """
        if blk.get("kind") not in ("bullet", "para"):
            return False
        if self.justify_mode != "original":
            return self.justify_mode == "on"
        if blk.get("justified"):
            return True
        ln = blk.get("_line") or {}
        size = float(ln.get("size", self.body))
        wrapped = float(ln.get("bottom", 0)) - float(ln.get("top", 0)) > size * 1.5
        return wrapped and self.src_right - float(ln.get("x1", 0)) <= max(2.0, size * 0.2)

    def _source_lines(self, blk: dict) -> int:
        """How many lines the block took in the uploaded document."""
        ln = blk.get("_line") or {}
        step = self.doc_leading or float(ln.get("size", self.body)) * 1.2
        span = float(ln.get("bottom", 0)) - float(ln.get("top", 0))
        return max(1, round(span / step))

    def _space_before(self, blk: dict, prev: dict | None) -> float:
        """Space above a block. `prev` is the last block that came from the
        source, so a spliced-in bullet never disturbs the gaps around it."""
        size = self._size(blk)
        if blk.get("inserted"):
            # The source never measured a gap for it; insert_blocks worked out
            # which one it should copy.
            extra = float(blk.get("_extra") or 0.0)
        elif prev is None:
            return 0.0
        elif (prev.get("_line") or {}).get("page") != (blk.get("_line") or {}).get("page"):
            return size * 0.5 * self.space_scale
        else:
            extra = gap_extra(prev, blk, self.doc_leading, self.body)
        # Clamp so a stray extraction artifact cannot blow a hole in the page.
        return max(0.0, min(extra, size * 2.2)) * self.space_scale

    # -- styled text --------------------------------------------------------

    @staticmethod
    def _block_font(blk: dict) -> str:
        """The base-14 face of the block's dominant style."""
        ln = blk.get("_line") or {}
        return _font(ln.get("font", ""), bool(ln.get("bold")), bool(ln.get("italic")))

    def _styled_words(self, blk: dict, which: str = "runs") -> list[Word]:
        """Flatten a block's runs into drawable words.

        A block with no runs (plain-text uploads, inserted bullets, a repaired
        block with a right column) takes the block's dominant style instead.

        A run's link is inherited by every word in it; a word the source left as
        bare URL text gets one synthesized, so a link is restored whether the
        original expressed it as an annotation or just typed it out.
        """
        ln = blk.get("_line") or {}
        base = ln.get("font", "")
        runs = blk.get(which) or []
        if which == "runs" and not runs:
            runs = [
                {
                    "text": blk.get("left") or blk.get("text", ""),
                    "bold": bool(ln.get("bold")),
                    "italic": bool(ln.get("italic")),
                }
            ]
        out: list[Word] = []
        for r in runs:
            f = _font(base, bool(r.get("bold")), bool(r.get("italic")))
            uri = str(r.get("uri") or "")
            color = str(r.get("color") or "")
            under = bool(r.get("underline")) or (self.legacy_underline and bool(uri and color))
            for w in str(r.get("text", "")).split():
                out.append(Word(w, f, uri or _autolink(w), color, under))
        return out

    def _columns(self, blk: dict) -> tuple[list[Word], list[Word]]:
        """The block's left-hand words and its right-aligned column.

        A date recovered from the text rather than from a tab stop has no
        runs of its own: the parser left it inside the left-hand runs. Peel it
        back off, or it is drawn inline and loses its alignment ("...
        Example University July 2021 - May 2023").
        """
        left = self._styled_words(blk)
        if not blk.get("right"):
            return left, []
        right = self._styled_words(blk, "right_runs")
        if right:
            return left, right
        tail = str(blk["right"]).split()
        n = len(tail)
        if n and len(left) > n and [w.text for w in left[-n:]] == tail:
            return left[:-n], left[-n:]
        f = self._block_font(blk)
        return left, [Word(w, f) for w in tail]

    @staticmethod
    def _restyle(text: str, source: list[Word], font: str) -> list[Word]:
        """Words for replacement text, keeping the source's styling where it survives.

        A replacement is plain text, so without this a rewritten skills line
        loses its bold label and a corrected contact line loses its links.
        Words the replacement kept verbatim take their original style: runs of
        two or more, the opening word, and any linked word. A lone common word
        ("and") is not trusted to be the same word as a bold one elsewhere.
        """
        words = text.split()
        out = [Word(w, font, _autolink(w)) for w in words]
        sm = difflib.SequenceMatcher(a=[w.text for w in source], b=words, autojunk=False)
        for a0, b0, n in sm.get_matching_blocks():
            for k in range(n):
                src = source[a0 + k]
                if n >= 2 or src.uri or (a0 == 0 and b0 == 0):
                    out[b0 + k] = src
        return out

    @staticmethod
    def _words_width(words: list[Word], size: float) -> float:
        if not words:
            return 0.0
        total = sum(pdfmetrics.stringWidth(w.text, w.font, size) for w in words)
        total += sum(pdfmetrics.stringWidth(" ", w.font, size) for w in words[:-1])
        return total

    def _wrap_words(self, words: list[Word], size: float, width: float) -> list[list[Word]]:
        if width <= 0 or not words:
            return [words] if words else [[]]
        lines: list[list[Word]] = []
        cur: list[Word] = []
        cur_w = 0.0
        for word in words:
            ww = pdfmetrics.stringWidth(word.text, word.font, size)
            sp = pdfmetrics.stringWidth(" ", word.font, size)
            add = ww if not cur else ww + sp
            # `len(cur)` is the gap count once this word joins the line.
            if cur and cur_w + add > width + sp * MAX_SQUEEZE * len(cur):
                lines.append(cur)
                cur, cur_w = [word], ww
            else:
                cur.append(word)
                cur_w += add
        if cur:
            lines.append(cur)
        return lines

    @staticmethod
    def _link_spans(c, x: float, y: float, words: list[Word], size: float, extra: float) -> None:
        """Lay a clickable rectangle over each linked span of a drawn line,
        and rule under each underlined one.

        Word positions are re-derived with the same accumulation the text object
        used -- each word's glyph widths, then one space in that word's own font
        widened by the justification word-spacing -- so the annotation tracks the
        glyphs on a justified line as well as a ragged one.
        """
        spans: list[list] = []
        rules: list[list] = []
        cur = x
        prev = prev_rule = -2
        for i, w in enumerate(words):
            end = cur + pdfmetrics.stringWidth(w.text, w.font, size)
            if w.uri:
                # Extend the open span only across words that are adjacent and
                # point at the same place, so two links separated by a plain
                # word ("LinkedIn | Github") stay two rectangles.
                if spans and spans[-1][2] == w.uri and prev == i - 1:
                    spans[-1][1] = end
                else:
                    spans.append([cur, end, w.uri])
                prev = i
            if w.underline:
                if rules and prev_rule == i - 1 and rules[-1][2] == w.color:
                    rules[-1][1] = end
                else:
                    rules.append([cur, end, w.color])
                prev_rule = i
            cur = end + pdfmetrics.stringWidth(" ", w.font, size) + extra
        for x0, x1, uri in spans:
            c.linkURL(uri, (x0, y - size * 0.24, x1, y + size * 0.86), relative=0)
        for x0, x1, color in rules:
            # Where Word puts it: about an eighth of the size under the baseline.
            c.setStrokeColorRGB(*(_rgb(color) or (0.0, 0.0, 0.0)))
            c.setLineWidth(max(0.4, size * 0.05))
            c.line(x0, y - size * 0.13, x1, y - size * 0.13)
        if rules:
            c.setStrokeColorRGB(0.0, 0.0, 0.0)

    def _draw_words(
        self, c, x: float, y: float, words: list[Word], size: float, avail: float, justify: bool
    ) -> None:
        """Draw one line, keeping real space characters in the text layer.

        Placing each word at a computed x would justify just as well, but the
        resulting PDF contains no space glyphs at all -- text extraction (and
        therefore every ATS) reads "JaneMarieDoe". So the line is drawn
        as real strings in a text object and justified with the PDF word-spacing
        operator instead.
        """
        if not words:
            return
        natural = self._words_width(words, size)
        gaps = len(words) - 1
        space_w = pdfmetrics.stringWidth(" ", words[0].font, size)

        extra = 0.0
        if gaps > 0:
            wanted = (avail - natural) / gaps
            if wanted < 0:
                # A line the wrapper let run a little long is squeezed back
                # inside the margin, justified or not.
                extra = max(wanted, -space_w * MAX_SQUEEZE)
            elif justify and wanted <= space_w * 2.5:
                # Never stretch absurdly.
                extra = wanted

        t = c.beginText(x, y)
        # Always write the word-spacing operator, even when it is zero. Tw is
        # text state in PDF and survives BT/ET, so omitting it here let one
        # justified line stretch every line drawn after it and push 22 rows past
        # the right margin.
        t.setWordSpace(extra)
        i = 0
        while i < len(words):
            font, color = words[i].font, words[i].color
            chunk = [words[i].text]
            i += 1
            while i < len(words) and words[i].font == font and words[i].color == color:
                chunk.append(words[i].text)
                i += 1
            segment = " ".join(chunk)
            if i < len(words):
                segment += " "  # the space before a differently-styled run
            t.setFont(font, size)
            # Always set the colour, never just on the coloured segment: fill
            # colour is graphics state, so leaving it unset would bleed a link's
            # blue onto every word drawn after it.
            t.setFillColorRGB(*(_rgb(color) or (0.0, 0.0, 0.0)))
            t.textOut(segment)
        c.drawText(t)
        self._link_spans(c, x, y, words, size, extra)

    # -- rendering ----------------------------------------------------------

    def render(self, out=None, overrides: dict[str, str] | None = None) -> int:
        """Draw the resume. Returns the number of pages used.

        With no `out` this is a measuring pass: the layout runs exactly as it
        would and `pages`, `content_height` and `stats` come out the same, but
        the PDF is never serialised. The fit ladder probes this way.
        """
        overrides = overrides or {}
        c = canvas.Canvas(io.BytesIO() if out is None else out, pagesize=(self.width, self.height))
        if isinstance(out, (str, Path)):
            c.setTitle(Path(str(out)).stem)
        y = self.height - self.mt
        prev: dict | None = None
        self.pages = 1
        self.content_height = 0.0

        for blk in self.blocks:
            kind = blk.get("kind", "para")
            size = self._size(blk)
            ln = blk.get("_line") or {}
            if self.doc_leading > 0:
                leading = self.doc_leading * self.font_scale * self.leading_scale
            else:
                leading = size * 1.20 * self.leading_scale
            indent = self._indent(blk)
            x = self.ml + indent
            avail = self.right_edge - x

            space = self._space_before(blk, prev)
            y -= space
            self.content_height += space

            words, right_words = self._columns(blk)
            # Rewritten text replaces the block's own words. It is plain text,
            # so a word it kept verbatim takes its original style and anything
            # new takes the block's dominant one; a link is otherwise only
            # recognised from the words themselves.
            override = overrides.get(blk["id"])
            if override is not None:
                words = self._restyle(override, words, self._block_font(blk))

            if right_words:
                avail -= self._words_width(right_words, size) + size * 0.8

            hang = 0.0
            heavy = kind == "bullet" and str(blk.get("bullet_char") or "") in HEAVY_BULLETS
            if kind == "bullet":
                gfont = _bullet_font()
                glyph = pdfmetrics.stringWidth(SAFE_BULLET, gfont, size)
                hang = glyph + size * 0.52
                # Replay the source's own hanging indent: its text started a
                # measured distance past the bullet, usually wider than ours.
                src_hang = float(ln.get("text_x0", 0)) - float(ln.get("x0", 0))
                if glyph < src_hang < size * 4:
                    hang = src_hang * self.font_scale
                avail -= hang

            justify = self._justified(blk)
            lines = self._wrap_words(words, size, avail)
            text_len = len(" ".join(w.text for w in words))
            self.stats[blk["id"]] = {
                "lines": len(lines),
                "source_lines": 0 if blk.get("inserted") else self._source_lines(blk),
                "chars_per_line": round(avail * text_len / max(self._words_width(words, size), 1.0))
                if words
                else 0,
            }

            for i, line_words in enumerate(lines):
                if y - leading < self.mb:
                    c.showPage()
                    self.pages += 1
                    y = self.height - self.mt
                y -= leading
                self.content_height += leading

                if kind == "bullet":
                    if i == 0 and heavy:
                        scale, dx, dy = HEAVY_BULLET_FIT
                        c.setFont(_bullet_font(), size * scale)
                        c.drawString(x + size * dx, y + size * dy, SAFE_BULLET)
                    elif i == 0:
                        c.setFont(_bullet_font(), size)
                        c.drawString(x, y, SAFE_BULLET)
                    # The last line of a justified paragraph stays ragged.
                    self._draw_words(
                        c, x + hang, y, line_words, size, avail, justify and i < len(lines) - 1
                    )
                elif self._align(blk) == "center" and not right_words:
                    w = self._words_width(line_words, size)
                    self._draw_words(c, (self.width - w) / 2, y, line_words, size, w, False)
                else:
                    self._draw_words(
                        c, x, y, line_words, size, avail, justify and i < len(lines) - 1
                    )

                if i == 0 and right_words:
                    rw = self._words_width(right_words, size)
                    self._draw_words(c, self.right_edge - rw, y, right_words, size, rw, False)

            if blk.get("rule_below"):
                self.content_height += size * RULE_SPACE
                y -= size * RULE_ABOVE
                c.setLineWidth(0.6)
                c.line(self.ml, y, self.right_edge, y)
                y -= size * RULE_BELOW

            if not blk.get("inserted"):
                prev = blk

        if out is not None:
            c.save()
        return self.pages


def render_resume(
    doc: dict,
    out_path: str | None = None,
    overrides: dict[str, str] | None = None,
    layout: dict | None = None,
) -> dict:
    """Render to `out_path`, compressing as needed to land on one page.

    Returns the chosen scales so callers can tell the user when a resume had to
    be squeezed, and how hard, plus per-block line counts under `blocks`. With
    no `out_path` nothing is written: the same fit, for callers that only need
    to know how the page lays out.
    """
    layout = clean_layout(layout)
    probe: ResumeRenderer | None = None

    def pages_at(step: tuple[float, float, float]) -> int:
        nonlocal probe
        probe = ResumeRenderer(doc, *step, layout=layout)
        return probe.render(None, overrides)

    chosen = None
    probes: list[dict] = []
    for i, step in enumerate(FIT_LADDER):
        pages = pages_at(step)
        probes.append({"lead": step[0], "space": step[1], "font": step[2], "pages": pages})
        if pages == 1:
            chosen = step
            if i:
                chosen = _refine(FIT_LADDER[i - 1], step, pages_at)
            break

    if chosen is None:
        # The ladder bottomed out. Rather than keep guessing, measure how much
        # room the content wants and solve for the type size that fits, then
        # step down a little at a time to absorb re-wrapping (a smaller font
        # re-flows lines, which does not shrink height perfectly linearly).
        # The loop's last probe was the tightest rung, so its measurements are
        # already in hand.
        tight = FIT_LADDER[-1]
        assert probe is not None
        usable = probe.height - probe.mt - probe.mb
        needed = max(probe.content_height, 1.0)
        scale = min(1.0, usable / needed)
        obs.warn(
            "render.ladder_exhausted",
            usable_pt=round(usable, 1),
            needed_pt=round(needed, 1),
            solved_font_scale=round(scale, 3),
        )
        iterations = 0
        while scale >= MIN_FONT_SCALE:
            iterations += 1
            step = (tight[0], tight[1], round(scale, 3))
            if pages_at(step) == 1:
                chosen = step
                break
            scale -= 0.02
        if chosen is None:
            chosen = (tight[0], tight[1], MIN_FONT_SCALE)
            obs.warn(
                "render.cannot_fit_one_page",
                floor=MIN_FONT_SCALE,
                iterations=iterations,
                note="content exceeds one readable page; trim it",
            )
        else:
            obs.event("render.analytic_fit", font_scale=chosen[2], iterations=iterations)

    renderer = ResumeRenderer(doc, *chosen, layout=layout)
    with obs.timer() as t:
        pages = renderer.render(out_path, overrides)
    changed = sorted(k for k, v in layout.items() if v != LAYOUT_DEFAULTS[k])
    obs.event(
        "render.done",
        pages=pages,
        leading_scale=chosen[0],
        space_scale=chosen[1],
        font_scale=chosen[2],
        compressed=chosen != FIT_LADDER[0],
        cramped=chosen[2] < 0.85,
        blocks=len(doc.get("blocks") or []),
        overrides=len(overrides or {}),
        probes=len(probes),
        layout=changed or None,
        content_pt=round(renderer.content_height, 1),
        ms=t.ms,
        ladder=[f"{x['lead']}/{x['space']}/{x['font']}:{x['pages']}p" for x in probes[-4:]],
    )
    return {
        "path": out_path,
        "pages": pages,
        "leading_scale": chosen[0],
        "space_scale": chosen[1],
        "font_scale": chosen[2],
        "compressed": chosen != FIT_LADDER[0],
        # Below this the type is small enough that the user should be told to
        # cut content rather than trust the shrink.
        "cramped": chosen[2] < 0.85,
        "layout": layout,
        "blocks": renderer.stats,
    }


def _refine(loose: tuple, tight: tuple, pages_at, rounds: int = 6) -> tuple:
    """The loosest layout between two ladder rungs that still fits one page.

    The rungs are coarse: stepping from one that overflows by a single line to
    the next can free far more room than needed, leaving a band of white at the
    foot of the page and the type needlessly tight. Bisecting between them
    costs a few ~10ms probes and lands within about 2% of the rung spacing.
    """
    best = tight
    lo, hi = 0.0, 1.0
    for _ in range(rounds):
        mid = (lo + hi) / 2
        step = tuple(round(a + (b - a) * mid, 4) for a, b in zip(loose, tight, strict=True))
        if pages_at(step) == 1:
            best, hi = step, mid
        else:
            lo = mid
    return best


# Greedy wrapping leaves on average about half a word unused per line.
WRAP_WASTE = 4


def _line_stats(doc: dict, layout: dict | None) -> dict[str, dict]:
    r = ResumeRenderer(doc, layout=layout)
    r.render(None, {})
    return r.stats


def _block_budgets(stats: dict[str, dict]) -> dict[str, int]:
    return {
        bid: max(0, st["source_lines"] * (st["chars_per_line"] - WRAP_WASTE))
        for bid, st in stats.items()
    }


def _bullet_budget(doc: dict, stats: dict[str, dict], lines: int) -> int:
    bullets = {b["id"] for b in doc.get("blocks") or [] if b["kind"] == "bullet"}
    cpl = sorted(st["chars_per_line"] for bid, st in stats.items() if bid in bullets)
    return lines * (cpl[len(cpl) // 2] - WRAP_WASTE) if cpl else 0


def line_budget(doc: dict, layout: dict | None = None) -> dict[str, int]:
    """Characters each block can hold without taking more lines than the source gave it.

    Measured with the renderer's own metrics at full size, so a rewrite held to
    this lands on the same number of lines the original occupied.
    """
    return _block_budgets(_line_stats(doc, layout))


def new_bullet_budget(doc: dict, layout: dict | None = None, lines: int = 2) -> int:
    """Characters a brand-new bullet may use to stay within `lines` lines."""
    return _bullet_budget(doc, _line_stats(doc, layout), lines)


class Budgets(NamedTuple):
    """Both length budgets, from one measuring pass."""

    lines: dict[str, int]  # per block, as line_budget
    new_bullet: int  # as new_bullet_budget with its default two lines


def budgets(doc: dict, layout: dict | None = None) -> Budgets:
    """line_budget and new_bullet_budget together, for the price of one render."""
    stats = _line_stats(doc, layout)
    return Budgets(_block_budgets(stats), _bullet_budget(doc, stats, 2))


# ---------------------------------------------------------------------------
# New bullets from the experience library
# ---------------------------------------------------------------------------


def _attribute_section(name: str) -> bool:
    """Skills, tools, certifications: a list of attributes, conventionally last."""
    return section_has(name, HARD_EXCLUDE_HINTS)


def insert_blocks(doc: dict, inserts: list[dict]) -> dict:
    """A copy of `doc` with new bullets spliced in where they were placed.

    Each insert is {"id", "text", "placement"}. A placement is the id of an
    entry (the bullet joins the end of that entry's bullets), the id of a
    section (the end of that section), or "new:<heading>", a section of its
    own set in front of a trailing skills-type section so Skills stays last.
    An unknown id falls back to a new section rather than losing the bullet.

    Spliced blocks borrow a neighbouring bullet's or heading's geometry, so
    they render in the resume's own style, and carry in `_extra` the block
    spacing they should have, since the source never measured any for them.
    """
    if not inserts:
        return doc
    blocks = doc.get("blocks") or []
    n = len(blocks)
    index = {b["id"]: i for i, b in enumerate(blocks)}
    lead = float(doc.get("leading") or 0.0)
    body = float(doc.get("body_size") or 10.5)
    first_bullet = next((b for b in blocks if b["kind"] == "bullet"), None)
    sections = [i for i, b in enumerate(blocks) if b["kind"] == "section"]

    def end_of(i: int, stops: tuple[str, ...]) -> int:
        j = i + 1
        while j < n and blocks[j]["kind"] not in stops:
            j += 1
        return j

    def bullet(
        ins: dict, template: dict | None, anchor: dict | None, section: str, extra: float
    ) -> dict:
        if template:
            line = dict(template["_line"])
        else:
            # A resume with no bullets at all: indent under whatever it follows.
            base = dict((anchor or blocks[-1])["_line"])
            x0 = float(base.get("x0", 54.0)) + body
            line = {**base, "x0": x0, "text_x0": x0 + body * 1.3, "bold": False, "italic": False}
        return {
            "id": ins["id"],
            "kind": "bullet",
            "section": section,
            "text": ins["text"],
            "left": "",
            "right": "",
            "bullet_char": (template or {}).get("bullet_char") or SAFE_BULLET,
            "rule_below": False,
            "mappable": True,
            "reason": None,
            "runs": [],
            "right_runs": [],
            "justified": bool((template or {}).get("justified")),
            "inserted": True,
            "_extra": extra,
            "_line": line,
        }

    before: dict[int, list[dict]] = {}
    groups: dict[str, list[dict]] = {}
    for ins in inserts:
        place = str(ins.get("placement") or "")
        i = index.get(place)
        if place.startswith("new:") or i is None or blocks[i]["kind"] not in ("entry", "section"):
            heading = place[4:].strip() if place.startswith("new:") else ""
            groups.setdefault(heading or "ADDITIONAL EXPERIENCE", []).append(ins)
            continue
        anchor = blocks[i]
        pos = end_of(i, ("entry", "section") if anchor["kind"] == "entry" else ("section",))
        template = next((b for b in blocks[i + 1 : pos] if b["kind"] == "bullet"), first_bullet)
        section = anchor["text"] if anchor["kind"] == "section" else anchor.get("section", "")
        before.setdefault(pos, []).append(bullet(ins, template, blocks[pos - 1], section, 0.0))

    if groups:
        tail = (
            sections[-1]
            if len(sections) > 1 and _attribute_section(blocks[sections[-1]]["text"])
            else n
        )
        model = blocks[sections[-1]] if sections else None
        # A new heading copies the spacing the source gives the section it now
        # precedes (or its last section); its first bullet copies the gap that
        # section leaves under its own heading.
        ref = tail if tail < n else (sections[-1] if sections else None)
        head_extra = gap_extra(blocks[ref - 1], blocks[ref], lead, body) if ref else body
        under_extra = (
            gap_extra(blocks[ref], blocks[ref + 1], lead, body)
            if ref is not None and ref + 1 < n
            else 0.0
        )
        caps = model is not None and model["text"].upper() == model["text"]
        for k, (heading, group) in enumerate(groups.items(), start=1):
            head_line = (
                dict(model["_line"])
                if model
                else {
                    **dict(blocks[-1]["_line"]),
                    "bold": True,
                    "x0": float((doc.get("geometry") or {}).get("left") or 54.0),
                }
            )
            before.setdefault(tail, []).append(
                {
                    "id": f"new{k:03d}",
                    "kind": "section",
                    "section": "",
                    "text": heading.upper() if caps else heading,
                    "left": "",
                    "right": "",
                    "bullet_char": "",
                    "rule_below": bool(model and model.get("rule_below")),
                    "mappable": False,
                    "reason": "inserted",
                    "runs": [],
                    "right_runs": [],
                    "justified": False,
                    "inserted": True,
                    "_extra": head_extra,
                    "_line": head_line,
                }
            )
            for j, ins in enumerate(group):
                before[tail].append(
                    bullet(ins, first_bullet, None, heading, under_extra if j == 0 else 0.0)
                )

    out: list[dict] = []
    for i in range(n + 1):
        out.extend(before.get(i, []))
        if i < n:
            out.append(blocks[i])
    return {**doc, "blocks": out}

"""`doc prep <pdf>`: the deterministic base layer of one datasheet, RM or PRD.

Writes under `docs/<doc_id>/`:
  doc.json             identity: file, sha256, kind, part, doc_no, rev, pages, printed labels that differ;
                       text_layer and pages_textless (`text_layer()`, shared with `inventory`)
  pages/p<NNN>.txt     `pdftotext -layout` of page NNN (1-based physical page), raw, glyphs untouched
  pages/p<NNN>.bbox.json  word boxes (PyMuPDF), with --bbox
  pages/pages.json     per page: tokens, body words and image share (running header, footer and
                       logo left out), classes and the evidence behind them, sha256 of the text
  toc.json, toc.md     the PDF outline as sections (document numbering or title slug), page
                       ranges, tokens, tier-2 file; then the pages that need an image Read, the
                       overlined (active-low) names per page, and the page classes

The text layer is the numeric oracle for every later check, so it is stored raw.
`normalize()` gives the forms the checker compares: Symbol-font private-use glyphs
(U+F057 Ohm, U+F0A3 <=, ...) mapped to real glyphs, or stripped, since the Read tool
shows them as nothing and a quote typed from it lacks them.
"""
from __future__ import annotations

import bisect
import math
import os
import re
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any

from . import __version__, common

# ---------------------------------------------------------------- glyphs and normalization

# Adobe Symbol encoding (Word exports put it at U+F020..U+F0FF, private use).
_SYMBOL_LETTERS = dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
                           "ΑΒΧΔΕΦΓΗΙϑΚΛΜΝΟΠΘΡΣΤΥςΩΞΨΖαβχδεφγηιϕκλμνοπθρστυϖωξψζ"))
_SYMBOL_SPECIAL = {0x22: "∀", 0x24: "∃", 0x27: "∋", 0x2D: "−", 0x40: "≅", 0x5E: "⊥", 0x7E: "∼",
                   0xA2: "′", 0xA3: "≤", 0xA5: "∞", 0xAC: "←", 0xAD: "↑", 0xAE: "→", 0xAF: "↓",
                   0xB0: "°", 0xB1: "±", 0xB2: "″", 0xB3: "≥", 0xB4: "×", 0xB5: "∝", 0xB6: "∂",
                   0xB7: "•", 0xB8: "÷", 0xB9: "≠", 0xBA: "≡", 0xBB: "≈", 0xD6: "√", 0xD7: "⋅",
                   0xE5: "∑", 0xF2: "∫"}
PUA_MAP: dict[str, str] = {}
for _code in range(0x20, 0x100):
    _ch = chr(_code)
    if _code in _SYMBOL_SPECIAL:
        PUA_MAP[chr(0xF000 + _code)] = _SYMBOL_SPECIAL[_code]
    elif _ch in _SYMBOL_LETTERS:
        PUA_MAP[chr(0xF000 + _code)] = _SYMBOL_LETTERS[_ch]
    elif 0x20 <= _code < 0x7F:
        PUA_MAP[chr(0xF000 + _code)] = _ch

_DASHES = str.maketrans({"–": "-", "−": "-", "—": "-", "‐": "-", "‑": "-", "μ": "µ",
                         "“": '"', "”": '"', "’": "'", "‘": "'", " ": " "})


def is_pua(c: str) -> bool:
    return "" <= c <= ""


def map_pua(s: str) -> str:
    return "".join(PUA_MAP.get(c, c) for c in s)


SOFT_HYPHEN = "\u00ad"
OVERLINE = "\u0305"


def normalize(s: str, pua: str = "map", hyphen: str = "keep") -> str:
    """Comparison form: PUA glyphs mapped ("map") or dropped ("strip"), NFKC, no combining marks
    (overlines), one dash, straight quotes, collapsed whitespace, no space before , ; : ) ] or after
    ( [, lower case, and a word broken over a line rejoined.

    Both text sources break words the same two ways, and both are joined the same way: a soft
    hyphen (U+00AD, what PyMuPDF gives for a word-processor line break) is dropped with the line
    break after it ("cali\\xad\\nbration" -> "calibration"), and a hard hyphen at the end of a
    line loses the break and, with `hyphen="keep"`, keeps the hyphen ("auto-\\n retry" ->
    "auto-retry"); with `hyphen="drop"` it goes too ("re-\\n quested" -> "requested"). The text
    layer cannot tell a hyphenated word from a compound broken at its own hyphen, so a page is
    compared in both forms. A soft hyphen inside a line is invisible, so it is dropped too."""
    if pua == "map":
        s = map_pua(s)
    elif pua == "strip":
        s = "".join(c for c in s if not is_pua(c))
    s = re.sub(SOFT_HYPHEN + r"[ \t]*(?:\r?\n\s*)?", "", s)
    s = unicodedata.normalize("NFKC", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.translate(_DASHES).lower()
    s = re.sub(r"(?<=[a-z])-[ \t]*\r?\n\s*(?=[a-z])", "" if hyphen == "drop" else "-", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r" (?=[,;:)\]])|(?<=[(\[]) ", "", s)   # "100 ," == "100," once a glyph between them is gone
    return re.sub(r"(?<=[a-z])- (?=[a-z])", "-", s)


# ---------------------------------------------------------------- text layer

def pdftotext_version() -> str:
    try:
        r = subprocess.run(["pdftotext", "-v"], capture_output=True, text=True)
        return (r.stderr or r.stdout).splitlines()[0].strip()
    except (OSError, IndexError):
        return "unknown"


def layout_pages(pdf: Path, n: int) -> list[str]:
    """`pdftotext -layout` per page, 1-based list index 0 = page 1. One call, split on form feeds."""
    r = subprocess.run(["pdftotext", "-layout", "-enc", "UTF-8", str(pdf), "-"],
                       capture_output=True, text=True, check=True)
    parts = r.stdout.split("\f")
    if len(parts) >= n and all(not p.strip() for p in parts[n:]):
        return parts[:n]
    out = []
    for i in range(1, n + 1):  # page count and form feeds disagree: fall back to one call per page
        r = subprocess.run(["pdftotext", "-layout", "-enc", "UTF-8", "-f", str(i), "-l", str(i), str(pdf), "-"],
                           capture_output=True, text=True, check=True)
        out.append(r.stdout.rstrip("\f"))
    return out


def page_file(docdir: Path, p: int, suffix: str = ".txt") -> Path:
    return Path(docdir) / "pages" / f"p{p:03d}{suffix}"


# ---------------------------------------------------------------- text-layer classification (shared with inventory)

MIN_WORDS = 20          # fewer body words than this: the page has no usable text layer
IMAGE_MIN_FRAC = 0.02   # content images covering less of the page than this are icons or bullets
MARGIN = 0.12           # running headers and footers sit in the top or bottom 12% of the page
DRAWING_MARGIN = 0.10   # vector paths entirely inside the top or bottom 10% are header rules and logos
RUNNING_SHARE = 0.5     # a header line or image repeated on half the pages (and on 3 or more) is running


def page_layer(page: Any) -> dict:
    """What the text-layer classification needs from one PyMuPDF page: its size, its words
    (x0, y0, x1, y1, text, block, line, word), and the rectangles of its images and vector paths."""
    words = page.get_text("words")
    images, paths = [], []
    try:
        for kind, rect in page.get_bboxlog():
            if "image" in kind or "imgmask" in kind:
                r = page.rect & rect
                if r.width > 0 and r.height > 0:
                    images.append(tuple(r))
            elif "path" in kind:
                paths.append(tuple(rect))
    except (AttributeError, RuntimeError):
        images = [tuple(i["bbox"]) for i in page.get_image_info()]
    return {"w": page.rect.width, "h": page.rect.height, "words": words, "images": images, "paths": paths}


def text_layer(layers: list[dict]) -> dict:
    """The one text-layer classification that `inventory` and `doc prep` share.

    A running header or footer is ignored: a line in the top or bottom 12% of the page whose text
    (digits read as `#`, so page numbers match) repeats on at least half the pages, and an image
    drawn at the same place on at least half the pages (a logo). So a logo on every page does not
    make every page "has an image", and a one-line header does not make an image page "text".

    Per page: `words` (body words), `running_words`, `image_frac` (content images' share of the
    page), `running_images`, and `body` (the body word tuples, for doc prep). A page is
    **textless** when it has fewer than MIN_WORDS body words and content images covering at least
    IMAGE_MIN_FRAC of it (an image with no text layer), **near-empty** when it has too few words and
    no image. `text_layer` is none (no body words at all), partial (some textless page) or full."""
    n = len(layers)
    need = max(3, math.ceil(RUNNING_SHARE * n))
    line_keys: list[dict] = []
    img_keys: list[list] = []
    seen_lines: dict[tuple, int] = {}
    seen_imgs: dict[tuple, int] = {}
    for L in layers:
        h = L["h"] or 1.0
        lines: dict[tuple, list[int]] = {}
        for k, w in enumerate(L["words"]):
            lines.setdefault((w[5], w[6]) if len(w) > 6 else (k, 0), []).append(k)
        keys: dict[tuple, list[int]] = {}
        for idx in lines.values():
            y0 = min(L["words"][k][1] for k in idx)
            y1 = max(L["words"][k][3] for k in idx)
            band = "top" if y1 <= MARGIN * h else ("bottom" if y0 >= (1 - MARGIN) * h else None)
            if band:
                text = re.sub(r"\d+", "#", " ".join(L["words"][k][4] for k in idx))
                keys.setdefault((band, text), []).extend(idx)
        line_keys.append(keys)
        for key in keys:
            seen_lines[key] = seen_lines.get(key, 0) + 1
        ik = [tuple(round(v / 2) for v in r) for r in L["images"]]
        img_keys.append(ik)
        for key in set(ik):
            seen_imgs[key] = seen_imgs.get(key, 0) + 1
    run_lines = {k for k, c in seen_lines.items() if c >= need}
    run_imgs = {k for k, c in seen_imgs.items() if c >= need}
    pages = []
    for L, keys, ik in zip(layers, line_keys, img_keys):
        drop = {k for key, idx in keys.items() if key in run_lines for k in idx}
        body = [w for k, w in enumerate(L["words"]) if k not in drop]
        area = (L["w"] * L["h"]) or 1.0
        content = [r for r, key in zip(L["images"], ik) if key not in run_imgs]
        frac = min(1.0, sum(max(0.0, r[2] - r[0]) * max(0.0, r[3] - r[1]) for r in content) / area)
        pages.append({"words": len(body), "running_words": len(drop), "image_frac": frac,
                      "running_images": len(ik) - len(content), "body": body})
    textless = [i for i, p in enumerate(pages, 1) if p["words"] < MIN_WORDS and p["image_frac"] >= IMAGE_MIN_FRAC]
    near_empty = [i for i, p in enumerate(pages, 1) if p["words"] < MIN_WORDS and p["image_frac"] < IMAGE_MIN_FRAC]
    with_images = [i for i, p in enumerate(pages, 1) if p["image_frac"] >= IMAGE_MIN_FRAC]
    total = sum(p["words"] for p in pages)
    return {"pages": pages, "textless": textless, "near_empty": near_empty, "with_images": with_images,
            "text_layer": "none" if total == 0 else ("partial" if textless else "full"),
            "running": sorted(f"{b}: {t}" for b, t in run_lines), "running_images": len(run_imgs)}


def text_layer_of_pdf(pdf: Path) -> dict:
    import pymupdf

    with pymupdf.open(pdf) as doc:
        return text_layer([page_layer(p) for p in doc])


# ---------------------------------------------------------------- identity

DOC_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._+-]*@[A-Za-z0-9._-]+$")
_PART_RE = re.compile(r"[A-Za-z]{1,6}\d{2,}[A-Za-z0-9]*")
_TI_LIT = re.compile(r"\b(S[A-Z]{3}[0-9A-Z]{3})([A-Z])?\b")
_ST_DOC = re.compile(r"\b((?:DS|RM|AN|ES|UM|PM)\d{4,5})\b")
_REV_TEXT = re.compile(r"\bRev(?:ision)?\.?\s*:?\s*([0-9]{1,3}(?:\.[0-9]+)?[a-z]?|[A-Z](?![A-Za-z]))(?![\w.])")


def _manifest_entry(atlas: Path, pdf: Path, sha: str) -> dict | None:
    path = atlas / "inventory" / "manifest.json"
    if not path.exists():
        return None
    try:
        data = common.read_json(path)
    except ValueError:
        return None
    if isinstance(data, dict):
        entries = data.get("documents") or data.get("files") or data.get("docs") or data.get("sources") or []
        if isinstance(entries, dict):
            entries = [dict(v, file=v.get("file", k)) for k, v in entries.items()]
    else:
        entries = data
    for e in entries if isinstance(entries, list) else []:
        if isinstance(e, dict) and e.get("sha256") == sha:
            return e
    for e in entries if isinstance(entries, list) else []:
        if isinstance(e, dict) and Path(str(e.get("file") or e.get("path") or "")).name == pdf.name:
            return e
    return None


def derive_identity(pdf: Path, meta: dict, first: str, last: str) -> dict:
    """Part, revision and document number from vendor conventions; used only when the
    inventory manifest has no entry and no --doc-id was given."""
    stem = pdf.stem
    part = None
    m = _PART_RE.search(stem)
    if m:
        part = m.group(0)
    else:
        m = _PART_RE.search(meta.get("title") or "")
        part = m.group(0) if m else stem
    rev, doc_no = None, None
    blob = " ".join([meta.get("title") or "", meta.get("keywords") or "", meta.get("subject") or ""])
    m = re.search(r"\(Rev\.\s*([A-Z0-9]{1,3})\)", blob)
    lit = _TI_LIT.search(blob) or _TI_LIT.search(first[:3000])
    if lit:
        doc_no = lit.group(0)
    if m:
        rev = m.group(1)
    elif lit and lit.group(2):
        rev = lit.group(2)
    tail = re.search(rf"{re.escape(part)}[-_ ]+(?:rev[-_ ]?)?([0-9]{{1,3}}[a-z]?|[A-Z])$", stem, re.I) if part else None
    if rev is None and tail:
        rev = tail.group(1)
    if rev is None:
        for text in (first, last):
            m = _REV_TEXT.search(text)
            if m:
                rev = m.group(1)
                break
    if doc_no is None:
        m = _ST_DOC.search(first[:3000] + " " + last[-3000:])
        doc_no = m.group(1) if m else None
    return {"part": part, "rev": rev or "norev", "doc_no": doc_no}


# ---------------------------------------------------------------- page classes

_MATH_FONT = re.compile(r"math|cmmi|cmsy|cmex|mt ?extra|euclid|stix", re.I)
_CAPTION = re.compile(r"^\s*(?:Figure|Fig\.)\s+\d+(?:[-.]\d+)*\.?\s+(.*?)\s*$")
def _is_toc_line(ln: str) -> bool:
    """A table-of-contents line: dot leaders or a wide gap, then a page number. (A regex with two
    adjacent whitespace quantifiers backtracks quadratically on layout text.)"""
    t = ln.rstrip()
    j = len(t)
    while j and t[j - 1].isdigit():
        j -= 1
    if j == len(t):
        return False
    head = t[:j]
    return head.endswith("....") or head.endswith("  ")
_TABLE_CAPTION = re.compile(r"^\s*Table\s+\d+(?:[-.]\d+)*\.?\s", re.M)
_REG_PAGE = re.compile(r"Register\s*\((?:Address|Offset)\s*=|Register Field Descriptions|"
                       r"^\s*Bits?\s+(?:Field|Name)\s+(?:Type|Access|R/W)\s+Reset", re.M | re.I)
_BIT_TOKEN = re.compile(r"\b(?:R/W|R|W|RC|R/W1C)-[01x]+b\b")
_EQ_NUMBER = re.compile(r"\S {8,}\(\d{1,3}\)\s*$", re.M)
_HDR_WORDS = {"min", "typ", "max", "nom", "minimum", "typical", "maximum"}


FIGURE_IMAGE_FRAC = 0.10   # content images covering this much of a page make it a figure page, captioned or not
_DOT_LEADER = re.compile(r"\.(?: ?\.){3,}")
_TOC_TITLE = re.compile(r"\s*(?:table\s+of\s+contents|contents|list\s+of\s+(?:figures|tables|illustrations))"
                        r"(?:\s*\(continued\))?\s*", re.I)


def is_toc_page(text: str) -> bool:
    """A table-of-contents or list-of-figures/tables page: a `Contents` / `List of ...` title line
    among its first lines and 3 or more dot-leader lines, or 5 or more dot-leader lines that make
    up 40% of its lines. Leaders count with or without a page number (pdftotext -layout cuts some
    off at the right margin)."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    leaders = sum(1 for ln in lines if _DOT_LEADER.search(ln))
    title = any(_TOC_TITLE.fullmatch(ln) for ln in lines[:8])
    return (title and leaders >= 3) or (leaders >= 5 and leaders >= 0.4 * len(lines))


def _in_margin(r, h: float) -> bool:
    return r[3] <= DRAWING_MARGIN * h or r[1] >= (1 - DRAWING_MARGIN) * h


def _snap(vals: list[float], tol: float = 1.5) -> list[float]:
    groups: list[list[float]] = []
    for v in sorted(vals):
        if groups and v - groups[-1][-1] <= tol:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [sum(g) / len(g) for g in groups]


def _grid_cells(hs: list[tuple], vs: list[tuple], words: list, tol: float = 1.5) -> tuple[int, int]:
    """Closed cells of a ruled grid (the rectangles between adjacent grid lines whose four sides
    are all drawn) and how many of them hold a word's centre. `hs` = (y, x0, x1) strokes, `vs` =
    (x, y0, y1) strokes."""
    ys, xs = _snap([h[0] for h in hs]), _snap([v[0] for v in vs])
    if len(ys) < 2 or len(xs) < 2:
        return 0, 0

    def runs(segs, grid):
        out: dict[int, list[list[float]]] = {}
        for c, a, b in segs:
            i = bisect.bisect_left(grid, c)
            i = min((j for j in (i - 1, i) if 0 <= j < len(grid)), key=lambda j: abs(grid[j] - c))
            out.setdefault(i, []).append([a, b])
        for i, iv in out.items():
            iv.sort()
            merged = [iv[0]]
            for a, b in iv[1:]:
                if a <= merged[-1][1] + tol:
                    merged[-1][1] = max(merged[-1][1], b)
                else:
                    merged.append([a, b])
            out[i] = merged
        return out

    H, V = runs(hs, ys), runs(vs, xs)

    def drawn(iv, a, b):
        return any(x0 <= a + tol and x1 >= b - tol for x0, x1 in iv)

    cells = set()
    for i in range(len(ys) - 1):
        if ys[i + 1] - ys[i] < 3 or i not in H or i + 1 not in H:
            continue
        for j in range(len(xs) - 1):
            if xs[j + 1] - xs[j] < 3 or j not in V or j + 1 not in V:
                continue
            if drawn(H[i], xs[j], xs[j + 1]) and drawn(H[i + 1], xs[j], xs[j + 1]) and \
                    drawn(V[j], ys[i], ys[i + 1]) and drawn(V[j + 1], ys[i], ys[i + 1]):
                cells.add((i, j))
    filled = {(bisect.bisect_right(ys, (w[1] + w[3]) / 2) - 1, bisect.bisect_right(xs, (w[0] + w[2]) / 2) - 1)
              for w in words} & cells
    return len(cells), len(filled)


def _clusters(rects: list[tuple], gap: float = 3.0) -> list[list[int]]:
    """Group rectangles that touch (within `gap`), via a coarse bucket grid and union-find."""
    parent = list(range(len(rects)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    buckets: dict[tuple[int, int], list[int]] = {}
    for k, r in enumerate(rects):
        x0, y0, x1, y1 = r[0] - gap, r[1] - gap, r[2] + gap, r[3] + gap
        for bx in range(int(x0 // 64), int(x1 // 64) + 1):
            for by in range(int(y0 // 64), int(y1 // 64) + 1):
                for o in buckets.get((bx, by), []):
                    q = rects[o]
                    if q[0] <= x1 and q[2] >= x0 and q[1] <= y1 and q[3] >= y0:
                        parent[find(o)] = find(k)
                buckets.setdefault((bx, by), []).append(k)
    groups: dict[int, list[int]] = {}
    for k in range(len(rects)):
        groups.setdefault(find(k), []).append(k)
    return list(groups.values())


def vector_drawing(page: Any, words: list) -> dict:
    """Is the vector drawing on a page text, or a picture?

    Paths entirely inside the top or bottom 10% (header rules, a vector logo) are ignored; the rest
    are grouped into clusters of touching drawings. A cluster is **text-covered** (a ruled table, a
    register bit diagram, a boxed message sequence) when it is drawn only with horizontal and
    vertical strokes and rectangles (at most 2 curves or slanted strokes: no arrowheads, plotted
    curves, component symbols or angled wires) and forms 2 or more closed cells, at least half of
    which hold a word. Every label of such a drawing is in the text layer and its geometry is only
    the grid, so the text layer covers it. Any other cluster of 12 or more strokes is a **graphic**
    (a schematic, a block diagram with arrows, a plot, a timing diagram): its labels may be in the
    text layer, but what connects to what is not.

    Returns counts of `grids` (text-covered clusters) and `graphics`."""
    h = page.rect.height
    drawings = [d for d in page.get_drawings() if not _in_margin(tuple(d["rect"]), h)]
    out = {"grids": 0, "graphics": 0}
    for group in _clusters([tuple(drawings[k]["rect"]) for k in range(len(drawings))]):
        hs, vs, other = [], [], 0
        for k in group:
            for it in drawings[k]["items"]:
                op = it[0]
                if op == "l":
                    a, b = it[1], it[2]
                    if abs(a.y - b.y) < 0.6:
                        hs.append(((a.y + b.y) / 2, min(a.x, b.x), max(a.x, b.x)))
                    elif abs(a.x - b.x) < 0.6:
                        vs.append(((a.x + b.x) / 2, min(a.y, b.y), max(a.y, b.y)))
                    else:
                        other += 1
                elif op in ("re", "qu"):
                    if op == "qu" and not it[1].is_rectangular:
                        other += 1
                        continue
                    r = it[1] if op == "re" else it[1].rect
                    if r.width < 0.8 or r.height < 0.8:     # a rule drawn as a thin filled rectangle
                        if r.width >= r.height:
                            hs.append(((r.y0 + r.y1) / 2, r.x0, r.x1))
                        else:
                            vs.append(((r.x0 + r.x1) / 2, r.y0, r.y1))
                        continue
                    hs += [(r.y0, r.x0, r.x1), (r.y1, r.x0, r.x1)]
                    vs += [(r.x0, r.y0, r.y1), (r.x1, r.y0, r.y1)]
                else:
                    other += 1
        strokes = len(hs) + len(vs) + other
        if other <= 2:
            cells, filled = _grid_cells(hs, vs, words)
            if cells >= 2 and filled >= 0.5 * cells:
                out["grids"] += 1
                continue
        if strokes >= 12:
            out["graphics"] += 1
    return out


def overlined_names(words: list) -> tuple[list[str], list[str]]:
    """Names printed with a combining overline (U+0305: active-low signals such as EN-bar), and
    those of them that also appear on the page without one. `normalize()` drops the bar, so doc
    check cannot tell EN from EN-bar."""
    def base(t: str) -> str:
        t = "".join(c for c in t if unicodedata.category(c) != "Mn")
        return t.strip("()[]{},.;:'\"")
    barred, plain = set(), set()
    for w in words:
        b = base(w[4])
        if not b:
            continue
        (barred if OVERLINE in w[4] else plain).add(b)
    return sorted(barred), sorted(barred & plain)


def page_classes(page: Any, text: str, words: list, layer: dict | None = None) -> tuple[list[str], dict]:
    """Classes for one page and the evidence behind them. `words` = PyMuPDF words; `layer` = this
    page's entry of `text_layer()` plus its path rectangles (running header and footer removed),
    computed from the page alone when not given.

    - image-heavy: textless (see `text_layer`), or content images over half the page, or under 80
      body words with some image or a vector graphic;
    - figure: content images covering FIGURE_IMAGE_FRAC of the page, captioned or not (word-
      processor exports never caption), or a `Figure N` caption over a vector graphic that the
      text layer does not cover (`vector_drawing`: a bit diagram or a ruled table does not count,
      a plot, block diagram or schematic of 20 paths or more does);
    - overline: the text layer holds combining overlines (active-low names, see `overlined_names`)."""
    if layer is None:
        pl = page_layer(page)
        layer = dict(text_layer([pl])["pages"][0], paths=pl["paths"])
    body = layer["body"]
    img_frac = layer["image_frac"]
    h = page.rect.height
    paths = sum(1 for r in layer.get("paths") or [] if not _in_margin(r, h))
    nwords = layer["words"]
    math_chars = 0
    eq_hint = bool(_EQ_NUMBER.search(text) or re.search(r"\bEquation\b", text))
    if eq_hint and any(_MATH_FONT.search(f[3] or "") for f in page.get_fonts()):  # dict extraction is slow
        for b in page.get_text("dict").get("blocks", []):
            for ln in b.get("lines", []):
                for sp in ln.get("spans", []):
                    if _MATH_FONT.search(sp.get("font", "")):
                        math_chars += len(sp.get("text", "").strip())
    pua = sum(1 for c in text if is_pua(c))
    # table header band: two or more of MIN/TYP/MAX/NOM on one line
    bands: dict[int, set] = {}
    for w in words:
        t = w[4].strip(".:").lower()
        if t in _HDR_WORDS:
            bands.setdefault(round((w[1] + w[3]) / 4), set()).add(t)
    ec_header = any(len(v) >= 2 for v in bands.values())
    num_lines = sum(1 for ln in text.splitlines() if len(re.findall(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])", ln)) >= 3)
    # a register bit diagram is captioned as a figure but is all text
    lines = text.splitlines()
    captions = 0
    for i, ln in enumerate(lines):
        m = _CAPTION.match(ln)
        if m and not re.search(r"\bRegisters?$", m.group(1)) and \
                not any(_BIT_TOKEN.search(x) for x in lines[i + 1:i + 6]):
            captions += 1
    cls = []
    ev = {"words": nwords, "running_words": layer.get("running_words", 0), "image_frac": round(img_frac, 2),
          "paths": paths, "math_chars": math_chars, "pua_glyphs": pua, "figure_captions": captions}
    vec: dict | None = None

    def graphic() -> bool:
        nonlocal vec
        if vec is None:
            vec = vector_drawing(page, body)
            ev.update(vector_grids=vec["grids"], vector_graphics=vec["graphics"])
        return vec["graphics"] > 0

    textless = nwords < MIN_WORDS and img_frac >= IMAGE_MIN_FRAC
    if nwords < 5 and img_frac < IMAGE_MIN_FRAC and paths < 5:
        cls.append("blank")
    if textless or img_frac >= 0.5 or (nwords < 80 and (img_frac > 0.05 or (paths > 40 and graphic()))):
        cls.append("image-heavy")
    by = []
    if img_frac >= FIGURE_IMAGE_FRAC:
        by.append("image")
    if captions and paths >= 20 and graphic():
        by.append("vector")
    if by:
        cls.append("figure")
        ev["figure_by"] = by
    if ec_header or (num_lines >= 8 and _TABLE_CAPTION.search(text)):
        cls.append("table-heavy")
    reg_text = "\n".join(ln for ln in text.splitlines() if not _is_toc_line(ln))
    if _REG_PAGE.search(reg_text) or len(_BIT_TOKEN.findall(reg_text)) >= 3:
        cls.append("register")
    if math_chars >= 15 and eq_hint:
        cls.append("equation")
    if pua:
        cls.append("pua-glyphs")
    if OVERLINE in text or any(OVERLINE in w[4] for w in words):
        cls.append("overline")
        barred, both = overlined_names(words)
        ev["overlined"] = barred
        if both:
            ev["overlined_also_plain"] = both
    return cls, ev


NEEDS_IMAGE = ("image-heavy", "figure", "equation")


def image_read_reasons(p: dict) -> list[str]:
    """Why page entry `p` of pages.json needs an image Read (empty: the text layer covers it). An
    overlined page needs one only when it is a table page and a name on it appears both with and
    without the bar (a row's condition then cannot be told from the text layer); every overlined
    page is listed in toc.md's "Overlined names"."""
    why = []
    cls = p.get("classes") or []
    if "image-heavy" in cls:
        why.append("little text layer" if p["words"] < 80 else "mostly raster image")
    if "figure" in cls:
        by = p.get("figure_by") or ["image" if p.get("image_frac", 0) >= FIGURE_IMAGE_FRAC else "vector"]
        if "vector" in by:
            why.append(f"{p.get('figure_captions', 0)} figure caption(s) over a vector graphic")
        if "image" in by:
            why.append(f"images cover {round(100 * p['image_frac'])}% of the page"
                       + ("" if p.get("figure_captions") else ", no caption"))
    if "equation" in cls:
        why.append("math font: equations are glyph soup in the text layer")
    if p.get("overlined_also_plain") and "table-heavy" in cls:
        why.append(f"{', '.join(p['overlined_also_plain'])} appear(s) with and without an overline; "
                   "the text layer cannot tell which row has the bar")
    return why


# ---------------------------------------------------------------- outline -> sections

_NUMBERED = re.compile(r"^\s*((?:\d{1,3}|[A-Z])(?:\.\d{1,3})*)\.?\s+(\S.*)$")
_SKIP_TITLE = re.compile(r"^\s*(Table|Figure|Fig\.|Equation)\s*\d", re.I)


def _heading_xy(page: Any, dest: dict | None, title: str) -> tuple[float | None, float | None]:
    """Where an outline entry's heading sits on its page: the destination point when the title is
    printed there, else the search hit for the title nearest to it (some writers point every
    entry at a fixed spot, such as one inch from the top, or at (0, 0)), else the destination.
    (None, None) when neither is known."""
    x = y = None
    if dest:
        to = dest.get("to")
        try:
            x, y = (float(to.x), float(to.y)) if to is not None else (None, None)
        except (AttributeError, TypeError, ValueError):
            x = y = None
        if x is not None and (math.isnan(x) or x < 0):
            x = None
        if y is not None and (math.isnan(y) or y <= 0):
            x = y = None
    probe = re.sub(r"\s+", " ", title).strip()[:40]
    try:
        hits = page.search_for(probe) if len(probe) >= 4 else []
    except RuntimeError:
        hits = []
    if hits and (y is None or not any(h.y0 - 8 <= y <= h.y1 + 8 for h in hits)):
        h = min(hits, key=lambda r: (abs(r.y0 - y) if y is not None else r.y0))
        x, y = h.x0, h.y0
    return x, y


def _gutter(words: list, width: float) -> float | None:
    """The x of the gutter of a two-column page, or None: the point in the middle 40% of the width
    that the fewest words cross, if at most 3% of the words cross it and a fifth of them lie on
    each side."""
    if len(words) < 40 or width <= 0:
        return None
    lo, hi, step = 0.3 * width, 0.7 * width, 2.0
    nb = int((hi - lo) / step) + 1
    diff = [0] * (nb + 1)
    for w in words:
        a, b = math.ceil((w[0] - lo) / step), math.floor((w[2] - lo) / step)
        a, b = max(a, 0), min(b, nb - 1)
        if a <= b:
            diff[a] += 1
            diff[b + 1] -= 1
    cover, run = [], 0
    for k in range(nb):
        run += diff[k]
        cover.append(run)
    k = min(range(nb), key=lambda i: (cover[i], abs(i - nb / 2)))
    g = lo + k * step
    left = sum(1 for w in words if w[2] <= g)
    right = sum(1 for w in words if w[0] >= g)
    if cover[k] <= max(2, 0.03 * len(words)) and min(left, right) >= 0.2 * len(words):
        return g
    return None


def _cut(words: list, x: float, y: float, gutter: float | None) -> tuple[int, int]:
    """How many of a page's body words come before a heading at (x, y), by position: on a
    one-column page the words above it; on a two-column page (gutter at x = `gutter`) also the
    whole left column when the heading is in the right one, and not the right column's words when
    it is in the left one (unless the right column ends above it: then it starts full-width
    content after both). Position, not the content stream, decides: the text of a figure at the
    top of a page, and some vendors' headings, come late in the stream.

    Returns (that count, the count of words above the heading ignoring columns): two headings on
    one page whose column-aware counts come out in the wrong order (a figure's labels make a
    full-width block look like a column) are apportioned by height alone."""
    hcol = 1 if gutter is not None and x >= gutter else 0
    # a left-edge heading below the end of the right-hand column starts full-width content that
    # follows both columns
    after_columns = gutter is not None and hcol == 0 and not any(w[0] >= gutter and w[1] > y for w in words)

    def before(w) -> bool:
        col = 1 if gutter is not None and w[0] >= gutter else 0
        if after_columns:
            return w[3] < y - 2
        return col < hcol or (col == hcol and w[3] < y - 2)

    return sum(1 for w in words if before(w)), sum(1 for w in words if w[3] < y - 2)


def _heading_x(words: list, x: float | None, y: float, title: str) -> float:
    """The heading's left edge: the word at its height that starts its title (the one nearest the
    outline's or the search's x, when that is known; a column to the left can hold the same word
    at the same height), else that x, else 0."""
    first = {normalize(t) for t in title.split()[:2]}
    hits = [w for w in words if w[1] - 3 <= y <= w[3] + 3 and normalize(w[4]) in first]
    if x is not None:
        near = min(hits, key=lambda w: abs(w[0] - x), default=None)
        return near[0] if near is not None and abs(near[0] - x) <= 20 else x
    return hits[0][0] if hits else 0.0


def _share(words: list, lo: int, hi: int | None) -> float:
    """The share of a page's body words between two headings, given as the counts of words
    before each (`_cut`); `hi` None: to the end of the page. A page with no words goes whole to
    the section that runs to its end."""
    if not words:
        return 1.0 if hi is None else 0.0
    return max(0, (len(words) if hi is None else hi) - lo) / len(words)


FRONT_MATTER = "front-matter"
UNLISTED = "unlisted"


def build_sections(doc: Any, page_tokens: list[int], page_words: list[list],
                   toc_pages: set[int] | frozenset = frozenset()) -> list[dict]:
    """PDF outline -> sections: id (document numbering or title slug), title, level, parent,
    pages [start, end], tokens. `page_words` are each page's body words (running header and
    footer removed, see `text_layer`), `toc_pages` the table-of-contents and list pages
    (`is_toc_page`). Table/Figure entries are dropped; an entry with no page takes its first
    child's.

    - A section ends on the page where the next same-or-higher entry starts when any body text
      precedes that entry's heading there (the section spills over), else on the page before.
    - A section does not run on into TOC/list pages that no entry starts on (front matter the
      outline skips) unless one of its own subsections starts after them.
    - `tokens` is the section's share of its pages' tokens: on a page it shares with a sibling it
      counts only the body words from its heading to the next one, not the whole page.
    - Pages no section covers (before the first entry, or skipped TOC/list pages) become a
      synthetic top-level entry `front-matter` (`unlisted` for any other gap), `synthetic: true`."""
    n = doc.page_count
    raw = doc.get_toc(simple=False)
    entries = []
    for i, e in enumerate(raw):
        lvl, title, pg = e[0], (e[1] or "").strip(), e[2]
        dest = e[3] if len(e) > 3 else None
        if not title or _SKIP_TITLE.match(title):
            continue
        if pg is None or pg < 1:
            kids = [x[2] for x in raw[i + 1:] if x[0] > lvl and x[2] and x[2] >= 1][:1]
            nxt = next((j for j, x in enumerate(raw[i + 1:]) if x[0] <= lvl), None)
            if nxt is not None:
                kids = [x[2] for x in raw[i + 1:i + 1 + nxt] if x[2] and x[2] >= 1][:1]
            if not kids:
                continue
            pg, dest = kids[0], None
        entries.append({"level": lvl, "title": re.sub(r"\s+", " ", title), "start": min(pg, n), "dest": dest})
    # body words below the top 8% of the page (a header line on one page only, such as the first)
    page_words = [[w for w in ws if w[1] > 0.08 * doc[i].rect.height] for i, ws in enumerate(page_words)]
    cuts: dict[int, tuple[int, int] | None] = {}
    gutters: dict[int, float | None] = {}

    def cut(k: int) -> tuple[int, int] | None:
        """Body words before entry k's heading on its start page (see `_cut`); None when it
        cannot be placed."""
        if k not in cuts:
            e = entries[k]
            p = e["start"]
            x, y = _heading_xy(doc[p - 1], e["dest"], e["title"])
            if y is None:
                cuts[k] = None
            else:
                if p not in gutters:
                    gutters[p] = _gutter(page_words[p - 1], doc[p - 1].rect.width)
                x = _heading_x(page_words[p - 1], x, y, e["title"])
                cuts[k] = _cut(page_words[p - 1], x, y, gutters[p])
        return cuts[k]

    secs: list[dict] = []
    stack: list[dict] = []
    used: set[str] = set()
    for k, e in enumerate(entries):
        while stack and stack[-1]["level"] >= e["level"]:
            stack.pop()
        parent = stack[-1] if stack else None
        m = _NUMBERED.match(e["title"])
        if m and not re.match(r"^\d{4,}", e["title"]):
            sid, title = m.group(1), m.group(2)
        else:
            sid, title = common_slug(e["title"]), e["title"]
        if sid in used and parent:
            sid = f"{parent['id']}.{sid}"
        base, j = sid, 2
        while sid in used:
            sid, j = f"{base}-{j}", j + 1
        used.add(sid)
        start, end = e["start"], n
        nk = next((k2 for k2 in range(k + 1, len(entries)) if entries[k2]["level"] <= e["level"]), None)
        if nk is not None:
            p2 = entries[nk]["start"]
            end = p2
            c = cut(nk) if p2 > start else None
            if c is not None and c[0] == 0:
                end = p2 - 1
        end = max(end, start)
        if start not in toc_pages:
            skipped = [p for p in range(start + 1, end + 1) if p in toc_pages]
            kids = [entries[k2]["start"] for k2 in range(k + 1, nk if nk is not None else len(entries))]
            if skipped and not any(ks >= skipped[0] for ks in kids):
                end = skipped[0] - 1
        lo = cut(k) or (0, 0)
        hi = cut(nk) if nk is not None and entries[nk]["start"] == end else None
        if start == end:
            k2 = 0 if hi is None or hi[0] >= lo[0] else 1
            tok = page_tokens[start - 1] * _share(page_words[start - 1], lo[k2], hi[k2] if hi else None)
        else:
            tok = page_tokens[start - 1] * _share(page_words[start - 1], lo[0], None)
            tok += sum(page_tokens[start:end - 1])
            tok += page_tokens[end - 1] * _share(page_words[end - 1], 0, hi[0] if hi else None)
        sec = {"id": sid, "title": title, "level": e["level"], "parent": parent["id"] if parent else None,
               "pages": [start, end], "tokens": round(tok)}
        secs.append(sec)
        stack.append({"level": e["level"], "id": sid})
    if not secs:
        return secs
    covered = {p for s in secs for p in range(s["pages"][0], s["pages"][1] + 1)}
    first = min(s["pages"][0] for s in secs)
    runs: list[list[int]] = []
    for p in range(1, n + 1):
        if p not in covered:
            if runs and runs[-1][-1] == p - 1:
                runs[-1].append(p)
            else:
                runs.append([p])
    top_level = min(s["level"] for s in secs)
    for run in runs:
        front = run[-1] < first or all(p in toc_pages for p in run)
        sid, title = (FRONT_MATTER, "Front matter: pages before the outline, contents and lists") if front \
            else (UNLISTED, "Pages no outline entry covers")
        base, j = sid, 2
        while sid in used:
            sid, j = f"{base}-{j}", j + 1
        used.add(sid)
        sec = {"id": sid, "title": title, "level": top_level, "parent": None, "pages": [run[0], run[-1]],
               "tokens": sum(page_tokens[p - 1] for p in run), "synthetic": True}
        at = next((i for i, s in enumerate(secs) if s["pages"][0] > run[0] and s["level"] == top_level), len(secs))
        secs.insert(at, sec)
    return secs


def common_slug(s: str) -> str:
    from .records import slug
    return slug(s)


# ---------------------------------------------------------------- toc.md

def _page_runs(ps: list[int]) -> str:
    ps = sorted(set(ps))
    runs, i = [], 0
    while i < len(ps):
        j = i
        while j + 1 < len(ps) and ps[j + 1] == ps[j] + 1:
            j += 1
        runs.append(str(ps[i]) if i == j else f"{ps[i]}-{ps[j]}")
        i = j + 1
    return ", ".join(runs)


def tier2_files(docdir: Path, sections: list[dict]) -> dict[str, list[str]]:
    """section id -> the tier-2 files that hold it, strongest claim first. A section's own claims win;
    a section with none takes its nearest claimed ancestor's files. The claims on a section:

    1. a file named after it, `s/<secid>.md`;
    2. every `<!-- atlas:covers a b c -->` line naming it, in file-name order (a section split over
       `s/<secid>.md`, `-part2`, `-part3` lists all of them).

    So an explicit cover of a subsection always beats a file named after a parent section (which
    reaches the subsection only by descent), and a cover naming a subsection beats one naming its
    parent. Ids that are not sections of the outline are ignored."""
    sdir = Path(docdir) / "s"
    if not sdir.is_dir():
        return {}
    ids = {s["id"] for s in sections}
    parent = {s["id"]: s.get("parent") for s in sections}
    files = sorted(sdir.glob("*.md"))
    claim: dict[str, list[str]] = {f.stem: [f"s/{f.name}"] for f in files if f.stem in ids}
    for f in files:
        for m in re.finditer(r"<!--\s*atlas:covers\s+([^>]*?)\s*-->", f.read_text(encoding="utf-8")):
            for sid in m.group(1).replace(",", " ").split():
                if sid in ids and f"s/{f.name}" not in claim.setdefault(sid, []):
                    claim[sid].append(f"s/{f.name}")
    out: dict[str, list[str]] = {}
    for s in sections:
        sid, seen = s["id"], set()
        while sid is not None and sid not in claim and sid not in seen:
            seen.add(sid)
            sid = parent.get(sid)
        if sid is not None and sid in claim:
            out[s["id"]] = claim[sid]
    return out


def tier2_map(docdir: Path, sections: list[dict]) -> dict[str, str]:
    """section id -> its strongest tier-2 file (`tier2_files`, first entry)."""
    return {k: v[0] for k, v in tier2_files(docdir, sections).items()}


def write_toc(docdir: Path) -> None:
    """toc.md from toc.json + pages/pages.json + the s/ files present now."""
    docdir = Path(docdir)
    meta = common.read_json(docdir / "doc.json")
    toc = common.read_json(docdir / "toc.json")
    pinfo = common.read_json(docdir / "pages" / "pages.json")
    t2 = {k: ", ".join(v) for k, v in tier2_files(docdir, toc["sections"]).items()}
    total = sum(p.get("tokens") or 0 for p in pinfo)
    lines = [f"# {meta['doc_id']}: outline", "",
             f"{meta['file']}, {meta['pages']} pages, {total} tokens (o200k) of text layer. "
             "Pages are 1-based physical PDF pages; `pages/p<NNN>.txt` is the text layer of page NNN.", ""]
    if toc["sections"]:
        lines += ["| Section | Title | Pages | Tier-2 file | Tokens |", "|---|---|---|---|---|"]
        for s in toc["sections"]:
            a, b = s["pages"]
            pr = str(a) if a == b else f"{a}-{b}"
            title = "· " * (s["level"] - 1) + s["title"].replace("|", "/")
            lines.append(f"| {s['id']} | {title} | {pr} | {t2.get(s['id'], '-')} | {s['tokens']} |")
    else:
        lines.append("The PDF has no outline. Navigate by page, `pages/p<NNN>.txt`.")
    need = [(p, image_read_reasons(p)) for p in pinfo]
    need = [(p, why) for p, why in need if why]
    lines += ["", "## Pages that need an image Read", ""]
    if need:
        lines += ["The text layer alone misses these: read them with `Read(pages=N)` (about 1.5K tokens a page), "
                  "not from `pages/`.", "", "| Page | Classes | Words | Image % | Why |", "|---|---|---|---|---|"]
        for p, why in need:
            lines.append(f"| {p['page']} | {', '.join(p['classes'])} | {p['words']} | "
                         f"{round(100 * p['image_frac'])} | {'; '.join(why)} |")
    else:
        lines.append("None: the text layer covers every page.")
    over = [p for p in pinfo if "overline" in p["classes"]]
    if over:
        lines += ["", "## Overlined names", "",
                  "These pages print names with a combining overline (U+0305), usually active-low signals. "
                  "`doc check` drops the bar, so a quote cannot tell EN from EN-bar, and `pages/p<NNN>.txt` "
                  "puts the bar on a line of its own above the name. Take the bar from the page image, or "
                  "from PyMuPDF's text of the page, before a record names one of these signals.", "",
                  "| Page | Overlined | Also printed without the bar |", "|---|---|---|"]
        for p in over:
            lines.append(f"| {p['page']} | {', '.join(p.get('overlined') or []) or '-'} | "
                         f"{', '.join(p.get('overlined_also_plain') or []) or '-'} |")
    lines += ["", "## Page classes", ""]
    by: dict[str, list[int]] = {}
    for p in pinfo:
        for c in p["classes"]:
            by.setdefault(c, []).append(p["page"])
    for c in ("table-heavy", "register", "figure", "equation", "image-heavy", "overline", "pua-glyphs", "blank"):
        if c in by:
            lines.append(f"- {c}: p.{_page_runs(by[c])}")
    if "pua-glyphs" in by:
        lines.append("  (Symbol-font private-use glyphs, such as Ohm and <=, which Read shows as nothing; "
                     "doc check matches quotes with them mapped or dropped.)")
    grids = [p["page"] for p in pinfo if p.get("vector_grids") and "figure" not in p["classes"]]
    if grids:
        lines.append(f"- vector drawings the text layer covers (ruled tables, bit diagrams): p.{_page_runs(grids)}")
    synth = [s for s in toc["sections"] if s.get("synthetic")]
    if synth:
        lines += ["", "`" + "`, `".join(s["id"] for s in synth) + "` "
                  + ("is" if len(synth) == 1 else "are")
                  + " not in the PDF outline: pages no outline entry covers (a cover, contents and lists, "
                  "an index, notices), kept out of the section before them."]
    common.write_text(docdir / "toc.md", "\n".join(lines))


# ---------------------------------------------------------------- prep

def prep(pdf: Path, atlas: Path, doc_id: str | None = None, bbox: bool = False, kind: str | None = None,
         log=print) -> Path:
    import pymupdf

    pdf = Path(pdf).resolve()
    if not pdf.is_file():
        raise FileNotFoundError(pdf)
    sha = common.sha256_file(pdf)
    doc = pymupdf.open(pdf)
    n = doc.page_count
    texts = layout_pages(pdf, n)
    meta_pdf = {k: v for k, v in (doc.metadata or {}).items() if v}
    entry = _manifest_entry(atlas, pdf, sha)
    ident = derive_identity(pdf, meta_pdf, texts[0] if texts else "", texts[-1] if texts else "")
    source = "derived"
    if entry:
        source = "manifest"
        found = {"part": entry.get("part") or (entry.get("part_numbers") or [None])[0],
                 "rev": entry.get("rev") or entry.get("revision"),
                 "doc_no": entry.get("doc_no") or entry.get("doc_number")}
        ident.update({k: v for k, v in found.items() if v})
    if doc_id:
        source = "argument"
        ident["rev"] = doc_id.split("@", 1)[1] if "@" in doc_id else ident["rev"]
    elif entry and isinstance(entry.get("doc_id"), str) and DOC_ID_RE.match(entry["doc_id"]):
        doc_id = entry["doc_id"]
    else:
        rev = re.sub(r"[^A-Za-z0-9._-]+", "", str(ident["rev"])) or "norev"
        doc_id = f"{common_slug(str(ident['part']))}@{rev}"
    if not DOC_ID_RE.match(doc_id):
        raise ValueError(f"doc id {doc_id!r} is not <slug>@<rev> (lower-case slug, no spaces)")
    docdir = atlas / "docs" / doc_id
    (docdir / "pages").mkdir(parents=True, exist_ok=True)

    layers = [page_layer(doc[i]) for i in range(n)]
    layer = text_layer(layers)
    page_tokens, body_words, pinfo = [], [], []
    for i in range(1, n + 1):
        t = texts[i - 1]
        page_file(docdir, i).write_text(t, encoding="utf-8")
        pg = doc[i - 1]
        words = layers[i - 1]["words"]
        lp = dict(layer["pages"][i - 1], paths=layers[i - 1]["paths"])
        body_words.append(lp["body"])
        tok = common.count_tokens(t)
        page_tokens.append(tok or 0)
        cls, ev = page_classes(pg, t, words, lp)
        pinfo.append(dict(page=i, tokens=tok, chars=len(t), sha256=common.sha256_text(t), classes=cls, **ev))
        if bbox:
            common.write_json(page_file(docdir, i, ".bbox.json"), {
                "page": i, "width": round(pg.rect.width, 2), "height": round(pg.rect.height, 2),
                "words": [[round(w[0], 2), round(w[1], 2), round(w[2], 2), round(w[3], 2), w[4]] for w in words]})
    labels = {}
    for i in range(n):
        try:
            lab = doc[i].get_label()
        except (AttributeError, RuntimeError):
            lab = ""
        if lab and lab != str(i + 1):
            labels[str(i + 1)] = lab
    rel = os.path.relpath(pdf, atlas.resolve())
    if rel.startswith(os.pardir * 1 + os.sep + os.pardir + os.sep + os.pardir):
        rel = str(pdf)  # far away: keep it absolute
    meta = {
        "doc_id": doc_id, "file": pdf.name, "path": rel, "sha256": sha,
        "kind": kind or (entry or {}).get("kind") or "datasheet",
        "part": ident.get("part"), "doc_no": ident.get("doc_no"), "rev": ident.get("rev"),
        "pages": n, "title": meta_pdf.get("title"), "labels": labels,
        "text_layer": layer["text_layer"], "pages_textless": layer["textless"],
        "running_header": layer["running"],
        "identity_from": source,
        "tool": {"embarch-atlas": __version__, "pymupdf": pymupdf.__version__, "pdftotext": pdftotext_version()},
    }
    common.write_json(docdir / "doc.json", meta)
    common.write_json(docdir / "pages" / "pages.json", pinfo)
    toc_pages = {i for i in range(1, n + 1) if is_toc_page(texts[i - 1])}
    secs = build_sections(doc, page_tokens, body_words, toc_pages)
    common.write_json(docdir / "toc.json", {"doc_id": doc_id, "sections": secs})
    write_toc(docdir)
    need = [p["page"] for p in pinfo if image_read_reasons(p)]
    log(f"{doc_id}: {n} pages, {sum(page_tokens)} tokens (o200k), {len(secs)} sections, "
        f"{len(need)} pages need an image Read ({_page_runs(need) or '-'}), identity from {source} -> {docdir}")
    return docdir


def resolve_pdf_path(docdir: Path, meta: dict) -> Path | None:
    """The source PDF recorded in doc.json, if it is still there."""
    p = Path(meta.get("path") or "")
    cands = [p] if p.is_absolute() else [(Path(docdir).parent.parent / p), (Path(docdir) / p)]
    for c in cands:
        if c.is_file():
            return c.resolve()
    return None


# ---------------------------------------------------------------- CLI

def cmd_prep(args) -> int:
    atlas = common.atlas_dir(args.atlas_dir)
    try:
        prep(Path(args.pdf), atlas, doc_id=args.doc_id, bbox=args.bbox, kind=args.kind)
    except (FileNotFoundError, ValueError, subprocess.CalledProcessError) as e:
        print(f"doc prep: {e}", file=sys.stderr)
        return 2
    return 0


def register(sub, groups) -> None:
    g = common.verb_group(sub, groups, "doc", "per-document steps: prep, check, render, regparse")
    p = g.add_parser("prep", help="text layer per page, outline -> toc.md, page classes")
    p.add_argument("pdf")
    p.add_argument("--doc-id", help="<slug>@<rev>; default: the inventory manifest's, else derived from the PDF")
    p.add_argument("--kind", help="datasheet | reference-manual | prd | ... (default: manifest's, else datasheet)")
    p.add_argument("--bbox", action="store_true", help="also write pages/p<NNN>.bbox.json word boxes")
    p.set_defaults(func=cmd_prep)

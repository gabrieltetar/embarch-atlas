"""`doc prep <pdf>`: the deterministic base layer of one datasheet, RM or PRD.

Writes under `docs/<doc_id>/`:
  doc.json             identity: file, sha256, kind, part, doc_no, rev, pages, printed labels that differ
  pages/p<NNN>.txt     `pdftotext -layout` of page NNN (1-based physical page), raw, glyphs untouched
  pages/p<NNN>.bbox.json  word boxes (PyMuPDF), with --bbox
  pages/pages.json     per page: tokens, words, images, classes, sha256 of the text
  toc.json, toc.md     the PDF outline as sections (document numbering or title slug), page
                       ranges, tokens, tier-2 file; then the pages that need an image Read

The text layer is the numeric oracle for every later check, so it is stored raw.
`normalize()` gives the forms the checker compares: Symbol-font private-use glyphs
(U+F057 Ohm, U+F0A3 <=, ...) mapped to real glyphs, or stripped, since the Read tool
shows them as nothing and a quote typed from it lacks them.
"""
from __future__ import annotations

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


def normalize(s: str, pua: str = "map") -> str:
    """Comparison form: PUA glyphs mapped ("map") or dropped ("strip"), NFKC, no combining marks
    (overlines), one dash, straight quotes, collapsed whitespace, no space before , ; : ) ] or after
    ( [, lower case, and a word broken over a line with a hyphen rejoined."""
    if pua == "map":
        s = map_pua(s)
    elif pua == "strip":
        s = "".join(c for c in s if not is_pua(c))
    s = unicodedata.normalize("NFKC", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.translate(_DASHES)
    s = re.sub(r"\s+", " ", s).strip().lower()
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


def page_classes(page: Any, text: str, words: list) -> tuple[list[str], dict]:
    """Classes for one page and the evidence behind them. `words` = PyMuPDF words."""
    area = page.rect.width * page.rect.height or 1.0
    img_area, paths = 0.0, 0
    try:
        for kind, rect in page.get_bboxlog():
            if "image" in kind or "imgmask" in kind:
                r = page.rect & rect
                img_area += max(0.0, r.width) * max(0.0, r.height)
            elif "path" in kind:
                paths += 1
    except (AttributeError, RuntimeError):
        img_area = sum(abs((i["bbox"][2] - i["bbox"][0]) * (i["bbox"][3] - i["bbox"][1]))
                       for i in page.get_image_info())
    img_frac = min(1.0, img_area / area)
    nwords = len(words)
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
    ev = {"words": nwords, "image_frac": round(img_frac, 2), "paths": paths, "math_chars": math_chars,
          "pua_glyphs": pua, "figure_captions": captions}
    if nwords < 5 and img_frac < 0.02 and paths < 5:
        cls.append("blank")
    if (nwords < 80 and (img_frac > 0.05 or paths > 40)) or img_frac >= 0.5:
        cls.append("image-heavy")
    if captions and (img_frac >= 0.15 or paths >= 100):
        cls.append("figure")
    if ec_header or (num_lines >= 8 and _TABLE_CAPTION.search(text)):
        cls.append("table-heavy")
    body = "\n".join(ln for ln in text.splitlines() if not _is_toc_line(ln))
    if _REG_PAGE.search(body) or len(_BIT_TOKEN.findall(body)) >= 3:
        cls.append("register")
    if math_chars >= 15 and eq_hint:
        cls.append("equation")
    if pua:
        cls.append("pua-glyphs")
    return cls, ev


NEEDS_IMAGE = ("image-heavy", "figure", "equation")


# ---------------------------------------------------------------- outline -> sections

_NUMBERED = re.compile(r"^\s*((?:\d{1,3}|[A-Z])(?:\.\d{1,3})*)\.?\s+(\S.*)$")
_SKIP_TITLE = re.compile(r"^\s*(Table|Figure|Fig\.|Equation)\s*\d", re.I)


def _heading_y(page: Any, dest: dict | None, title: str) -> float | None:
    y = None
    if dest:
        to = dest.get("to")
        try:
            y = float(to.y) if to is not None else None
        except (AttributeError, TypeError, ValueError):
            y = None
        if y is not None and (math.isnan(y) or y <= 0):
            y = None
    if y is None:
        probe = re.sub(r"\s+", " ", title).strip()[:40]
        try:
            hits = page.search_for(probe) if len(probe) >= 4 else []
        except RuntimeError:
            hits = []
        if hits:
            y = min(h.y0 for h in hits)
    return y


def build_sections(doc: Any, page_tokens: list[int], page_words: list[list]) -> list[dict]:
    """PDF outline -> sections: id (document numbering or title slug), title, level, parent,
    pages [start, end], tokens. Table/Figure entries are dropped; an entry with no page takes its
    first child's. A section ends on the page where the next same-or-higher entry starts, or on the
    page before when that entry sits at the top of its page."""
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
        end = n
        for e2 in entries[k + 1:]:
            if e2["level"] <= e["level"]:
                p2 = e2["start"]
                end = p2
                if p2 > e["start"]:
                    page = doc[p2 - 1]
                    y = _heading_y(page, e2["dest"], e2["title"])
                    if y is not None:
                        top = page.rect.height * 0.08
                        above = [w for w in page_words[p2 - 1] if w[3] < y - 2 and w[1] > top]
                        if len(above) <= 3:
                            end = p2 - 1
                break
        end = max(end, e["start"])
        sec = {"id": sid, "title": title, "level": e["level"], "parent": parent["id"] if parent else None,
               "pages": [e["start"], end], "tokens": sum(page_tokens[e["start"] - 1:end])}
        secs.append(sec)
        stack.append({"level": e["level"], "id": sid})
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


def tier2_map(docdir: Path, sections: list[dict]) -> dict[str, str]:
    """section id -> tier-2 file. A file `s/<secid>.md` covers its own section, its descendants,
    and any section listed in an `<!-- atlas:covers a b c -->` line inside it."""
    sdir = Path(docdir) / "s"
    out: dict[str, str] = {}
    if not sdir.is_dir():
        return out
    ids = {s["id"] for s in sections}
    children: dict[str, list[str]] = {}
    for s in sections:
        if s["parent"]:
            children.setdefault(s["parent"], []).append(s["id"])

    def descend(sid: str):
        yield sid
        for c in children.get(sid, []):
            yield from descend(c)

    files = sorted(sdir.glob("*.md"))
    explicit: dict[str, str] = {}
    for f in files:
        rel = f"s/{f.name}"
        for m in re.finditer(r"<!--\s*atlas:covers\s+([^>]*?)\s*-->", f.read_text(encoding="utf-8")):
            for sid in m.group(1).replace(",", " ").split():
                explicit.setdefault(sid, rel)
    for f in files:
        sid = f.stem
        if sid in ids:
            for d in descend(sid):
                out.setdefault(d, f"s/{f.name}")
    for sid, rel in explicit.items():
        if sid in ids:
            for d in descend(sid):
                out.setdefault(d, rel)
    return out


def write_toc(docdir: Path) -> None:
    """toc.md from toc.json + pages/pages.json + the s/ files present now."""
    docdir = Path(docdir)
    meta = common.read_json(docdir / "doc.json")
    toc = common.read_json(docdir / "toc.json")
    pinfo = common.read_json(docdir / "pages" / "pages.json")
    t2 = tier2_map(docdir, toc["sections"])
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
    need = [p for p in pinfo if set(p["classes"]) & set(NEEDS_IMAGE)]
    lines += ["", "## Pages that need an image Read", ""]
    if need:
        lines += ["The text layer alone misses these: read them with `Read(pages=N)` (about 1.5K tokens a page), "
                  "not from `pages/`.", "", "| Page | Classes | Words | Image % | Why |", "|---|---|---|---|---|"]
        for p in need:
            why = []
            if "image-heavy" in p["classes"]:
                why.append("little text layer" if p["words"] < 80 else "mostly raster image")
            if "figure" in p["classes"]:
                why.append(f"{p['figure_captions']} figure caption(s) over graphics")
            if "equation" in p["classes"]:
                why.append("math font: equations are glyph soup in the text layer")
            lines.append(f"| {p['page']} | {', '.join(p['classes'])} | {p['words']} | "
                         f"{round(100 * p['image_frac'])} | {'; '.join(why)} |")
    else:
        lines.append("None: the text layer covers every page.")
    lines += ["", "## Page classes", ""]
    by: dict[str, list[int]] = {}
    for p in pinfo:
        for c in p["classes"]:
            by.setdefault(c, []).append(p["page"])
    for c in ("table-heavy", "register", "figure", "equation", "image-heavy", "pua-glyphs", "blank"):
        if c in by:
            lines.append(f"- {c}: p.{_page_runs(by[c])}")
    if "pua-glyphs" in by:
        lines.append("  (Symbol-font private-use glyphs, such as Ohm and <=, which Read shows as nothing; "
                     "doc check matches quotes with them mapped or dropped.)")
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

    page_tokens, page_words, pinfo = [], [], []
    for i in range(1, n + 1):
        t = texts[i - 1]
        page_file(docdir, i).write_text(t, encoding="utf-8")
        pg = doc[i - 1]
        words = pg.get_text("words")
        page_words.append(words)
        tok = common.count_tokens(t)
        page_tokens.append(tok or 0)
        cls, ev = page_classes(pg, t, words)
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
    empty = sum(1 for p in pinfo if p["words"] < 5)
    meta = {
        "doc_id": doc_id, "file": pdf.name, "path": rel, "sha256": sha,
        "kind": kind or (entry or {}).get("kind") or "datasheet",
        "part": ident.get("part"), "doc_no": ident.get("doc_no"), "rev": ident.get("rev"),
        "pages": n, "title": meta_pdf.get("title"), "labels": labels,
        "text_layer": "none" if empty == n else ("partial" if empty else "full"),
        "identity_from": source,
        "tool": {"embarch-atlas": __version__, "pymupdf": pymupdf.__version__, "pdftotext": pdftotext_version()},
    }
    common.write_json(docdir / "doc.json", meta)
    common.write_json(docdir / "pages" / "pages.json", pinfo)
    secs = build_sections(doc, page_tokens, page_words)
    common.write_json(docdir / "toc.json", {"doc_id": doc_id, "sections": secs})
    write_toc(docdir)
    need = [p["page"] for p in pinfo if set(p["classes"]) & set(NEEDS_IMAGE)]
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

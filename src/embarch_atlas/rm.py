"""Reference-manual navigation: outline, the in-use section worklist, section text, cite checks, freezing.

Verbs (all deterministic; the LLM step between `rm-text` and `rm-freeze` is the
skill's, see `skills/atlas-translate-docs/reference/rm-procedure.md`):

  mcu rm-outline <rm_doc>            outline -> sections with page ranges and token costs (outline.json)
  mcu rm-sections <atlas|in_use.json> RM sections to distil for the peripherals in use -> worklist.json
  mcu rm-text <rm_doc> <item|sec>    one work item's text, cut at its headings, with [p.N] markers;
                                     it fails rather than print a text missing a page of the item's range
  mcu rm-check <rm_doc> [files]      SVD names exist, fields and numbers on the cited page, section holds page
  mcu rm-freeze <rm_doc> --item ID   record a distillation as frozen LLM output, keyed by page content;
                                     an entry it replaces (same files and sections) is marked superseded
  mcu rm-freeze <rm_doc> --retire E  mark a frozen entry superseded (kept, never deleted)

Selection rule (owner, 2026-09-29): prose is distilled per peripheral **in use**
(instantiated in the build's ELF, or touched by SoC code) plus a fixed
cross-cutting core; DT-okay-without-driver is a mismatch item, not in use.
Register sections the SVD covers are left out: the SVD carries the map and
`mcu vote` carries their field constraints as `rule` records.

Text layer: `docs/<rm>/pages/p<NNN>.txt` when `doc prep` wrote it, otherwise
`pdftotext -layout` on the PDF, run once per process. Token counts are o200k
(tiktoken) over PyMuPDF plain text per page, the proxy the research used;
the layout text layer runs about 14% higher.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable

from . import __version__
from .common import atlas_dir, count_tokens, read_json, sha256_file, sha256_text, verb_group, write_json

PROCEDURE = "rm-procedure/1"
# Cross-cutting core, always distilled when any RM section is (owner answer 5).
CORE = ["RCC", "PWR", "EXTI", "GPIO", "DMA", "DMAMUX", "NVIC", "SYSCFG", "FLASH"]
SPLIT_TOKENS = 6000   # a section costlier than this is split into its children
MERGE_TOKENS = 4000   # consecutive to-do units are batched into one item up to this

NUM_RE = re.compile(r"^(\d+(?:\.\d+)*)\s+(.*)$")
TABFIG_RE = re.compile(r"^(Table|Figure)\s+\d+", re.I)
REG_CONTAINER_RE = re.compile(r"\bregisters\s*$|\bregister map\s*$", re.I)
REG_NAME_RE = re.compile(r"\(([A-Za-z][A-Za-z0-9_]*_[A-Za-z0-9_]*)\)(?:\s*\(\s*[a-z]\s*=[^()]*\))?\s*$")


def tokens(text: str) -> int:
    n = count_tokens(text)
    return n if n is not None else (len(text) + 3) // 4


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "x"


def doc_number(doc_id: str) -> str:
    """`demo-rm@1` -> `DEMO-RM`: the token running headers and footers carry (doc.json `doc_no` wins)."""
    return doc_id.split("@")[0].upper()


# --------------------------------------------------------------------------- text layer

def norm_page(text: str) -> str:
    """Normalization before hashing: no form feeds, no trailing blanks, no trailing empty lines."""
    lines = [ln.rstrip() for ln in text.replace("\f", "").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def page_sha(text: str) -> str:
    return sha256_text(norm_page(text))


def pdftotext_pages(pdf: Path, n_pages: int | None = None) -> list[str]:
    """Layout text of every page, one pdftotext run. Falls back to per-page runs if the split is off."""
    out = subprocess.run(["pdftotext", "-layout", str(pdf), "-"], capture_output=True, text=True, check=True).stdout
    pages = out.split("\f")
    if pages and not pages[-1].strip():
        pages = pages[:-1]
    if n_pages is not None and len(pages) != n_pages:
        pages = [subprocess.run(["pdftotext", "-f", str(p), "-l", str(p), "-layout", str(pdf), "-"],
                                capture_output=True, text=True, check=True).stdout for p in range(1, n_pages + 1)]
    return pages


class TextLayer:
    """1-based page -> layout text. Reads `pages/p<NNN>.txt` when present, else the PDF."""

    def __init__(self, pages_dir: Path | None = None, pdf: Path | None = None,
                 n_pages: int | None = None, pages: list[str] | None = None):
        self.pages_dir = pages_dir
        self.pdf = pdf
        self.n_pages = n_pages
        self._pages = pages

    def _file(self, p: int) -> Path | None:
        if self.pages_dir is None:
            return None
        for name in (f"p{p:03d}.txt", f"p{p:04d}.txt"):
            f = self.pages_dir / name
            if f.exists():
                return f
        return None

    def __call__(self, p: int) -> str:
        if self._pages is not None:
            return self._pages[p - 1] if 0 < p <= len(self._pages) else ""
        f = self._file(p)
        if f is not None:
            return f.read_text(encoding="utf-8")
        if self.pdf is None:
            raise FileNotFoundError(f"no text layer for page {p} (no pages/ and no PDF)")
        self._pages = pdftotext_pages(self.pdf, self.n_pages)
        return self(p)


def strip_running(text: str, doc_no: str, n_pages: int) -> list[str]:
    """Page lines without the running header and footer (doc number, `N/total`, bare page numbers)."""
    lines = text.replace("\f", "").split("\n")
    doc_re = re.compile(rf"\b{re.escape(doc_no)}\b", re.I) if doc_no else None
    counter = re.compile(rf"^\s*\d+\s*/\s*{n_pages}\b|\b\d+\s*/\s*{n_pages}\s*$")

    def running(ln: str) -> bool:
        s = ln.strip()
        return bool(counter.search(s) or re.fullmatch(r"\d{1,5}", s) or (doc_re and doc_re.search(s) and len(s.split()) <= 12))

    idx = [i for i, ln in enumerate(lines) if ln.strip()]
    drop = set()
    for i in idx[:2]:
        if running(lines[i]):
            drop.add(i)
        else:
            break
    for i in reversed(idx[-3:]):
        if running(lines[i]):
            drop.add(i)
        else:
            break
    return [ln for i, ln in enumerate(lines) if i not in drop]


# --------------------------------------------------------------------------- outline

def _abbrevs(title: str) -> list[str]:
    out: list[str] = []
    for grp in re.findall(r"\(([^()]*)\)", title):
        for tok in re.split(r"\s*(?:/|,|\band\b)\s*", grp):
            tok = tok.strip()
            if re.fullmatch(r"[A-Z][A-Za-z0-9_-]*", tok):
                out.append(tok)
    return out


def _heading_re(e: dict) -> re.Pattern:
    """A line that starts `e`'s heading: its number plus the first title word, or its first title words."""
    words = e["title"].split()
    if re.fullmatch(r"\d+(?:\.\d+)*", e["id"]):
        tail = rf"\s+{re.escape(words[0][:12])}" if words else r"\s"
        return re.compile(rf"^\s*{re.escape(e['id'])}{tail}")
    return re.compile(r"^\s*" + r"\s+".join(re.escape(w) for w in words[:4]))


ROTATED_FRAC = 0.45  # a page whose characters run vertically at least this much is a rotated (landscape) page


def vertical_fraction(page) -> float:
    """Share of a PyMuPDF page's characters on lines whose text direction is vertical. A landscape
    table printed on a portrait page runs about 0.5-0.7 (its running header and footer stay
    horizontal); a flowchart or a figure with a rotated axis label stays well under 0.45."""
    h = v = 0
    for b in page.get_text("dict")["blocks"]:
        for ln in b.get("lines", []):
            n = sum(len(s["text"].strip()) for s in ln["spans"])
            dx, dy = ln["dir"]
            if abs(dy) > abs(dx):
                v += n
            else:
                h += n
    return round(v / (h + v), 3) if h + v else 0.0


def build_outline(toc: list, plain_tokens: list[int], layout: TextLayer | None, doc_id: str,
                  n_pages: int, meta: dict | None = None, doc_no: str | None = None,
                  vertical: list[float] | None = None) -> dict:
    """PDF outline -> entries with page ranges, token costs and register-section flags.

    `toc` is PyMuPDF's `get_toc()` ([level, title, page], 1-based pages). Table and
    Figure entries are split off (`tables`); left in, they break section splitting.
    `vertical` is each page's vertical_fraction(), kept so a later cut can tell a rotated
    page from its text direction.
    """
    doc_no = doc_no or doc_number(doc_id)
    entries: list[dict] = []
    tables: list[dict] = []
    seen: dict[str, int] = {}
    for level, title, page in toc:
        title = " ".join(str(title).split())
        if TABFIG_RE.match(title):
            tables.append({"title": title, "page": page, "level": level})
            continue
        m = NUM_RE.match(title)
        sid, name = (m.group(1), m.group(2)) if m else (slug(title), title)
        if sid in seen:
            seen[sid] += 1
            sid = f"{sid}-{seen[sid]}"
        else:
            seen[sid] = 1
        entries.append({"id": sid, "title": name, "level": level, "start": max(1, page)})
    # parents, chapters
    stack: list[dict] = []
    for e in entries:
        while stack and stack[-1]["level"] >= e["level"]:
            stack.pop()
        e["parent"] = stack[-1]["id"] if stack else None
        e["chapter"] = stack[0]["id"] if stack else e["id"]
        stack.append(e)
    # end pages: the next entry at the same or a higher level; that page too unless its heading is at the top
    body_cache: dict[int, list[str]] = {}

    def first_line(p: int) -> str:
        if layout is None:
            return ""
        if p not in body_cache:
            body_cache[p] = strip_running(layout(p), doc_no, n_pages)
        return next((ln for ln in body_cache[p] if ln.strip()), "")

    for i, e in enumerate(entries):
        nxt = next((x for x in entries[i + 1:] if x["level"] <= e["level"]), None)
        if nxt is None:
            end = n_pages
        elif nxt["start"] > e["start"] and (layout is None or _heading_re(nxt).match(first_line(nxt["start"]))):
            end = nxt["start"] - 1
        else:
            end = nxt["start"]
        e["end"] = max(e["start"], min(end, n_pages))
        e["pages"] = e["end"] - e["start"] + 1
        e["tokens"] = sum(plain_tokens[e["start"] - 1:e["end"]])
    # register sections
    kids: dict[str, list[dict]] = {}
    for e in entries:
        kids.setdefault(e["parent"], []).append(e)
    for e in entries:
        m = REG_NAME_RE.search(e["title"])
        if m:
            e["regname"] = m.group(1)
        if REG_CONTAINER_RE.search(e["title"]) and (
                e["level"] == 2 or any(REG_NAME_RE.search(k["title"]) for k in kids.get(e["id"], []))):
            e["register"] = True
    by_id = {e["id"]: e for e in entries}
    for e in entries:
        a = by_id.get(e["parent"])
        while a is not None and not e.get("register"):
            if a.get("register"):
                e["register"] = True
            a = by_id.get(a["parent"])
    for e in entries:
        if e["level"] == 1:
            e["abbrevs"] = _abbrevs(e["title"])
    return {
        "doc": doc_id, "pages": n_pages, "toc_entries": len(toc), "entries": entries, "tables": tables,
        "doc_no": doc_no, "total_tokens": sum(plain_tokens), "page_tokens": plain_tokens,
        "token_method": "o200k over PyMuPDF plain text" if count_tokens("x") is not None else "chars/4 estimate",
        **({"vertical": list(vertical)} if vertical is not None else {}),
        **(meta or {}),
    }


def outline_from_pdf(pdf: Path, doc_id: str, pages_dir: Path | None = None, doc_no: str | None = None) -> dict:
    import pymupdf

    doc = pymupdf.open(pdf)
    n = doc.page_count
    plain = [tokens(doc[i].get_text("text")) for i in range(n)]
    vertical = [vertical_fraction(doc[i]) for i in range(n)]
    layout = TextLayer(pages_dir if pages_dir and pages_dir.exists() else None, pdf, n)
    out = build_outline(doc.get_toc(simple=True), plain, layout, doc_id, n,
                        {"source": {"file": str(pdf.resolve()), "sha256": sha256_file(pdf)}}, doc_no, vertical)
    out["page_sha"] = [page_sha(layout(p)) for p in range(1, n + 1)]
    return out


# --------------------------------------------------------------------------- the RM doc

class RmDoc:
    def __init__(self, doc_id: str, outline: dict, layer: TextLayer, root: Path | None = None):
        self.doc_id = doc_id
        self.o = outline
        self.layer = layer
        self.root = root
        self.n_pages = outline["pages"]
        self.doc_no = outline.get("doc_no") or doc_number(doc_id)
        self.entries: list[dict] = outline["entries"]
        self.by_id = {e["id"]: e for e in self.entries}
        self.index = {e["id"]: i for i, e in enumerate(self.entries)}
        self._body: dict[int, list[str]] = {}
        self._sha: list[str] | None = outline.get("page_sha")
        self._vertical: dict[int, float] = {}
        self._pdf = None

    @classmethod
    def load(cls, root: Path, doc_id: str) -> "RmDoc":
        d = root / "docs" / doc_id
        if not (d / "outline.json").exists():
            raise SystemExit(f"{d}: no outline.json (run `embarch-atlas mcu rm-outline {doc_id} --pdf <rm.pdf>` first)")
        o = read_json(d / "outline.json")
        pdf = Path(o["source"]["file"]) if o.get("source") else None
        pages_dir = d / "pages"
        return cls(doc_id, o, TextLayer(pages_dir if pages_dir.exists() else None, pdf, o["pages"]), root)

    @property
    def dir(self) -> Path:
        return self.root / "docs" / self.doc_id

    def body(self, p: int) -> list[str]:
        if p not in self._body:
            self._body[p] = strip_running(self.layer(p), self.doc_no, self.n_pages)
        return self._body[p]

    def page_plain(self, p: int) -> str:
        """Whitespace-normalized page text, for 'is it on the page' checks."""
        return " ".join(" ".join(self.body(p)).split())

    def sha(self, p: int) -> str:
        if self._sha is not None and 0 < p <= len(self._sha):
            return self._sha[p - 1]
        return page_sha(self.layer(p))

    def chapters(self) -> list[dict]:
        return [e for e in self.entries if e["level"] == 1]

    def children(self, sid: str) -> list[dict]:
        return [e for e in self.entries if e["parent"] == sid]

    def descendants(self, sid: str) -> list[dict]:
        out, todo = [], [sid]
        while todo:
            k = self.children(todo.pop(0))
            out += k
            todo += [x["id"] for x in k]
        return sorted(out, key=lambda e: self.index[e["id"]])

    def next_after(self, e: dict) -> dict | None:
        """The first entry after `e` that is not inside it."""
        for x in self.entries[self.index[e["id"]] + 1:]:
            if x["level"] <= e["level"]:
                return x
        return None

    def section_range(self, sid: str) -> tuple[int, int] | None:
        e = self.by_id.get(sid)
        return (e["start"], e["end"]) if e else None

    def vertical(self, p: int) -> float | None:
        """Share of page p's characters that run vertically: outline.json `vertical` (rm-outline
        writes it), else PyMuPDF on the source PDF; None when neither is there."""
        vs = self.o.get("vertical")
        if vs is not None and 0 < p <= len(vs):
            return vs[p - 1]
        if p in self._vertical:
            return self._vertical[p]
        pdf = self.layer.pdf
        if pdf is None or not Path(pdf).exists() or not 0 < p <= self.n_pages:
            return None
        if self._pdf is None:
            import pymupdf

            self._pdf = pymupdf.open(pdf)
        self._vertical[p] = vertical_fraction(self._pdf[p - 1])
        return self._vertical[p]

    def rotated(self, p: int) -> bool:
        """A landscape page printed rotated, decided from the text direction of its words (a
        flowchart's one-word-per-line layout text is not a rotated table). False when unknown."""
        v = self.vertical(p)
        return v is not None and v >= ROTATED_FRAC

    def cut(self, start: dict, stop: dict | None, first: int, last: int,
            keep_pages: bool = False) -> list[tuple[int, str]]:
        """(page, line) from `start`'s heading to `stop`'s heading, within pages first..last.

        The stop heading counts only on or after the page the outline starts it on. When it is not
        found there (it lies past `last`, or the layout text does not show it), the cut runs to
        `last`: a missing heading never shortens the range. One exception, for the register parser
        (`keep_pages=False`): when the stop starts inside the range on a rotated page, whose heading
        the layout text splits, the cut ends where that page begins. `keep_pages=True` (rm-text)
        keeps every page of the range: that page is kept whole, and a start heading missing from
        `first` starts the cut at the top of `first`.
        """
        lines: list[tuple[int, str]] = []
        for p in range(first, last + 1):
            lines += [(p, ln) for ln in self.body(p)]
        hs = _heading_re(start)
        s = next((k for k, (p, ln) in enumerate(lines) if p == first and hs.match(ln)), None)
        if s is None:
            s = 0 if keep_pages else next((k for k, (p, ln) in enumerate(lines) if hs.match(ln)), 0)
        e = len(lines)
        if stop is not None:
            he = _heading_re(stop)
            at = stop.get("start", first)
            e = next((k for k, (p, ln) in enumerate(lines) if k > s and p >= at and he.match(ln)), None)
            if e is None and first < at <= last:
                if not keep_pages and self.rotated(at):
                    # a rotated (landscape) page: its heading is split and its line order scrambled
                    e = next((k for k, (p, _) in enumerate(lines) if k > s and p == at), None)
                else:  # the heading split, the id standing alone on its line
                    e = next((k for k, (p, ln) in enumerate(lines) if k > s and p >= at and ln.strip() == stop["id"]), None)
            if e is None:
                e = len(lines)
        return lines[s:e]

    def unit_lines(self, u: dict) -> list[tuple[int, str]]:
        e = self.by_id[u["id"]]
        if u.get("intro"):
            kids = self.children(e["id"])
            stop = kids[0] if kids else self.next_after(e)
        else:
            stop = self.next_after(e)
        return self.cut(e, stop, u["pages"][0], u["pages"][1], keep_pages=True)


def missing_pages(u: dict, lines: list[tuple[int, str]]) -> list[int]:
    """Pages of a unit's range that its cut text does not reach (rm-text's guard: it must be none)."""
    got = {p for p, _ in lines}
    return [p for p in range(u["pages"][0], u["pages"][1] + 1) if p not in got]


def render_lines(lines: list[tuple[int, str]]) -> str:
    out, cur = [], None
    for p, ln in lines:
        if p != cur:
            out.append(f"[p.{p}]")
            cur = p
        out.append(ln.rstrip())
    return "\n".join(out).strip("\n") + "\n"


# --------------------------------------------------------------------------- mapping instances to chapters

def _candidates(name: str, svd) -> list[str]:
    c = [name]
    if svd is not None and name in svd.peripherals:
        base = svd.base_of(name)
        c += [base, svd.peripherals[name].get("group") or "", svd.peripherals.get(base, {}).get("group") or ""]
    c += [re.sub(r"\d+$", "", name), *name.split("_")]
    if re.fullmatch(r"[A-Z]{4,}[A-Z]", name):  # GPIOA -> GPIO
        c.append(name[:-1])
    out: list[str] = []
    for x in c:
        if x and x not in out:
            out.append(x)
    return out


def map_chapter(name: str, rm: RmDoc, svd=None) -> tuple[str | None, str | None]:
    """Peripheral instance (UART1, GPIOA, NVIC) -> RM chapter id, via SVD groups and outline titles."""
    chs = rm.chapters()
    for cand in _candidates(name, svd):
        hits = [ch for ch in chs if cand.upper() in [a.upper() for a in ch.get("abbrevs", [])]]
        if len(hits) == 1:
            return hits[0]["id"], f"{cand} in the chapter title"
    for cand in _candidates(name, svd):
        if len(cand) < 3:
            continue
        hits = [ch for ch in chs if re.search(rf"\b{re.escape(cand)}\b", ch["title"], re.I)]
        if len(hits) == 1:
            return hits[0]["id"], f"'{cand}' in the chapter title"
    return None, None


# --------------------------------------------------------------------------- in use -> worklist

def load_in_use(path: Path) -> tuple[dict, Path]:
    f = path / "in_use.json" if path.is_dir() else path
    if not f.exists():
        raise SystemExit(f"{f}: no in_use.json (atlas-build's `join` writes it)")
    return read_json(f), f


def peripheral_states(in_use: dict) -> dict[str, dict]:
    out = {}
    for name, v in (in_use.get("peripherals") or {}).items():
        why = [k for k in ("instantiated", "touched_by_code") if v.get(k)]
        state = "in-use" if why else ("dt-only" if v.get("dt_okay") else "off")
        out[name] = {"state": state, "why": why or (["dt_okay"] if v.get("dt_okay") else [])}
    return out


def _svd_covers(rm: RmDoc, container: dict, svd, periphs: list[str]) -> bool:
    """True when the SVD defines any register named in this register section (or no SVD is given)."""
    if svd is None:
        return True
    names = [d["regname"] for d in rm.descendants(container["id"]) if d.get("regname")]
    if not names:
        return bool(periphs)
    pool = {r for per in periphs for r in svd.regs.get(per, {})}
    for n in names:
        core = n.split("_", 1)[1] if "_" in n else n
        pat = re.compile("^(?:[A-Za-z0-9]+_)?" + re.escape(core).replace("x", "[0-9A-Za-z]*") + "$")
        if any(pat.match(r) for r in pool):
            return True
    return False


def chapter_svd_periphs(rm: RmDoc, chapter: str, svd) -> list[str]:
    if svd is None:
        return []
    return [n for n in svd.definitions() if map_chapter(n, rm, svd)[0] == chapter]


def _mentions_other(title: str, others: list[str], mine: list[str]) -> bool:
    t = title.upper()
    hit_other = any(re.search(rf"\b{re.escape(o.upper())}\b", t) for o in others)
    hit_mine = any(re.search(rf"\b{re.escape(m.upper())}\b", t) for m in mine)
    return hit_other and not hit_mine


def units_for_chapter(rm: RmDoc, ch: dict, svd, used_abbrevs: list[str] | None) -> tuple[list[dict], list[dict]]:
    """(units to distil, excluded sections) for one chapter."""
    periphs = chapter_svd_periphs(rm, ch["id"], svd)
    abbrevs = ch.get("abbrevs", [])
    others = [a for a in abbrevs if used_abbrevs and a not in used_abbrevs]
    units: list[dict] = []
    excluded: list[dict] = []

    def unit(e: dict, intro: bool) -> dict:
        if intro:
            k0 = rm.children(e["id"])[0]
            top = _heading_re(k0).match(next((ln for ln in rm.body(k0["start"]) if ln.strip()), ""))
            last = k0["start"] - 1 if (k0["start"] > e["start"] and top) else k0["start"]
            pages = [e["start"], max(e["start"], last)]
        else:
            pages = [e["start"], e["end"]]
        u = {"id": e["id"], "title": e["title"], "chapter": ch["id"], "pages": pages, **({"intro": True} if intro else {})}
        u["tokens"] = tokens(render_lines(rm.unit_lines(u)))
        u["key"] = unit_key(rm, u)
        return u

    def walk(e: dict) -> None:
        if e.get("register"):
            if _svd_covers(rm, e, svd, periphs):
                excluded.append({"id": e["id"], "title": e["title"], "pages": [e["start"], e["end"]],
                                 "tokens": e["tokens"], "why": "register section: SVD map + `mcu vote` rule records"})
                return
        if others and _mentions_other(e["title"], others, used_abbrevs or []):
            excluded.append({"id": e["id"], "title": e["title"], "pages": [e["start"], e["end"]],
                             "tokens": e["tokens"], "why": f"about {', '.join(others)}, not in use"})
            return
        kids = [k for k in rm.children(e["id"])]
        if e["tokens"] > SPLIT_TOKENS and kids:
            units.append(unit(e, True))
            for k in kids:
                walk(k)
        else:
            units.append(unit(e, False))

    for k in rm.children(ch["id"]):
        walk(k)
    if not rm.children(ch["id"]):
        units.append(unit(ch, False))
    return units, excluded


def unit_key(rm: RmDoc, u: dict) -> str:
    """Content key of one unit: its section id and the hashes of its pages (doc-independent)."""
    payload = {"sec": u["id"], "intro": bool(u.get("intro")), "procedure": PROCEDURE,
               "pages": [rm.sha(p) for p in range(u["pages"][0], u["pages"][1] + 1)]}
    return sha256_text(json.dumps(payload, sort_keys=True))


def rm_entries(root: Path, doc_id: str) -> list[tuple[Path, dict]]:
    """This RM's frozen cache entries (`kind: rm-distillation`), in file-name order."""
    cache = root / "cache" / "llm"
    out = []
    for f in sorted(cache.glob("*.json")) if cache.is_dir() else []:
        try:
            e = read_json(f)
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(e, dict) and e.get("kind") == "rm-distillation" and e.get("doc") == doc_id:
            out.append((f, e))
    return out


def frozen_index(root: Path, doc_id: str) -> dict[str, dict]:
    """unit key -> the frozen cache entry that decides the unit's status (`entry` = its file name,
    `files_ok`, `others` = the other entries holding the same key).

    Several entries can hold one unit key: a section file and a must file frozen over the same
    section, or one card frozen by two models. The one whose recorded file hashes match the files
    now on disk wins, then the newest (`created`); a superseded entry (`rm-freeze --retire`, or
    replaced by a later freeze that covers it) is never used. So a stale entry cannot shadow a
    current one, whatever its file name sorts as.
    """
    cands: dict[str, list[dict]] = {}
    d = root / "docs" / doc_id
    for f, e in rm_entries(root, doc_id):
        if e.get("superseded"):
            continue
        ok = all((d / rel).exists() and sha256_file(d / rel) == h for rel, h in e.get("files", {}).items())
        for sec, key in e.get("sections", {}).items():
            cands.setdefault(key, []).append({"entry": f.name, "sec": sec, "files": sorted(e.get("files", {})),
                                              "files_ok": ok, "created": e.get("created") or "",
                                              "model": e.get("model")})
    out: dict[str, dict] = {}
    for key, cs in cands.items():
        cs.sort(key=lambda c: (c["files_ok"], c["created"].rstrip("Z"), c["entry"]), reverse=True)
        out[key] = dict(cs[0], others=[c["entry"] for c in cs[1:]],
                        stale={c["entry"]: c["files"] for c in cs if not c["files_ok"]})
    return out


def _now() -> str:
    """UTC with microseconds, so two freezes in one second still order (`created` sorts as text)."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def supersede(path: Path, by: str | None, why: str) -> dict:
    """Mark a frozen entry superseded (never deleted): frozen_index() stops using it."""
    e = read_json(path)
    e["superseded"] = {"at": _now(), "by": by, "why": why}
    write_json(path, e)
    return e


def covered_by(old: dict, new: dict) -> bool:
    """A new entry replaces an old one when it freezes every file and every section the old one did."""
    return (set(old.get("files", {})) <= set(new.get("files", {}))
            and set(old.get("sections", {})) <= set(new.get("sections", {})))


def find_entry(root: Path, doc_id: str, name: str) -> Path:
    """A cache entry by file name, key, or a unique key prefix (rm-sections prints 12 characters)."""
    stem = name.removesuffix(".json")
    hits = [f for f, _ in rm_entries(root, doc_id) if f.stem == stem] or \
           [f for f, _ in rm_entries(root, doc_id) if f.stem.startswith(stem)]
    if len(hits) != 1:
        raise SystemExit(f"{doc_id}: {'no' if not hits else len(hits)} frozen entries match {name!r} in {root / 'cache' / 'llm'}")
    return hits[0]


def batch(units: list[dict], budget: int = MERGE_TOKENS) -> list[list[dict]]:
    items: list[list[dict]] = []
    cur: list[dict] = []
    for u in units:
        if cur and sum(x["tokens"] for x in cur) + u["tokens"] > budget:
            items.append(cur)
            cur = []
        cur.append(u)
    if cur:
        items.append(cur)
    return items


def build_worklist(rm: RmDoc, in_use: dict, svd=None, core: Iterable[str] = CORE,
                   frozen: dict[str, dict] | None = None) -> dict:
    frozen = frozen or {}
    states = peripheral_states(in_use)
    periph_out: dict[str, dict] = {}
    chapters: dict[str, dict] = {}
    used_abbrevs: dict[str, list[str]] = {}
    for name, st in sorted(states.items()):
        ch, how = map_chapter(name, rm, svd)
        periph_out[name] = {**st, "chapter": ch, "mapped_by": how}
        if st["state"] == "in-use" and ch:
            chapters.setdefault(ch, {"why": []})["why"].append(f"{name}: {'+'.join(st['why'])}")
            abbr = next((c for c in _candidates(name, svd)
                         if c.upper() in [a.upper() for a in rm.by_id[ch].get("abbrevs", [])]), None)
            if abbr:
                used_abbrevs.setdefault(ch, []).append(next(a for a in rm.by_id[ch]["abbrevs"] if a.upper() == abbr.upper()))
    core_out = {}
    for name in core:
        ch, how = map_chapter(name, rm, svd)
        core_out[name] = ch
        if ch:
            chapters.setdefault(ch, {"why": []})["why"].append(f"{name}: core")
            used_abbrevs[ch] = []  # core chapters are taken whole (minus register sections)
    ch_rows, items, excluded_all = [], [], []
    order = sorted(chapters, key=lambda c: rm.index[c])
    for cid in order:
        ch = rm.by_id[cid]
        units, excluded = units_for_chapter(rm, ch, svd, used_abbrevs.get(cid) or None)
        excluded_all += [dict(x, chapter=cid) for x in excluded]
        reg_tok = sum(x["tokens"] for x in excluded)
        todo: list[dict] = []
        n_frozen = 0
        run: list[dict] = []
        stale: dict[str, list[str]] = {}
        for u in units:
            fz = frozen.get(u["key"])
            stale.update(fz.get("stale", {}) if fz else {})
            if fz:
                u["status"] = "frozen" if fz["files_ok"] else "frozen-files-changed"
                u["frozen_by"] = fz["entry"]
                n_frozen += 1
                if run:
                    todo.append(run)
                    run = []
            else:
                u["status"] = "todo"
                run.append(u)
        if run:
            todo.append(run)
        for group in todo:
            for b in batch(group):
                iid = b[0]["id"] if len(b) == 1 else f"{b[0]['id']}-{b[-1]['id']}"
                items.append({"id": iid, "chapter": cid, "sections": [u["id"] for u in b],
                              "pages": [min(u["pages"][0] for u in b), max(u["pages"][1] for u in b)],
                              "tokens": sum(u["tokens"] for u in b), "output": f"s/{iid}.md"})
        d = rm.dir if rm.root else None
        ch_rows.append({
            "chapter": cid, "title": ch["title"], "why": chapters[cid]["why"],
            "pages": [ch["start"], ch["end"]], "n_pages": ch["pages"], "tokens": ch["tokens"],
            "register_tokens": reg_tok, "prose_tokens": ch["tokens"] - reg_tok,
            "units": units, "n_units": len(units), "n_frozen": n_frozen, "n_todo": len(units) - n_frozen,
            "unit_tokens": sum(u["tokens"] for u in units),
            "files": {k: f"s/{cid}.{k}.md" for k in ("card", "must", "fields")},
            "files_present": {k: bool(d and (d / "s" / f"{cid}.{k}.md").exists()) for k in ("card", "must", "fields")},
            # frozen entries over this chapter's units whose files changed since: re-freeze or retire them
            "stale_entries": dict(sorted(stale.items())),
        })
    not_in_use = []
    for name, p in sorted(periph_out.items()):
        if p["state"] == "dt-only":
            ch = rm.by_id.get(p["chapter"]) if p["chapter"] else None
            not_in_use.append({"peripheral": name, "chapter": p["chapter"],
                               "pages": [ch["start"], ch["end"]] if ch else None,
                               "tokens": ch["tokens"] if ch else None,
                               "why": "DT okay, no driver instantiated and not touched by code: a mismatch item"})
    tot = {
        "chapters": len(ch_rows), "pages": sum(c["n_pages"] for c in ch_rows),
        "tokens": sum(c["tokens"] for c in ch_rows), "prose_tokens": sum(c["prose_tokens"] for c in ch_rows),
        "units": sum(c["n_units"] for c in ch_rows), "frozen": sum(c["n_frozen"] for c in ch_rows),
        "todo_units": sum(c["n_todo"] for c in ch_rows), "items": len(items),
        "todo_tokens": sum(i["tokens"] for i in items), "rm_pages": rm.n_pages, "rm_tokens": rm.o["total_tokens"],
    }
    return {
        "doc": rm.doc_id, "procedure": PROCEDURE, "tool": f"embarch-atlas {__version__} mcu rm-sections",
        "rule": "in use = instantiated or touched_by_code; plus core; register sections the SVD covers excluded",
        "core": core_out, "peripherals": periph_out, "chapters": ch_rows, "items": items,
        "excluded": excluded_all, "not_in_use": not_in_use,
        "unmapped": sorted(n for n, p in periph_out.items() if p["state"] == "in-use" and not p["chapter"]),
        "totals": tot,
    }


# --------------------------------------------------------------------------- rm-check (cite verification)

CITE_RE = re.compile(r"\[§([\d.]+) p\.(\d+)(?:\s*[-–]\s*(\d+))?\]")
IDENT_RE = re.compile(r"`([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+){1,2})`")
BARE_RE = re.compile(r"`([A-Z][A-Z0-9_]*[A-Z0-9])`")
OTHER_CITE_RE = re.compile(r"\[(?:DB|[A-Za-z0-9_-]+@[A-Za-z0-9_.-]+)[^\]]*\]")
# a number: hex, binary or decimal, not glued after letters or digits (EN1, I2C are names); a unit
# or an ordinal may follow it (10ms, 8th)
NUMBER_RE = re.compile(r"(?<![\w.])(0x[0-9A-Fa-f]+|0b[01]+|\d+(?:\.\d+)?)(?![0-9A-Fa-f]*\.?\d)")
TRIVIAL = {"0", "1", "2", "3"}  # too common to test


def _spaces(m: re.Match) -> str:
    return " " * len(m.group())


def number_claims(line: str) -> list[tuple[str, str | None]]:
    """(number as written, tag class or None) for one line of distilled RM prose, by M's rule
    (check.tagged_pieces): a tag or an `[SVD]` cite covers the numbers before it in its clause and
    inside itself, never the whole line. Cites, section references, list markers, `Tier N`, token
    counts and backticked identifiers or file names are not claims."""
    from .check import SECTION_NUM, SECTION_REF, blank_not_claims, tagged_pieces

    body = CITE_RE.sub(_spaces, line)
    body = OTHER_CITE_RE.sub(_spaces, body)
    body = SECTION_NUM.sub(_spaces, SECTION_REF.sub(_spaces, body))   # §6.3, 9.4.16-9.8: references
    body = blank_not_claims(body)
    body = re.sub(r"(?<=\d)\.\.(?=\d)", "  ", body)       # 1..15 is 1 and 15
    out = []
    for piece, cls in tagged_pieces(body):
        out += [(n, cls) for n in NUMBER_RE.findall(piece)]
    return out


def on_pages(n: str, text: str) -> bool:
    """Is the number `n` (as written) on the page text? 0x.. matches any case; 0b.. also matches its
    bare digits (the RM prints binary field values as `10:`)."""
    if n.lower().startswith("0x"):
        return bool(re.search(rf"(?<![\w.]){re.escape(n)}(?![0-9A-Fa-f])", text, re.I))
    if n.lower().startswith("0b"):
        return bool(re.search(rf"(?<![\w.])(?:0b)?{n[2:]}(?![01])", text))
    return bool(re.search(rf"(?<![\w.]){re.escape(n)}(?!\.?\d)", text))


def _stem(name: str) -> str:
    return re.sub(r"\d+$", "", name)


def _unprefixed(name: str) -> str:
    """`BUS_TXDR` -> `TXDR`, `PORTx_MODER` -> `MODER`: without an instance or block prefix."""
    m = re.match(r"^[A-Z][A-Z0-9]*x?_(.+)$", name)
    return m.group(1) if m else name


def bare_name(svd, name: str, periphs: list[str]) -> bool | None:
    """A bare backticked name (`TRGR1`, `BUS_TXDR`) on a chapter's page, against the SVD: True when
    it is a register or field of the chapter's peripherals, or the family stem of registers
    (`SELCR` for SELCR1..4); False when it has the shape of one of their registers (the same stem)
    and is none of them (`TRGR3`); None when it is some other name (an interrupt `LINE0_1`, a pin,
    a peripheral): not checked."""
    if svd is None or not periphs:
        return None
    regs: set[str] = set()
    fields: set[str] = set()
    for per in periphs:
        for r in (svd.regs.get(per) or svd.regs.get(svd.base_of(per)) or {}).values():
            regs |= {r["name"], _unprefixed(r["name"])}
            fields |= set(r["fields"])
    if not regs:
        return None
    if name in regs or name in fields or name in svd.peripherals:
        return True
    stems = {_stem(r) for r in regs} - {""}
    if name in stems:
        return True
    if _unprefixed(name) in regs:
        return True
    if _stem(name) in stems or _stem(_unprefixed(name)) in stems:
        return False
    return None


def check_files(rm: RmDoc, svd, files: list[Path], periphs: list[str] | None = None,
                base: Path | None = None) -> dict:
    """The deterministic second pass over distilled RM files (spike 13's verify_cites).

    1. every backticked `REG.FIELD`, `PERIPH.REG` or `PERIPH.REG.FIELD` exists in the SVD, and so
       does every bare backticked name shaped like one of the chapter's registers (bare_name);
    2. every field named on a cited line is on a cited page (fields of other peripherals'
       registers, written `PERIPH.REG.FIELD`, are exempt: they cite another chapter's facts);
    3. every `[§S p.N]` has page N inside section S;
    4. every number on a cited line is on a cited page (hex, 0b binary, decimals, a number glued
       to its unit), unless a tag (derived), (inference), (figure), (equation) or an `[SVD]` cite
       covers it: a tag covers the numbers before it in its clause, as doc check M reads prose
       (number_claims). 0-3 are too common to test.
    """
    tot = dict(lines=0, cites=0, names=0, names_bad=0, support=0, support_bad=0, sec_bad=0, numbers=0, numbers_bad=0,
               numbers_tagged=0)
    fails: list[str] = []
    local = set(periphs or [])
    for fn in files:
        label = str(fn.relative_to(base)) if base and fn.is_relative_to(base) else fn.name
        for ln, line in enumerate(fn.read_text(encoding="utf-8").splitlines(), 1):
            cites = []
            for m in CITE_RE.finditer(line):
                a = int(m.group(2))
                b = int(m.group(3)) if m.group(3) else a
                cites.append((m.group(1), list(range(a, b + 1))))
            idents = IDENT_RE.findall(line)
            bare = BARE_RE.findall(line)
            if not cites and not idents and not bare:
                continue
            tot["lines"] += 1
            tot["cites"] += len(cites)
            pages = sorted({p for _, ps in cites for p in ps})
            for s, ps in cites:
                r = rm.section_range(s)
                for p in ps:
                    if not r or not (r[0] <= p <= r[1]):
                        tot["sec_bad"] += 1
                        fails.append(f"{label}:{ln} §{s} does not contain p.{p} (section pages {list(r) if r else None})")
            text = " ".join(rm.page_plain(p) for p in pages) if pages else ""
            if cites:
                seen: set[str] = set()
                for n, tag in number_claims(line):
                    if tag is not None:
                        tot["numbers_tagged"] += 1
                        continue
                    if n in TRIVIAL or (n.lower().startswith("0b") and len(n) <= 3) or n in seen:
                        continue
                    seen.add(n)
                    tot["numbers"] += 1
                    if not on_pages(n, text):
                        tot["numbers_bad"] += 1
                        fails.append(f"{label}:{ln} number {n} not on cited page(s) {pages}")
            for ident in idents:
                if ident == ident.lower():  # a file name (`worklist.json`), not a register
                    continue
                ok = svd.has(ident, sorted(local) or None) if svd is not None else None
                if ok is None:
                    continue
                tot["names"] += 1
                if not ok:
                    tot["names_bad"] += 1
                    fails.append(f"{label}:{ln} `{ident}` not in the SVD")
                parts = ident.split(".")
                foreign = len(parts) == 3 and parts[0] not in local
                if cites and not foreign and not (len(parts) == 2 and parts[0] in svd.regs):
                    f = parts[-1]
                    tot["support"] += 1
                    if not re.search(rf"\b{re.escape(f)}\b", text):
                        tot["support_bad"] += 1
                        fails.append(f"{label}:{ln} field {f} not on cited page(s) {pages}")
            for name in dict.fromkeys(bare):
                ok = bare_name(svd, name, sorted(local))
                if ok is None:
                    continue
                tot["names"] += 1
                if not ok:
                    tot["names_bad"] += 1
                    fails.append(f"{label}:{ln} `{name}` not in the SVD (no such register of {', '.join(sorted(local))})")
    return {"totals": tot, "fails": fails,
            "ok": tot["names_bad"] == 0 and tot["support_bad"] == 0 and tot["sec_bad"] == 0 and tot["numbers_bad"] == 0}


# --------------------------------------------------------------------------- CLI

def _project(root: Path) -> dict:
    toml = root / "atlas.toml"
    if not toml.exists():
        return {}
    import tomllib

    return tomllib.loads(toml.read_text(encoding="utf-8"))


def doc_meta(root: Path, doc_id: str) -> dict:
    """What the inventory manifest and doc.json know about a document (file, doc_no, part, rev)."""
    meta: dict = {}
    man = root / "inventory" / "manifest.json"
    if man.exists():
        docs = read_json(man).get("documents", [])
        for e in docs if isinstance(docs, list) else docs.values():
            if e.get("doc_id") == doc_id:
                meta.update({k: e.get(k) for k in ("file", "doc_no", "part", "rev") if e.get(k)})
    dj = root / "docs" / doc_id / "doc.json"
    if dj.exists():
        meta.update({k: v for k, v in read_json(dj).items() if k in ("file", "doc_no", "part", "rev") and v})
    return meta


def _resolve_pdf(root: Path, doc_id: str, explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    f = doc_meta(root, doc_id).get("file")
    if f:
        proj = _project(root).get("project", {})
        fw = Path(proj.get("fw", root.parent.parent))
        for cand in (Path(f), root / f, fw / proj.get("sources", "") / f, fw / f):
            if cand.exists():
                return cand
    raise SystemExit(f"{doc_id}: no PDF found in the manifest or doc.json; pass --pdf <file>")


def default_core(root: Path) -> list[str]:
    """`[mcu] rm_core` in atlas.toml, else the owner's cross-cutting core."""
    core = _project(root).get("mcu", {}).get("rm_core")
    return list(core) if core else list(CORE)


def cmd_outline(args) -> int:
    root = atlas_dir(args.atlas_dir)
    pdf = _resolve_pdf(root, args.rm_doc, args.pdf)
    d = root / "docs" / args.rm_doc
    o = outline_from_pdf(pdf, args.rm_doc, d / "pages", doc_meta(root, args.rm_doc).get("doc_no"))
    write_json(d / "outline.json", o)
    chs = [e for e in o["entries"] if e["level"] == 1]
    print(f"{args.rm_doc}: {o['pages']} pages, {o['toc_entries']} outline entries "
          f"({len(o['tables'])} Table/Figure entries set aside), {len(o['entries'])} sections, "
          f"{len(chs)} chapters, {o['total_tokens']:,} tokens ({o['token_method']}) -> {d / 'outline.json'}")
    if args.verbose:
        for c in chs:
            print(f"  {c['id']:>4} {c['start']:5}-{c['end']:<5} {c['pages']:4}p {c['tokens']:7,}  {c['title']}")
    return 0


def _rm_doc_arg(root: Path, given: str | None) -> str:
    if given:
        return given
    found = [p.parent.name for p in (root / "docs").glob("*/outline.json")]
    if len(found) == 1:
        return found[0]
    raise SystemExit(f"pass --rm-doc (outline.json found for: {found or 'none'})")


def _svd(root: Path, given: str | None):
    from .svd import SvdDoc, find_svd_doc

    doc = given or find_svd_doc(root)
    return SvdDoc.load(root, doc) if doc else None


def cmd_sections(args) -> int:
    root = atlas_dir(args.atlas_dir)
    rm = RmDoc.load(root, _rm_doc_arg(root, args.rm_doc))
    svd = _svd(root, args.svd_doc)
    in_use, src = load_in_use(Path(args.atlas))
    core = [c for c in args.core.split(",") if c] if args.core is not None else default_core(root)
    wl = build_worklist(rm, in_use, svd, core, frozen_index(root, rm.doc_id))
    wl["atlas"] = str(Path(args.atlas).resolve())
    wl["in_use"] = str(src.resolve())
    wl["created"] = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = rm.dir / "worklist.json"
    write_json(out, wl)
    t = wl["totals"]
    print(f"{rm.doc_id}: {t['chapters']} chapters, {t['pages']} pages, {t['tokens']:,} tokens, "
          f"{t['prose_tokens']:,} prose tokens (register sections left to the SVD); "
          f"{t['units']} units: {t['frozen']} frozen, {t['todo_units']} to do in {t['items']} items "
          f"({t['todo_tokens']:,} tokens) -> {out}")
    for c in wl["chapters"]:
        print(f"  {c['chapter']:>4} {c['pages'][0]:5}-{c['pages'][1]:<5} {c['n_pages']:4}p "
              f"prose {c['prose_tokens']:7,}  units {c['n_units']:3} (frozen {c['n_frozen']})  {c['title']}  [{'; '.join(c['why'])}]")
        for entry, files in c.get("stale_entries", {}).items():
            print(f"       stale frozen entry {entry[:12]} ({', '.join(files)} changed since): re-freeze, "
                  "or `rm-freeze --retire`")
    if wl["not_in_use"]:
        print("  not in use (DT okay, no driver): " + ", ".join(
            f"{x['peripheral']} (ch.{x['chapter']})" for x in wl["not_in_use"]))
    if wl["unmapped"]:
        print("  in use, no RM chapter: " + ", ".join(wl["unmapped"]))
    return 0


def _units_by_id(rm: RmDoc) -> dict[str, dict]:
    wl = rm.dir / "worklist.json"
    out: dict[str, dict] = {}
    if wl.exists():
        for c in read_json(wl)["chapters"]:
            for u in c["units"]:
                out[u["id"]] = u
    return out


def _resolve_units(rm: RmDoc, item: str | None, sections: str | None) -> tuple[str, list[dict]]:
    known = _units_by_id(rm)
    if item:
        wl = read_json(rm.dir / "worklist.json") if (rm.dir / "worklist.json").exists() else {"items": []}
        it = next((i for i in wl["items"] if i["id"] == item), None)
        ids = it["sections"] if it else ([item] if item in known or item in rm.by_id else None)
        if ids is None:
            raise SystemExit(f"no item or section {item!r} in {rm.dir / 'worklist.json'}")
        name = item
    else:
        ids = [s for s in (sections or "").split(",") if s]
        if not ids:
            raise SystemExit("give --item or --sections")
        name = ids[0] if len(ids) == 1 else f"{ids[0]}-{ids[-1]}"
    units = []
    for sid in ids:
        if sid in known:
            u = dict(known[sid])
        elif sid in rm.by_id:
            e = rm.by_id[sid]
            u = {"id": sid, "title": e["title"], "chapter": e["chapter"], "pages": [e["start"], e["end"]]}
        else:
            raise SystemExit(f"{rm.doc_id}: no section {sid!r}")
        u["key"] = unit_key(rm, u)
        units.append(u)
    return name, units


def cmd_text(args) -> int:
    root = atlas_dir(args.atlas_dir)
    rm = RmDoc.load(root, args.rm_doc)
    _, units = _resolve_units(rm, args.what, None)
    out = []
    for u in units:
        lines = rm.unit_lines(u)
        gap = missing_pages(u, lines)
        if gap:  # the guard: a cut that loses pages would be distilled without them, silently
            raise SystemExit(f"rm-text {u['id']}: the text misses p.{', p.'.join(map(str, gap))} of its range "
                             f"p.{u['pages'][0]}-{u['pages'][1]}; nothing printed (a tool bug: report it, "
                             "and read those pages with Read meanwhile)")
        out.append(render_lines(lines))
    sys.stdout.write("".join(out))
    return 0


def cmd_check(args) -> int:
    root = atlas_dir(args.atlas_dir)
    rm = RmDoc.load(root, args.rm_doc)
    svd = _svd(root, args.svd_doc)
    files = [Path(f) for f in args.files] if args.files else sorted(
        f for f in (rm.dir / "s").glob("*.md") if not f.name.endswith(".fields.md"))
    if args.chapter:
        files = [f for f in files if f.name.split(".")[0] == args.chapter or f.name.startswith(args.chapter + "-")]
    by_chapter: dict[str, list[Path]] = {}
    for f in files:
        by_chapter.setdefault(f.name.split(".")[0].split("-")[0], []).append(f)
    report_path = rm.dir / "rm_checks.json"
    report = read_json(report_path) if report_path.exists() else {"files": {}}
    all_ok = True
    for ch, fs in sorted(by_chapter.items()):
        periphs = []
        if svd is not None and ch in rm.by_id:
            periphs = [n for n in svd.peripherals if map_chapter(n, rm, svd)[0] == ch]
        res = check_files(rm, svd, fs, periphs, rm.dir)
        all_ok &= res["ok"]
        for f in fs:
            rel = str(f.relative_to(rm.dir)) if f.is_relative_to(rm.dir) else str(f)
            mine = [x for x in res["fails"] if x.startswith(rel + ":")]
            report["files"][rel] = {"sha256": sha256_file(f), "fails": mine}
        print(f"ch.{ch}: {json.dumps(res['totals'])}")
        for x in res["fails"]:
            print("  " + x)
    report["updated"] = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    write_json(report_path, report)
    return 0 if all_ok else 1


def cmd_freeze(args) -> int:
    root = atlas_dir(args.atlas_dir)
    if args.retire:
        path = find_entry(root, args.rm_doc, args.retire)
        by = find_entry(root, args.rm_doc, args.by).name if args.by else None
        if by == path.name:
            raise SystemExit("--by names the entry being retired")
        e = supersede(path, by, args.note or "retired by rm-freeze --retire")
        print(f"retired cache/llm/{path.name} ({', '.join(e.get('sections', {}))}; model {e.get('model')})"
              + (f", superseded by {by}" if by else "") + ": marked, not deleted")
        return 0
    if not (args.item or args.sections):
        raise SystemExit("rm-freeze: give --item or --sections (or --retire <entry>)")
    if not args.model:
        raise SystemExit("rm-freeze: --model is required when freezing")
    rm = RmDoc.load(root, args.rm_doc)
    name, units = _resolve_units(rm, args.item, args.sections)
    files = [f for f in (args.files or "").split(",") if f] or [f"s/{name}.md"]
    hashes = {}
    for rel in files:
        f = rm.dir / rel
        if not f.exists():
            raise SystemExit(f"{f}: missing; write the distillation first")
        hashes[rel] = sha256_file(f)
    checks = read_json(rm.dir / "rm_checks.json")["files"] if (rm.dir / "rm_checks.json").exists() else {}
    entry = {
        "kind": "rm-distillation", "doc": rm.doc_id, "procedure": PROCEDURE, "model": args.model,
        "sections": {u["id"]: u["key"] for u in units},
        "pages": {u["id"]: u["pages"] for u in units},
        "files": hashes,
        "checks": {rel: (len(checks[rel]["fails"]) if rel in checks and checks[rel]["sha256"] == h else None)
                   for rel, h in hashes.items()},
        "created": _now(),
        **({"note": args.note} if args.note else {}),
    }
    key = sha256_text(json.dumps({"procedure": PROCEDURE, "model": args.model,
                                  "units": sorted(entry["sections"].values())}, sort_keys=True))
    # entries this one replaces (the same files and sections, frozen earlier or by another model):
    # marked superseded so they can never decide a unit's status again
    old = [(f, e) for f, e in rm_entries(root, rm.doc_id)
           if f.stem != key and not e.get("superseded") and covered_by(e, entry)]
    write_json(root / "cache" / "llm" / f"{key}.json", entry)
    print(f"frozen {name}: {len(units)} sections, {len(files)} files -> cache/llm/{key}.json")
    for f, e in old:
        supersede(f, f"{key}.json", f"replaced by a freeze of the same files and sections (model {args.model})")
        print(f"  superseded cache/llm/{f.name} (model {e.get('model')}, {e.get('created')}): marked, not deleted")
    return 0


def register(sub, groups) -> None:
    mcu = verb_group(sub, groups, "mcu", "MCU knowledge: SVD, register vote, reference-manual sections")

    p = mcu.add_parser("rm-outline", help="RM outline -> sections, page ranges, token costs (docs/<rm>/outline.json)")
    p.add_argument("rm_doc", help="the RM's doc id, e.g. demo-rm@1")
    p.add_argument("--pdf", help="the RM PDF (default: doc.json's file)")
    p.add_argument("-v", "--verbose", action="store_true", help="print the chapter table")
    p.set_defaults(func=cmd_outline)

    p = mcu.add_parser("rm-sections", help="RM sections to distil for the peripherals in use -> docs/<rm>/worklist.json")
    p.add_argument("atlas", help="an atlas dir holding in_use.json, or an in_use.json file")
    p.add_argument("--rm-doc", help="RM doc id (default: the only docs/*/outline.json)")
    p.add_argument("--svd-doc", help="SVD doc id (default: the only docs/*/peripherals.json)")
    p.add_argument("--core", help=f"comma list of always-distilled blocks (default {','.join(CORE)}; '' for none)")
    p.set_defaults(func=cmd_sections)

    p = mcu.add_parser("rm-text", help="print one work item's or section's text with [p.N] markers")
    p.add_argument("rm_doc")
    p.add_argument("what", help="a worklist item id (32.4.3-32.4.6) or a section id (32.4.5)")
    p.set_defaults(func=cmd_text)

    p = mcu.add_parser("rm-check", help="verify distilled RM files: SVD names, fields/numbers on cited pages, § holds page")
    p.add_argument("rm_doc")
    p.add_argument("files", nargs="*", help="files to check (default: docs/<rm>/s/*.md except *.fields.md)")
    p.add_argument("--svd-doc", help="SVD doc id (default: the only one)")
    p.add_argument("--chapter", help="only files of this chapter")
    p.set_defaults(func=cmd_check)

    p = mcu.add_parser("rm-freeze", help="record distilled RM files as frozen LLM output, keyed by page content; "
                                         "--retire marks an old entry superseded")
    p.add_argument("rm_doc")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--item", help="a worklist item id")
    g.add_argument("--sections", help="comma list of section ids the files cover")
    g.add_argument("--retire", metavar="ENTRY",
                   help="mark a cache/llm/ entry (file name or unique key prefix) superseded; it is kept, never deleted")
    p.add_argument("--by", metavar="ENTRY", help="with --retire: the entry that replaces it")
    p.add_argument("--files", help="comma list of files under docs/<rm>/ (default s/<item>.md)")
    p.add_argument("--model", help="the model that wrote them (required when freezing)")
    p.add_argument("--note", help="free text kept in the cache entry (with --retire: the reason)")
    p.set_defaults(func=cmd_freeze)

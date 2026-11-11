"""The fifteen atlas lookups, as plain functions returning markdown.

One implementation behind three doors: the `embarch-atlas q <verb>` CLI, the MCP
server (`mcp_server.py`), and later the UI. Each answer starts with one cite header
line (`src: ...`), returns the same bytes as the files where a file exists, and is
cut at a token budget with a `next=` offset when it is longer. DESIGN.md, "CLI" and
the MCP tool surface drafted in research note 14.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

import functools

from . import common, stale
from .pdftext import tier2_files

CHARS_PER_TOKEN = 4  # o200k proxy; real Claude counts run ~1.4x, budgets below allow for it
BUDGET = {"part_card": 1500, "doc_toc": 2500, "doc_section": 6000, "find_fact": 2000, "find_text": 2500,
          "register": 1500, "pin_trace": 4000, "net_trace": 4000, "mismatch_report": 4000,
          "config": 1500, "dt_node": 2500, "footprint": 2500, "stacks": 1500, "applies": 2500, "transitions": 2500}
BUDGET.update({"code": 3000, "states": 4000})


# --------------------------------------------------------------------------- plumbing

def _cap(text: str, tool: str, offset: int = 0) -> str:
    """Keep the `src:` header, then page the body from `offset` to the tool's budget on a line
    boundary, and say where the next page starts. Offsets count body characters only."""
    head, body = "", text
    if text.startswith("src:"):
        h, sep, rest = text.partition("\n\n")
        head, body = h + sep, rest
    limit = BUDGET[tool] * CHARS_PER_TOKEN
    page = body[offset:]
    if len(page) <= limit:
        return head + page
    cut = page.rfind("\n", 0, limit)
    cut = cut + 1 if cut > 0 else limit
    nxt = offset + cut
    return head + page[:cut] + f"\n… truncated: call again with offset={nxt} (next={nxt})\n"


def current_atlas(atlas_dir: Path, atlas: str | None = None) -> Path | None:
    """The named atlas, else the one current.json says matches HEAD, else the newest one that a running
    bump is not still building (`bump.joining`)."""
    root = atlas_dir / "atlases"
    if atlas:
        p = root / atlas
        return p if p.is_dir() else None
    cur = atlas_dir / "current.json"
    if cur.is_file():
        try:
            c = common.read_json(cur)
            head = stale.head(atlas_dir)
            for m in ([] if head and c.get("commit") != head else c.get("matches") or []):
                p = root / m.get("atlas", "")
                if p.is_dir():
                    return p
        except (OSError, ValueError):
            pass
    from . import bump
    stamps = sorted(root.glob("*/atlas.json"), key=lambda p: p.stat().st_mtime)
    for st in reversed(stamps):                  # an atlas a running bump has not joined yet is not the newest answer
        try:
            if bump.joining(atlas_dir, st.parent, common.read_json(st)):
                continue
        except (OSError, ValueError):
            pass
        return st.parent
    return None


def _stamped(fn):
    """Say on the answer's `src:` line when the atlas it came from is behind HEAD or a source changed."""
    @functools.wraps(fn)
    def wrapper(a, *args, **kw):
        out = fn(a, *args, **kw)
        if not out.startswith("src:"):
            return out
        first, nl, rest = out.partition("\n")
        if "stale=" in first:
            return out
        m = stale.mark(a.dir, a.atlas)
        return f"{first} {m}{nl}{rest}" if m else out
    return wrapper


def _json(path: Path, default: Any = None) -> Any:
    try:
        return common.read_json(path)
    except (OSError, ValueError):
        return default


def _records(docdir: Path) -> Iterable[dict]:
    for f in sorted((docdir / "records").glob("*.jsonl")):
        try:
            yield from common.read_jsonl(f)
        except ValueError:
            continue


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


class Atlas:
    """Read-only view over one project's atlas dir and (optionally) one atlas in it."""

    def __init__(self, atlas_dir: Path, atlas: str | None = None):
        self.dir = Path(atlas_dir)
        self.atlas = current_atlas(self.dir, atlas)
        self.docs = sorted(p for p in (self.dir / "docs").glob("*")
                           if p.is_dir() and any((p / n).exists() for n in ("doc.json", "records", "outline.json")))
        self._pins = None
        self._cov_part_docs = None

    # ---- documents
    def doc_meta(self, docdir: Path) -> dict:
        return _json(docdir / "doc.json", {}) or {}

    def mpn_docs(self) -> dict[str, set[str]]:
        """MPN -> doc ids, from the atlas's part_docs join and the board netlists."""
        out: dict[str, set[str]] = {}
        part_docs = self.part_docs()
        mpn_of = self.ref_mpns()
        for ref, docs in part_docs.items():
            mpn = mpn_of.get(ref)
            if mpn:
                out.setdefault(mpn, set()).update(docs)
        return out

    def part_docs(self) -> dict[str, list[str]]:
        """`board:ref` -> doc ids: the atlas's `pins.json` join when an atlas exists, else (before the
        first build) the same join made from the netlists and `coverage.py` (`Coverage.part_docs`)."""
        if self.atlas is not None:
            return (self.pins_json() or {}).get("part_docs") or {}
        if self._cov_part_docs is None:
            from .join import Docs
            docs = Docs(self.dir)
            cov = self.coverage()
            self._cov_part_docs = {}
            for nl in sorted((self.dir / "boards").glob("*/netlist.json")):
                for ref, c in ((_json(nl, {}) or {}).get("components") or {}).items():
                    ids = cov.part_docs(c.get("mpn"), docs) if c.get("mpn") else []
                    if ids:
                        self._cov_part_docs[f"{nl.parent.name}:{ref}"] = ids
        return self._cov_part_docs

    def coverage(self):
        """`coverage.load` of this atlas dir; a malformed answers.toml reads as no answers."""
        from . import coverage
        try:
            return coverage.load(self.dir)
        except ValueError:
            return coverage.load(self.dir, {}, {})

    def ref_mpns(self) -> dict[str, str]:
        out = {}
        for nl in sorted((self.dir / "boards").glob("*/netlist.json")):
            board = nl.parent.name
            data = _json(nl, {}) or {}
            for ref, c in (data.get("components") or {}).items():
                if c.get("mpn"):
                    out[f"{board}:{ref}"] = c["mpn"]
        return out

    def find_doc(self, name: str) -> Path | None:
        """A doc by id, part, document number or an MPN on the board (normalised prefix match)."""
        n = _norm(name)
        if not n:
            return None
        by_id = {d.name: d for d in self.docs}
        if name in by_id:
            return by_id[name]
        for mpn, ids in sorted(self.mpn_docs().items()):
            if _norm(mpn).startswith(n) or n.startswith(_norm(mpn)):
                for i in sorted(ids):
                    if i in by_id:
                        return by_id[i]
        scored = []
        for d in self.docs:
            meta = self.doc_meta(d)
            keys = [d.name.split("@")[0], str(meta.get("part") or ""), str(meta.get("doc_no") or "")]
            for k in keys:
                kn = _norm(k)
                if kn and (kn == n or kn.startswith(n) or n.startswith(kn)):
                    scored.append((0 if kn == n else 1, meta.get("kind") == "appnote", d.name, d))
        return sorted(scored, key=lambda t: t[:3])[0][3] if scored else None

    # ---- atlas files
    def pins_json(self) -> dict | None:
        if self._pins is None and self.atlas:
            self._pins = _json(self.atlas / "pins.json", {}) or {}
        return self._pins


def pdf_rel(a: Atlas, d: Path) -> str:
    """The doc's source PDF as a path relative to the firmware repo (the atlas dir is
    `<fw>/embarch/atlas`), from doc.json's `path`, which is relative to the atlas dir. A model that
    has only `demo-ds@6 p102` guesses a path and the Read fails; this is the path to Read."""
    p = a.doc_meta(d).get("path")
    if not p:
        return ""
    ab = Path(p) if Path(p).is_absolute() else Path(os.path.normpath(a.dir.resolve() / p))
    fw = a.dir.resolve().parent.parent
    try:
        return ab.relative_to(fw).as_posix()
    except ValueError:
        return str(ab)


def _header(src: str, prov: str | None = None, extra: str = "") -> str:
    return f"src: {src}" + (f" prov={prov}" if prov else "") + (f" {extra}" if extra else "") + "\n\n"


def _missing(what: str, hint: str) -> str:
    return f"src: none\n\n{what} not found. {hint}\n"


# --------------------------------------------------------------------------- the fifteen

@_stamped
def part_card(a: Atlas, part: str, board: str | None = None, offset: int = 0) -> str:
    """The part's card.md; with `board`, the refs of that part on the boards and their nets."""
    d = a.find_doc(part)
    if not d:
        if a.atlas is None:
            undistilled = _covered_undistilled(a, part, board)
            if undistilled:
                return _cap(undistilled, "part_card", offset)
        return _missing(f"part {part!r}", "Try an MPN from MAP.md, a doc id from docs/, or see inventory/missing.md "
                        "for parts without a supplied datasheet.")
    card = d / "card.md"
    body = card.read_text(encoding="utf-8") if card.is_file() else "(no card.md yet: run /atlas-translate-docs)\n"
    extra = _pdf_extra(a, d)
    if a.atlas is None:
        extra = f"{extra} {NO_ATLAS_NOTE}".strip()
    out = _header(f"{d.name} card", extra=extra) + body
    if board is not None:
        # "all" (and "" from the CLI) is every board; anything else is a board label
        every = board.strip().lower() in ("", "all")
        out += "\n## On the boards\n\n" + _bindings(a, d.name, None if every else board)
    res = _cap(out, "part_card", offset)
    _log_card(a, d.name, res.split("\n## On the boards")[0])
    return res


def _pdf_extra(a: Atlas, d: Path) -> str:
    p = pdf_rel(a, d)
    return f"pdf={p}" if p else ""


NO_ATLAS_NOTE = "(no atlas built: part-to-document from coverage, refs and nets from the netlists)"


def _covered_undistilled(a: Atlas, part: str, board: str | None) -> str:
    """No atlas, and the MPN is on a board but its document has no distillation: say which file covers
    it (`coverage.py`), that it is not translated yet, and with `board` the refs and nets. "" when the
    part is on no board."""
    n = _norm(part)
    refs = {r: m for r, m in a.ref_mpns().items() if n and (_norm(m).startswith(n) or n.startswith(_norm(m)))}
    if not refs:
        return ""
    cov = a.coverage()
    mpns = sorted(set(refs.values()))
    files = sorted({d["file"] for m in mpns for d in cov.documents(m)})
    head = _header(f"boards netlist.json + coverage of {', '.join(mpns)}", extra=NO_ATLAS_NOTE)
    if files:
        body = (f"{', '.join(mpns)} is on the boards and covered by {', '.join(files)}, which is not translated yet: "
                "there is no card. Run /atlas-translate-docs.\n")
    else:
        body = (f"{', '.join(mpns)} is on the boards and no document in the manifest covers it: see "
                "inventory/missing.md.\n")
    if board is not None:
        every = board.strip().lower() in ("", "all")
        body += "\n## On the boards\n\n" + _ref_lines(a, sorted(r for r in refs if every or r.startswith(board + ":")))
    return head + body


def _bindings(a: Atlas, doc_id: str, board: str | None) -> str:
    part_docs = a.part_docs()
    refs = sorted(r for r, ids in part_docs.items() if doc_id in ids and (not board or r.startswith(board + ":")))
    return _ref_lines(a, refs)


def _ref_lines(a: Atlas, refs: list[str]) -> str:
    if not refs:
        return "(no board ref is mapped to this document)\n"
    lines = []
    for ref in refs:
        b, r = ref.split(":", 1)
        comp = ((_json(a.dir / "boards" / b / "netlist.json", {}) or {}).get("components") or {}).get(r) or {}
        pins = comp.get("pins") or {}
        nets = ", ".join(f"{p}={v.get('net')}" for p, v in sorted(pins.items(), key=lambda kv: _pin_key(kv[0]))
                         if v.get("net"))
        lines.append(f"- {ref} {comp.get('mpn') or ''}{' DNP' if comp.get('dni') else ''}: {nets}")
    return "\n".join(lines) + "\n"


def _pin_key(p: str):
    m = re.match(r"(\d+)", p)
    return (int(m.group(1)) if m else 10**6, p)


@_stamped
def doc_toc(a: Atlas, doc: str, under: str | None = None, depth: int = 2, offset: int = 0) -> str:
    """Outline rows: id | title | pages | tokens | tier-2 file (when distilled)."""
    d = a.find_doc(doc)
    if not d:
        return _missing(f"document {doc!r}", "Doc ids are listed in MAP.md and docs/.")
    rows = _toc_rows(d)
    if not rows:
        toc_md = d / "toc.md"
        return _cap(_header(f"{d.name} toc") + (toc_md.read_text(encoding="utf-8") if toc_md.is_file() else "(no outline)\n"),
                    "doc_toc", offset)
    base = None
    if under:
        found = _toc_find(rows, under)
        if not found:
            near = _nearest_titles(rows, under)
            return _missing(f"section {under!r} in {d.name}",
                            "Call doc_toc without `under` to list the top levels."
                            + (("\nNearest titles:\n" + "\n".join(f"{r['id']} | {r['title']} | p{r['start']}"
                                                                   for r in near)) if near else "")
                            + f"\nTo search the page text instead: `find_text doc={d.name} pattern=<words>`.")
        if len(found) > 1:
            listed = "\n".join(f"{r['id']} | {r['title']} | p{r['start']}" for r in found)
            return _cap(_header(f"{d.name} toc", extra=f"under={under!r} matches={len(found)}")
                        + f"{len(found)} sections match {under!r}; pass one id as `under`:\n{listed}\n", "doc_toc", offset)
        base = found[0]
    files = {p.stem for p in (d / "s").glob("*.md")} if (d / "s").is_dir() else set()
    toc = _json(d / "toc.json")
    t2 = tier2_files(d, toc["sections"]) if toc and toc.get("sections") else {}
    out = []
    lo = (base["level"] + 1) if base else 1
    hi = lo + depth - 1
    inside = base is None
    for r in rows:
        if base is not None:
            if r is base:
                inside = True
                continue
            if inside and r["level"] <= base["level"]:
                break
        if inside and lo <= r["level"] <= hi:
            f = next((s for s in (r["id"], _slug(r["title"])) if s in files), "")
            held = ", ".join(t2.get(r["id"]) or ([f"s/{f}.md"] if f else []))
            pages = f"p{r['start']}" + (f"-{r['end']}" if r["end"] != r["start"] else "")
            out.append(f"{'  ' * (r['level'] - lo)}{r['id']} | {r['title']} | {pages} | {r['tokens']} tok"
                       + (f" | {held}" if held else ""))
    return _cap(_header(f"{d.name} toc" + (f" under {base['id']}" if base else "")) + "\n".join(out) + "\n", "doc_toc", offset)


def _nearest_titles(rows: list[dict], under: str, k: int = 5) -> list[dict]:
    """The k toc rows whose titles share the most words with `under`, else the closest by spelling."""
    import difflib
    words = {w for w in re.findall(r"[a-z0-9]+", under.lower()) if len(w) > 2}
    scored = []
    for r in rows:
        tw = set(re.findall(r"[a-z0-9]+", r["title"].lower()))
        sc = len(words & tw) + difflib.SequenceMatcher(None, _norm(under), _norm(r["title"])).ratio()
        scored.append((-sc, r["start"] or 0, r["id"] or "", r))
    return [x[3] for x in sorted(scored, key=lambda x: x[:3])[:k]]


def _toc_find(rows: list[dict], under: str) -> list[dict]:
    """An exact id or title first; else the rows whose title contains `under`, cut to the one
    that holds all the others when there is one (a peripheral's name finds its chapter, not the
    subsections that repeat it)."""
    u = _norm(under)
    exact = next((r for r in rows if r["id"] == under or _norm(r["title"]) == u), None)
    if exact:
        return [exact]
    hits, seen = [], set()
    for r in rows:
        if u and u in _norm(r["title"]) and r["id"] not in seen:
            seen.add(r["id"])
            hits.append(r)
    roots = [h for h in hits if all(o is h or str(o["id"]).startswith(f"{h['id']}.") for o in hits)]
    return roots if len(roots) == 1 else hits


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def _toc_rows(d: Path) -> list[dict]:
    """Normalise toc.json (datasheets) and outline.json (reference manuals) to one shape."""
    rows = []
    toc = _json(d / "toc.json")
    if toc and toc.get("sections"):
        for s in toc["sections"]:
            p = s.get("pages") or [None, None]
            rows.append({"id": s.get("id"), "title": s.get("title", ""), "level": s.get("level", 1),
                         "start": p[0], "end": p[-1], "tokens": s.get("tokens")})
    out = _json(d / "outline.json")
    if out and out.get("entries"):
        for e in out["entries"]:
            rows.append({"id": e.get("id"), "title": e.get("title", ""), "level": e.get("level", 1),
                         "start": e.get("start"), "end": e.get("end"), "tokens": e.get("tokens")})
    return rows


@_stamped
def doc_section(a: Atlas, doc: str, section: str | None = None, page: int | None = None,
                tier: str = "distilled", offset: int = 0) -> str:
    """A tier-2 section file (distilled), or one page's text layer (tier=text)."""
    d = a.find_doc(doc)
    if not d:
        return _missing(f"document {doc!r}", "Doc ids are listed in MAP.md and docs/.")
    if tier == "text" or (page is not None and not section):
        if page is None:
            return _missing("page", "tier=text needs `page` (1-based physical PDF page).")
        f = d / "pages" / f"p{int(page):03d}.txt"
        if not f.is_file():
            f = d / "pages" / f"p{int(page)}.txt"
        if not f.is_file():
            return _missing(f"page {page} of {d.name}", "Run `embarch-atlas doc prep` for this document.")
        body = f.read_text(encoding="utf-8", errors="replace")
        rows = [r for r in _toc_rows(d) if isinstance(r["start"], int)]
        cover = [r for r in rows if r["start"] <= int(page) <= (r["end"] or r["start"])]
        sec = max(cover, key=lambda r: (r["level"], r["start"]))["id"] if cover else None
        # what `doc sections --suggest` ranks undistilled sections by, beside find_text's hits
        _append_usage(a, [{"doc": d.name, "section": sec, "page": int(page), "tool": "doc_section"}])
        return _cap(_header(f"{d.name} p{page} text-layer",
                            extra=(_pdf_extra(a, d) + " " if pdf_rel(a, d) else "")
                            + "(grep target; Read the PDF page to see it)") + body,
                    "doc_section", offset)
    files = sorted((d / "s").glob("*.md")) if (d / "s").is_dir() else []
    if not files and not (section and _toc_rows(d)):
        return _missing(f"distilled sections of {d.name}", "Not translated yet: run /atlas-translate-docs.")
    if not section:
        return _cap(_header(f"{d.name} sections") + "\n".join(f"- s/{f.name}" for f in files) + "\n", "doc_section", offset)
    want = _norm(section)
    rows = {r["id"]: r for r in _toc_rows(d)}
    title = _norm((rows.get(section) or {}).get("title", ""))
    pick = next((f for f in files if f.stem == section), None) \
        or next((f for f in files if _norm(f.stem) == want or (title and _norm(f.stem) == title)), None) \
        or next((f for f in files if want and want in _norm(f.stem)), None)
    if not pick:
        row = _toc_row(list(rows.values()), section)
        if row:
            return _undistilled(a, d, row)
        near = _nearest_titles(list(rows.values()), section) if rows else []
        return _missing(f"section {section!r} in {d.name}",
                        "Call doc_section without `section` to list the files, or doc_toc for the outline."
                        + (("\nNearest titles:\n" + "\n".join(f"{r['id']} | {r['title']} | p{r['start']}"
                                                               for r in near)) if near else "")
                        + f"\nTo search the page text instead: `find_text doc={d.name} pattern=<words>`.")
    return _cap(_header(f"{d.name} s/{pick.name}", extra=_pdf_extra(a, d)) + pick.read_text(encoding="utf-8"), "doc_section", offset)


def _toc_row(rows: list[dict], section: str) -> dict | None:
    """The toc row a section argument names: its id, or its title (punctuation ignored)."""
    n = _norm(section)
    return next((r for r in rows if r["id"] == section), None) \
        or next((r for r in rows if n and _norm(r["title"]) == n), None)


def _undistilled(a: Atlas, d: Path, r: dict) -> str:
    """A toc section with no `s/` file: say what it is and the next call, not "not found"."""
    pages = f"p{r['start']}" + (f"-{r['end']}" if r["end"] != r["start"] else "")
    out = [f"Section {r['id']} {r['title']!r} ({pages}, {r['tokens']} tok) is in the toc and not distilled."]
    meta = _json(d / "outline.json", {}) or {}
    ent = next((e for e in meta.get("entries") or [] if e.get("id") == r["id"]), {})
    if ent.get("register") or ent.get("regname"):
        reg = ent.get("regname") or "<REG>"
        out.append(f"A reference-manual register section is left to the SVD: call `register <PERIPH>.{reg}`.")
    out.append(f"Next: `doc_section doc={d.name} tier=text page={r['start']}` reads its first page's text layer "
               f"(pages {pages}), or `find_text doc={d.name} pattern=<words> section={r['id']}` searches all of "
               f"its pages, or Read the PDF at those pages.")
    return _header(f"{d.name} toc", extra=_pdf_extra(a, d)) + "\n".join(out) + "\n"


_TOKEN = re.compile(r"[A-Za-z0-9_]+")


FIND_TEXT_LINE = 200   # characters of a matching or context line shown
USAGE_LOG = "usage.jsonl"


def _page_files(d: Path) -> list[tuple[int, Path]]:
    """`pages/p<N>.txt` as (physical page, file), in page order."""
    out = []
    for f in (d / "pages").glob("p*.txt") if (d / "pages").is_dir() else []:
        m = re.fullmatch(r"p(\d+)\.txt", f.name)
        if m:
            out.append((int(m.group(1)), f))
    return sorted(out)


def _append_usage(a: Atlas, rows: list[dict]) -> None:
    """Append rows to `<atlas dir>/usage.jsonl`. Write-only: no answer reads it back; a failed write is ignored."""
    if not rows:
        return
    try:
        with open(a.dir / USAGE_LOG, "a", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
    except OSError:
        pass


def _log_usage(a: Atlas, doc: str, hits: list[tuple[str | None, int]]) -> None:
    """Append what a `find_text` answer returned, for ranking undistilled sections by use."""
    _append_usage(a, [{"doc": doc, "section": sec, "page": page, "tool": "find_text"} for sec, page in hits])


CITE_IN_LINE = re.compile(r"\bcite=(\S+)")
CARD_PAGE = re.compile(r"\bp\.\s?(\d+)")


def _log_facts(a: Atlas, tool: str, facts: list[tuple[str, str]]) -> None:
    """Append the record ids an answer returned (`find_fact`, `register`): `second-look queue --used` batches the
    unverified ones sessions actually read."""
    _append_usage(a, [{"doc": d, "id": i, "tool": tool} for d, i in dict.fromkeys(facts) if i])


def _log_card(a: Atlas, doc: str, text: str) -> None:
    """Append the pages a returned card cites (`p.N`): the facts on them are the ones the card presented."""
    pages = sorted({int(m) for m in CARD_PAGE.findall(text)})
    _append_usage(a, [{"doc": doc, "page": p, "tool": "part_card"} for p in pages])


@_stamped
def find_text(a: Atlas, doc: str, pattern: str, section: str | None = None, context: int = 2,
              offset: int = 0) -> str:
    """Search every page of a document's text layer (`pages/p<NNN>.txt`) for plain words or a
    regex (case-insensitive); one block per hit: page, the deepest toc section holding it, whether
    that section is distilled, the line and `context` lines around it. `section` limits the search
    to that section's pages."""
    d = a.find_doc(doc)
    if not d:
        return _missing(f"document {doc!r}", "Doc ids are listed in MAP.md and docs/.")
    if not (pattern or "").strip():
        return _missing("pattern", "Give plain words or a regex, e.g. `VBUS` or `Source_Capabilities`.")
    try:
        rx = re.compile(pattern, re.I)
    except re.error:
        rx = re.compile(re.escape(pattern), re.I)
    pages = _page_files(d)
    if not pages:
        return _missing(f"text layer of {d.name}", "Run `embarch-atlas doc prep` for this document.")
    rows = [r for r in _toc_rows(d) if isinstance(r["start"], int)]
    lo, hi, scope = 0, 10**9, ""
    if section:
        found = _toc_find(_toc_rows(d), section)
        if not found:
            near = _nearest_titles(_toc_rows(d), section)
            return _missing(f"section {section!r} in {d.name}",
                            "Call doc_toc for the outline." + (("\nNearest titles:\n" + "\n".join(
                                f"{r['id']} | {r['title']} | p{r['start']}" for r in near)) if near else ""))
        if len(found) > 1:
            return _missing(f"one section {section!r} in {d.name}", f"{len(found)} sections match; pass one id:\n"
                            + "\n".join(f"{r['id']} | {r['title']} | p{r['start']}" for r in found))
        lo, hi = found[0]["start"] or 0, found[0]["end"] or found[0]["start"] or 0
        scope = f" section={found[0]['id']}"
        pages = [(n, f) for n, f in pages if lo <= n <= hi]
    files = {p.stem for p in (d / "s").glob("*.md")} if (d / "s").is_dir() else set()
    toc = _json(d / "toc.json")
    t2 = tier2_files(d, toc["sections"]) if toc and toc.get("sections") else {}

    def holder(n: int) -> dict | None:
        cover = [r for r in rows if r["start"] <= n <= (r["end"] or r["start"])]
        return max(cover, key=lambda r: (r["level"], r["start"])) if cover else None

    def held(r: dict | None) -> str:
        if not r:
            return "no section"
        f = next((s for s in (r["id"], _slug(r["title"])) if s in files), "")
        h = ", ".join(t2.get(r["id"]) or ([f"s/{f}.md"] if f else []))
        return h or "not distilled"

    context = max(0, int(context))
    blocks, meta = [], []
    for n, f in pages:
        lines = [x.rstrip() for x in f.read_text(encoding="utf-8", errors="replace").splitlines()]
        hit = [i for i, x in enumerate(lines) if rx.search(x)]
        if not hit:
            continue
        r = holder(n)
        head = f"p{n} " + (f"§{r['id']} {r['title']} " if r else "") + f"[{held(r)}]"
        for i in hit:
            body = [("> " if j == i else "  ") + lines[j].strip()[:FIND_TEXT_LINE]
                    for j in range(max(0, i - context), min(len(lines), i + context + 1)) if lines[j].strip()]
            blocks.append(head + "\n" + "\n".join(body) + "\n")
            meta.append((r["id"] if r else None, n))
    out = _header(f"{d.name} text-layer, {len(pages)} pages",
                  extra=f"hits={len(blocks)}{scope} " + (_pdf_extra(a, d) if pdf_rel(a, d) else "")).rstrip(" \n") + "\n\n"
    if not blocks:
        return out + f"No line matches {pattern!r}. Try other words, a regex, or `find_fact` for the distilled facts.\n"
    starts, pos = [], 0
    for b in blocks:
        starts.append(pos)
        pos += len(b) + 1
    res = _cap(out + "\n".join(blocks), "find_text", offset)
    m = re.search(r"\(next=(\d+)\)\n$", res)
    end = int(m.group(1)) if m else 10**12
    _log_usage(a, d.name, [mt for mt, s in zip(meta, starts) if offset <= s < end])
    return res


PROSE_TABLE_START = re.compile(r"^<!--\s*atlas:table\b")
PROSE_TABLE_END = re.compile(r"^<!--\s*atlas:end\s*-->")
PROSE_MIN_CHARS = 24   # a prose line shorter than this is a label, not a fact


def _prose_lines(d: Path) -> list[str]:
    """A document's distilled prose as find_fact lines: every line of `card.md` and `s/*.md` that
    states something, outside the rendered `atlas:table` blocks and the generated `*.fields.md`
    (both are records, already in facts.txt), as `<doc> <file>:<line> kind=prose; text=<line>`.

    Without it a fact the distillation wrote only as prose — a must-note, a step, a caveat — is in
    the atlas and unreachable by search: a reference manual's "debugger read access to SRAM during
    Sleep mode is not possible" sat in a chapter's section file and must-list while find_fact
    answered `unmatched=debug,debugger`."""
    files = ([d / "card.md"] if (d / "card.md").is_file() else []) + sorted((d / "s").glob("*.md"))
    out = []
    for f in files:
        if f.name.endswith(".fields.md"):
            continue
        rel = f.relative_to(d).as_posix()
        inside = False
        for i, raw in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if PROSE_TABLE_START.match(raw):
                inside = True
                continue
            if PROSE_TABLE_END.match(raw):
                inside = False
                continue
            line = re.sub(r"^\s*(?:[-*]|\d+\.)\s+", "", raw).strip()
            if inside or not line or line.startswith("#") or len(line) < PROSE_MIN_CHARS:
                continue
            out.append(f"{d.name} {rel}:{i} kind=prose; text={line}")
    return out


FIND_FACT_LIMIT = 10   # hits shown when the caller gives no limit
FIND_FACT_FLOOR = 0.4  # ... and only those scoring at least this fraction of the top hit


def _query_part(a: Atlas, terms: list[str]) -> tuple[Path | None, set[str]]:
    """The one document the query's terms name, and those terms. A term names a document when it
    equals its part, doc id or document number, or a board MPN, or extends one of them with an
    ordering suffix (`xyz1234rgtr` of part XYZ1234), or prefixes a board MPN (`abc5678` of
    ABC5678G+T). Only
    terms of 4+ characters with a digit count, so words never narrow a search."""
    keys: dict[str, set[str]] = {}
    for d in a.docs:
        meta = a.doc_meta(d)
        for k in (d.name.split("@")[0], meta.get("part"), meta.get("doc_no")):
            if k:
                keys.setdefault(_norm(str(k)), set()).add(d.name)
    mpns = {_norm(m): ids for m, ids in a.mpn_docs().items()}
    for m, ids in mpns.items():
        keys.setdefault(m, set()).update(ids)
    named: set[str] = set()
    used: set[str] = set()
    for t in terms:
        tn = _norm(t)
        if len(tn) < 4 or not re.search(r"\d", tn):
            continue
        for k, ids in keys.items():
            if tn == k or (len(k) >= 5 and re.search(r"\d", k) and tn.startswith(k)) \
                    or (k in mpns and k.startswith(tn)):
                named |= ids
                used.add(t)
    if len(named) != 1:
        return None, set()
    return a.find_doc(next(iter(named))), used


@_stamped
def find_fact(a: Atlas, query: str, part: str | None = None, kind: str | None = None,
              limit: int | None = None, offset: int = 0) -> str:
    """Lines of facts.txt, and of the distilled prose (`kind=prose`), ranked lexically (BM25)
    across every document, each with its cite. A query term naming one part narrows the search to
    it, as `part` does. With no `limit`, shows
    up to FIND_FACT_LIMIT hits scoring at least FIND_FACT_FLOOR of the top one; a `limit` lists
    exactly that many, past the floor."""
    q = [t.lower() for t in _TOKEN.findall(query)]
    if not q:
        return _missing("query terms", "Give words or symbols, e.g. `OVP threshold` or `VOVP`.")
    docs = a.docs
    narrowed = ""
    if part:
        d = a.find_doc(part)
        if not d:
            return _missing(f"part {part!r}", "Omit `part` to search every document.")
        docs = [d]
    else:
        d, used = _query_part(a, q)
        if d:
            docs = [d]
            narrowed = f" part={d.name} (named in the query)"
            q = [t for t in q if t not in used] or q
    lines = []
    for d in docs:
        f = d / "facts.txt"
        if f.is_file() and kind != "prose":
            for ln in f.read_text(encoding="utf-8").splitlines():
                if ln.strip() and (not kind or f"kind={kind};" in ln or f"kind={kind} " in ln):
                    lines.append(ln)
        if not kind or kind == "prose":
            lines += _prose_lines(d)
    toks = [[t.lower() for t in _TOKEN.findall(ln)] for ln in lines]
    n = len(toks) or 1
    avg = sum(map(len, toks)) / n if toks else 1
    df = Counter(t for ts in toks for t in set(ts))
    scored = []
    for ln, ts in zip(lines, toks):
        tf = Counter(ts)
        s = 0.0
        for t in q:
            if tf[t]:
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                s += idf * tf[t] * 2.2 / (tf[t] + 1.2 * (0.25 + 0.75 * len(ts) / avg))
        if s > 0:
            scored.append((-s, ln))
    scored.sort()
    if limit is None:
        floor = -scored[0][0] * FIND_FACT_FLOOR if scored else 0.0
        hits = [ln for s, ln in scored[:FIND_FACT_LIMIT] if -s >= floor]
    else:
        hits = [ln for _, ln in scored[: max(1, limit)]]
    unmatched = [t for t in dict.fromkeys(q) if not df[t]]
    head = _header(f"facts.txt + prose ({len(docs)} docs)" if kind != "prose" else f"prose ({len(docs)} docs)",
                   extra=f"query={query!r}{narrowed} hits={len(scored)} shown={len(hits)}"
                   + (f" unmatched={','.join(unmatched)}" if unmatched else ""))
    body = "\n".join(hits) if hits else "(no fact matches; try a symbol, or doc_toc + doc_section)"
    if limit is None and len(hits) < len(scored):
        body += (f"\n({len(scored) - len(hits)} more hits, each under {FIND_FACT_FLOOR:g} of the top score or past "
                 f"the first {FIND_FACT_LIMIT}; pass `limit` to list them)")
    res = _cap(head + body + "\n", "find_fact", offset)
    _log_facts(a, "find_fact", [(m.group(1), ln.split(" ", 1)[0]) for ln in hits if " kind=prose;" not in ln and ln in res
                                and (m := CITE_IN_LINE.search(ln))])
    return res


def _reg_cands(a: Atlas, hn: str, rn: str, head_doc: str) -> list[tuple[Path, dict]]:
    """Register records named `rn` (with or without an instance prefix) owned by `hn`, an MCU
    instance or a part; one per id."""
    cands: list[tuple[Path, dict]] = []
    for d in a.docs:
        for r in _records(d):
            if r.get("kind") != "register":
                continue
            name = r.get("name", "")
            names = {_norm(name)} | ({_norm(name.split("_", 1)[1])} if "_" in name else set())
            if rn not in names:
                continue
            rid_ = r.get("id") or ""
            inst = rid_.split(".reg.", 1)[1].split(".", 1)[0] if ".reg." in rid_ else ""
            owner = _norm(r.get("periph") or inst)
            if owner == hn or head_doc == d.name or (not owner and _norm(d.name).startswith(hn)):
                cands.append((d, r))
    seen_ids: set = set()
    return [c for c in cands if not (c[1].get("id") in seen_ids or seen_ids.add(c[1].get("id")))]


def _names_reg(r: dict, rid: str) -> bool:
    refs = r.get("refs") or []
    note = r.get("note") or ""
    return bool(rid) and (rid in refs or r.get("reg") == rid or f"{rid}." in note + " " or note.endswith(rid)
                          or any(str(x).startswith(rid + ".") for x in refs))


def _reg_joins(a: Atlas, rid: str, field: str | None) -> tuple[list[dict], list[tuple[str, dict]]]:
    """The register's fields (twins merged) and the rules, quirks and conflicts that name it."""
    fields, related = [], []
    for dd in a.docs:
        for r in _records(dd):
            if r.get("kind") == "field" and r.get("reg") == rid and (not field or _norm(r.get("name", "")) == _norm(field)):
                fields.append(r)
            elif r.get("kind") in ("conflict", "rule", "quirk") and _names_reg(r, rid):
                related.append((dd.name, r))
    return _merge_twins(fields), related


def _field_line(f: dict) -> str:
    bits = f.get("bits") or []
    rng = f"[{bits[0]}:{bits[1]}]" if len(bits) == 2 and bits[0] != bits[1] else (f"[{bits[0]}]" if bits else "")
    return (f"- {f.get('name')}{rng} access={f.get('access')} reset={f.get('reset')}"
            + (f" enum={f.get('enum')}" if f.get("enum") and len(str(f.get("enum"))) < 200 else "")
            + f" cite={_cite(f)}" + (f" prov={f.get('prov')}" if f.get("prov") else ""))


def _reg_tail(a: Atlas, related: list[tuple[str, dict]], rids: list[str]) -> list[str]:
    out = []
    if related:
        out += ["", "## Rules, quirks and conflicts"]
        for _, r in related[:30]:
            text = r.get("text") or r.get("note") or ""
            out.append(f"- {r.get('kind')} {r.get('id')}: {text} ({_cite(r)}; prov={r.get('prov')})")
    code = [c for rid in rids for c in _codecheck_refs(a, rid)]
    if code:
        out += ["", "## Firmware references"] + code[:20]
    elif a.atlas is not None and common.no_build(_json(a.atlas / "atlas.json", {})):
        out += ["", "## Firmware references", "(none: the target has no Zephyr board yet, so no driver sources are "
                "built to check against this register)"]
    return out


def _byte_value(v: Any) -> int | None:
    s = str(v or "").strip().lower()
    try:
        n = int(s[2:], 2) if s.startswith("0b") else int(s, 16) if s.startswith("0x") else int(s)
    except ValueError:
        return None
    return n if 0 <= n <= 0xFF else None


def _register_pair(a: Atlas, reg: str, msb: tuple[Path, dict], lsb: tuple[Path, dict], field: str | None, offset: int = 0) -> str:
    """A 16-bit register a datasheet tables as two bytes (`<REG>_MSB` + `<REG>_LSB`), shown as one:
    MSB fields move up to bits 15:8, and the reset is joined when both bytes state one."""
    d, m = msb
    l = lsb[1]
    if m.get("width") != 8 or l.get("width") != 8:
        return _missing(f"register {reg!r}", f"It is split into {m.get('name')} and {l.get('name')}, which are "
                        "not both 8 bits wide; ask for each.")
    ids = [m.get("id"), l.get("id")]
    mf, related = _reg_joins(a, ids[0], field)
    lf, rel_l = _reg_joins(a, ids[1], field)
    seen = {r.get("id") for _, r in related}
    related += [x for x in rel_l if x[1].get("id") not in seen]
    fields = [dict(f, bits=[b + 8 for b in f.get("bits") or []]) for f in mf] + lf
    fields.sort(key=lambda r: -(r.get("bits") or [0])[0])
    hi, lo = _byte_value(m.get("reset")), _byte_value(l.get("reset"))
    reset = f"0x{hi << 8 | lo:04X}" if hi is not None and lo is not None else f"MSB {m.get('reset')} / LSB {l.get('reset')}"
    access = m.get("access") if m.get("access") == l.get("access") else f"MSB {m.get('access')} / LSB {l.get('access')}"
    provs = sorted({str(p) for p in (m.get("prov"), l.get("prov")) if p})
    out = [_header(f"{d.name} {ids[0]} + {ids[1]}", " & ".join(provs) or None).rstrip("\n"), "",
           f"{reg}: {m.get('name')} addr={m.get('addr')} is bits 15:8, {l.get('name')} addr={l.get('addr')} is bits 7:0",
           f"width=16 reset={reset} access={access} cite={_cite(m)}; {_cite(l)}", ""]
    out += [_field_line(f) for f in fields]
    _log_facts(a, "register", [(d.name, i) for i in ids] + [(d.name, f.get("id")) for f in fields]
               + [(dn, r.get("id")) for dn, r in related])
    return _cap("\n".join(out + _reg_tail(a, related, ids)) + "\n", "register", offset)


@_stamped
def register_lookup(a: Atlas, target: str, field: str | None = None, offset: int = 0) -> str:
    """A register (MCU `PERIPH.REG` or external `part.REG`) with its fields, rules and conflicts.
    A name a datasheet splits into `<REG>_MSB` and `<REG>_LSB` returns both as one 16-bit view."""
    head, _, reg = target.partition(".")
    if not reg:
        return _missing(f"register {target!r}", "Use `PERIPH.REG` (MCU, e.g. I2C1.CR1) or `<part>.<REG>`.")
    hn, rn = _norm(head), _norm(reg)
    head_doc = (a.find_doc(head) or Path("-")).name
    cands = _reg_cands(a, hn, rn, head_doc)
    alias = ""
    if not cands:
        msb, lsb = _reg_cands(a, hn, rn + "msb", head_doc), _reg_cands(a, hn, rn + "lsb", head_doc)
        if msb and lsb and msb[0][0] == lsb[0][0]:
            return _register_pair(a, reg, msb[0], lsb[0], field, offset)
        al = _cmsis_alias(a, head, reg)
        if al:
            cands = [al]
            alias = f"CMSIS `{head}->{reg}` is {al[1].get('name')} (same offset in the device header)"
        else:
            return _missing(f"register {target!r}", "Check the name in the SVD slice (docs/<svd>/s/<PERIPH>.md) or the "
                        "part's register section; names are matched without punctuation.")
    d, reg_rec = cands[0]
    rid = reg_rec.get("id")
    fields, related = _reg_joins(a, rid, field)
    fields.sort(key=lambda r: -(r.get("bits") or [0])[0])
    out = [_header(f"{d.name} {rid}", reg_rec.get("prov")).rstrip("\n"), ""] + ([alias, ""] if alias else []) + [
           f"{reg_rec.get('name')} addr={reg_rec.get('addr')} width={reg_rec.get('width')} "
           f"reset={reg_rec.get('reset')} access={reg_rec.get('access')} cite={_cite(reg_rec)}", ""]
    out += [_field_line(f) for f in fields]
    out += _reg_tail(a, related, [rid])
    if len(cands) > 1:
        out += ["", "Also named " + reg + " in: " + ", ".join(sorted({c[0].name for c in cands[1:]}))]
    _log_facts(a, "register", [(d.name, rid)] + [(d.name, f.get("id")) for f in fields] + [(dn, r.get("id")) for dn, r in related])
    return _cap("\n".join(out) + "\n", "register", offset)


def _cmsis_alias(a: Atlas, head: str, member: str) -> tuple[Path, dict] | None:
    """`PERIPH.MEMBER` as the CMSIS device header names it -> the SVD register at the same address."""
    from . import cmsisalias
    hdr = cmsisalias.header_path(a.dir)
    if not hdr:
        return None
    addr = cmsisalias.member_address(hdr, head.upper(), member)
    if addr is None:
        return None
    for d in a.docs:
        for r in _records(d):
            if r.get("kind") == "register" and r.get("src") == "svd":
                try:
                    if int(str(r.get("addr")), 16) == addr:
                        return d, r
                except ValueError:
                    continue
    return None


def _merge_twins(recs: list[dict]) -> list[dict]:
    """A deterministic record and an LLM record may share an id (DESIGN.md): show one line,
    and say both readings agree, or show both when they differ."""
    by: dict[tuple, list[dict]] = {}
    for r in recs:
        by.setdefault((r.get("name"), tuple(r.get("bits") or [])), []).append(r)
    out = []
    for group in by.values():
        keyf = lambda r: (r.get("access"), str(r.get("reset")))
        if len({keyf(r) for r in group}) == 1:
            first = dict(sorted(group, key=lambda r: r.get("prov") != "deterministic")[0])
            provs = sorted({r.get("prov") for r in group if r.get("prov")})
            first["prov"] = " & ".join(provs) if len(provs) > 1 else (provs[0] if provs else None)
            out.append(first)
        else:
            out.extend(group)
    return out


def _cite(r: dict) -> str:
    p = r.get("p")
    if isinstance(p, list):
        p = ",".join(map(str, p))
    return f"{r.get('doc')} p{p}" if p else f"{r.get('doc')}"


def _codecheck_refs(a: Atlas, rid: str | None) -> list[str]:
    if not (a.atlas and rid):
        return []
    cc = _json(a.atlas / "codecheck.json")
    if not cc:
        return []
    out = []
    for item in cc.get("matches") or cc.get("items") or []:
        if rid in (item.get("register"), item.get("record"), item.get("reg")):
            out.append(f"- {item.get('macro') or item.get('name')} at {item.get('loc') or item.get('file')}")
    return out[:20]


@_stamped
def pin_trace(a: Atlas, query: str, board: str | None = None, offset: int = 0) -> str:
    """One chain per line: MCU pin -> net -> far ends -> DT use -> driver, with provenance per hop."""
    if not a.atlas:
        return _missing("atlas", "Run /atlas-build first.")
    pins = (a.pins_json() or {}).get("pins") or []
    q = query.strip()
    qn = _norm(q)
    hits = []
    for p in pins:
        names = [p.get("port"), p.get("pin"), p.get("name"), p.get("key"), str(p.get("num") or p.get("number") or "")]
        nets = [p.get("net")] + [f.get("net") for f in p.get("far") or []] + [f.get("to_net") for f in p.get("far") or []]
        parts = [f.get("pin") for f in p.get("far") or []] + [f"{f.get('board')}:{f.get('pin')}" for f in p.get("far") or []]
        if any(n and _norm(n) == qn for n in names) or any(n and _norm(n) == qn for n in nets) \
                or any(n and _norm(n) == qn for n in parts):
            hits.append(p)
    if not hits:
        return _missing(f"pin/net/part-pin {query!r}", "pins.md lists the MCU pins and the nets they reach. "
                        "For a net no MCU pin reaches (a rail, a sense line), or a part or part pin, use net_trace.")
    out = [_header(f"{a.atlas.name} pins.json", extra=f"query={query!r}").rstrip("\n"), ""]
    for p in hits:
        far = "; ".join(_far(f) for f in (p.get("far") or []) if not board or f.get("board") == board) or "no far end"
        dt = "; ".join(_dt(u) for u in (p.get("dt") or [])) or "not in DT"
        num = p.get("num") or p.get("number") or ""
        out.append(f"pin {num} {p.get('port') or p.get('name')} net={p.get('net')} -> {far} || DT: {dt}")
    return _cap("\n".join(out) + "\n", "pin_trace", offset)


def _far(f: dict) -> str:
    kind = f.get("kind")
    if kind == "board-to-board":
        return f"{f.get('board')}:{f.get('pin')} => {f.get('to')} ({f.get('to_net')})"
    name = f"/{f.get('pin_name')}" if f.get("pin_name") else ""
    via = f" via {','.join(f['via'])}" if f.get("via") else ""
    dnp = " DNP" if f.get("dni") else ""
    return f"{f.get('board')}:{f.get('pin')}{name} {f.get('mpn') or ''}{dnp}{via}".strip()


def _dt(u: dict) -> str:
    drv = u.get("driver") or {}
    files = ",".join(drv.get("files") or [])
    bits = [f"{u.get('label') or u.get('node')}.{u.get('prop')}", u.get("func") or u.get("flags") or ""]
    if u.get("irq_armed"):
        bits.append("irq")
    if files:
        bits.append(f"driver {files}{'' if drv.get('compiled', True) else ' (not compiled)'}")
    bits.append(u.get("loc") or "")
    return " ".join(b for b in bits if b)


@_stamped
def net_trace(a: Atlas, query: str, board: str | None = None, offset: int = 0) -> str:
    """Any net, part pin (number or name) or part on any board: its members across connector mates,
    then what feeds it, from datasheet pin records with `flow` only, and the datasheets still needed."""
    from . import nettrace
    if not any((a.dir / "boards").glob("*/netlist.json")):
        return _missing("netlists", "Run atlas-translate-docs on the schematics first.")
    J = nettrace.load(a.dir, a.atlas, _json)
    src, body = nettrace.answer(J, query, board)
    if not src:
        return f"src: none\n\n{body}\n"
    return _cap(_header(src, extra=f"query={query!r}") + body + "\n", "net_trace", offset)


def _build_src(a: Atlas, build: str | None, query: str, fn):
    from . import buildq
    try:
        src = buildq.source(a, build)
        return src, fn(src, query)
    except (LookupError, FileNotFoundError, OSError, RuntimeError) as e:
        return None, f"src: none\n\n{e}\n"


def _build_vs(a: Atlas, build: str | None, vs: str, what: str, fn):
    """(`src:` text naming both builds, body) for a lookup comparing `build` (default the pinned one)
    with the build `vs` names."""
    from . import buildq
    try:
        src, other = buildq.source(a, build), buildq.versus(a, vs)
        return f"{src.label} {what} vs {other.label} {what}", fn(src, other)
    except (LookupError, FileNotFoundError, OSError, RuntimeError) as e:
        return None, f"src: none\n\n{e}\n"


@_stamped
def config(a: Atlas, symbol: str | list[str] | None = None, build: str | None = None, vs: str | None = None,
           offset: int = 0) -> str:
    """A Kconfig symbol (CONFIG_X or X): its value, the fragment that set it, its depends on and whether
    it is met, who selects or implies it, the default that applies, where it is defined. Several names (a
    list, or comma-separated) or `*` patterns: one line per symbol, value, the fragment line or select or
    default that set it, an unmet depends on. With `vs` (a build dir, or "pinned"): only the symbols
    whose value differs from that build, with both values and where each was set; no `symbol` compares
    all. From the pinned build, or the build dir `build` names."""
    from . import buildq
    names = buildq.names_of(symbol)
    q = ",".join(names) or "*"
    if vs:
        src, body = _build_vs(a, build, vs, "kconfig", lambda s, o: buildq.config_diff_answer(s, o, names))
        return body if src is None else _cap(_header(src, extra=f"query={q!r}") + body + "\n", "config", offset)
    if not names:
        return "src: none\n\nName a symbol, several (a list or comma-separated) or a `*` pattern, or pass `vs`.\n"
    src, body = _build_src(a, build, names, buildq.config_answer)
    if src is None:
        return body
    return _cap(_header(f"{src.label} kconfig", extra=f"query={q!r}") + body + "\n", "config", offset)


@_stamped
def dt_node(a: Atlas, node: str | None = None, build: str | None = None, vs: str | None = None,
            offset: int = 0) -> str:
    """A devicetree node by path, label, alias, chosen name or compatible: status, binding, the
    driver compiled for it, whether its device is in the ELF, every property with the file:line
    that set it, its pins and children. With `vs` (a build dir, or "pinned"): only what differs from
    that build, both values and the line that set each; no `node` compares every node. From the
    pinned build, or the build dir `build` names."""
    from . import buildq
    if vs:
        src, body = _build_vs(a, build, vs, "devicetree", lambda s, o: buildq.dt_diff_answer(s, o, node))
        return body if src is None else _cap(_header(src, extra=f"query={node or '*'!r}") + body + "\n",
                                             "dt_node", offset)
    if not node:
        return "src: none\n\nName a node (path, label, alias, chosen name or compatible), or pass `vs`.\n"
    src, body = _build_src(a, build, node, buildq.dt_answer)
    if src is None:
        return body
    return _cap(_header(f"{src.label} devicetree", extra=f"query={node!r}") + body + "\n", "dt_node", offset)


@_stamped
def footprint(a: Atlas, build: str | None = None, vs: str | None = None, by: str = "dir", offset: int = 0) -> str:
    """Flash image and RAM against the flash, SRAM and code partition the devicetree gives, then the
    largest contributors `by` dir, file or symbol; with `vs` (a build dir, or "pinned"), the change
    against that build instead. From the pinned build, or the build dir `build` names."""
    from . import buildq
    try:
        src = buildq.source(a, build)
        other = buildq.versus(a, vs) if vs else None
        body = buildq.footprint_answer(src, by, other)
    except (LookupError, FileNotFoundError, OSError, RuntimeError, subprocess.CalledProcessError) as e:
        return f"src: none\n\n{e}\n"
    return _cap(_header(f"{src.label} zephyr.elf", extra=f"by={by}" + (f" vs={vs!r}" if vs else "")) + body + "\n",
                "footprint", offset)


@_stamped
def stacks(a: Atlas, build: str | None = None, offset: int = 0) -> str:
    """Every thread stack in the image: size, symbol, the line that defined it, the *_STACK_SIZE option
    it most likely comes from (by inference). Static sizes only; high-water marks are live."""
    from . import buildq
    try:
        src = buildq.source(a, build)
        body = buildq.stacks_answer(src)
    except (LookupError, FileNotFoundError, OSError, RuntimeError, subprocess.CalledProcessError) as e:
        return f"src: none\n\n{e}\n"
    return _cap(_header(f"{src.label} zephyr.elf") + body + "\n", "stacks", offset)


@_stamped
def code(a: Atlas, symbol: str, build: str | None = None, show: str = "all", offset: int = 0) -> str:
    """A function, struct, union, enum, typedef, macro, enumerator or file-scope variable as the build
    compiled it: each definition's file:line and compiled unit, its body, then every reference in the
    compiled sources as `file:line function`. `<file>:<symbol>` narrows to a file, `<file>:<line>` gives
    the definition holding that line, a file alone lists its definitions, `*` lists matching names.
    `show`: all, def (no references) or refs (no bodies). From the pinned build, or the build dir `build` names."""
    from . import buildq, codeq
    try:
        src = buildq.source(a, build)
        idx = codeq.for_source(src)
        body = codeq.code_answer(idx, symbol, show)
    except (LookupError, FileNotFoundError, OSError, RuntimeError, ValueError) as e:
        return f"src: none\n\n{e}\n"
    extra = f"query={symbol!r}" + (f" show={show}" if show != "all" else "")
    return _cap(_header(codeq.src_line(src, idx), extra=extra) + body + "\n", "code", offset)


@_stamped
def states(a: Atlas, table: str, build: str | None = None, offset: int = 0) -> str:
    """Zephyr SMF state tables (`SMF_CREATE_STATE` entries) by table name, or every table in a directory or
    file: one row per compiled state with its parent, its entry/run/exit functions and their lines, and the
    states each can move to (`smf_set_state` targets in it, and one call level down). From the pinned build,
    or the build dir `build` names."""
    from . import buildq, codeq
    try:
        src = buildq.source(a, build)
        idx = codeq.for_source(src)
        body = codeq.states_answer(idx, table)
    except (LookupError, FileNotFoundError, OSError, RuntimeError, ValueError) as e:
        return f"src: none\n\n{e}\n"
    return _cap(_header(codeq.src_line(src, idx), extra=f"query={table!r}") + body + "\n", "states", offset)


@_stamped
def mismatch_report(a: Atlas, scope: str | None = None, cls: str | None = None, offset: int = 0,
                    build: str | None = None) -> str:
    """Checks run and passed first, then candidates (claim, evidence, what would settle it). With `build` (a build
    dir, or "pinned"): the config-vs-standard checks only, run now against that build, writing nothing."""
    if build:
        return _mismatch_build(a, build, scope, cls, offset)
    if not a.atlas:
        return _mismatch_no_atlas(a, scope, cls, offset)
    m = _json(a.atlas / "mismatches.json")
    if not m:
        md = a.atlas / "checks.md"
        return _cap(_header(f"{a.atlas.name} checks.md") + (md.read_text(encoding="utf-8") if md.is_file() else ""),
                    "mismatch_report", offset)
    out = [_header(f"{a.atlas.name} mismatches.json").rstrip("\n"), ""]
    return _cap("\n".join(_mismatch_body(m, out, scope, cls)) + "\n", "mismatch_report", offset)


def _mismatch_no_atlas(a: Atlas, scope: str | None, cls: str | None, offset: int) -> str:
    """Before the first build: the checks that read only the netlists, the BOM and the distillations,
    run now, and each check that needs a devicetree, an ELF or a build listed under Not run."""
    from . import mismatch, nettrace, project
    if not any((a.dir / "boards").glob("*/netlist.json")):
        return _missing("atlas", "Run /atlas-build first (no board netlist yet either: "
                        "run atlas-translate-docs on the schematics).")
    J = nettrace.load(a.dir, None, _json)
    try:                                   # the configured MCU family finds the MCU when the part count would not
        part = (project.load_config(a.dir).get("mcu") or {}).get("part")
    except (OSError, ValueError):
        part = None
    if part:
        J.mcu = J._find_mcu(part, None) or J.mcu
    m = mismatch.run_netlist_only(J)
    labels = ",".join(sorted(J.boards))
    out = [_header(f"boards/{labels} netlist.json + BOM + coverage + distillations, no atlas",
                   extra="(only the checks that need no build; the rest are under Not run)").rstrip("\n"), ""]
    return _cap("\n".join(_mismatch_body(m, out, scope, cls)) + "\n", "mismatch_report", offset)


def _mismatch_build(a: Atlas, build: str, scope: str | None, cls: str | None, offset: int) -> str:
    """Every requirement and check record's fw_check against a named build, in memory: the stored report is the
    pinned build's join and is not re-run (it needs the netlists joined to that build)."""
    from . import buildq, fwcheck
    try:
        src = buildq.versus(a, build)
        rep = fwcheck.run_source(a.dir, src)
    except (LookupError, FileNotFoundError, OSError, RuntimeError) as e:
        return f"src: none\n\n{e}\n"
    out = [_header(f"{src.label} devicetree + kconfig",
                   extra=f"(the {fwcheck.CLS} checks only, run now; every other check is the pinned atlas's join: "
                         "call without build)").rstrip("\n"), ""]
    m = {"summary": rep["summary"] or "no requirement or check record carries a fw_check, and atlas.toml declares "
                                      "no [standards] values",
         "checks": rep["checks"], "not_run": rep["not_run"], "candidates": rep["findings"]}
    body = _mismatch_body(m, out, scope, cls)
    skipped = rep["folded"].get(fwcheck.FOLD_GUARD) or []
    if skipped:
        body += ["Skipped (applies_when false):"] + [f"- {s}" for s in skipped]
    return _cap("\n".join(body).rstrip("\n") + "\n", "mismatch_report", offset)


def _mismatch_body(m: dict, out: list[str], scope: str | None, cls: str | None) -> list[str]:
    """The summary, checks, Not run and candidate lines of a mismatch report, appended to `out`."""
    summ = m.get("summary")
    if summ:
        out.append(f"summary: {json.dumps(summ, sort_keys=True) if not isinstance(summ, str) else summ}")
    for c in m.get("checks") or []:
        if isinstance(c, dict):
            name = c.get("check") or c.get("name") or c.get("id")
            out.append(f"- {name}: {c.get('passed')}/{c.get('ran', c.get('total'))} passed"
                       + (f", {c['candidates']} candidate(s)" if c.get("candidates") else ""))
    if m.get("not_run"):
        out += ["", "Not run:"] + [f"- {x.get('check') or x.get('name')}: {x.get('why') or ''}" if isinstance(x, dict)
                                   else f"- {x}" for x in m["not_run"]]
    out.append("")
    sn = _norm(scope) if scope else ""
    for c in m.get("candidates") or []:
        if cls and c.get("class") != cls:
            continue
        blob = _norm(" ".join(str(v) for v in (c.get("key"), c.get("summary"), c.get("class"))))
        if sn and sn not in blob:
            continue
        ev = "; ".join(c.get("evidence") or [])
        out.append(f"- [{c.get('class')}] {c.get('summary')} | evidence: {ev} | settle: {c.get('settle')}")
    return out


# --------------------------------------------------------------------------- what a role must support, what a state does

def _nearest_names(names: Iterable[str], q: str, k: int = 5) -> list[str]:
    """The k names sharing the most words with `q`, else the closest by spelling (doc_toc's miss rule)."""
    import difflib
    words = {w for w in re.findall(r"[a-z0-9]+", q.lower()) if len(w) > 2}
    scored = []
    for n in {n for n in names if n}:
        sc = len(words & set(re.findall(r"[a-z0-9]+", n.lower()))) \
            + difflib.SequenceMatcher(None, _norm(q), _norm(n)).ratio()
        scored.append((-sc, n))
    return [n for _, n in sorted(scored)[:k]]


def _pages(r: dict) -> list[int]:
    p = r.get("p")
    return list(p) if isinstance(p, list) else [p] if isinstance(p, int) else []


def _pp(r: dict) -> str:
    """A record's pages as `p.N[,M]`: the answer's `src:` line already names the document."""
    ps = _pages(r)
    return "p." + ",".join(map(str, ps)) if ps else ""


def _unverified(r: dict) -> str:
    return " (unverified)" if r.get("prov") in (None, "llm-unverified") else ""


def _protocol_records(a: Atlas, doc: str, kinds: tuple[str, ...]) -> tuple[Path | None, list[dict]]:
    d = a.find_doc(doc)
    return d, ([r for r in _records(d) if r.get("kind") in kinds] if d else [])


def _word_rx(term: str) -> re.Pattern:
    """`term` as a whole name, case-insensitive: no letter, digit or `_` may extend it."""
    return re.compile(r"(?<![A-Za-z0-9_])" + re.escape(term.strip()) + r"(?![A-Za-z0-9_])", re.I)


LEVEL_ORDER = (   # an applicability level's rank, strongest force first; the first pattern that matches decides
    (r"\b(?:shall|must)\s+not\b|\bnot\s+(?:allowed|permitted)\b|\bprohibited\b", 3),
    (r"\bn\s*/\s*a\b|\bnot\s+applicable\b", 4),
    (r"\bnormative\b|\bmandatory\b|\brequired\b|\bshall\b|\bmust\b", 0),
    (r"\brecommended\b|\bshould\b", 1),
    (r"\boptional\b|\bmay\b|\bconditional\b", 2),
)


def _level_rank(level: str) -> int:
    s = (level or "").lower()
    return next((rank for rx, rank in LEVEL_ORDER if re.search(rx, s)), len(LEVEL_ORDER))


def _cell_tail(r: dict) -> str:
    """` (<table> p.N)` and the unverified mark, after a cell's level or item."""
    return f" ({' '.join(x for x in (r.get('table'), _pp(r)) if x)}){_unverified(r)}"


@_stamped
def applies(a: Atlas, doc: str, role: str | None = None, item: str | None = None, cond: str | None = None,
            offset: int = 0) -> str:
    """What a standard's applicability tables require of a role: `role` lists every sequence and message
    for it grouped by level (strongest first, each level as printed); `cond` keeps the cells under that
    condition and the unconditional ones; `item` lists one item's level for every role. Neither lists the
    roles, conditions and tables. A role, item or condition that matches no cell lists the nearest names."""
    d, recs = _protocol_records(a, doc, ("applicability",))
    if not d:
        return _missing(f"document {doc!r}", "Doc ids are listed in MAP.md and docs/.")
    if not recs:
        return _missing(f"applicability records of {d.name}",
                        f"Not distilled yet: find the applicability tables with `doc_toc doc={d.name}` or "
                        f"`find_text doc={d.name} pattern=<table title>`, read them there, and suggest "
                        f"`/atlas-translate-docs standard {d.name} <section>`.")
    asked = " ".join(f"{k}={v!r}" for k, v in (("role", role), ("item", item), ("cond", cond)) if v)
    head = _header(f"{d.name} applicability records", extra=" ".join(
        x for x in (f"cells={len(recs)}", f"query={asked!r}" if asked else "", _pdf_extra(a, d)) if x))
    pick = recs
    for key, want in (("role", role), ("item", item)):
        if want:
            hit = [r for r in pick if _norm(r.get(key)) == _norm(want)]
            if not hit:
                near = _nearest_names((r.get(key) for r in recs), want)
                return _missing(f"{key} {want!r} in {d.name}'s applicability records",
                                f"Nearest {key}s: {', '.join(near)}.")
            pick = hit
    if cond:
        conds = {r.get("cond") for r in recs if r.get("cond")}
        if not any(_norm(cond) in _norm(c) for c in conds):
            near = _nearest_names(conds, cond)
            return _missing(f"condition {cond!r} in {d.name}'s applicability records",
                            f"Conditions: {', '.join(near) if near else '(no cell is conditional)'}.")
        pick = [r for r in pick if not r.get("cond") or _norm(cond) in _norm(r["cond"])]
    out: list[str] = []
    if not role and not item:
        count = lambda key: Counter(r.get(key) for r in pick if r.get(key))  # noqa: E731
        out.append("Roles: " + ", ".join(f"{k} ({n} cells)" for k, n in sorted(count("role").items())))
        if count("cond"):
            out.append("Conditions: " + ", ".join(f"{k} ({n} cells)" for k, n in sorted(count("cond").items())))
        tables: dict[str, set[int]] = {}
        for r in pick:
            if r.get("table"):
                tables.setdefault(r["table"], set()).update(_pages(r))
        if tables:
            out.append("Tables: " + ", ".join(f"{t} ({_pp({'p': sorted(ps)})})" for t, ps in sorted(tables.items())))
        kinds = Counter(k for _, k in {(r.get("item"), r.get("item_kind")) for r in pick})
        out.append(f"Items: {len({r.get('item') for r in pick})} ("
                   + ", ".join(f"{n} {k}s" for k, n in sorted(kinds.items())) + ")")
        out.append("Pass role=<role> [cond=<condition>] for what a role must support, or item=<name> for one "
                   "item's level per role.")
    elif item:
        groups: dict[tuple, list[dict]] = {}
        for r in pick:
            groups.setdefault((r.get("item"), r.get("item_kind")), []).append(r)
        for (name, kind), rs in sorted(groups.items(), key=lambda kv: tuple(map(str, kv[0]))):
            out += ([""] if out else []) + [f"## {name} ({kind})"]
            for r in sorted(rs, key=lambda r: (str(r.get("role")), str(r.get("as")), str(r.get("cond")))):
                who = " ".join(x for x in (r.get("role"), f"as {r['as']}" if r.get("as") else "",
                                           f"if {r['cond']}" if r.get("cond") else "") if x)
                out.append(f"- {who}: {r.get('level')}{_cell_tail(r)}")
    else:
        levels: dict[str, list[dict]] = {}
        for r in pick:
            levels.setdefault(r.get("level"), []).append(r)
        for lv in sorted(levels, key=lambda lv: (_level_rank(lv), str(lv))):
            rs = sorted(levels[lv], key=lambda r: (str(r.get("item_kind")), str(r.get("item")), str(r.get("as"))))
            out += ([""] if out else []) + [f"## {lv} ({len(rs)})"]
            for r in rs:
                what = ", ".join(x for x in (r.get("item_kind"), f"as {r['as']}" if r.get("as") else "",
                                             f"if {r['cond']}" if r.get("cond") else "") if x)
                out.append(f"- {r.get('item')} ({what}){_cell_tail(r)}")
    return _cap(head + "\n".join(out) + "\n", "applies", offset)


# a name in an event or an entry action that reads as a message, timer or counter: Snake_Case or CamelCase
SIGNAL_NAME = re.compile(r"\b[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+\b|\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+\b")


def _s(v: Any) -> str:
    return str(v or "").strip()


def _edge(t: dict, end: str) -> str:
    """`<state at end> on <event> [trigger <name>] (p.N)`: the trigger is shown when the event does not name it."""
    trig = _s(t.get("trigger"))
    tail = f" [trigger {trig}]" if trig and trig.lower() not in _s(t.get("event")).lower() else ""
    return f"{_s(t.get(end))} on {_s(t.get('event'))}{tail} ({_pp(t)}){_unverified(t)}"


@_stamped
def transitions(a: Atlas, doc: str, state: str | None = None, on: str | None = None, machine: str | None = None,
                offset: int = 0) -> str:
    """A document's state machines, from its `state` and `transition` records. `state`: its entry actions,
    its exits with the event that takes each, and the states that lead to it. `on` (a message or timer):
    every transition it triggers or whose event names it, and the states whose entry actions name it; with
    `state` too, only that state's. `machine` narrows to one machine: a state name two machines share asks
    for it. Neither `state` nor `on`: each machine and its states. A miss lists the nearest names."""
    d, recs = _protocol_records(a, doc, ("state", "transition", "figure"))
    if not d:
        return _missing(f"document {doc!r}", "Doc ids are listed in MAP.md and docs/.")
    states = [r for r in recs if r.get("kind") == "state"]
    trans = [r for r in recs if r.get("kind") == "transition"]
    figs = [r for r in recs if r.get("kind") == "figure" and _s(r.get("machine"))]
    if not states and not trans:
        return _missing(f"state machine records of {d.name}",
                        f"Not distilled yet: find the state's section with `doc_toc doc={d.name} under=<state>` or "
                        f"`find_text doc={d.name} pattern=<state>`, read it there, and suggest "
                        f"`/atlas-translate-docs standard {d.name} <section>`.")
    machines = sorted({_s(r.get("machine")) for r in states + trans if _s(r.get("machine"))})
    asked = " ".join(f"{k}={v!r}" for k, v in (("state", state), ("on", on), ("machine", machine)) if v)
    head = _header(f"{d.name} state machines", extra=" ".join(
        x for x in (f"states={len(states)} transitions={len(trans)}", f"query={asked!r}" if asked else "",
                    _pdf_extra(a, d)) if x))
    if machine:
        mn = _norm(machine)
        ms = [m for m in machines if _norm(m) == mn] or [m for m in machines if mn and mn in _norm(m)]
        if not ms:
            return _missing(f"machine {machine!r} in {d.name}", f"Machines: {', '.join(machines)}.")
        if len(ms) > 1:
            return _cap(head + f"{len(ms)} machines match {machine!r}; pass one as `machine`:\n"
                        + "".join(f"- {m}\n" for m in ms), "transitions", offset)
        machines = ms
        states, trans, figs = ([r for r in rs if _s(r.get("machine")) == ms[0]] for rs in (states, trans, figs))
    react = names_it = None
    if on:
        rx = _word_rx(on)
        react = lambda t: _norm(t.get("trigger")) == _norm(on) or bool(rx.search(_s(t.get("event"))))  # noqa: E731
        names_it = lambda act: bool(rx.search(act))  # noqa: E731
    out: list[str] = []
    if state:
        known = {(_s(r.get("machine")), _s(r.get("name"))) for r in states} \
            | {(_s(t.get("machine")), _s(t.get(e))) for t in trans for e in ("from", "to")}
        hits = sorted(k for k in known if _norm(k[1]) == _norm(state))
        if not hits:
            near = _nearest_names({n for _, n in known}, state)
            where = {n: sorted(m for m, x in known if x == n) for n in near}
            listed = "\n".join(f"- {n}" + (f" ({', '.join(where[n])})" if len(machines) > 1 else "") for n in near)
            return _missing(f"state {state!r} in {d.name}" + (f" machine {machines[0]!r}" if machine else ""),
                            f"Nearest states:\n{listed}\nCall transitions without `state` to list every machine's states.")
        if len({m for m, _ in hits}) > 1:
            rec_of = {(_s(r.get("machine")), _s(r.get("name"))): r for r in states}
            return _cap(head + f"{len(hits)} machines have a state named {state!r}; pass one as `machine`:\n"
                        + "".join(f"- {m}" + (f" ({_pp(rec_of[(m, n)])})" if (m, n) in rec_of else "") + "\n"
                                  for m, n in hits), "transitions", offset)
        m, name = hits[0]
        rec = next((r for r in states if (_s(r.get("machine")), _s(r.get("name"))) == (m, name)), None)
        out.append(f"## {m}: {name}" + (f" ({_pp(rec)}){_unverified(rec)}" if rec else
                                        " (no state record: `doc check` E fails the transitions naming it)"))
        entry = [x for x in (rec or {}).get("entry") or [] if not names_it or names_it(x)]
        exits = [t for t in trans if (_s(t.get("machine")), _s(t.get("from"))) == (m, name) and (not react or react(t))]
        ins = [t for t in trans if (_s(t.get("machine")), _s(t.get("to"))) == (m, name) and (not react or react(t))]
        none = ["- (none recorded" + (f" naming {on}" if on else "") + ")"]
        out += ["On entry:"] + ([f"- {x}" for x in entry] or none)
        out += ["Exits:"] + ([f"- to {_edge(t, 'to')}" for t in exits] or none)
        out += ["Entered from:"] + ([f"- {_edge(t, 'from')}" for t in ins] or none)
        return _cap(head + "\n".join(out) + "\n", "transitions", offset)
    if on:
        hit_t = [t for t in trans if react(t)]
        hit_e = [(s, x) for s in states for x in s.get("entry") or [] if names_it(x)]
        if not hit_t and not hit_e:
            names = {_s(t.get("trigger")) for t in trans} \
                | {x.group(0) for t in trans for x in SIGNAL_NAME.finditer(_s(t.get("event")))} \
                | {x.group(0) for s in states for act in s.get("entry") or [] for x in SIGNAL_NAME.finditer(act)}
            return _missing(f"a transition or an entry action naming {on!r} in {d.name}",
                            f"Nearest messages and timers: {', '.join(_nearest_names(names, on))}.")
        for m in machines:
            ts = [t for t in hit_t if _s(t.get("machine")) == m]
            es = [(s, x) for s, x in hit_e if _s(s.get("machine")) == m]
            if not ts and not es:
                continue
            out += ([""] if out else []) + [f"## {m}"]
            if ts:
                out += ["Transitions:"] + [f"- {_s(t.get('from'))} -> {_edge(t, 'to')}" for t in ts]
            if es:
                out += ["Entry actions:"] + [f"- {_s(s.get('name'))}: {x} ({_pp(s)}){_unverified(s)}" for s, x in es]
        return _cap(head + "\n".join(out) + "\n", "transitions", offset)
    for m in machines:
        ss = sorted((s for s in states if _s(s.get("machine")) == m), key=lambda s: (_pages(s)[:1], _s(s.get("name"))))
        n_t = sum(1 for t in trans if _s(t.get("machine")) == m)
        drawn = "".join(f"; {_s(f.get('fig'))} {_pp(f)}" for f in figs if _s(f.get("machine")) == m)
        out += ([""] if out else []) + [f"## {m} ({len(ss)} states, {n_t} transitions{drawn})",
                                         ", ".join(f"{_s(s.get('name'))} ({_pp(s)})" for s in ss) or "(no state records)"]
    out += ["", "Pass state=<name> for a state's entry actions, exits and the states that lead to it, or "
                "on=<message or timer> for every state that reacts to it."]
    return _cap(head + "\n".join(out) + "\n", "transitions", offset)


TOOLS = {
    "part_card": part_card, "doc_toc": doc_toc, "doc_section": doc_section, "find_fact": find_fact,
    "find_text": find_text,
    "register": register_lookup, "pin_trace": pin_trace, "net_trace": net_trace, "mismatch_report": mismatch_report,
    "config": config, "dt_node": dt_node, "footprint": footprint, "stacks": stacks,
    "applies": applies, "transitions": transitions,
}
TOOLS.update({"code": code, "states": states})


# --------------------------------------------------------------------------- CLI

def register(sub, groups) -> None:
    """cli.py hook: the `q` verbs, one per MCP tool."""
    q = common.verb_group(sub, groups, "q", "query the atlas (same answers as the MCP tools)")

    def add(name, fn, *args):
        p = q.add_parser(name.replace("_", "-"), help=(fn.__doc__ or "").splitlines()[0])
        for a_ in args:
            p.add_argument(*a_[0], **a_[1])
        p.add_argument("--atlas", help="atlas id (default: current)")
        p.set_defaults(func=lambda ns, fn=fn: _run(ns, fn))

    add("part_card", part_card, (["part"], {}), (["--board"], {"nargs": "?", "const": "all"}), (["--offset"], {"type": int, "default": 0}))
    add("doc_toc", doc_toc, (["doc"], {}), (["--under"], {}), (["--depth"], {"type": int, "default": 2}), (["--offset"], {"type": int, "default": 0}))
    add("doc_section", doc_section, (["doc"], {}), (["--section"], {}), (["--page"], {"type": int}),
        (["--tier"], {"choices": ["distilled", "text"], "default": "distilled"}), (["--offset"], {"type": int, "default": 0}))
    add("find_fact", find_fact, (["query"], {"nargs": "+"}), (["--part"], {}), (["--kind"], {}), (["--limit"], {"type": int}), (["--offset"], {"type": int, "default": 0}))
    add("find_text", find_text, (["doc"], {}), (["pattern"], {}), (["--section"], {}),
        (["--context"], {"type": int, "default": 2}), (["--offset"], {"type": int, "default": 0}))
    add("register", register_lookup, (["target"], {}), (["--field"], {}), (["--offset"], {"type": int, "default": 0}))
    add("pin_trace", pin_trace, (["query"], {}), (["--board"], {}), (["--offset"], {"type": int, "default": 0}))
    add("net_trace", net_trace, (["query"], {}), (["--board"], {}), (["--offset"], {"type": int, "default": 0}))
    add("config", config, (["symbol"], {"nargs": "*", "help": "names or `*` patterns, space- or comma-separated"}),
        (["--build"], {"help": "a build dir (default: the atlas's pinned build)"}),
        (["--vs"], {"help": "list only the differences against this build dir, or `pinned`"}), (["--offset"], {"type": int, "default": 0}))
    add("dt_node", dt_node, (["node"], {"nargs": "?"}), (["--build"], {"help": "a build dir (default: the atlas's pinned build)"}),
        (["--vs"], {"help": "list only the differences against this build dir, or `pinned`"}), (["--offset"], {"type": int, "default": 0}))
    add("footprint", footprint, (["--build"], {"help": "a build dir (default: the atlas's pinned build)"}),
        (["--vs"], {"help": "compare against this build dir, or `pinned`"}),
        (["--by"], {"choices": ["dir", "file", "symbol"], "default": "dir"}), (["--offset"], {"type": int, "default": 0}))
    add("stacks", stacks, (["--build"], {"help": "a build dir (default: the atlas's pinned build)"}), (["--offset"], {"type": int, "default": 0}))
    add("mismatch_report", mismatch_report, (["--scope"], {}), (["--cls"], {}), (["--offset"], {"type": int, "default": 0}),
        (["--build"], {"help": "run the config-vs-standard checks against this build dir (or `pinned`) instead"}))
    add("applies", applies, (["doc"], {}), (["--role"], {}), (["--item"], {}), (["--cond"], {}),
        (["--offset"], {"type": int, "default": 0}))
    add("transitions", transitions, (["doc"], {}), (["--state"], {}), (["--on"], {"help": "a message or timer name"}),
        (["--machine"], {}), (["--offset"], {"type": int, "default": 0}))
    add("code", code, (["symbol"], {}), (["--build"], {"help": "a build dir (default: the atlas's pinned build)"}),
        (["--show"], {"choices": ["all", "def", "refs"], "default": "all"}), (["--offset"], {"type": int, "default": 0}))
    add("states", states, (["table"], {"help": "an SMF table name, or a directory or file holding tables"}),
        (["--build"], {"help": "a build dir (default: the atlas's pinned build)"}), (["--offset"], {"type": int, "default": 0}))


def _run(ns, fn) -> int:
    a = Atlas(common.atlas_dir(ns.atlas_dir), getattr(ns, "atlas", None))
    kwargs = {k: v for k, v in vars(ns).items()
              if k not in ("func", "verb", "q_verb", "atlas", "atlas_dir") and v is not None}
    if isinstance(kwargs.get("query"), list):
        kwargs["query"] = " ".join(kwargs["query"])
    print(fn(a, **kwargs), end="")
    return 0


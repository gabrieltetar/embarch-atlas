"""The eight atlas lookups, as plain functions returning markdown.

One implementation behind three doors: the `embarch-atlas q <verb>` CLI, the MCP
server (`mcp_server.py`), and later the UI. Each answer starts with one cite header
line (`src: ...`), returns the same bytes as the files where a file exists, and is
cut at a token budget with a `next=` offset when it is longer. DESIGN.md, "CLI" and
the MCP tool surface drafted in research note 14.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from . import common
from .pdftext import tier2_files

CHARS_PER_TOKEN = 4  # o200k proxy; real Claude counts run ~1.4x, budgets below allow for it
BUDGET = {"part_card": 1500, "doc_toc": 2500, "doc_section": 6000, "find_fact": 2000,
          "register": 1500, "pin_trace": 4000, "net_trace": 4000, "mismatch_report": 4000}


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
    """The named atlas, else the one current.json says matches HEAD, else the newest."""
    root = atlas_dir / "atlases"
    if atlas:
        p = root / atlas
        return p if p.is_dir() else None
    cur = atlas_dir / "current.json"
    if cur.is_file():
        try:
            for m in common.read_json(cur).get("matches") or []:
                p = root / m.get("atlas", "")
                if p.is_dir():
                    return p
        except (OSError, ValueError):
            pass
    stamps = sorted(root.glob("*/atlas.json"), key=lambda p: p.stat().st_mtime)
    return stamps[-1].parent if stamps else None


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

    # ---- documents
    def doc_meta(self, docdir: Path) -> dict:
        return _json(docdir / "doc.json", {}) or {}

    def mpn_docs(self) -> dict[str, set[str]]:
        """MPN -> doc ids, from the atlas's part_docs join and the board netlists."""
        out: dict[str, set[str]] = {}
        pins = self.pins_json()
        part_docs = (pins or {}).get("part_docs") or {}
        mpn_of = self.ref_mpns()
        for ref, docs in part_docs.items():
            mpn = mpn_of.get(ref)
            if mpn:
                out.setdefault(mpn, set()).update(docs)
        return out

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
                    scored.append((0 if kn == n else 1, d.name, d))
        return sorted(scored)[0][2] if scored else None

    # ---- atlas files
    def pins_json(self) -> dict | None:
        if self._pins is None and self.atlas:
            self._pins = _json(self.atlas / "pins.json", {}) or {}
        return self._pins


def _header(src: str, prov: str | None = None, extra: str = "") -> str:
    return f"src: {src}" + (f" prov={prov}" if prov else "") + (f" {extra}" if extra else "") + "\n\n"


def _missing(what: str, hint: str) -> str:
    return f"src: none\n\n{what} not found. {hint}\n"


# --------------------------------------------------------------------------- the eight

def part_card(a: Atlas, part: str, board: str | None = None) -> str:
    """The part's card.md; with `board`, the refs of that part on the boards and their nets."""
    d = a.find_doc(part)
    if not d:
        return _missing(f"part {part!r}", "Try an MPN from MAP.md, a doc id from docs/, or see inventory/missing.md "
                        "for parts without a supplied datasheet.")
    card = d / "card.md"
    body = card.read_text(encoding="utf-8") if card.is_file() else "(no card.md yet: run /atlas-translate-docs)\n"
    out = _header(f"{d.name} card") + body
    if board is not None:
        out += "\n## On the boards\n\n" + _bindings(a, d.name, board or None)
    return _cap(out, "part_card")


def _bindings(a: Atlas, doc_id: str, board: str | None) -> str:
    part_docs = (a.pins_json() or {}).get("part_docs") or {}
    refs = sorted(r for r, ids in part_docs.items() if doc_id in ids and (not board or r.startswith(board + ":")))
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


def doc_toc(a: Atlas, doc: str, under: str | None = None, depth: int = 2) -> str:
    """Outline rows: id | title | pages | tokens | tier-2 file (when distilled)."""
    d = a.find_doc(doc)
    if not d:
        return _missing(f"document {doc!r}", "Doc ids are listed in MAP.md and docs/.")
    rows = _toc_rows(d)
    if not rows:
        toc_md = d / "toc.md"
        return _cap(_header(f"{d.name} toc") + (toc_md.read_text(encoding="utf-8") if toc_md.is_file() else "(no outline)\n"),
                    "doc_toc")
    base = None
    if under:
        found = _toc_find(rows, under)
        if not found:
            return _missing(f"section {under!r} in {d.name}", "Call doc_toc without `under` to list the top levels.")
        if len(found) > 1:
            listed = "\n".join(f"{r['id']} | {r['title']} | p{r['start']}" for r in found)
            return _cap(_header(f"{d.name} toc", extra=f"under={under!r} matches={len(found)}")
                        + f"{len(found)} sections match {under!r}; pass one id as `under`:\n{listed}\n", "doc_toc")
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
    return _cap(_header(f"{d.name} toc" + (f" under {base['id']}" if base else "")) + "\n".join(out) + "\n", "doc_toc")


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
        return _cap(_header(f"{d.name} p{page} text-layer", extra="(grep target; Read the PDF page to see it)") + body,
                    "doc_section", offset)
    files = sorted((d / "s").glob("*.md")) if (d / "s").is_dir() else []
    if not files:
        return _missing(f"distilled sections of {d.name}", "Not translated yet: run /atlas-translate-docs.")
    if not section:
        return _cap(_header(f"{d.name} sections") + "\n".join(f"- s/{f.name}" for f in files) + "\n", "doc_section")
    want = _norm(section)
    rows = {r["id"]: r for r in _toc_rows(d)}
    title = _norm((rows.get(section) or {}).get("title", ""))
    pick = next((f for f in files if f.stem == section), None) \
        or next((f for f in files if _norm(f.stem) == want or (title and _norm(f.stem) == title)), None) \
        or next((f for f in files if want and want in _norm(f.stem)), None)
    if not pick:
        return _missing(f"section {section!r} in {d.name}",
                        "Call doc_section without `section` to list the files, or doc_toc for the outline.")
    return _cap(_header(f"{d.name} s/{pick.name}") + pick.read_text(encoding="utf-8"), "doc_section", offset)


_TOKEN = re.compile(r"[A-Za-z0-9_]+")


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


def find_fact(a: Atlas, query: str, part: str | None = None, kind: str | None = None,
              limit: int | None = None) -> str:
    """Lines of facts.txt ranked lexically (BM25) across every document, each with its cite.
    A query term naming one part narrows the search to it, as `part` does. With no `limit`, shows
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
        if f.is_file():
            for ln in f.read_text(encoding="utf-8").splitlines():
                if ln.strip() and (not kind or f"kind={kind};" in ln or f"kind={kind} " in ln):
                    lines.append(ln)
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
    head = _header(f"facts.txt ({len(docs)} docs)",
                   extra=f"query={query!r}{narrowed} hits={len(scored)} shown={len(hits)}"
                   + (f" unmatched={','.join(unmatched)}" if unmatched else ""))
    body = "\n".join(hits) if hits else "(no fact matches; try a symbol, or doc_toc + doc_section)"
    if limit is None and len(hits) < len(scored):
        body += (f"\n({len(scored) - len(hits)} more hits, each under {FIND_FACT_FLOOR:g} of the top score or past "
                 f"the first {FIND_FACT_LIMIT}; pass `limit` to list them)")
    return _cap(head + body + "\n", "find_fact")


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
    return out


def _byte_value(v: Any) -> int | None:
    s = str(v or "").strip().lower()
    try:
        n = int(s[2:], 2) if s.startswith("0b") else int(s, 16) if s.startswith("0x") else int(s)
    except ValueError:
        return None
    return n if 0 <= n <= 0xFF else None


def _register_pair(a: Atlas, reg: str, msb: tuple[Path, dict], lsb: tuple[Path, dict], field: str | None) -> str:
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
    return _cap("\n".join(out + _reg_tail(a, related, ids)) + "\n", "register")


def register_lookup(a: Atlas, target: str, field: str | None = None) -> str:
    """A register (MCU `PERIPH.REG` or external `part.REG`) with its fields, rules and conflicts.
    A name a datasheet splits into `<REG>_MSB` and `<REG>_LSB` returns both as one 16-bit view."""
    head, _, reg = target.partition(".")
    if not reg:
        return _missing(f"register {target!r}", "Use `PERIPH.REG` (MCU, e.g. I2C1.CR1) or `<part>.<REG>`.")
    hn, rn = _norm(head), _norm(reg)
    head_doc = (a.find_doc(head) or Path("-")).name
    cands = _reg_cands(a, hn, rn, head_doc)
    if not cands:
        msb, lsb = _reg_cands(a, hn, rn + "msb", head_doc), _reg_cands(a, hn, rn + "lsb", head_doc)
        if msb and lsb and msb[0][0] == lsb[0][0]:
            return _register_pair(a, reg, msb[0], lsb[0], field)
        return _missing(f"register {target!r}", "Check the name in the SVD slice (docs/<svd>/s/<PERIPH>.md) or the "
                        "part's register section; names are matched without punctuation.")
    d, reg_rec = cands[0]
    rid = reg_rec.get("id")
    fields, related = _reg_joins(a, rid, field)
    fields.sort(key=lambda r: -(r.get("bits") or [0])[0])
    out = [_header(f"{d.name} {rid}", reg_rec.get("prov")).rstrip("\n"), "",
           f"{reg_rec.get('name')} addr={reg_rec.get('addr')} width={reg_rec.get('width')} "
           f"reset={reg_rec.get('reset')} access={reg_rec.get('access')} cite={_cite(reg_rec)}", ""]
    out += [_field_line(f) for f in fields]
    out += _reg_tail(a, related, [rid])
    if len(cands) > 1:
        out += ["", "Also named " + reg + " in: " + ", ".join(sorted({c[0].name for c in cands[1:]}))]
    return _cap("\n".join(out) + "\n", "register")


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


def pin_trace(a: Atlas, query: str, board: str | None = None) -> str:
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
    return _cap("\n".join(out) + "\n", "pin_trace")


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


def mismatch_report(a: Atlas, scope: str | None = None, cls: str | None = None, offset: int = 0) -> str:
    """Checks run and passed first, then candidates (claim, evidence, what would settle it)."""
    if not a.atlas:
        return _missing("atlas", "Run /atlas-build first.")
    m = _json(a.atlas / "mismatches.json")
    if not m:
        md = a.atlas / "checks.md"
        return _cap(_header(f"{a.atlas.name} checks.md") + (md.read_text(encoding="utf-8") if md.is_file() else ""),
                    "mismatch_report", offset)
    out = [_header(f"{a.atlas.name} mismatches.json").rstrip("\n"), ""]
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
    return _cap("\n".join(out) + "\n", "mismatch_report", offset)


TOOLS = {
    "part_card": part_card, "doc_toc": doc_toc, "doc_section": doc_section, "find_fact": find_fact,
    "register": register_lookup, "pin_trace": pin_trace, "net_trace": net_trace, "mismatch_report": mismatch_report,
}


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

    add("part_card", part_card, (["part"], {}), (["--board"], {"nargs": "?", "const": ""}))
    add("doc_toc", doc_toc, (["doc"], {}), (["--under"], {}), (["--depth"], {"type": int, "default": 2}))
    add("doc_section", doc_section, (["doc"], {}), (["--section"], {}), (["--page"], {"type": int}),
        (["--tier"], {"choices": ["distilled", "text"], "default": "distilled"}), (["--offset"], {"type": int, "default": 0}))
    add("find_fact", find_fact, (["query"], {"nargs": "+"}), (["--part"], {}), (["--kind"], {}), (["--limit"], {"type": int}))
    add("register", register_lookup, (["target"], {}), (["--field"], {}))
    add("pin_trace", pin_trace, (["query"], {}), (["--board"], {}))
    add("net_trace", net_trace, (["query"], {}), (["--board"], {}), (["--offset"], {"type": int, "default": 0}))
    add("mismatch_report", mismatch_report, (["--scope"], {}), (["--cls"], {}), (["--offset"], {"type": int, "default": 0}))


def _run(ns, fn) -> int:
    a = Atlas(common.atlas_dir(ns.atlas_dir), getattr(ns, "atlas", None))
    kwargs = {k: v for k, v in vars(ns).items()
              if k not in ("func", "verb", "q_verb", "atlas", "atlas_dir") and v is not None}
    if isinstance(kwargs.get("query"), list):
        kwargs["query"] = " ".join(kwargs["query"])
    print(fn(a, **kwargs), end="")
    return 0


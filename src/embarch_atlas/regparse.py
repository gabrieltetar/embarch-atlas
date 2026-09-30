"""`doc regparse <doc_id> --template ti`: deterministic register records from the text layer.

One parser per vendor template; the LLM's register records (if any) are then its independent twin,
compared by `doc check` (G). A template also audits the document: where its own bit diagram, field
table, heading and summary table disagree, that is written as a `conflict` record.

Output: `records/regparse.jsonl` (register, field and conflict records, prov deterministic). A re-run
replaces the file; no other record file is touched.

Adding a template: subclass `Template`, implement `parse(pages) -> Parse`, add it to TEMPLATES.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field as dfield
from pathlib import Path

from . import common, records as R

OUT_FILE = "regparse.jsonl"


@dataclass
class RawField:
    name: str
    hi: int
    lo: int
    access: str
    reset: int | None
    reset_width: int | None
    desc: str
    p: int
    q: str


@dataclass
class RawReg:
    name: str
    addrs: list[int]
    resets: list[str]
    p: int
    q: str
    fields: list[RawField] = dfield(default_factory=list)
    fig_tokens: list[tuple[str, str]] = dfield(default_factory=list)
    figure: str | None = None
    table: str | None = None


@dataclass
class Parse:
    registers: list[RawReg]
    summary: dict[int, dict]          # address -> {name, access, reset, p, q}
    notes: list[str] = dfield(default_factory=list)


class Template:
    name = "?"

    def parse(self, pages: dict[int, str]) -> Parse:  # pragma: no cover - interface
        raise NotImplementedError


# ---------------------------------------------------------------- TI

ACCESS = {"R/W": "rw", "RW": "rw", "R": "ro", "RO": "ro", "W": "wo", "WO": "wo", "RC": "rclr", "R/WC": "woclr",
          "R/W1C": "woclr", "W1C": "woclr", "R/W1S": "w1s", "W1S": "w1s"}
_ACC = r"R/W1C|R/W1S|R/WC|R/W|RC|W1C|W1S|RW|RO|WO|R|W"


def _bits_value(tok: str, width: int | None = None) -> tuple[int | None, int | None]:
    """A TI reset token -> (value, printed width). `0101b` binary, `E4h` hex, `x`/`-` undefined."""
    t = tok.replace(" ", "")
    if not t or set(t.lower()) <= {"x", "-"}:
        return None, None
    if t.lower().endswith("b") and set(t[:-1]) <= {"0", "1"} and t[:-1]:
        return int(t[:-1], 2), len(t[:-1])
    if t.lower().endswith("h"):
        body = t[:-1]
        return int(body, 16), None
    if set(t) <= {"0", "1"}:
        return int(t, 2), len(t)
    if t.isdigit():
        return int(t), None
    return None, None


class TI(Template):
    """TI's register-description template:
    `7.6.1 NAME Register (Address = 0h[, 1h]) [reset = <value>h[, <value>h]]` headings; `Figure a-b.`
    bit diagrams with `R/W-0101b` tokens; `Table a-b. NAME Register Field Descriptions` with
    `Bit Field Type Reset Description` rows; and a register summary / default-settings table
    (`00h NAME R/W 11010010`). A heading reset that is all 0/1 and 8 digits per byte is binary printed
    with an `h` suffix, a known TI artefact, and is read as binary."""
    name = "ti"
    HEAD = re.compile(r"^\s*(?P<sec>\d+(?:\.\d+)+)\s+(?P<name>[A-Za-z0-9_]+)\s+Register\s*"
                      r"\((?:Address|Offset)\s*=\s*(?P<addr>[^)]*)\)\s*\[\s*[Rr]eset\s*=\s*(?P<reset>[^\]]*)\]")
    ROW = re.compile(rf"^\s*(\d+)(?:\s*[-:]\s*(\d+))?\s+([A-Za-z_][A-Za-z0-9_]*)\s+({_ACC})\s+"
                     r"([01][01 ]*b|[0-9A-Fa-f]+h|[01]+|[xX-]+)(?=\s|$)\s*(.*)$")
    FIGTOK = re.compile(rf"(?<![\w/])({_ACC})-([01xX]+b|[0-9A-Fa-f]+h)(?![\w])")
    TABLE_HDR = re.compile(r"^\s*Bits?\s+Field\s+Type\s+Reset\s+(?:\w+\s+)*?Description", re.I)
    SUMROW = re.compile(rf"^\s*([0-9A-Fa-f]{{1,4}})h\s+([A-Za-z_][A-Za-z0-9_]*)\s+({_ACC})\s+([01]{{8,}}|[0-9A-Fa-f]+h)\s*$")
    OTHER_HEAD = re.compile(r"^\s?\d{1,2}(?:\.\d{1,2})+\s+[A-Z][A-Za-z]")
    CAPTION = re.compile(r"^\s*(Figure|Table)\s+(\d+(?:-\d+)?)\.\s+(.*?)\s*$")
    RCLR = re.compile(r"does not reset until (?:it|the register) is read|clear(?:ed|s)?\s+(?:on|after|when)\s+"
                      r"(?:it is\s+|the register is\s+)?read", re.I)

    @staticmethod
    def _addrs(s: str) -> list[int]:
        out = []
        for tok in re.split(r"[,/]", s):
            t = tok.strip()
            if not t:
                continue
            if t.lower().startswith("0x"):
                out.append(int(t, 16))
            elif t.lower().endswith("h"):
                out.append(int(t[:-1], 16))
            elif re.fullmatch(r"[0-9A-Fa-f]+", t):
                out.append(int(t, 16))
        return out

    def parse(self, pages: dict[int, str]) -> Parse:
        regs: list[RawReg] = []
        summary: dict[int, dict] = {}
        cur: RawReg | None = None
        desc_col: int | None = None
        for pno in sorted(pages):
            lines = pages[pno].splitlines()
            last: RawField | None = None
            for ln in lines:
                m = self.HEAD.match(ln)
                if m:
                    resets = [x.strip() for x in m.group("reset").split(",") if x.strip()]
                    cur = RawReg(name=m.group("name"), addrs=self._addrs(m.group("addr")), resets=resets, p=pno,
                                 q=re.sub(r"\s+", " ", ln.strip()))
                    regs.append(cur)
                    last, desc_col = None, None
                    continue
                s = self.SUMROW.match(ln)
                if s:
                    a = int(s.group(1), 16)
                    summary[a] = {"name": s.group(2), "access": ACCESS[s.group(3)], "reset": s.group(4), "p": pno,
                                  "q": re.sub(r"\s+", " ", ln.strip())}
                    continue
                if cur is None:
                    continue
                if self.OTHER_HEAD.match(ln):
                    cur, last = None, None  # a numbered section that is not a register ends the map
                    continue
                c = self.CAPTION.match(ln)
                if c:
                    if c.group(1) == "Figure":
                        cur.figure = f"Figure {c.group(2)}"
                    else:
                        cur.table = f"Table {c.group(2)}"
                    last = None
                    continue
                h = self.TABLE_HDR.match(ln)
                if h:
                    desc_col = ln.lower().find("description")
                    last = None
                    continue
                if cur.table is None:
                    cur.fig_tokens += self.FIGTOK.findall(ln)
                r = self.ROW.match(ln)
                if r and cur.table is not None:
                    hi = int(r.group(1))
                    lo = int(r.group(2)) if r.group(2) else hi
                    hi, lo = max(hi, lo), min(hi, lo)
                    val, width = _bits_value(r.group(5))
                    last = RawField(name=r.group(3), hi=hi, lo=lo, access=ACCESS[r.group(4)], reset=val,
                                    reset_width=width, desc=r.group(6).strip(), p=pno,
                                    q=re.sub(r"\s+", " ", ln.strip()))
                    cur.fields.append(last)
                    continue
                if last is not None and ln.strip():
                    indent = len(ln) - len(ln.lstrip())
                    if desc_col is not None and indent >= desc_col - 2:
                        last.desc = (last.desc + " " + ln.strip()).strip()
                    else:
                        last = None
        return Parse(registers=regs, summary=summary)


TEMPLATES: dict[str, type[Template]] = {"ti": TI}


# ---------------------------------------------------------------- raw parse -> records

def _reset_str(v: int | None, width: int) -> str | None:
    return None if v is None else "0b" + format(v, f"0{width}b")


def _heading_reset(tok: str, nbytes: int) -> tuple[int | None, str | None]:
    """(value, note). `11100100h` is binary with an h suffix when it is 8 digits of 0/1 per byte."""
    t = tok.strip()
    low = t.lower()
    body = t[:-1] if low.endswith(("h", "b")) else t
    if body and set(body) <= {"0", "1"} and (len(body) == 8 * nbytes or low.endswith("b")):
        return int(body, 2), ("binary printed with an h suffix" if low.endswith("h") else None)
    if body and re.fullmatch(r"[0-9A-Fa-f]+", body):
        return int(body, 16), None
    return None, None


def _split_names(rr: RawReg, nbytes: int) -> tuple[list[str], dict[int, list[str]]]:
    """Record ids of a logical register split into bus bytes: the register ids, and per raw field
    (by index) the ids of its pieces. Naming rules as in to_records."""
    regs, pieces = [], {}
    for k in range(nbytes):
        name = rr.name if nbytes == 1 else f"{rr.name}_{'LSB' if k == 0 else ('MSB' if k == nbytes - 1 else f'B{k}')}"
        regs.append(name)
        lo_b, hi_b = 8 * k, 8 * k + 7
        for i, f in enumerate(rr.fields):
            lo, hi = max(f.lo, lo_b), min(f.hi, hi_b)
            if lo > hi:
                continue
            if f.name.upper() == "RESERVED":
                fname = f"RESERVED_{hi - lo_b}_{lo - lo_b}" if hi != lo else f"RESERVED_{hi - lo_b}"
            elif nbytes > 1:
                fname = f"{f.name}_{hi}_{lo}"
            else:
                fname = f.name
            pieces.setdefault(i, []).append(f"{name}.{fname}")
    return regs, pieces


def _check_document(rr: RawReg, part: str, head_full: int | None, nbytes: int, conflict) -> None:
    """Where the document disagrees with itself: field resets vs heading, bit diagram vs field table."""
    width_total = 8 * nbytes
    reg_names, pieces = _split_names(rr, nbytes)
    reg_refs = [f"{part}.reg.{n}" for n in reg_names]
    fig_src = f"bit diagram ({rr.figure or 'figure'}, p{rr.p})"
    tab_src = f"field table ({rr.table or 'table'}, p{rr.fields[0].p if rr.fields else rr.p})"
    comp = 0
    for f in rr.fields:
        comp |= (f.reset or 0) << f.lo
    if rr.fields and head_full is not None and all(f.reset is not None for f in rr.fields) and comp != head_full:
        conflict(f"{rr.name}.reset", reg_refs, [f"heading p{rr.p}", tab_src],
                 f"field resets compose to {comp:0{width_total}b}, the heading says {head_full:0{width_total}b}",
                 rr.p, rr.q)
    order = sorted(range(len(rr.fields)), key=lambda i: -rr.fields[i].hi)
    tbl = [rr.fields[i] for i in order]
    if rr.fig_tokens and len(rr.fig_tokens) == len(tbl):
        for (fa, fr), i in zip(rr.fig_tokens, order):
            f = rr.fields[i]
            ref = [f"{part}.reg.{x}" for x in pieces.get(i, [])]
            span = f"{f.hi}" if f.hi == f.lo else f"{f.hi}:{f.lo}"
            if ACCESS[fa] != f.access:
                conflict(f"{rr.name}.{f.name}_{span.replace(':', '_')}.access", ref, [fig_src, tab_src],
                         f"{rr.name}[{span}] {f.name}: access {ACCESS[fa]} in the bit diagram, {f.access} in the field table",
                         f.p, f.q)
            fv, fw = _bits_value(fr)
            if fv != f.reset or (fw is not None and fw != f.hi - f.lo + 1):
                conflict(f"{rr.name}.{f.name}_{span.replace(':', '_')}.reset", ref, [fig_src, tab_src],
                         f"{rr.name}[{span}] {f.name}: reset {fr} ({fw or '?'} bits) in the bit diagram, "
                         f"{_reset_str(f.reset, f.hi - f.lo + 1)} ({f.hi - f.lo + 1} bits) in the field table",
                         f.p, f.q)
    elif rr.fig_tokens or rr.fields:
        conflict(f"{rr.name}.bit_diagram", reg_refs, [fig_src, tab_src],
                 f"{rr.name}: {len(rr.fig_tokens)} access/reset tokens in the bit diagram, "
                 f"{len(tbl)} rows in the field table", rr.p, rr.q)


def to_records(parse: Parse, doc_id: str, part: str, template: str) -> list[dict]:
    """Registers split into bus-addressable bytes when a heading lists several addresses (`NAME` at 0h, 1h
    becomes NAME_LSB / NAME_MSB); a field split across bytes is renamed `<FIELD>_<hi>_<lo>` (absolute bits), a
    reserved one `RESERVED_<hi>_<lo>` (bits within the byte)."""
    out: list[dict] = []
    conflicts: list[dict] = []
    used: set[str] = set()

    def conflict(key: str, refs: list[str], sources: list[str], text: str, p: int, q: str | None):
        cid = f"{part}.conflict.{key}"
        base, j = cid, 2
        while cid in used:
            cid, j = f"{base}.{j}", j + 1
        used.add(cid)
        conflicts.append({"id": cid, "kind": "conflict", "doc": doc_id, "p": p, "q": q, "src": "table",
                          "prov": "deterministic", "refs": refs, "sources": sources, "text": text})

    for rr in parse.registers:
        top = max([f.hi for f in rr.fields] + [8 * max(1, len(rr.addrs)) - 1])
        nbytes = max(len(rr.addrs), (top + 8) // 8)
        note = None
        if len(rr.resets) > 1:                       # one reset per address, LSB address first
            byte_vals = []
            for tok in rr.resets:
                v, n = _heading_reset(tok, 1)
                byte_vals.append(v)
                note = note or n
            full = None if any(v is None for v in byte_vals) else sum(v << (8 * k) for k, v in enumerate(byte_vals))
        else:
            full, note = _heading_reset(rr.resets[0], nbytes) if rr.resets else (None, None)
            byte_vals = [None if full is None else (full >> (8 * k)) & 0xFF for k in range(nbytes)]
        _check_document(rr, part, full, nbytes, conflict)
        addrs = rr.addrs or [0]
        for k in range(nbytes):
            a = addrs[k] if k < len(addrs) else addrs[-1] + (k - len(addrs) + 1)
            lo_b, hi_b = 8 * k, 8 * k + 7
            name = rr.name if nbytes == 1 else f"{rr.name}_{'LSB' if k == 0 else ('MSB' if k == nbytes - 1 else f'B{k}')}"
            rid = f"{part}.reg.{name}"
            rv = byte_vals[k] if k < len(byte_vals) else None
            fields, accs = [], set()
            for f in rr.fields:
                lo, hi = max(f.lo, lo_b), min(f.hi, hi_b)
                if lo > hi:
                    continue
                if f.name.upper() == "RESERVED":
                    fname = f"RESERVED_{hi - lo_b}_{lo - lo_b}" if hi != lo else f"RESERVED_{hi - lo_b}"
                elif nbytes > 1:
                    fname = f"{f.name}_{hi}_{lo}"
                else:
                    fname = f.name
                sub = None if f.reset is None else (f.reset >> (lo - f.lo)) & ((1 << (hi - lo + 1)) - 1)
                acc = "rclr" if f.access == "ro" and TI.RCLR.search(f.desc) else f.access
                fields.append({"id": f"{rid}.{fname}", "kind": "field", "doc": doc_id, "p": f.p, "q": f.q,
                               "src": "table", "prov": "deterministic", "reg": rid, "name": fname,
                               "bits": [hi - lo_b, lo - lo_b], "reset": _reset_str(sub, hi - lo + 1),
                               "access": acc, "desc": f.desc or None})
                if not f.name.upper().startswith("RESERVED"):
                    accs.add(acc)
            reg = {"id": rid, "kind": "register", "doc": doc_id, "p": rr.p, "q": rr.q, "src": "table",
                   "prov": "deterministic", "name": name, "addr": f"0x{a:02X}", "width": 8,
                   "reset": _reset_str(rv, 8),
                   "access": "rw" if accs and accs <= {"rw"} else ("ro" if accs and accs <= {"ro", "rclr"} else None),
                   "note": note}
            sm = parse.summary.get(a)
            if sm:
                raw = sm["reset"]
                sv, _ = _bits_value(raw + "b" if re.fullmatch(r"[01]{8,}", raw) else raw)
                reg["summary"] = {"name": sm["name"], "access": sm["access"], "reset": _reset_str(sv, 8)}
                if rv is not None and sv is not None and sv != rv:
                    conflict(f"{name}.summary_reset", [rid], [f"heading p{rr.p}", f"summary table p{sm['p']}"],
                             f"{name}: reset {_reset_str(rv, 8)} in the heading, {_reset_str(sv, 8)} in the summary table",
                             sm["p"], sm["q"])
            out.append(reg)
            out += fields
    covered = {R.to_int(r["addr"]) for r in out if r["kind"] == "register"}
    for a, sm in sorted(parse.summary.items()):
        if a not in covered:
            conflict(f"summary.{sm['name']}", [], [f"summary table p{sm['p']}"],
                     f"{sm['name']} at 0x{a:02X} is in the summary table but has no register section", sm["p"], sm["q"])
    for r in out + conflicts:
        for k in [k for k, v in r.items() if v is None and k != "q"]:
            del r[k]
    return out + conflicts


def run(docdir: Path, template: str) -> dict:
    docdir = Path(docdir)
    if template not in TEMPLATES:
        raise ValueError(f"unknown template {template!r}; known: {', '.join(sorted(TEMPLATES))}")
    meta = common.read_json(docdir / "doc.json")
    pages = {}
    for f in sorted((docdir / "pages").glob("p[0-9][0-9][0-9]*.txt")):
        m = re.fullmatch(r"p(\d+)\.txt", f.name)
        if m:
            pages[int(m.group(1))] = f.read_text(encoding="utf-8")
    parse = TEMPLATES[template]().parse(pages)
    part = meta["doc_id"].split("@")[0]
    recs = to_records(parse, meta["doc_id"], part, template)
    R.save_file(docdir, OUT_FILE, recs)
    regs = [r for r in recs if r["kind"] == "register"]
    return {"registers": len(regs), "fields": sum(1 for r in recs if r["kind"] == "field"),
            "summary_rows": len(parse.summary), "summary_covered": sum(1 for r in regs if r.get("summary")),
            "conflicts": [r for r in recs if r["kind"] == "conflict"]}


def cmd_regparse(args) -> int:
    atlas = common.atlas_dir(args.atlas_dir)
    docdir = atlas / "docs" / args.doc_id
    if not (docdir / "doc.json").exists():
        print(f"doc regparse: {docdir} has no doc.json; run doc prep first", file=sys.stderr)
        return 2
    try:
        st = run(docdir, args.template)
    except ValueError as e:
        print(f"doc regparse: {e}", file=sys.stderr)
        return 2
    print(f"{args.doc_id}: {st['registers']} registers, {st['fields']} fields, summary table "
          f"{st['summary_covered']}/{st['summary_rows']} covered, {len(st['conflicts'])} inconsistencies inside "
          f"the document -> records/{OUT_FILE}")
    for c in st["conflicts"]:
        print(f"   {c['id']}: {c['text']}")
    return 0 if st["registers"] else 1


def register(sub, groups) -> None:
    g = common.verb_group(sub, groups, "doc", "per-document steps: prep, check, render, regparse")
    p = g.add_parser("regparse", help="deterministic register/field records from a vendor register-map template")
    p.add_argument("doc_id")
    p.add_argument("--template", required=True, choices=sorted(TEMPLATES))
    p.set_defaults(func=cmd_regparse)

"""`doc regparse <doc_id> --template ti|maxim`: deterministic register records from a vendor template.

One parser per vendor template; the LLM's register records (if any) are then its independent twin,
compared by `doc check` (G). A template also audits the document: where its own bit diagram, field
table, heading and summary table disagree, that is written as a `conflict` record.

Output: `records/regparse.jsonl` (register, field and conflict records, prov deterministic). A re-run
replaces the file; no other record file is touched. Ids follow the LLM records' scheme so the two twin:
`<part>.reg.<NAME>` and `<part>.reg.<NAME>.<FIELD>`. `name` keeps the printed name; a register id drops
every character outside [A-Za-z0-9_] ("Floor Guard" -> FloorGuard, "ADCVIN/VOUT" -> ADCVINVOUT), a field
id turns each run of them into `_` ("Fault OVP_IN" -> Fault_OVP_IN).

Templates
- `ti` reads the text layer (pages/p<NNN>.txt). Headings `<sec> NAME Register (Address = 0h)`,
  `<sec> Long Name (ACRONYM) Register (...)`, `(Offset = ...)`, `(I2C address = 01/00h)`, with or without
  `[reset = ...]`; per-register field tables (`Bit Field Type Reset Description`), also one table per
  bus address; bit diagrams with `R/W-0101b` tokens; summary tables (`00h NAME R/W 11010010`,
  `0h ACRONYM Long Name 16 Go`, `01/00h Name() R/W desc`). A register printed with several addresses is
  split into bus bytes, NAME_LSB / NAME_MSB. In a slash pair the byte order is taken from the document:
  a per-address table numbering its bits 15-8, or the summary table's `(MSB/LSB)` header.
- `maxim` reads the source PDF with PyMuPDF (two-column pages defeat a text-layer parser). Headings
  `Name Register (NNNh)` with their `Register Type:`, `Nonvolatile Backup/Restore:` and
  `[Alternate] Initial Value:` lines; grid memory maps (row `Nh` x column `0Mxh`); factory-default
  tables (`180h nName 0x0000`); D15..D0 bit diagrams, read from word x-positions under the header row
  and the cell boundaries PyMuPDF `page.get_drawings()` returns. Registers are 16-bit words. The
  document prints no per-field reset, so field resets are derived from the register reset and say so.

A template that finds no register makes the verb exit 1 with the patterns it tried and the lines that
almost matched, so a template mismatch never looks like success.

Adding a template: subclass `Template`, implement `parse(pages, pdf) -> Parse`, `patterns()` and
`near_misses()`, add it to TEMPLATES.
"""
from __future__ import annotations

import re
import statistics
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
    access: str | None
    reset: int | None
    reset_width: int | None
    desc: str
    p: int | list[int]
    q: str | list[str] | None
    note: str | None = None
    byte_addr: int | None = None   # TI: the bus address of the per-address field table the row sits in
    reset_text: str = ""           # TI: the reset cell as printed, continuation lines included
    fixed: int | None = None       # Maxim: the value a run of "0"/"1" cells fixes
    derived: bool = False          # the reset was derived from the register reset


@dataclass
class RawReg:
    name: str                      # printed name, verbatim
    addrs: list[int]               # bus addresses; more than one: split into bytes, LSB first
    resets: list[str]              # heading reset tokens as printed (TI)
    p: int | list[int]
    q: str | list[str]
    fields: list[RawField] = dfield(default_factory=list)
    fig_tokens: list[tuple[str, str]] = dfield(default_factory=list)   # (access, reset), bit order hi -> lo
    figure: str | None = None
    table: str | None = None
    id_name: str | None = None     # the name the id is built from, when it is not `name`
    width: int | None = None       # a single-address register's width
    reset: int | None = None       # full reset (all bytes), when known
    byte_resets: list[int | None] | None = None   # split registers: per byte, LSB first
    notes: list[str] = dfield(default_factory=list)
    extra: dict = dfield(default_factory=dict)     # reg_type; nv_backup / mirror_of (RawReg)
    addr_digits: int = 2
    # TI parse state
    title: tuple[str, str | None] = ("", None)
    form: str = "single"           # single | list (comma, LSB first) | pair (slash, printed order)
    spec: dict = dfield(default_factory=dict)
    fig_spec: dict | None = None
    fig_top: int = 7
    fig_seq: list[tuple[int, int, str, str]] = dfield(default_factory=list)


@dataclass
class Issue:
    """An inconsistency inside the document, found while parsing; refs resolve to record ids later."""
    key: str
    reg: RawReg | None
    fld: RawField | None
    sources: list[str]
    text: str
    p: int
    q: str | None
    addr: int | None = None        # Maxim: the register the issue is about, by address, until it exists


@dataclass
class Parse:
    registers: list[RawReg]
    summary: dict[int, dict]          # address -> {name, access, reset (int), size, p, q}
    notes: list[str] = dfield(default_factory=list)
    issues: list[Issue] = dfield(default_factory=list)


class Template:
    name = "?"
    needs_pdf = False

    def parse(self, pages: dict[int, str], pdf: Path | None = None) -> Parse:  # pragma: no cover - interface
        raise NotImplementedError

    def patterns(self) -> list[tuple[str, str]]:  # pragma: no cover - interface
        return []

    def near_misses(self, pages: dict[int, str], pdf: Path | None = None, limit: int = 8) -> list[tuple[int, str]]:
        return []


# ---------------------------------------------------------------- shared helpers

ACCESS = {"R/W": "rw", "RW": "rw", "R": "ro", "RO": "ro", "W": "wo", "WO": "wo", "RC": "rclr", "R/WC": "woclr",
          "R/W1C": "woclr", "W1C": "woclr", "R/W1S": "w1s", "W1S": "w1s"}
_ACC = r"R/W1C|R/W1S|R/WC|R/W|RC|W1C|W1S|RW|RO|WO|R|W"
RESET_TOK = re.compile(r"(?<![\w.])(?:[01](?:[01 ]*[01])?b|[0-9A-Fa-f]+h|[01]+)(?![\w])")
PERBIT = re.compile(r"^(?P<base>.+?),\s*bit\s*(?P<k>\d+)$", re.I)


def _ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _reg_slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "", name) or "REG"


def _field_slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_") or "FIELD"


def _upper_snake(name: str) -> str:
    return _field_slug(name).upper()


def _is_reserved(name: str) -> bool:
    return bool(re.fullmatch(r"reserved?(?:[\s_]*\d+(?:[\s_:-]+\d+)?)?", name.strip(), re.I))


def _bits_value(tok: str, width: int | None = None) -> tuple[int | None, int | None]:
    """A TI reset token -> (value, printed width). `0101b` binary, `E4h` hex, `x`/`-` undefined."""
    t = tok.replace(" ", "")
    if not t or set(t.lower()) <= {"x", "-"}:
        return None, None
    if t.lower().endswith("b") and set(t[:-1]) <= {"0", "1"} and t[:-1]:
        return int(t[:-1], 2), len(t[:-1])
    if t.lower().endswith("h"):
        body = t[:-1]
        return (int(body, 16), None) if re.fullmatch(r"[0-9A-Fa-f]+", body) else (None, None)
    if set(t) <= {"0", "1"}:
        return int(t, 2), len(t)
    if t.isdigit():
        return int(t), None
    return None, None


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


def _reset_spec(text: str | None) -> dict:
    """The bracket of a TI heading or figure caption: `reset = 8203h`, `reset = 11h, 00h` (one per address),
    `reset = 0058h(variant A)/0000h(variant B)` (variants), `reset value based on ...` (text), or none."""
    if text is None:
        return {"kind": "none"}
    t = _ws(text)
    m = re.match(r"reset\s*=\s*(.*)$", t, re.I)
    if not m:
        return {"kind": "text", "text": t}
    body = m.group(1).strip()
    var = re.findall(r"([0-9A-Fa-f]+)h?\s*\(([^)]*)\)", body)
    if len(var) >= 2:
        return {"kind": "variants", "values": [(v + "h", c) for v, c in var], "text": body}
    if "," in body:
        toks = [x.strip() for x in body.split(",") if x.strip()]
        if all(re.fullmatch(r"[0-9A-Fa-f]+[hb]?", x) for x in toks):
            return {"kind": "list", "values": toks, "text": body}
    if "/" in body:
        toks = [x.strip() for x in body.split("/") if x.strip()]
        if len(toks) >= 2 and all(re.fullmatch(r"[0-9A-Fa-f]+h?", x) for x in toks):
            return {"kind": "variants", "values": [(x, "") for x in toks], "text": body}
    if re.fullmatch(r"[0-9A-Fa-f]+[hb]?", body):
        return {"kind": "value", "value": body, "text": body}
    return {"kind": "text", "text": "reset = " + body}


def _spec_values(spec: dict, nbytes: int) -> list[int] | None:
    """Every value a reset spec allows (one for a plain value), or None when it gives no number."""
    k = spec.get("kind")
    if k == "value":
        v, _ = _heading_reset(spec["value"], nbytes)
        return None if v is None else [v]
    if k == "variants":
        vals = [_heading_reset(v, nbytes)[0] for v, _ in spec["values"]]
        return None if any(v is None for v in vals) else vals
    if k == "list":
        vals = [_heading_reset(v, 1)[0] for v in spec["values"]]
        return None if any(v is None for v in vals) else [sum(v << (8 * i) for i, v in enumerate(vals))]
    return None


def _field_reset(text: str) -> tuple[int | None, int | None, str | None]:
    """A field-table reset cell, continuation lines included -> (value, printed width, note). Two different
    values in one cell (`010110b(variant A) 000000b(variant B)`) are variants: no single reset."""
    t = _ws(text)
    if not t:
        return None, None, None
    parsed = [_bits_value(m.group(0)) for m in RESET_TOK.finditer(t)]
    distinct = {v for v, _ in parsed if v is not None}
    if len(distinct) > 1:
        return None, None, f"reset varies: {t}"
    for v, w in parsed:
        if v is not None:
            return v, w, None
    return None, None, None


def _reset_str(v: int | None, width: int) -> str | None:
    if v is None:
        return None
    if width <= 8:
        return "0b" + format(v, f"0{width}b")
    return "0x" + format(v, f"0{(width + 3) // 4}X")


def _bin_str(v: int | None, width: int) -> str | None:
    return None if v is None else "0b" + format(v, f"0{width}b")


def _runs(pages: list[int]) -> str:
    out, i = [], 0
    ps = sorted(set(pages))
    while i < len(ps):
        j = i
        while j + 1 < len(ps) and ps[j + 1] == ps[j] + 1:
            j += 1
        out.append(f"{ps[i]}" if i == j else f"{ps[i]}-{ps[j]}")
        i = j + 1
    return ",".join(out)


# ---------------------------------------------------------------- TI

class TI(Template):
    """TI's register-description template, in the variants current datasheets print (see the module doc).
    A heading reset that is all 0/1 and 8 digits per byte is binary printed with an `h` suffix, a known TI
    artefact, and is read as binary."""
    name = "ti"
    HEAD = re.compile(r"^\s*(?P<sec>\d+(?:\.\d+)+)\s+(?P<title>\S.*?)\s*Register\s*"
                      r"\(\s*(?:(?:I2C|I3C|SPI|SMBus|PMBus)\s+)?(?:Address|Offset)\s*=\s*(?P<addr>[^)]*)\)"
                      r"\s*(?:\[(?P<rb>[^\]]*)\]?)?", re.I)
    BITS = re.compile(r"^\s*(\d{1,2})(?:\s*[-:]\s*(\d{1,2}))?(?=\s)")
    FIGTOK = re.compile(rf"(?<![\w/])({_ACC})-([01xX]+b|[0-9A-Fa-f]+h)(?![\w])")
    FIGHDR = re.compile(r"^\s*\d{1,2}(?:\s+\d{1,2}){1,15}\s*$")
    TABLE_HDR = re.compile(r"^\s*Bits?\s+Field\s+Type\s+Reset\s+(?:\w+\s+)*?Description", re.I)
    SUM_TI8 = re.compile(rf"^\s*([0-9A-Fa-f]{{1,4}})h\s+([A-Za-z_][A-Za-z0-9_]*)\s+({_ACC})\s+([01]{{8,}}|[0-9A-Fa-f]+h)\s*$")
    SUM_ACR = re.compile(r"^\s*(?P<addr>[0-9A-Fa-f]{1,4})h\s+(?P<acr>[A-Za-z_][A-Za-z0-9_]*)\s+(?P<long>\S.*?)"
                         r"(?:\s+(?P<size>\d{1,3}))?\s+Go\s*$")
    SUM_CMD = re.compile(rf"^\s*(?P<addr>[0-9A-Fa-f]{{1,4}}(?:/[0-9A-Fa-f]{{1,4}})?)h\s+(?P<name>\S.*?)\(\)\s+"
                         rf"(?P<acc>R with R/W|R and R/W|{_ACC})(?=\s|$)")
    ORDER = re.compile(r"\b(MSB|LSB)\s*/\s*(MSB|LSB)\b")
    OTHER_HEAD = re.compile(r"^\s?\d{1,2}(?:\.\d{1,2})+\s+[A-Z][A-Za-z]")
    CAPTION = re.compile(r"^\s*(Figure|Table)\s+(\d+(?:-\d+)?)\.\s+(.*?)\s*$")
    CAP_ADDR = re.compile(r"\(\s*(?:(?:I2C|I3C|SPI)\s+)?(?:address|offset)\s*=\s*([0-9A-Fa-f]{1,4})h\s*\)", re.I)
    LEADER = re.compile(r"\.{4,}|(?:\.\s){4,}")
    RCLR = re.compile(r"does not reset until (?:it|the register) is read|clear(?:ed|s)?\s+(?:on|after|when)\s+"
                      r"(?:it is\s+|the register is\s+)?read|cleared by reading (?:this|the) register|"
                      r"latched\b[^.]*?\buntil (?:a |the )?read", re.I)

    def patterns(self) -> list[tuple[str, str]]:
        return [("register heading", "<sec> <NAME | Long Name (ACRONYM)> Register ([I2C ]Address|Offset = <addr>[, <addr> | /<addr>]) [[reset = <value>]]"),
                ("field table header", "Bit Field Type Reset Description"),
                ("field row", "<hi>[-<lo>] <FIELD> <R|W|R/W|...> <reset> <description>"),
                ("summary row", "00h NAME R/W 11010010 | 0h ACRONYM Long Name 16 Go | 01/00h Name() R/W desc")]

    def near_misses(self, pages, pdf=None, limit=8):
        ranked: list[tuple[int, int, str]] = []
        for pno in sorted(pages):
            for ln in pages[pno].splitlines():
                t = _ws(ln)
                if not t or self.HEAD.match(ln) or self.LEADER.search(ln):
                    continue
                if re.search(r"\bRegisters?\s*\(", t):
                    ranked.append((0, pno, t[:160]))                 # a heading in another shape
                elif re.match(r"\d+(?:\.\d+)+\s+\S.*\bRegister", t):
                    ranked.append((1, pno, t[:160]))
                elif re.match(r"^[0-9A-Fa-f]{1,4}(?:/[0-9A-Fa-f]{1,4})?h\s+[A-Za-z_]", t) and not (
                        self.SUM_TI8.match(ln) or self.SUM_ACR.match(ln) or self.SUM_CMD.match(ln)):
                    ranked.append((2, pno, t[:160]))                 # a summary row in another shape
                elif re.match(r"^\s*(?:Bits?\s+Field|\d{1,2}(?:-\d{1,2})?\s+\S+\s+(?:R/W|R|W)\s)", t, re.I):
                    ranked.append((3, pno, t[:160]))                 # a field table without a heading
        return [(p, t) for _, p, t in sorted(ranked, key=lambda x: (x[0], x[1]))[:limit]]

    @staticmethod
    def _addrs(s: str) -> list[int]:
        out = []
        for tok in re.split(r"[,/]", s):
            t = tok.strip()
            if not t:
                continue
            if t.lower().startswith("0x"):
                out.append(int(t, 16))
            elif t.lower().endswith("h") and re.fullmatch(r"[0-9A-Fa-f]+", t[:-1]):
                out.append(int(t[:-1], 16))
            elif re.fullmatch(r"[0-9A-Fa-f]+", t):
                out.append(int(t, 16))
        return out

    @staticmethod
    def _cols(ln: str) -> tuple[int, int, int, int]:
        low = ln.lower()
        f = low.find("field")
        return f, low.find("type", f), low.find("reset", f), low.find("description", f)

    # -------------------------------------------------- one line

    def _summary(self, ln: str, pno: int, summary: dict) -> bool:
        q = _ws(ln)
        m = self.SUM_CMD.match(ln)
        if m:
            addrs = self._addrs(m.group("addr") + "h")
            acc = ACCESS.get(m.group("acc"))
            for a in addrs:
                summary[a] = {"name": _ws(m.group("name")), "access": acc, "reset": None, "size": None,
                              "pair": addrs if len(addrs) > 1 else None, "p": pno, "q": q}
            return True
        m = self.SUM_TI8.match(ln)
        if m:
            raw = m.group(4)
            v, _ = _bits_value(raw + "b" if re.fullmatch(r"[01]{8,}", raw) else raw)
            summary[int(m.group(1), 16)] = {"name": m.group(2), "access": ACCESS[m.group(3)], "reset": v,
                                            "size": None, "p": pno, "q": q}
            return True
        m = self.SUM_ACR.match(ln)
        if m:
            summary[int(m.group("addr"), 16)] = {"name": m.group("acr"), "long": _ws(m.group("long")), "access": None,
                                                 "reset": None, "size": int(m.group("size")) if m.group("size") else None,
                                                 "p": pno, "q": q}
            return True
        return False

    def _new_reg(self, m, pno: int, ln: str) -> RawReg:
        title = _ws(m.group("title"))
        t = re.fullmatch(r"(?P<long>.*?)\s*\((?P<acr>[^()]+)\)", title)
        long_, acr = (t.group("long").strip(), t.group("acr").strip()) if t and t.group("long") else (title, None)
        a = m.group("addr")
        form = "pair" if "/" in a else ("list" if "," in a else "single")
        spec = _reset_spec(m.group("rb"))
        resets = [x for x in spec.get("values", [])] if spec["kind"] == "list" else (
            [spec["value"]] if spec["kind"] == "value" else [])
        return RawReg(name=acr or long_, addrs=self._addrs(a), resets=resets, p=pno, q=_ws(ln), title=(long_, acr),
                      form=form, spec=spec)

    def _row(self, ln: str, cols) -> tuple | None:
        m = self.BITS.match(ln)
        if not m:
            return None
        hi = int(m.group(1))
        lo = int(m.group(2)) if m.group(2) else hi
        toks = [(t.start() + m.end(), t.group()) for t in re.finditer(r"\S+", ln[m.end():])]
        acc_i = None
        for i, (c, t) in enumerate(toks):
            if t not in ACCESS:
                continue
            if cols and cols[1] > 0 and (c < (cols[0] + cols[1]) / 2 - 1 or (cols[2] > 0 and c > cols[2] + 1)):
                continue
            acc_i = i
            break
        if acc_i is None:
            return None
        name = " ".join(t for _, t in toks[:acc_i])
        rest = toks[acc_i + 1:]
        if cols and cols[3] > 0:
            rtoks = [(c, t) for c, t in rest if c < cols[3] - 2]
            dtoks = [(c, t) for c, t in rest if c >= cols[3] - 2]
        elif rest and (RESET_TOK.fullmatch(rest[0][1]) or set(rest[0][1].lower()) <= {"x", "-"}):
            rtoks, dtoks = rest[:1], rest[1:]
        else:
            rtoks, dtoks = [], rest
        desc = ln[dtoks[0][0]:].strip() if dtoks else ""
        return max(hi, lo), min(hi, lo), name, ACCESS[toks[acc_i][1]], " ".join(t for _, t in rtoks), desc

    @staticmethod
    def _join_name(name: str, frag: str) -> str:
        if not name:
            return frag
        if name.endswith("_") or frag.startswith("_") or (
                re.fullmatch(r"[A-Z0-9_]+", frag) and re.fullmatch(r"[A-Z0-9_]+", name.split()[-1])):
            return name + frag
        return name + " " + frag

    def _continue(self, ln: str, cols, last: RawField, desc_col: int | None) -> bool:
        toks = [(t.start(), t.group()) for t in re.finditer(r"\S+", ln)]
        if not toks:
            return True
        if not cols or cols[3] <= 0:
            if desc_col is not None and toks[0][0] >= desc_col - 2:
                last.desc = (last.desc + " " + ln.strip()).strip()
                return True
            return False
        fcol, tcol, rcol, dcol = cols
        if toks[0][0] < fcol - 2:
            return False
        name_t = [t for c, t in toks if c < tcol - 2]
        reset_t = [t for c, t in toks if rcol - 2 <= c < dcol - 2]
        desc_t = [c for c, t in toks if c >= dcol - 2]
        if name_t:
            last.name = self._join_name(last.name, " ".join(name_t))
        if reset_t:
            last.reset_text = (last.reset_text + " " + " ".join(reset_t)).strip()
        if desc_t:
            last.desc = (last.desc + " " + ln[desc_t[0]:].strip()).strip()
        return True

    # -------------------------------------------------- the document

    def parse(self, pages: dict[int, str], pdf: Path | None = None) -> Parse:
        regs: list[RawReg] = []
        summary: dict[int, dict] = {}
        order_hint: tuple[str, int] | None = None
        cur: RawReg | None = None
        last: RawField | None = None
        cols = None
        tctx: dict = {}
        for pno in sorted(pages):
            for ln in pages[pno].splitlines():
                m = self.HEAD.match(ln)
                if m and self.LEADER.search(ln):
                    continue                           # a table-of-contents entry
                if m:
                    cur = self._new_reg(m, pno, ln)
                    regs.append(cur)
                    last, cols, tctx = None, None, {}
                    continue
                if self._summary(ln, pno, summary):
                    last = None
                    continue
                o = self.ORDER.search(ln)
                if o and order_hint is None and o.group(1) != o.group(2):
                    order_hint = ("msb_first" if o.group(1) == "MSB" else "lsb_first", pno)
                if cur is None:
                    continue
                if self.OTHER_HEAD.match(ln):
                    cur, last, cols, tctx = None, None, None, {}
                    continue
                c = self.CAPTION.match(ln)
                if c:
                    if c.group(1) == "Figure":
                        cur.figure = cur.figure or f"Figure {c.group(2)}"
                        rb = re.search(r"\[([^\]]*)\]?\s*$", c.group(3))
                        if rb and cur.fig_spec is None:
                            cur.fig_spec = dict(_reset_spec(rb.group(1)), caption=f"Figure {c.group(2)}", p=pno, q=_ws(ln))
                    else:
                        cur.table = cur.table or f"Table {c.group(2)}"
                        a = self.CAP_ADDR.search(c.group(3))
                        nm = re.match(r"(.*?)\s+Register\b", c.group(3))
                        tctx = {"addr": int(a.group(1), 16) if a else None, "table": f"Table {c.group(2)}",
                                "name": _ws(nm.group(1)) if nm else None}
                    last = None
                    continue
                if self.TABLE_HDR.match(ln):
                    cols = self._cols(ln)
                    last = None
                    continue
                if cur.table is None:
                    if self.FIGHDR.match(ln):
                        cur.fig_top = int(ln.split()[0])
                    for acc, rst in self.FIGTOK.findall(ln):
                        cur.fig_seq.append((-cur.fig_top, len(cur.fig_seq), acc, rst))
                    continue
                r = self._row(ln, cols)
                if r:
                    hi, lo, name, acc, rtext, desc = r
                    note = None
                    if not name:
                        name = tctx.get("name") or "FIELD"
                        note = f"field unnamed in the table; named after {tctx.get('table') or 'the register'}"
                    last = RawField(name=name, hi=hi, lo=lo, access=acc, reset=None, reset_width=None, desc=desc,
                                    p=pno, q=_ws(ln), note=note, byte_addr=tctx.get("addr"), reset_text=rtext)
                    cur.fields.append(last)
                    continue
                if last is not None and ln.strip():
                    if not self._continue(ln, cols, last, cols[3] if cols else None):
                        last = None
        notes: list[str] = []
        issues: list[Issue] = []
        regs = self._dedupe(regs)
        self._finish(regs, summary, order_hint, notes, issues)
        return Parse(registers=regs, summary=summary, notes=notes, issues=issues)

    # -------------------------------------------------- after the last page

    @staticmethod
    def _dedupe(regs: list[RawReg]) -> list[RawReg]:
        """A heading repeated without a field table (a table of contents, a cross-reference) is dropped when
        the same register has a section with fields."""
        with_fields = {tuple(r.addrs) for r in regs if r.fields}
        return [r for r in regs if r.fields or tuple(r.addrs) not in with_fields]

    def _finish(self, regs, summary, order_hint, notes, issues) -> None:
        for rr in regs:
            self._name(rr, summary)
            for f in rr.fields:
                f.name = re.sub(r"\s*_\s*", "_", _ws(f.name))
                f.reset, f.reset_width, vnote = _field_reset(f.reset_text)
                if vnote:
                    f.note = "; ".join(x for x in (f.note, vnote) if x)
        own = {id(rr): self._own_order(rr) for rr in regs if rr.form == "pair" and len(rr.addrs) == 2}
        seen = {o for o, _ in own.values() if o}
        doc = None
        if order_hint:
            doc = (order_hint[0], f"the summary table header p{order_hint[1]}")
        elif len(seen) == 1:
            which = [f"{rr.name} p{rr.p}" for rr in regs if own.get(id(rr), (None,))[0]]
            doc = (seen.pop(), "bit numbering 15-8 in the per-address field table of " + ", ".join(which))
        told = False
        for rr in regs:
            if rr.form == "list":
                pass                                   # comma form: the addresses are printed LSB first
            elif rr.form == "pair" and len(rr.addrs) == 2:
                o, why = own.get(id(rr), (None, None))
                if o and doc and o != doc[0] and order_hint:
                    issues.append(Issue(f"{_reg_slug(rr.name)}.byte_order", rr, None,
                                        [f"field table p{rr.p}", doc[1]],
                                        f"{rr.name}: {why}, but {doc[1]} gives the other byte order", rr.p, rr.q))
                use = o or (doc[0] if doc else "msb_first")
                if not o and not doc:
                    rr.notes.append("byte order not stated in the document; assumed the first address printed is the MSB")
                elif not o and not told:
                    notes.append(f"byte order of printed address pairs: {'MSB/LSB' if doc[0] == 'msb_first' else 'LSB/MSB'} "
                                 f"({doc[1]})")
                    told = True
                if use == "msb_first":
                    rr.addrs = [rr.addrs[1], rr.addrs[0]]
            self._place_fields(rr, issues)
            self._merge_per_bit(rr)
            self._resets(rr, summary, issues)
            rr.fig_tokens = [(acc, rst) for _, _, acc, rst in sorted(rr.fig_seq)]

    @staticmethod
    def _name(rr: RawReg, summary: dict) -> None:
        """`Long Name (ACRONYM)`: the one the summary table names, else the acronym. The id is the slug."""
        long_, acr = rr.title
        cands = [c for c in (acr, long_) if c]
        sm = next((summary[a] for a in rr.addrs if a in summary), None)
        pick = None
        if sm:
            key = _reg_slug(sm["name"]).lower()
            pick = next((c for c in cands if _reg_slug(c).lower() == key), None)
            if pick is None and sm.get("long"):
                pick = next((c for c in cands if _reg_slug(c).lower() == _reg_slug(sm["long"]).lower()), None)
        rr.name = pick or acr or long_
        if acr and rr.name != acr:
            rr.notes.append(f"the heading prints {long_} ({acr}); the summary table names it {sm['name']}")

    @staticmethod
    def _own_order(rr: RawReg) -> tuple[str | None, str | None]:
        """From a per-address table that numbers its bits 15-8: which printed address is the MSB."""
        first, second = rr.addrs
        for f in rr.fields:
            if f.byte_addr is not None and f.hi >= 8:
                k = f.hi // 8
                if f.byte_addr == first:
                    return ("msb_first" if k == 1 else "lsb_first"), f"the table for {first:02X}h numbers its bits {f.hi}-{f.lo}"
                if f.byte_addr == second:
                    return ("lsb_first" if k == 1 else "msb_first"), f"the table for {second:02X}h numbers its bits {f.hi}-{f.lo}"
        return None, None

    @staticmethod
    def _place_fields(rr: RawReg, issues: list) -> None:
        """Rows of a per-address table that restart at bit 7 move to their byte: bits 15..8 for the MSB."""
        if len(rr.addrs) < 2:
            return
        for f in rr.fields:
            if f.byte_addr is None or f.byte_addr not in rr.addrs:
                continue
            k = rr.addrs.index(f.byte_addr)
            if f.hi <= 7:
                f.hi, f.lo = f.hi + 8 * k, f.lo + 8 * k
            elif f.hi // 8 != k:
                issues.append(Issue(f"{_reg_slug(rr.name)}.{_field_slug(f.name)}.bits", rr, f,
                                    [f"field table p{f.p}", f"heading p{rr.p}"],
                                    f"{rr.name} {f.name}: the table for {f.byte_addr:02X}h numbers it {f.hi}-{f.lo}, "
                                    f"but {f.byte_addr:02X}h is byte {k}", f.p, f.q if isinstance(f.q, str) else None))
                f.hi, f.lo = 8 * k + f.hi % 8, 8 * k + f.lo % 8

    @staticmethod
    def _merge_per_bit(rr: RawReg) -> None:
        """Rows `<Value>, bit 6` .. `<Value>, bit 0` on consecutive bits are one value field."""
        fs = sorted(rr.fields, key=lambda f: -f.hi)
        out: list[RawField] = []
        i = 0
        while i < len(fs):
            m = PERBIT.match(fs[i].name) if fs[i].hi == fs[i].lo else None
            j = i
            if m:
                while j + 1 < len(fs):
                    n = PERBIT.match(fs[j + 1].name) if fs[j + 1].hi == fs[j + 1].lo else None
                    if not n or n.group("base") != m.group("base") or fs[j + 1].access != fs[i].access \
                            or fs[j + 1].hi != fs[j].lo - 1 or int(n.group("k")) != int(PERBIT.match(fs[j].name).group("k")) - 1:
                        break
                    j += 1
            if j == i:
                out.append(fs[i])
                i += 1
                continue
            run = fs[i:j + 1]
            base, k_hi, k_lo = m.group("base"), int(m.group("k")), int(PERBIT.match(run[-1].name).group("k"))
            reset = None
            if all(f.reset is not None for f in run):
                reset = 0
                for f in run:
                    reset = (reset << 1) | (f.reset & 1)
            pages = sorted({f.p for f in run if isinstance(f.p, int)})
            vnotes = [f.note for f in run if f.note]
            out.append(RawField(name=_upper_snake(base), hi=run[0].hi, lo=run[-1].lo, access=run[0].access, reset=reset,
                                reset_width=len(run), desc=f"{base}, value bits {k_hi}:{k_lo}",
                                p=pages[0] if len(pages) == 1 else pages, q=[run[0].q, run[-1].q],
                                note="; ".join([f"merged from the per-bit rows '{run[0].name}' .. '{run[-1].name}'"] + vnotes[:1]),
                                byte_addr=None))
            i = j + 1
        rr.fields = out

    def _resets(self, rr: RawReg, summary: dict, issues: list) -> None:
        split = len(rr.addrs) > 1
        top = max([f.hi for f in rr.fields] + [8 * max(1, len(rr.addrs)) - 1])
        if not split:
            sm = summary.get(rr.addrs[0]) if rr.addrs else None
            rr.width = (sm or {}).get("size") or max(8, (top // 8 + 1) * 8)
        nbytes = max(len(rr.addrs), (top + 8) // 8) if split else rr.width // 8
        spec = rr.spec
        full, note = None, None
        byte_vals: list[int | None] | None = None
        if spec["kind"] == "value":
            full, note = _heading_reset(spec["value"], nbytes)
        elif spec["kind"] == "list":
            vals = [_heading_reset(v, 1) for v in spec["values"]]
            note = next((n for _, n in vals if n), None)
            byte_vals = [v for v, _ in vals] + [None] * max(0, nbytes - len(vals))
            if all(v is not None for v in byte_vals):
                full = sum(v << (8 * k) for k, v in enumerate(byte_vals))
        elif spec["kind"] == "variants":
            vals = _spec_values(spec, nbytes)
            conds = ", ".join(f"{v} ({c})" if c else v for v, c in spec["values"])
            note = f"reset varies: {conds}"
            if vals and split:
                byte_vals = [vals[0] >> (8 * k) & 0xFF if all((v >> (8 * k)) & 0xFF == (vals[0] >> (8 * k)) & 0xFF
                                                              for v in vals) else None for k in range(nbytes)]
        elif spec["kind"] == "text":
            note = spec["text"]
        rr.reset = full
        if split:
            rr.byte_resets = byte_vals if byte_vals is not None else [
                None if full is None else (full >> (8 * k)) & 0xFF for k in range(nbytes)]
        if note:
            rr.notes.append(note)
        fs = rr.fig_spec
        if fs and spec["kind"] in ("value", "list", "variants") and fs["kind"] in ("value", "variants", "list"):
            a, b = _spec_values(spec, nbytes), _spec_values(fs, nbytes)
            if a is not None and b is not None and sorted(a) != sorted(b):
                issues.append(Issue(f"{_reg_slug(rr.name)}.caption_reset", rr, None,
                                    [f"heading p{rr.p}", f"{fs['caption']} caption p{fs['p']}"],
                                    f"{rr.name}: reset {spec.get('text') or spec.get('value')} in the heading, "
                                    f"{fs.get('text') or fs.get('value')} in the {fs['caption']} caption", fs["p"], fs["q"]))


# ---------------------------------------------------------------- Maxim

@dataclass
class _Line:
    p: int
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    bold: bool
    size: float


@dataclass
class _Diagram:
    p: int
    nbits: int
    cells: list[tuple[int, int, str]]      # (hi, lo, text), hi -> lo
    caption: str | None
    pairs: list[tuple[str, int]]


def _page_lines(page, pno: int) -> list[_Line]:
    out = []
    for b in page.get_text("dict")["blocks"]:
        for ln in b.get("lines", []):
            spans = [s for s in ln["spans"] if s["text"].strip()]
            if not spans:
                continue
            text = "".join(s["text"] for s in ln["spans"]).strip()
            bold = all("bold" in s["font"].lower() or s["flags"] & 16 for s in spans)
            out.append(_Line(pno, *ln["bbox"], text, bold, max(s["size"] for s in spans)))
    return out


def _segments(page) -> tuple[list[tuple[float, float, float]], list[tuple[float, float, float]]]:
    """Horizontal (x0, x1, y) and vertical (x, y0, y1) strokes: lines, and the edges of rectangles."""
    hs, vs = [], []
    for d in page.get_drawings():
        for it in d["items"]:
            if it[0] == "l":
                a, b = it[1], it[2]
                if abs(a.y - b.y) < 0.8:
                    hs.append((min(a.x, b.x), max(a.x, b.x), (a.y + b.y) / 2))
                elif abs(a.x - b.x) < 0.8:
                    vs.append(((a.x + b.x) / 2, min(a.y, b.y), max(a.y, b.y)))
            elif it[0] == "re":
                r = it[1]
                hs += [(r.x0, r.x1, r.y0), (r.x0, r.x1, r.y1)]
                vs += [(r.x0, r.y0, r.y1), (r.x1, r.y0, r.y1)]
    return hs, vs


def _merge_h(hs: list[tuple[float, float, float]]) -> list[tuple[float, float, float]]:
    """Collinear horizontal strokes joined: a table rule is often drawn cell by cell."""
    out: list[list[float]] = []
    for x0, x1, y in sorted(hs, key=lambda s: (round(s[2] * 2), s[0])):
        if out and abs(out[-1][2] - y) < 0.6 and x0 <= out[-1][1] + 2:
            out[-1][1] = max(out[-1][1], x1)
        else:
            out.append([x0, x1, y])
    return [tuple(s) for s in out]


def _cluster(xs: list[float], tol: float) -> list[float]:
    out: list[list[float]] = []
    for x in sorted(xs):
        if out and x - out[-1][-1] <= tol:
            out[-1].append(x)
        else:
            out.append([x])
    return [sum(c) / len(c) for c in out]


def _group_lines(words: list) -> list[list]:
    rows: list[list] = []
    for w in sorted(words, key=lambda w: (w[1], w[0])):
        if rows and abs(rows[-1][0][1] - w[1]) < 2.5:
            rows[-1].append(w)
        else:
            rows.append([w])
    return [sorted(r, key=lambda w: w[0]) for r in rows]


def _join_wrapped(parts: list[str], space: bool) -> str:
    out = ""
    for s in parts:
        if not out:
            out = s
        elif out.endswith(("-", "\xad")):
            out = out[:-1] + s
        else:
            out = out + (" " if space else "") + s
    return out.replace("\xad", "")


class Maxim(Template):
    """Maxim/ADI register descriptions, read from the PDF (see the module doc). Register names come from
    the register's own heading, else a figure caption, else the memory map, else a factory-default table;
    a name the memory map places at another address than its heading is a conflict, and the memory map's
    address wins."""
    name = "maxim"
    needs_pdf = True
    PAIR = re.compile(r"([A-Za-z][A-Za-z0-9_]*)\s*(?:Register\s*)?\(([0-9A-Fa-f]{2,3})h\)")
    LBL_TYPE = re.compile(r"^Register\s+type\s*:\s*(?P<v>.*)$", re.I)
    LBL_NV = re.compile(r"^Nonvolatile\s+(?P<k>Backup\s+and\s+Restore(?:\s+From)?|Backup|Restore)\b[^:]*:\s*(?P<v>.*)$", re.I)
    LBL_INIT = re.compile(r"^(?P<k>Alternate\s+)?Initial\s+Value\s*:\s*(?P<v>.*)$", re.I)
    BITHDR = re.compile(r"^D(1[0-5]|[0-9])$")
    GRID_COL = re.compile(r"^([0-9A-Fa-f]{2,3})xh\**$")
    GRID_ROW = re.compile(r"^([0-9A-Fa-f])h$")
    DEF_ROW = re.compile(r"^\s*([0-9A-Fa-f]{3,4})h\s+([A-Za-z][A-Za-z0-9_]*)\s+(0x[0-9A-Fa-f]{1,4}|Varies)(?=\s|$)")
    INIT_EQ = re.compile(r"([A-Za-z][A-Za-z0-9_]*)\s*=\s*(0x[0-9A-Fa-f]+)")
    INIT_FOR = re.compile(r"(0x[0-9A-Fa-f]+)\s+for\s+([A-Za-z][A-Za-z0-9_]*)")
    INIT_DEF = re.compile(r"([A-Za-z][A-Za-z0-9_]*)\s+defaults\s+(?:to\s+)?(0x[0-9A-Fa-f]+)")
    LEADER = re.compile(r"\.{4,}|(?:\.\s){4,}")
    FUNC_END = {"if", "and", "or", "to", "from", "of", "the", "a", "an", "for", "with", "is", "be", "="}

    def patterns(self) -> list[tuple[str, str]]:
        return [("register heading (bold, >= 10 pt)", "<Name> Register (<NNN>h) | <Name> (<NNN>h)/<Name> (<NNN>h) Registers"),
                ("section lines", "Register Type: ... | Nonvolatile Backup|Restore: ... | [Alternate] Initial Value: ..."),
                ("memory map", "WORD <00x>h <01x>h ... rows <N>h"),
                ("bit diagram", "a D15 .. D0 header row with drawn cell boundaries, captioned Figure N. <Name> (<NNN>h) Format"),
                ("factory-default table", "<NNN>h <Name> 0x<VVVV>")]

    def near_misses(self, pages, pdf=None, limit=8):
        if pdf is None:
            return []
        import pymupdf
        out = []
        with pymupdf.open(pdf) as doc:
            for i, page in enumerate(doc):
                for ln in _page_lines(page, i + 1):
                    if self.PAIR.search(ln.text) and "register" in ln.text.lower() and not self.LEADER.search(ln.text) \
                            and not ln.text.startswith("Figure"):
                        out.append((i + 1, f"{ln.text[:140]}  [{'bold' if ln.bold else 'regular'} {ln.size:.1f} pt]"))
                if len(out) >= limit:
                    break
        return out[:limit]

    # -------------------------------------------------- page readers

    @classmethod
    def _bit_headers(cls, words) -> list[dict[int, tuple]]:
        out = []
        for row in _group_lines([w for w in words if cls.BITHDR.match(w[4])]):
            bits = {int(w[4][1:]): w for w in row}
            if len(bits) != len(row) or set(bits) not in (set(range(16)), set(range(8))):
                continue
            xs = [(bits[b][0] + bits[b][2]) / 2 for b in sorted(bits, reverse=True)]
            if all(a < b for a, b in zip(xs, xs[1:])):
                out.append(bits)
        return out

    def _diagram(self, pno, words, lines, bits, segs, notes) -> _Diagram | None:
        n = len(bits)
        top = n - 1
        cx = {b: (w[0] + w[2]) / 2 for b, w in bits.items()}
        pitch = (cx[0] - cx[top]) / top
        gx0, gx1 = cx[top] - pitch / 2, cx[0] + pitch / 2
        hy1 = max(w[3] for w in bits.values())
        hs, vs = segs
        wide = [s for s in _merge_h(hs) if min(s[1], gx1) - max(s[0], gx0) >= 0.5 * (gx1 - gx0)]
        tops = [y for _, _, y in wide if hy1 - 4 <= y <= hy1 + 4]
        ctop = max(tops) if tops else hy1
        bots = [y for _, _, y in wide if y > ctop + 4]
        if not bots or min(bots) - ctop > 80:
            notes.append(f"p{pno}: a D{top}..D0 header without a drawn row boundary below it; diagram not read")
            return None
        cbot = min(bots)
        h = cbot - ctop
        xs = [x for x, y0, y1 in vs if gx0 - pitch <= x <= gx1 + pitch and min(y1, cbot) - max(y0, ctop) >= 0.5 * h]
        bounds = _cluster(xs, 1.5)
        if len(bounds) < 2:
            notes.append(f"p{pno}: a D{top}..D0 header without drawn cell boundaries; diagram not read")
            return None
        if bounds[0] > cx[top]:
            bounds.insert(0, gx0)
        if bounds[-1] < cx[0]:
            bounds.append(gx1)
        cells = []
        head = {id(w) for w in bits.values()}
        for left, right in zip(bounds, bounds[1:]):
            bs = [b for b in bits if left < cx[b] < right]
            if not bs:
                continue
            ws = [w for w in words if id(w) not in head and left < (w[0] + w[2]) / 2 < right
                  and ctop < (w[1] + w[3]) / 2 < cbot]
            text = _join_wrapped([" ".join(w[4] for w in r) for r in _group_lines(ws)], space=True)
            cells.append((max(bs), min(bs), _ws(text)))
        cap = next((ln for ln in sorted(lines, key=lambda ln: ln.y0)
                    if cbot - 1 <= ln.y0 <= cbot + 90 and re.match(r"Figure\s+\d+\.", ln.text)), None)
        pairs = [(nm, int(a, 16)) for nm, a in self.PAIR.findall(cap.text)] if cap else []
        return _Diagram(pno, n, cells, cap.text if cap else None, pairs)

    def _grids(self, pno, words) -> dict[int, dict]:
        out: dict[int, dict] = {}
        for row in _group_lines([w for w in words if self.GRID_COL.match(w[4])]):
            if len(row) < 2:
                continue
            ccx = [((w[0] + w[2]) / 2, int(self.GRID_COL.match(w[4]).group(1), 16)) for w in row]
            cpitch = statistics.median(b[0] - a[0] for a, b in zip(ccx, ccx[1:]))
            left = ccx[0][0] - cpitch / 2
            hy1 = max(w[3] for w in row)
            labels = sorted((w for w in words if self.GRID_ROW.match(w[4]) and w[1] > hy1 - 1 and w[2] <= left + 2),
                            key=lambda w: w[1])
            rows, x0 = [], None
            for w in labels:
                v = int(w[4][:-1], 16)
                if x0 is None and v == 0:
                    x0 = w[0]
                if x0 is not None and abs(w[0] - x0) < 8 and v == len(rows):
                    rows.append(w)
            if len(rows) < 2:
                continue
            yc = [(w[1] + w[3]) / 2 for w in rows]
            bands = []
            for i, y in enumerate(yc):
                lo = hy1 if i == 0 else (yc[i - 1] + y) / 2
                hi = (y + yc[i + 1]) / 2 if i + 1 < len(yc) else y + (y - yc[i - 1]) / 2
                bands.append((lo, hi))
            used = {id(w) for w in row} | {id(w) for w in rows}
            for i, (lo, hi) in enumerate(bands):
                rline = [w for w in words if abs(w[1] - rows[i][1]) < 2.5]
                q = " ".join(w[4] for w in sorted(rline, key=lambda w: w[0]))
                for j, (x, base) in enumerate(ccx):
                    cl = left if j == 0 else (ccx[j - 1][0] + x) / 2
                    cr = (x + ccx[j + 1][0]) / 2 if j + 1 < len(ccx) else x + cpitch / 2
                    ws = [w for w in words if id(w) not in used and cl < (w[0] + w[2]) / 2 < cr and lo < (w[1] + w[3]) / 2 < hi]
                    text = _join_wrapped([" ".join(w[4] for w in r) for r in _group_lines(ws)], space=False)
                    text = text.replace("*", "").replace("—", "").strip()
                    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", text):
                        out[base * 16 + i] = {"name": text, "p": pno, "q": q}
        return out

    def _defaults(self, pno, text) -> dict[int, dict]:
        if "DEFAULT" not in text:
            return {}
        out = {}
        for ln in text.splitlines():
            m = self.DEF_ROW.match(ln)
            if m:
                out[int(m.group(1), 16)] = {"name": m.group(2), "reset": None if m.group(3) == "Varies" else int(m.group(3), 16),
                                            "p": pno, "q": _ws(ln)}
        return out

    def _sections(self, stream: list[_Line]) -> list[dict]:
        heads = []
        i = 0
        while i < len(stream):
            ln = stream[i]
            if ln.bold and ln.size >= 9.8 and not self.LEADER.search(ln.text):
                text, j = ln.text, i
                while j + 1 < len(stream):
                    nx = stream[j + 1]
                    if nx.p == ln.p and nx.bold and nx.size >= 9.8 and abs(nx.x0 - ln.x0) < 30 \
                            and 0 <= nx.y0 - stream[j].y1 < 6 and not re.search(r"Registers?\s*(\([^)]*\))?\s*$", text):
                        text, j = text + " " + nx.text, j + 1
                    else:
                        break
                pairs = [(n, int(a, 16)) for n, a in self.PAIR.findall(text)]
                if pairs and re.search(r"\bRegisters?\b", text):
                    heads.append((i, j, text, pairs, bool(re.search(r"\)\s*to\s+", text))))
                i = j + 1
                continue
            i += 1
        secs = []
        for k, (i, j, text, pairs, rng) in enumerate(heads):
            end = heads[k + 1][0] if k + 1 < len(heads) else len(stream)
            sec = {"pairs": pairs, "range": rng, "p": stream[i].p, "q": _ws(text), "type": None, "nv": None, "init": None}
            body = stream[j + 1:end]
            n = 0
            while n < len(body):
                t = body[n].text
                hit = None
                for key, rx in (("type", self.LBL_TYPE), ("nv", self.LBL_NV), ("init", self.LBL_INIT)):
                    m = rx.match(t)
                    if m:
                        hit = (key, m)
                        break
                if not hit:
                    n += 1
                    continue
                key, m = hit
                val, n2 = m.group("v").strip(), n + 1
                while n2 < len(body) and self._continues(val, body[n2].text):
                    val, n2 = _join_wrapped([val, body[n2].text], space=True), n2 + 1
                if sec[key] is None:
                    kind = _ws(m.group("k") or "") if key != "type" else ""
                    sec[key] = {"kind": kind.lower(), "text": _ws(val.replace("\xad", "")), "p": body[n].p,
                                "q": _ws(body[n].text.replace("\xad", ""))}
                n = n2
            secs.append(sec)
        return secs

    def _continues(self, prev: str, nxt: str) -> bool:
        if any(rx.match(nxt) for rx in (self.LBL_TYPE, self.LBL_NV, self.LBL_INIT)) or nxt.startswith("Figure"):
            return False
        words = prev.split()
        return prev.endswith(("\xad", "-", ",")) or (words and words[-1].lower() in self.FUNC_END) \
            or nxt[:1].islower() or nxt[:1] == "("

    # -------------------------------------------------- the document

    def parse(self, pages: dict[int, str], pdf: Path | None = None) -> Parse:
        import pymupdf
        if pdf is None:
            raise ValueError("the maxim template reads the source PDF, and doc.json names none that exists")
        notes: list[str] = []
        issues: list[Issue] = []
        stream: list[_Line] = []
        diagrams: list[_Diagram] = []
        grid: dict[int, dict] = {}
        defaults: dict[int, dict] = {}
        with pymupdf.open(pdf) as doc:
            for i, page in enumerate(doc):
                pno = i + 1
                words = page.get_text("words")
                lines = _page_lines(page, pno)
                mid = page.rect.width / 2
                stream += sorted(lines, key=lambda ln: (0 if ln.x0 < mid - 10 else 1, ln.y0))
                hdrs = self._bit_headers(words)
                if hdrs:
                    segs = _segments(page)
                    for bits in hdrs:
                        d = self._diagram(pno, words, lines, bits, segs, notes)
                        if d:
                            diagrams.append(d)
                grid.update(self._grids(pno, words))
                defaults.update(self._defaults(pno, pages.get(pno, "")))
        secs = self._sections(stream)
        regs = self._assemble(secs, diagrams, grid, defaults, notes, issues)
        summary = {}
        for a in set(grid) | set(defaults):
            g, d = grid.get(a), defaults.get(a)
            summary[a] = {"name": (g or d)["name"], "access": None, "reset": d["reset"] if d else None, "size": None,
                          "p": (g or d)["p"], "q": (g or d)["q"]}
        return Parse(registers=regs, summary=summary, notes=notes, issues=issues)

    def _assemble(self, secs, diagrams, grid, defaults, notes, issues) -> list[RawReg]:
        # every (address, name) the document prints, best source first
        names: dict[int, list[tuple[int, str, str, int, str]]] = {}

        def add(a, prio, nm, how, p, q):
            names.setdefault(a, []).append((prio, nm, how, p, q))

        for s in secs:
            for nm, a in s["pairs"]:
                add(a, 0, nm, "heading", s["p"], s["q"])
        for d in diagrams:
            for nm, a in d.pairs:
                add(a, 1, nm, "caption", d.p, d.caption)
        for a, g in grid.items():
            add(a, 2, g["name"], "memory map", g["p"], g["q"])
        for a, g in defaults.items():
            add(a, 3, g["name"], "default table", g["p"], g["q"])
        # a name at two addresses: the memory map (or default table) places it
        where: dict[str, set[int]] = {}
        for a, es in names.items():
            for e in es:
                where.setdefault(e[1].lower(), set()).add(a)
        alias: dict[int, int] = {}
        for nm, aset in sorted(where.items()):
            if len(aset) < 2:
                continue
            mapped = sorted(a for a in aset if any(e[1].lower() == nm and e[2] in ("memory map", "default table")
                                                   for e in names[a]))
            if len(mapped) != 1:
                continue
            canon = mapped[0]
            for a in sorted(aset - {canon}):
                moved = [e for e in names[a] if e[1].lower() == nm]
                names[a] = [e for e in names[a] if e[1].lower() != nm]
                names[canon] += moved
                e0 = moved[0]
                issues.append(Issue(f"{_reg_slug(e0[1])}.addr", None, None,
                                    [f"{e0[2]} p{e0[3]}", f"memory map p{names[canon][0][3]}"],
                                    f"{e0[1]} is at {a:03X}h in its {e0[2]} p{e0[3]}, at {canon:03X}h in the memory map",
                                    e0[3], e0[4]))
                issues[-1].addr = canon
                if not names[a]:
                    del names[a]
                    alias[a] = canon
        regs: dict[int, RawReg] = {}
        for a in sorted(names):
            es = sorted(names[a], key=lambda e: e[0])
            nm = es[0][1]
            ps, qs = [], []
            for e in es:
                if e[0] == 1 and len({x[0] for x in es}) > 1:
                    continue            # a caption is cited by the field records
                if e[3] not in ps:
                    ps.append(e[3])
                if e[4] and e[4] not in qs:
                    qs.append(e[4])
            rr = RawReg(name=nm, addrs=[a], resets=[], p=ps[0] if len(ps) == 1 else ps,
                        q=qs[0] if len(qs) == 1 else qs, width=16, addr_digits=3)
            others = sorted({(e[1], e[2], e[3]) for e in es if e[1] != nm})
            for o, how, p in others:
                rr.notes.append(f"{how} p{p} prints it {o}")
            regs[a] = rr
        for iss in issues:
            if iss.addr in regs:
                iss.reg = regs[iss.addr]
        by_name = {rr.name.lower(): rr for rr in regs.values()}

        def reg(nm: str | None, a: int | None) -> RawReg | None:
            if a is not None:
                a = alias.get(a, a)
                if a in regs and (nm is None or regs[a].name.lower() == nm.lower() or
                                  any(e[1].lower() == nm.lower() for e in names.get(a, []))):
                    return regs[a]
            return by_name.get(nm.lower()) if nm else None

        def twin(nv: RawReg, ram: RawReg) -> bool:
            return nv.name.lower() == "n" + ram.name.lower()

        pairs: list[tuple[RawReg, RawReg]] = []
        values: dict[int, list[tuple[int, int, str | None, int, str]]] = {}

        def value(r: RawReg, prio, v, note, p, q):
            values.setdefault(id(r), []).append((prio, v, note, p, q))

        for s in secs:
            own = [reg(nm, a) for nm, a in s["pairs"]]
            own = [r for r in own if r]
            if s["range"] and len(s["pairs"]) == 2:
                lo_a, hi_a = sorted(alias.get(a, a) for _, a in s["pairs"])
                span = [r for a, r in regs.items() if lo_a <= a <= hi_a]
            else:
                span = own
            for r in span:
                if s["type"] and "reg_type" not in r.extra:
                    r.extra["reg_type"] = s["type"]["text"].rstrip(".")
            single = own[0] if len(own) == 1 and not s["range"] else None
            target = None
            nv = s["nv"]
            if single and nv and not re.match(r"(?:derived|translated|there (?:is|are) no|none)\b", nv["text"], re.I):
                cut = re.split(r"\bif\b|,\s*but\b|\(does not", nv["text"])[0]
                refs = [reg(n, int(a, 16)) for n, a in self.PAIR.findall(cut)]
                if len(refs) == 1 and refs[0] is not None and refs[0] is not single:
                    t = refs[0]
                    if twin(single, t):
                        pairs.append((t, single))
                        target = t
                    elif twin(t, single):
                        pairs.append((single, t))
                    elif "restore" in nv["kind"] and "backup" not in nv["kind"]:
                        target = t
            ini = s["init"]
            if ini:
                txt = re.sub(r"\([^)]*\)", "", ini["text"])
                targeted = []
                for rx, ni, vi in ((self.INIT_EQ, 1, 2), (self.INIT_FOR, 2, 1), (self.INIT_DEF, 1, 2)):
                    for m in rx.finditer(txt):
                        r = by_name.get(m.group(ni).lower())
                        if r:
                            targeted.append((r, int(m.group(vi), 16)))
                src = f"alternate initial value in the {s['pairs'][0][0]} section"
                if targeted:
                    for r, v in targeted:
                        value(r, 1, v, src, ini["p"], ini["q"])
                else:
                    m = re.match(r"\s*(?:[\d.]+\s*%\s*)?\(?\s*(0x[0-9A-Fa-f]{1,4})\b", ini["text"])
                    if m and target is not None:
                        value(target, 1, int(m.group(1), 16), src, ini["p"], ini["q"])
                    elif m and single is not None:
                        alt = "alternate" in ini["kind"]
                        value(single, 0, int(m.group(1), 16), "alternate initial value" if alt else None, ini["p"], ini["q"])
        for d in diagrams:
            rs = [r for r in (reg(nm, a) for nm, a in d.pairs) if r]
            if len(rs) == 2 and (twin(rs[0], rs[1]) or twin(rs[1], rs[0])):
                ram, nvr = (rs[1], rs[0]) if twin(rs[0], rs[1]) else (rs[0], rs[1])
                pairs.append((ram, nvr))
                target = ram
            elif rs:
                target = rs[0]
            else:
                notes.append(f"p{d.p}: bit diagram '{d.caption or 'uncaptioned'}' names no register address; not read")
                continue
            if target.fields:
                notes.append(f"p{d.p}: a second bit diagram for {target.name} ({d.caption}); the first one is kept")
                continue
            target.fields = self._cells_to_fields(d)
            target.width = d.nbits
            target.figure = (re.match(r"Figure\s+\d+", d.caption).group(0) if d.caption else None)
        for a, g in defaults.items():
            r = regs.get(alias.get(a, a))
            if r is not None and g["reset"] is not None:
                value(r, 2, g["reset"], f"factory default, p{g['p']}", g["p"], g["q"])
        for r in regs.values():
            vs = sorted(values.get(id(r), []), key=lambda v: v[0])
            if not vs:
                continue
            prio, v, note, p, q = vs[0]
            r.reset = v
            if note:
                r.notes.insert(0, f"reset: {note}")
            if isinstance(r.p, int):
                r.p = [r.p]
            if p not in r.p:
                r.p.append(p)
            if isinstance(r.q, str):
                r.q = [r.q]
            if q and q not in r.q:
                r.q.append(q)
            other = [x for x in vs[1:] if x[1] != v]
            if other:
                x = other[0]
                issues.append(Issue(f"{_reg_slug(r.name)}.reset", r, None, [f"p{p}", f"p{x[3]}"],
                                    f"{r.name}: reset 0x{v:04X} ({note or 'initial value'}), 0x{x[1]:04X} ({x[2] or 'initial value'})",
                                    x[3], x[4]))
        for ram, nvr in pairs:
            ram.extra.setdefault("nv_backup", nvr)
            if not nvr.fields:
                nvr.extra.setdefault("mirror_of", ram)
        for r in regs.values():
            for f in r.fields:
                if r.reset is not None and f.access != "rsvd":
                    f.reset = (r.reset >> f.lo) & ((1 << (f.hi - f.lo + 1)) - 1)
                    f.reset_width = f.hi - f.lo + 1
                    f.derived = True
                    f.note = "; ".join(x for x in (f.note, "reset derived from register reset") if x)
        return list(regs.values())

    @staticmethod
    def _cells_to_fields(d: _Diagram) -> list[RawField]:
        """Named cells are fields; runs of "0"/"1" cells, of "X" cells and of empty cells are RESERVED."""
        fig = re.match(r"Figure\s+\d+", d.caption).group(0) if d.caption else "the bit diagram"
        runs: list[list] = []            # [kind, hi, lo, text or fixed value]
        for hi, lo, text in d.cells:
            k = "fixed" if text in ("0", "1") else ("x" if text.upper() == "X" else ("empty" if not text else "name"))
            fill = int(text) * ((1 << (hi - lo + 1)) - 1) if k == "fixed" else None
            if k != "name" and runs and runs[-1][0] == k and runs[-1][2] == hi + 1:
                runs[-1][2] = lo
                if k == "fixed":
                    runs[-1][3] = (runs[-1][3] << (hi - lo + 1)) | fill
                continue
            runs.append([k, hi, lo, fill if k == "fixed" else text])
        out = []
        for k, hi, lo, v in runs:
            note = f"bits from {fig} cell boundaries"
            if k == "fixed":
                note += f"; fixed at {_bin_str(v, hi - lo + 1)} in the bit diagram (must be written so)"
            elif k == "x":
                note += "; X (don't care) in the bit diagram"
            elif k == "empty":
                note += "; empty cell in the bit diagram"
            out.append(RawField(name=v if k == "name" else "RESERVED", hi=hi, lo=lo, access="rsvd" if k == "x" else None,
                                reset=None, reset_width=None, desc="", p=d.p, q=d.caption, note=note,
                                fixed=v if k == "fixed" else None))
        return out


TEMPLATES: dict[str, type[Template]] = {"ti": TI, "maxim": Maxim}


# ---------------------------------------------------------------- raw parse -> records

def _layout(rr: RawReg) -> list[dict]:
    """The records a raw register becomes: one per bus byte when it has several addresses, else one."""
    base = rr.id_name or _reg_slug(rr.name)
    if len(rr.addrs) > 1:
        top = max([f.hi for f in rr.fields] + [8 * len(rr.addrs) - 1])
        n = max(len(rr.addrs), (top + 8) // 8)
        rows = []
        for k in range(n):
            a = rr.addrs[k] if k < len(rr.addrs) else rr.addrs[-1] + (k - len(rr.addrs) + 1)
            sfx = "LSB" if k == 0 else ("MSB" if k == n - 1 else f"B{k}")
            rows.append({"name": f"{rr.name}_{sfx}", "idn": f"{base}_{sfx}", "addr": a, "lo": 8 * k, "hi": 8 * k + 7,
                         "width": 8, "k": k})
        return rows
    width = rr.width or max(8, (max([f.hi for f in rr.fields], default=7) // 8 + 1) * 8)
    return [{"name": rr.name, "idn": base, "addr": (rr.addrs or [0])[0], "lo": 0, "hi": width - 1, "width": width, "k": 0}]


def _pieces(rr: RawReg, lay: list[dict]) -> dict[int, list[tuple[int, str, int, int]]]:
    """Per field (index): (layout row, field name, abs hi, abs lo). A field split across bytes is renamed
    `<FIELD>_<hi>_<lo>` (absolute bits), a reserved one `RESERVED_<hi>_<lo>` (bits within its byte)."""
    out: dict[int, list] = {}
    for i, f in enumerate(rr.fields):
        parts = [(j, max(f.lo, r["lo"]), min(f.hi, r["hi"])) for j, r in enumerate(lay)
                 if max(f.lo, r["lo"]) <= min(f.hi, r["hi"])]
        for j, lo, hi in parts:
            base = lay[j]["lo"]
            if _is_reserved(f.name):
                nm = f"RESERVED_{hi - base}_{lo - base}" if hi != lo else f"RESERVED_{hi - base}"
            elif len(parts) > 1:
                nm = f"{f.name}_{hi}_{lo}"
            else:
                nm = f.name
            out.setdefault(i, []).append((j, nm, hi, lo))
    return out


def _clear_on_read(access: str | None, desc: str) -> tuple[str | None, str | None]:
    """A read-only bit whose description says reading clears it is `rclr`; when the sentence makes that
    conditional ("When ALATCH = 1 this bit is cleared by reading ...") it stays `ro`, and the note says so,
    since SystemRDL has no conditional clear-on-read."""
    if access != "ro":
        return access, None
    m = TI.RCLR.search(desc)
    if not m:
        return access, None
    c = re.search(r"\b((?:[Oo]nly\s+)?(?:[Ww]hen|[Ii]f)\b[^.;]{0,60}?),?\s+(?:this|the)\s+(?:bit|flag|field)s?\s+"
                  r"(?:is|are)\s*$", desc[:m.start()])
    if c:
        cond = _ws(c.group(1))
        return access, f"clears on read only {cond[0].lower() + cond[1:]}"
    return "rclr", None


def _check_document(rr: RawReg, lay, pieces, reg_refs, piece_refs, conflict) -> None:
    """Where the document disagrees with itself: field resets vs the register reset, the bit diagram vs the
    field table, fixed bits vs the reset, overlapping fields."""
    width_total = sum(r["width"] for r in lay)
    fig_src = f"bit diagram ({rr.figure or 'figure'}, p{rr.p if isinstance(rr.p, int) else rr.p[0]})"
    tab_src = f"field table ({rr.table or 'table'}, p{rr.fields[0].p if rr.fields else rr.p})"
    full = rr.reset
    if full is None and rr.byte_resets and all(v is not None for v in rr.byte_resets):
        full = sum(v << (8 * k) for k, v in enumerate(rr.byte_resets))
    fields = [f for f in rr.fields if not f.derived]
    comp = 0
    for f in fields:
        comp |= (f.reset or 0) << f.lo
    q0 = rr.q if isinstance(rr.q, str) else rr.q[0]
    p0 = rr.p if isinstance(rr.p, int) else rr.p[0]
    if fields and full is not None and all(f.reset is not None for f in fields) and comp != full:
        conflict(f"{_reg_slug(rr.name)}.reset", reg_refs, [f"heading p{p0}", tab_src],
                 f"field resets compose to {comp:0{width_total}b}, the heading says {full:0{width_total}b}", p0, q0)
    # bit diagram tokens: one per field, or one per field piece when the diagram is drawn byte by byte
    order = sorted(range(len(rr.fields)), key=lambda i: -rr.fields[i].hi)
    units = [(i, rr.fields[i].hi, rr.fields[i].lo, rr.fields[i].reset) for i in order]
    split_units = []
    for i in order:
        f = rr.fields[i]
        for _, _, hi, lo in sorted(pieces.get(i, []), key=lambda x: -x[2]):
            sub = None if f.reset is None else (f.reset >> (lo - f.lo)) & ((1 << (hi - lo + 1)) - 1)
            split_units.append((i, hi, lo, sub))
    toks = rr.fig_tokens
    use = units if len(toks) == len(units) else (split_units if len(toks) == len(split_units) else None)
    if toks and use is not None:
        for (fa, fr), (i, hi, lo, sub) in zip(toks, use):
            f = rr.fields[i]
            ref = piece_refs.get(i, [])
            span = f"{hi}" if hi == lo else f"{hi}:{lo}"
            key = f"{_reg_slug(rr.name)}.{_field_slug(f.name)}_{span.replace(':', '_')}"
            if ACCESS[fa] != f.access:
                conflict(f"{key}.access", ref, [fig_src, tab_src],
                         f"{rr.name}[{span}] {f.name}: access {ACCESS[fa]} in the bit diagram, {f.access} in the field table",
                         f.p, f.q)
            fv, fw = _bits_value(fr)
            if fv != sub or (fw is not None and fw != hi - lo + 1):
                conflict(f"{key}.reset", ref, [fig_src, tab_src],
                         f"{rr.name}[{span}] {f.name}: reset {fr} ({fw or '?'} bits) in the bit diagram, "
                         f"{_bin_str(sub, hi - lo + 1)} ({hi - lo + 1} bits) in the field table", f.p, f.q)
    elif toks:
        conflict(f"{_reg_slug(rr.name)}.bit_diagram", reg_refs, [fig_src, tab_src],
                 f"{rr.name}: {len(toks)} access/reset tokens in the bit diagram, {len(units)} rows in the field table",
                 p0, q0)
    for i, f in enumerate(rr.fields):
        if f.fixed is not None and rr.reset is not None:
            w = f.hi - f.lo + 1
            sub = (rr.reset >> f.lo) & ((1 << w) - 1)
            if sub != f.fixed:
                span = f"{f.hi}" if f.hi == f.lo else f"{f.hi}:{f.lo}"
                conflict(f"{_reg_slug(rr.name)}.fixed_{span.replace(':', '_')}", piece_refs.get(i, []),
                         [f"bit diagram ({rr.figure or 'figure'}, p{f.p})", f"register reset p{p0}"],
                         f"{rr.name}[{span}]: the bit diagram fixes {_bin_str(f.fixed, w)}, the register reset "
                         f"0x{rr.reset:04X} has {_bin_str(sub, w)} there", f.p, f.q if isinstance(f.q, str) else None)
    owner: dict[int, int] = {}
    for i in order:
        f = rr.fields[i]
        hit = sorted({owner[b] for b in range(f.lo, f.hi + 1) if b in owner})
        if hit:
            g = rr.fields[hit[0]]
            conflict(f"{_reg_slug(rr.name)}.{_field_slug(f.name)}.overlap", piece_refs.get(i, []) + piece_refs.get(hit[0], []),
                     [tab_src], f"{rr.name}: {f.name} [{f.hi}:{f.lo}] overlaps {g.name} [{g.hi}:{g.lo}]",
                     f.p if isinstance(f.p, int) else f.p[0], f.q if isinstance(f.q, str) else None)
        for b in range(f.lo, f.hi + 1):
            owner.setdefault(b, i)


def to_records(parse: Parse, doc_id: str, part: str, template: str) -> list[dict]:
    """Registers split into bus-addressable bytes when a heading lists several addresses (`NAME` at 0h, 1h
    becomes NAME_LSB / NAME_MSB); a field split across bytes is renamed `<FIELD>_<hi>_<lo>` (absolute bits), a
    reserved one `RESERVED_<hi>_<lo>` (bits within the byte)."""
    out: list[dict] = []
    conflicts: list[dict] = []
    used: set[str] = set()
    ids: set[str] = set()

    def conflict(key: str, refs: list[str], sources: list[str], text: str, p, q):
        cid = f"{part}.conflict.{key}"
        base, j = cid, 2
        while cid in used:
            cid, j = f"{base}.{j}", j + 1
        used.add(cid)
        conflicts.append({"id": cid, "kind": "conflict", "doc": doc_id, "p": p, "q": q if isinstance(q, str) else None,
                          "src": "table", "prov": "deterministic", "refs": refs, "sources": sources, "text": text})

    def uniq(rid: str) -> str:
        cand, j = rid, 2
        while cand in ids:
            cand, j = f"{rid}_{j}", j + 1
        ids.add(cand)
        return cand

    reg_ids: dict[int, list[str]] = {}
    piece_ids: dict[tuple[int, int], list[str]] = {}
    later: list[tuple[dict, RawReg]] = []
    for rr in parse.registers:
        lay = _layout(rr)
        pieces = _pieces(rr, lay)
        rids = [uniq(f"{part}.reg.{r['idn']}") for r in lay]
        reg_ids[id(rr)] = rids
        fid_by_piece: dict[int, list[str]] = {}
        field_recs: list[list[dict]] = [[] for _ in lay]
        accs: list[set] = [set() for _ in lay]
        for i, f in enumerate(rr.fields):
            for j, nm, hi, lo in pieces.get(i, []):
                r = lay[j]
                fid = uniq(f"{rids[j]}.{_field_slug(nm)}")
                fid_by_piece.setdefault(i, []).append(fid)
                sub = None if f.reset is None else (f.reset >> (lo - f.lo)) & ((1 << (hi - lo + 1)) - 1)
                acc, cnote = _clear_on_read(f.access, f.desc or "")
                field_recs[j].append({"id": fid, "kind": "field", "doc": doc_id, "p": f.p, "q": f.q, "src": "table",
                                      "prov": "deterministic", "reg": rids[j], "name": nm,
                                      "bits": [hi - r["lo"], lo - r["lo"]], "reset": _bin_str(sub, hi - lo + 1),
                                      "access": acc, "desc": f.desc or None,
                                      "note": "; ".join(x for x in (f.note, cnote) if x) or None})
                if acc and not _is_reserved(nm):
                    accs[j].add(acc)
        for i, v in fid_by_piece.items():
            piece_ids[(id(rr), i)] = v
        _check_document(rr, lay, pieces, rids, {i: v for i, v in fid_by_piece.items()}, conflict)
        note = "; ".join(rr.notes) or None
        for j, r in enumerate(lay):
            if len(lay) > 1:
                rv = rr.byte_resets[r["k"]] if rr.byte_resets and r["k"] < len(rr.byte_resets) else None
            else:
                rv = rr.reset
            reg = {"id": rids[j], "kind": "register", "doc": doc_id, "p": rr.p, "q": rr.q, "src": "table",
                   "prov": "deterministic", "name": r["name"], "addr": f"0x{r['addr']:0{rr.addr_digits}X}",
                   "width": r["width"], "reset": _reset_str(rv, r["width"]),
                   "access": "rw" if accs[j] and accs[j] <= {"rw"} else ("ro" if accs[j] and accs[j] <= {"ro", "rclr"} else None),
                   "note": note}
            if rr.extra.get("reg_type"):
                reg["reg_type"] = rr.extra["reg_type"]
            sm = parse.summary.get(r["addr"])
            if sm:
                reg["summary"] = {k: v for k, v in (("name", sm["name"]), ("access", sm.get("access")),
                                                    ("reset", _reset_str(sm.get("reset"), r["width"]))) if v is not None}
                sv = sm.get("reset")
                if rv is not None and sv is not None and sv != rv:
                    conflict(f"{_reg_slug(r['name'])}.summary_reset", [rids[j]],
                             [f"heading p{rr.p if isinstance(rr.p, int) else rr.p[0]}", f"summary table p{sm['p']}"],
                             f"{r['name']}: reset {_reset_str(rv, r['width'])} in the heading, "
                             f"{_reset_str(sv, r['width'])} in the summary table", sm["p"], sm["q"])
            out.append(reg)
            out += field_recs[j]
            later.append((reg, rr))
    for reg, rr in later:        # relations between registers, once every id exists
        for key in ("nv_backup", "mirror_of"):
            other = rr.extra.get(key)
            if other is not None and id(other) in reg_ids:
                reg[key] = reg_ids[id(other)][0]
    for iss in parse.issues:
        refs = []
        if iss.fld is not None and iss.reg is not None:
            i = next((k for k, f in enumerate(iss.reg.fields) if f is iss.fld), None)
            refs = piece_ids.get((id(iss.reg), i), []) if i is not None else []
        if not refs and iss.reg is not None:
            refs = reg_ids.get(id(iss.reg), [])
        conflict(iss.key, refs, iss.sources, iss.text, iss.p, iss.q)
    covered = {R.to_int(r["addr"]) for r in out if r["kind"] == "register"}
    for a, sm in sorted(parse.summary.items()):
        if a not in covered:
            conflict(f"summary.{_reg_slug(sm['name'])}", [], [f"summary table p{sm['p']}"],
                     f"{sm['name']} at 0x{a:02X} is in the summary table but has no register section", sm["p"], sm["q"])
    for r in out + conflicts:
        for k in [k for k, v in r.items() if v is None and k != "q"]:
            del r[k]
    return out + conflicts


# ---------------------------------------------------------------- run / CLI

def load_pages(docdir: Path) -> dict[int, str]:
    pages = {}
    for f in sorted((Path(docdir) / "pages").glob("p[0-9][0-9][0-9]*.txt")):
        m = re.fullmatch(r"p(\d+)\.txt", f.name)
        if m:
            pages[int(m.group(1))] = f.read_text(encoding="utf-8")
    return pages


def run(docdir: Path, template: str) -> dict:
    docdir = Path(docdir)
    if template not in TEMPLATES:
        raise ValueError(f"unknown template {template!r}; known: {', '.join(sorted(TEMPLATES))}")
    meta = common.read_json(docdir / "doc.json")
    pages = load_pages(docdir)
    tpl = TEMPLATES[template]()
    pdf = None
    if tpl.needs_pdf:
        from .pdftext import resolve_pdf_path
        pdf = resolve_pdf_path(docdir, meta)
        if pdf is None:
            raise ValueError(f"template {template} reads the source PDF, and {meta.get('path') or 'doc.json path'} "
                             f"is not there")
    parse = tpl.parse(pages, pdf)
    part = meta["doc_id"].split("@")[0]
    recs = to_records(parse, meta["doc_id"], part, template)
    R.save_file(docdir, OUT_FILE, recs)
    regs = [r for r in recs if r["kind"] == "register"]
    cited = sorted({p for r in recs if r["kind"] in ("register", "field") for p in R.pages(r)})
    st = {"registers": len(regs), "fields": sum(1 for r in recs if r["kind"] == "field"),
          "summary_rows": len(parse.summary), "summary_covered": sum(1 for r in regs if r.get("summary")),
          "conflicts": [r for r in recs if r["kind"] == "conflict"], "pages": cited, "notes": parse.notes,
          "template": template}
    if not regs:
        st["diagnostic"] = {"patterns": tpl.patterns(), "near_misses": tpl.near_misses(pages, pdf)}
    return st


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
    nc = len(st["conflicts"])
    print(f"{args.doc_id}: {st['registers']} registers, {st['fields']} fields, {nc} conflict{'' if nc == 1 else 's'}, "
          f"{len(st['pages'])} pages ({_runs(st['pages']) or '-'}); summary table {st['summary_covered']}/"
          f"{st['summary_rows']} covered -> records/{OUT_FILE}")
    for c in st["conflicts"]:
        print(f"   {c['id']}: {c['text']}")
    for n in st["notes"]:
        print(f"   note: {n}")
    if st["registers"]:
        return 0
    sys.stdout.flush()
    d = st["diagnostic"]
    print(f"doc regparse: template {args.template} found no register in {args.doc_id}. Patterns tried:", file=sys.stderr)
    for label, pat in d["patterns"]:
        print(f"   {label}: {pat}", file=sys.stderr)
    if d["near_misses"]:
        print("   lines that almost matched:", file=sys.stderr)
        for p, t in d["near_misses"]:
            print(f"   p{p}: {t}", file=sys.stderr)
    else:
        print("   no line came close; the document may use another vendor template", file=sys.stderr)
    return 1


def register(sub, groups) -> None:
    g = common.verb_group(sub, groups, "doc", "per-document steps: prep, check, render, regparse")
    p = g.add_parser("regparse", help="deterministic register/field records from a vendor register-map template")
    p.add_argument("doc_id")
    p.add_argument("--template", required=True, choices=sorted(TEMPLATES))
    p.set_defaults(func=cmd_regparse)

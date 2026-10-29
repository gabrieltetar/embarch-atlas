"""`mcu vote`: SVD vs reference-manual register sections vs CMSIS header -> `conflict` records.

Ported from research spike 13 (rm_regs.py, svd_rm_diff.py, rm_avail.py,
adjudicate.py, gen_field_notes.py). Deterministic: no LLM.

1. Parse every RM register section (`Address offset`, `Reset value`, `Bit(s) N[:M] NAME`,
   the access row of the bit diagram). A section is self-checked: its fields
   cover 32 bits and its access tokens count equals its non-reserved bits.
   Parametric names expand per bit when the RM gives the range: `IMx (x = 36 to 32)`
   on bits 4:0 is IM36..IM32.
2. Diff each against the SVD register it maps to, bit by bit: offset, reset
   (RM `X` digits masked), names, reserved vs defined, access. Names compare by bit range
   first: an SVD field whose bits are exactly an RM field's, or one element's of an RM vector
   or parametric field (`MODEy[1:0]` with `(y = 15 to 0)` is MODE0..MODE15, two bits each;
   `SELm+3[7:0]` with `(m = 4 * (x - 1))` in SELCR2 is SEL7), must carry the same name; a
   different one is a `name differs` finding, marked `family` (MODERy for the RM's MODEy) or
   `bit-range` (SEL24_31 for the RM's SEL3) when it has that shape. SVD fields that line up
   with no RM piece keep the looser per-bit comparison.
3. Adjudicate each difference with the header (`_Pos`/`_Msk`, bases) and a device
   filter: RM Table 1 ("peripherals versus devices") and per-bit "only available
   in <parts>" / "reserved in <parts>" notes. A family-wide RM feature the part
   lacks is a `correct-omission` (a `quirk` of class `variant`), never an SVD defect.
4. Constraint sentences from the RM field text ("only when", "must", "cleared by
   hardware", "Note: ...") become `rule` records keyed by SVD register/field names,
   page-cited, and are rendered to `docs/<rm>/s/<chapter>.fields.md`.

Outputs go to the RM's doc, because every record cites an RM page: `records/conflict.jsonl`,
`records/quirk.jsonl`, `records/rule.jsonl`, `s/<chapter>.fields.md`, `vote.md`, `vote.json`,
`availability.json`, `rm_regs.json`. A record's verdict leads its `note` (`svd-defect: ...`).

Verdicts: `svd-defect` (RM and header agree against the SVD), `svd-naming-defect`
(the header uses the RM's name), `svd-defect-candidate` (the RM is the only other
source: resets, access types, or a silent header), `rm-naming-differs` / `rm-differs` (the header
sides with the SVD), `alternate-view` (the header defines both: two views of one register),
`unresolved`.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from . import __version__
from .common import atlas_dir, read_jsonl, sha256_file, verb_group, write_json, write_jsonl, write_text
from .rm import RmDoc, _rm_doc_arg, map_chapter, slug
from .svd import SvdDoc, find_svd_doc

ACC_TOKENS = {"rw", "r", "w", "rc_w1", "rc_w0", "rs", "rt_w1", "rt_w0", "t", "rc_r", "w1", "rw1", "ro", "wo", "rc_w", "rs_r"}
# RM access token -> coarse class; SVD (SystemRDL-style) access -> coarse class
RM_COARSE = {"rw": "rw", "r": "r", "w": "w", "rs": "rw", "rc_w1": "rw", "rc_w0": "rw", "rt_w1": "rw", "rt_w0": "rw",
             "t": "rw", "rc_r": "r", "rs_r": "r", "ro": "r", "wo": "w", "w1": "w", "rw1": "rw", "rc_w": "rw"}
SVD_COARSE = {"rw": "rw", "ro": "r", "wo": "w", "w1": "w", "rw1": "rw", "rclr": "r", "woclr": "rw", "w1s": "rw", "wzc": "rw",
              "wos": "rw", "wzs": "rw", "wot": "rw", "wzt": "rw", "wclr": "rw", "wset": "rw"}
BIT_RE = re.compile(r"^\s*Bits? (\d+)(?::(\d+))?\s+(Reserved|[A-Za-z][A-Za-z0-9_+]*(?:\[\d+(?::\d+)?\])?)\s*(:|,|\.|$)(.*)")
CONSTRAINT_RE = re.compile(
    r"\b(only when|only if|must|cannot|can not|can be \w+ only|can only be|only be \w+|not allowed|forbidden|"
    r"prohibited|no effect|cleared by hardware|write-protected|locked)\b", re.I)
# A part-number token as reference manuals print them in availability notes: capitals and digits,
# lower-case `x` wildcards, `/` alternatives (vendor-neutral; the part itself comes from the SVD).
PART_TOKEN = r"[A-Z][A-Z0-9]*\d[A-Za-z0-9]*(?:/[A-Za-z0-9]+)*"
PARTS_RE = rf"(?:{PART_TOKEN}(?:\s*(?:,|and|or)\s*)?)+"


# --------------------------------------------------------------------------- RM register sections

def _strip_idx(name: str | None) -> str:
    return re.sub(r"\[.*?\]", "", name or "")


def _param_names(name: str, hi: int, lo: int, text: str) -> list[str] | None:
    """`IMx` over bits hi..lo with '(x = 36 to 32)' in its text -> per-bit names, lo first."""
    base = _strip_idx(name)
    m = re.search(r"\(\s*([a-z])\s*=\s*(\d+)\s*to\s*(\d+)\s*\)", text)
    if not m or not base.endswith(m.group(1)):
        return None
    a, b = int(m.group(2)), int(m.group(3))
    if abs(a - b) != hi - lo:
        return None
    first = min(a, b)
    return [base[:-1] + str(first + k) for k in range(hi - lo + 1)]


def parse_register_section(rm: RmDoc, e: dict) -> tuple[dict, list[tuple[int, str]]]:
    """One RM register section -> (offset, reset, per-bit names and access, self-check; its (page, line)s)."""
    nxt = rm.next_after(e)
    lines = rm.cut(e, nxt, e["start"], e["end"])
    txt = "\n".join(ln for _, ln in lines)
    off = re.search(r"Address offset:\s*(.+)", txt, re.I)
    rsts = re.findall(r"(?:Power-on )?[Rr]eset value:\s*(.+)", txt)
    r: dict = {"name": e["regname"], "section": e["id"], "title": e["title"], "page": e["start"],
               "pages": sorted({p for p, _ in lines}), "offset_text": off.group(1).strip() if off else None,
               "reset_text": rsts[0].strip() if rsts else None, "fields": [], "access_tokens": []}
    r["q_offset"] = next((" ".join(ln.split()) for _, ln in lines if "Address offset" in ln), None)
    r["q_reset"] = next((" ".join(ln.split()) for _, ln in lines if re.search(r"[Rr]eset value:", ln)), None)
    om = re.match(r"\s*(0x[0-9A-Fa-f]+)", r["offset_text"] or "")
    r["offset"] = int(om.group(1), 16) if om else None
    r["reset"], r["reset_mask"] = None, None
    if r["reset_text"]:
        rt = r["reset_text"].split("(")[0].replace(" ", "")
        if rt.lower().startswith("0x0x"):
            r["rm_typo"] = "reset value printed as " + r["reset_text"]
            rt = rt[2:]
        hexes = re.findall(r"0x[0-9A-Fa-fXx]+", r["reset_text"].replace(" ", ""))
        if len(set(h.lower() for h in hexes)) > 1 and not r.get("rm_typo"):
            r["reset_ambiguous"] = True
        try:
            if rt.lower().startswith("0x"):
                h = rt[2:10]
                r["reset"] = int(re.sub("[Xx]", "0", h), 16)
                r["reset_mask"] = int("".join("0" if c in "Xx" else "F" for c in h), 16)
            elif rt.lower().startswith("0b"):
                b = rt[2:34]
                r["reset"] = int(re.sub("[Xx]", "0", b), 2)
                r["reset_mask"] = int("".join("0" if c in "Xx" else "1" for c in b), 2)
        except ValueError:
            pass
        if len(rsts) > 1:
            r["reset_ambiguous"] = True
    cur = None
    stop = next((k for k, (_, ln) in enumerate(lines) if re.search(r"\bregister map\b", ln, re.I) and k > 0), None)
    if stop is not None:
        lines = lines[:stop]
    for p, ln in lines:
        m = BIT_RE.match(ln)
        if m:
            hi = int(m.group(1))
            lo = int(m.group(2)) if m.group(2) else hi
            cur = {"name": m.group(3), "hi": hi, "lo": lo, "page": p, "q": " ".join(ln.split()),
                   "lines": [(p, m.group(5).strip())] if m.group(5).strip() else []}
            r["fields"].append(cur)
        elif cur is not None and ln.strip():
            cur["lines"].append((p, ln.strip()))
    first_bit = next((k for k, (_, ln) in enumerate(lines) if re.match(r"^\s*Bits? \d", ln)), len(lines))
    for _, ln in lines[:first_bit]:
        toks = ln.split()
        if toks and all(t in ACC_TOKENS for t in toks):
            r["access_tokens"].append(toks)
    bitname: list[str | None] = [None] * 32
    bitdecl: list[str | None] = [None] * 32
    bitalias: list[str | None] = [None] * 32
    for f in r["fields"]:
        f["text"] = " ".join(t for _, t in f["lines"])
        names = None if f["name"] == "Reserved" else _param_names(f["name"], f["hi"], f["lo"], f["text"])
        vec = re.fullmatch(r"([A-Za-z][A-Za-z0-9_]*?)\[(\d+):(\d+)\]", f["name"])
        for k, b in enumerate(range(f["lo"], f["hi"] + 1)):
            if 0 <= b < 32:
                bitdecl[b] = f["name"]
                bitname[b] = names[k] if names else f["name"]
                if vec and f["hi"] > f["lo"]:  # CH[18:0] -> CH0..CH18, the per-bit spelling
                    bitalias[b] = f"{vec.group(1)}{int(vec.group(3)) + k}"
    r["bitname"], r["bitdecl"], r["bitalias"] = bitname, bitdecl, bitalias
    # width: a 16-bit register shows only the 15..0 diagram row and a 4-digit reset
    hi_bit = max((f["hi"] for f in r["fields"]), default=31)
    digits = len(re.sub(r"[^0-9A-Fa-fXx]", "", (r["reset_text"] or "").split("(")[0])[2:]) if r["reset_text"] else 8
    r["width"] = 16 if hi_bit <= 15 and (digits <= 4 or len(r["access_tokens"]) <= 1) else 32
    w = r["width"]
    acc: list[str | None] = [None] * 32
    toks = r["access_tokens"]
    nonres = lambda hi, lo: [b for b in range(hi, lo - 1, -1) if bitname[b] and bitname[b] != "Reserved"]
    if len(toks) == 1:
        top, bot = nonres(31, 16), nonres(15, 0)
        if not top and len(bot) == len(toks[0]):
            toks = [[], toks[0]]
        elif not bot and len(top) == len(toks[0]):
            toks = [toks[0], []]
    ok = len(toks) == 2
    if ok:
        for (hi, lo), row in zip(((31, 16), (15, 0)), toks):
            bits = nonres(hi, lo)
            if len(bits) != len(row):
                ok = False
                break
            for b, t in zip(bits, row):
                acc[b] = t
    r["access_ok"] = ok
    r["bitacc"] = acc if ok else [None] * 32
    r["coverage"] = sum(1 for b in bitname[:w] if b)
    r["parse_ok"] = r["coverage"] == w and ok
    for f in r["fields"]:
        f.pop("lines", None)
    return r, lines


def register_sections(rm: RmDoc, chapter: str) -> list[dict]:
    return [e for e in rm.entries if e["chapter"] == chapter and e.get("register") and e.get("regname")]


def expand_parametric(r: dict) -> list[dict]:
    """`BLK_SELx` with offset `0x060 + 0x4 * (x - 1), (x = 1 to 4)` -> SEL1..4; `BLK_CxCR` with
    `0x000 + 0x04 * x (x = 0 to 11)` -> C0CR..C11CR. Without a numeric range in the offset, one register."""
    m = re.search(r"\(\s*x\s*=\s*(\d+)\s*to\s*(\d+)\s*\)", r["offset_text"] or "")
    pm = list(re.finditer(r"(?<=[A-Z0-9_])x(?=[A-Z0-9_]|$)", r["name"]))
    if not (m and pm):
        return [dict(r, base=r["name"])]
    at = pm[-1].start()
    a, b = sorted((int(m.group(1)), int(m.group(2))))
    ot = r["offset_text"]
    f1 = re.search(r"(0x[0-9A-Fa-f]+)\s*\+\s*(0x[0-9A-Fa-f]+|\d+)\s*\*\s*\(\s*x\s*-\s*(\d+)\s*\)", ot)
    f2 = re.search(r"(0x[0-9A-Fa-f]+)\s*\+\s*(0x[0-9A-Fa-f]+|\d+)\s*\*\s*x\b", ot)
    f3 = re.search(r"^\s*(0x[0-9A-Fa-f]+|\d+)\s*\*\s*x\b", ot)
    out = []
    for x in range(a, b + 1):
        if f1:
            off = int(f1.group(1), 16) + int(f1.group(2), 0) * (x - int(f1.group(3)))
        elif f2:
            off = int(f2.group(1), 16) + int(f2.group(2), 0) * x
        elif f3:
            off = int(f3.group(1), 0) * x
        else:
            off = (r["offset"] or 0) + 4 * (x - a)
        out.append(dict(r, name=r["name"][:at] + str(x) + r["name"][at + 1:], base=r["name"], offset=off, x=x))
    return out


# --------------------------------------------------------------------------- availability

CELL_RE = re.compile(r"^(?:X(?:\(\d+\))?|-|—|Yes|No)$")


def _tokens(line: str) -> list[tuple[int, str]]:
    return [(m.start(), m.group(0)) for m in re.finditer(r"\S+", line)]


def parse_availability_lines(lines: list[str]) -> dict:
    """A 'features versus devices' table in layout text -> {columns, features: {label: {col: cell}}}."""
    toks = [_tokens(ln) for ln in lines]

    def cell_run(t: list) -> int:
        n = 0
        for _, s in reversed(t):
            if CELL_RE.match(s):
                n += 1
            else:
                break
        return n

    runs = [cell_run(t) for t in toks]
    data = [i for i, n in enumerate(runs) if n >= 2]
    if not data:
        return {"columns": [], "features": {}}
    k = max(set(runs[i] for i in data), key=lambda n: sum(1 for i in data if runs[i] == n))
    rows = [i for i in data if runs[i] == k]
    xs = [toks[rows[0]][-k + j][0] for j in range(k)]  # column positions, from the first full row
    first_cell = min(xs) - 2

    def header(t: list) -> bool:  # k tokens over the cell columns, none of them a cell
        return len(t) == k and t[0][0] >= first_cell - 10 and not any(CELL_RE.match(x) for _, x in t)

    head = [t for t in toks[:rows[0]] if header(t)]
    cols = ["".join(t[j][1] for t in head) for j in range(k)] if head else [f"col{j + 1}" for j in range(k)]

    def label_only(i: int) -> bool:
        return (0 <= i < len(toks) and bool(toks[i]) and runs[i] == 0
                and not re.fullmatch(r"\(\d+\)", lines[i].strip()) and not re.match(r"^\s*\d+\.\s", lines[i])
                and all(x < first_cell for x, _ in toks[i]))

    features: dict[str, dict] = {}
    used: set[int] = set()
    for i in rows:
        t = toks[i]
        label = lines[i][:t[-k][0]].strip()
        label = re.sub(r"\s*\(\d+\)\s*$", "", label)
        if not label or re.fullmatch(r"\(\d+\)", label):
            around = [j for j in (i - 1, i + 1) if label_only(j) and j not in used]
            used.update(around)
            label = " ".join(lines[j].strip() for j in around)
        elif label.endswith(",") and label_only(i + 1):
            label = f"{label} {lines[i + 1].strip()}"
            used.add(i + 1)
        if label:
            features[label] = dict(zip(cols, [s for _, s in t[-k:]]))
    return {"columns": cols, "features": features}


def part_matches(pattern: str, part: str) -> bool:
    """A printed part pattern against the part (the SVD's device name).

    Lower-case `x` is a wildcard and ends the literal prefix (`ABC12Dxx` -> `ABC12D`); `/` separates
    alternatives of the suffix, which the prefix already decides. A short pattern (a table column
    such as `12D`) matches the end of the part name."""
    p = pattern.split("/")[0]
    if "x" in p:
        p = p[:p.index("x")]
    p, part = p.upper(), part.upper()
    if not p:
        return False
    return part.startswith(p) or p.startswith(part) or (len(p) >= 3 and part.endswith(p))


def feature_names(label: str) -> list[str]:
    out = []
    for piece in re.split(r"\s*,\s*|\s+and\s+", label):
        w = piece.split()
        if w and re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", w[0]):
            out.append(w[0])
    return out


def availability(rm: RmDoc, part: str) -> dict:
    tab = next((t for t in rm.o.get("tables", []) if re.search(r"versus devices|availability", t["title"], re.I)), None)
    if tab is None:
        return {"table": None, "features": {}, "absent": [], "present": []}
    title = re.sub(r"\s*\(continued\)\s*$", "", tab["title"])
    cap = re.escape(title.split(".")[0] + ".")
    lines: list[str] = []
    pages = []
    for p in range(tab["page"], min(tab["page"] + 3, rm.n_pages) + 1):
        body = rm.body(p)
        idx = next((i for i, ln in enumerate(body) if re.search(cap, ln)), None)
        if idx is None:
            break
        pages.append(p)
        seg = body[idx + 1:]
        end = next((i for i, ln in enumerate(seg) if re.match(r"^\s*\d+\.\s+\S", ln)), len(seg))
        lines += seg[:end]
    res = parse_availability_lines(lines)
    col = next((c for c in res["columns"] if part_matches(c, part)), None)
    absent, present = [], []
    for label, cells in res["features"].items():
        v = cells.get(col) if col else None
        (absent if v in ("-", "—", "No") else present if v else []).append(label)
    return {"table": title, "pages": pages, "columns": res["columns"], "part": part, "part_column": col,
            "features": res["features"], "absent": absent, "present": present,
            "absent_names": sorted({n for a in absent for n in feature_names(a)}),
            "present_names": sorted({n for a in present for n in feature_names(a)})}


def availability_notes(text: str, part: str) -> list[dict]:
    """'only available in <parts>', 'not available in <parts>', 'reserved in <parts>' -> [{names, available, q}]."""
    out = []
    for sent in re.split(r"(?<=\.)\s+", text):
        for m in re.finditer(rf"(not\s+)?(?:only\s+)?available\s+(?:only\s+)?(?:in|on|for)\s+({PARTS_RE})", sent, re.I):
            parts = re.findall(PART_TOKEN, m.group(2))
            hit = any(part_matches(x, part) for x in parts)
            only = "only" in m.group(0).lower()
            if m.group(1):
                avail = False if hit else None
            else:
                avail = True if hit else (False if only else None)
            if avail is not None:
                out.append({"names": re.findall(r"\b([A-Z][A-Z0-9_]*\d[A-Z0-9_]*)\b", sent[:m.start()]),
                            "available": avail, "q": sent.strip()})
        for m in re.finditer(rf"reserved\s+(?:in|on)\s+({PARTS_RE})", sent, re.I):
            parts = re.findall(PART_TOKEN, m.group(1))
            if any(part_matches(x, part) for x in parts):
                out.append({"names": re.findall(r"\b([A-Z][A-Z0-9_]*\d[A-Z0-9_]*)\b", sent[:m.start()]),
                            "available": False, "q": sent.strip()})
    return out


# --------------------------------------------------------------------------- CMSIS header

def parse_header(path: Path) -> dict:
    pos: dict[str, tuple[int, int]] = {}
    msk: dict[str, tuple[int, int]] = {}
    defs: dict[str, tuple[str, int]] = {}
    types: dict[str, str] = {}
    irqs: dict[str, int] = {}
    for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        m = re.match(r"\s*#define\s+(\w+)_Pos\s+\((\d+)U?\)", line)
        if m:
            pos[m.group(1)] = (int(m.group(2)), n)
            continue
        m = re.match(r"\s*#define\s+(\w+)_Msk\s+\((0x[0-9A-Fa-f]+)U?L?\s*<<\s*(\w+)_Pos\)", line)
        if m:
            msk[m.group(1)] = (int(m.group(2), 16) << pos.get(m.group(3), (0, 0))[0], n)
            continue
        m = re.match(r"\s*#define\s+(\w+)\s+\(\(\s*(\w+)_TypeDef\s*\*\s*\)\s*(\w+)\s*\)", line)
        if m:
            types[m.group(1)] = m.group(2)
            continue
        m = re.match(r"\s*#define\s+(\w+_BASE)\s+(.+?)\s*(?:/\*.*)?$", line)
        if m:
            defs[m.group(1)] = (m.group(2), n)
            continue
        m = re.match(r"\s*(\w+)_IRQn\s*=\s*(-?\d+)", line)
        if m:
            irqs[m.group(1)] = int(m.group(2))
    bases: dict[str, tuple[int, int]] = {}

    def ev(name: str, depth: int = 0) -> int | None:
        if name in bases:
            return bases[name][0]
        if name not in defs or depth > 20:
            return None
        expr, line = defs[name]
        total = 0
        for sign, tok in re.findall(r"([+-]?)\s*([A-Za-z_]\w*|0x[0-9A-Fa-f]+|\d+)U?L?", expr.replace("(", " ").replace(")", " ")):
            if tok[0].isalpha() or tok[0] == "_":
                v = ev(tok, depth + 1)
                if v is None:
                    return None
            else:
                v = int(tok.rstrip("UL"), 0)
            total += -v if sign == "-" else v
        bases[name] = (total, line)
        return total

    for k in defs:
        ev(k)
    return {"file": str(path), "name": path.name, "pos": pos, "msk": msk, "types": types, "bases": bases, "irqs": irqs}


class Header:
    def __init__(self, h: dict | None):
        self.h = h

    def __bool__(self) -> bool:
        return self.h is not None

    def type_of(self, periph: str, fallback: str) -> str:
        if self.h and periph in self.h["types"]:
            return self.h["types"][periph]
        return fallback

    def field(self, typ: str, reg: str, name: str) -> tuple[str, int, int] | None:
        """(define, bit position, line) for TYPE_REG_FIELD, allowing a suffix (`..._STOP`)."""
        if not self.h:
            return None
        key = f"{typ}_{reg}_{name}"
        if key in self.h["pos"]:
            return (key, *self.h["pos"][key])
        for k, v in self.h["pos"].items():
            if k.startswith(key + "_"):
                return (k, *v)
        return None

    def at_bit(self, typ: str, reg: str, bit: int) -> list[tuple[str, int]]:
        if not self.h:
            return []
        pre = f"{typ}_{reg}_"
        out = []
        for k, (p, line) in self.h["pos"].items():
            if k.startswith(pre) and p == bit:
                out.append((k[len(pre):], line))
        return out

    def mask(self, typ: str, reg: str, name: str) -> tuple[int, int] | None:
        if not self.h:
            return None
        key = f"{typ}_{reg}_{name}"
        return self.h["msk"].get(key)

    def base(self, name: str) -> tuple[int, int] | None:
        if not self.h:
            return None
        return self.h["bases"].get(f"{name}_BASE")

    def has_reg(self, typ: str, reg: str) -> bool:
        return bool(self.h) and any(k.startswith(f"{typ}_{reg}_") for k in self.h["pos"])


# --------------------------------------------------------------------------- diff + adjudication

def _core(name: str) -> str:
    return name.split("_", 1)[1] if "_" in name else name


def match_svd_reg(svd_regs: dict[str, dict], rm_name: str, rmr: dict | None = None) -> dict | None:
    """The SVD register an RM register section describes: exact name, name without prefix, or one of
    the SVD's alternate views (`CR1_MODE_A`, `CR1_MODE_B`) chosen by bit-by-bit agreement."""
    if rm_name in svd_regs:
        return svd_regs[rm_name]
    core = _core(rm_name)
    same = [r for n, r in svd_regs.items() if n == core or _core(n) == core]
    if len(same) == 1:
        return same[0]
    alts = same or [r for n, r in svd_regs.items()
                    if n.startswith(core + "_") or _core(n).startswith(core + "_") or n.startswith(rm_name + "_")]
    if not alts:
        return None
    return max(alts, key=lambda r: (agreement(rmr, r) if rmr else 0, -len(r["name"])))


def agreement(rmr: dict, sreg: dict) -> int:
    """Bits on which an RM register section and an SVD register agree (same name, or both reserved)."""
    sn, _ = _svd_bits(sreg)
    n = 0
    for b in range(rmr.get("width", 32)):
        rmn = rmr["bitname"][b]
        al = (rmr.get("bitalias") or [None] * 32)[b]
        if rmn in (None, "Reserved"):
            n += sn[b] is None
        elif sn[b] and (_strip_idx(rmn) == sn[b] or al == sn[b]):
            n += 1
    return n


def _equiv(rm_name: str, svd_name: str) -> bool:
    a = _strip_idx(rm_name)
    if a == svd_name:
        return True
    m = re.match(r"^([A-Z0-9_]*[A-Z_])(?=[a-z])", a)  # parametric: SELm+3, RTx
    return bool(m and svd_name.startswith(m.group(1)) and re.search(r"[a-z]", a))


# ---- field names: by bit range first, then by name

PARAM_RE = re.compile(r"^([A-Za-z0-9_]*?[A-Z0-9_])([a-z])([+-]\d+)?(?:\[(\d+):(\d+)\])?$")  # MODEy[1:0], SELm+3[7:0], IMx
VEC_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_]*?)\[(\d+):(\d+)\]$")                          # CH[18:0]


def _var_defs(text: str) -> dict[str, tuple]:
    """Index variables a field text defines: `(y = 15 to 0)` -> ("range", 15, 0); `(m = 4 * (x - 1))`
    -> ("expr", "4 * (x - 1)")."""
    out: dict[str, tuple] = {}
    for m in re.finditer(r"\(\s*([a-z])\s*=\s*", text or ""):
        depth, i = 1, m.end()
        while i < len(text) and depth:
            depth += {"(": 1, ")": -1}.get(text[i], 0)
            i += 1
        expr = text[m.end():i - 1].strip() if depth == 0 else ""
        r = re.fullmatch(r"(\d+)\s*to\s*(\d+)", expr)
        if r:
            out.setdefault(m.group(1), ("range", int(r.group(1)), int(r.group(2))))
        elif expr and re.fullmatch(r"[\dx\s+*()-]+", expr):
            out.setdefault(m.group(1), ("expr", expr))
    return out


def _eval_index(expr: str, x: int) -> int | None:
    """`4 * (x - 1)` at x: integer arithmetic over x only."""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return None
    ok = (ast.Expression, ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.UnaryOp, ast.USub, ast.Constant, ast.Name, ast.Load)
    for n in ast.walk(tree):
        if not isinstance(n, ok) or (isinstance(n, ast.Name) and n.id != "x") or \
                (isinstance(n, ast.Constant) and not isinstance(n.value, int)):
            return None
    return eval(compile(tree, "<index>", "eval"), {"__builtins__": {}}, {"x": x})


def rm_elements(f: dict, defs: dict, x: int | None = None) -> list[tuple[str, int, int, bool]]:
    """(name, hi, lo, resolved) for the named pieces of one RM field: the field itself, then each
    element of a vector or parametric field.

    `CH[18:0]` over 18:0 -> CH, then CH0..CH18 one bit each; `MODEy[1:0]` over 31:0 with
    `(y = 15 to 0)` -> MODE0 at 1:0 .. MODE15 at 31:30; `SELm+3[7:0]` with `(m = 4 * (x - 1))`
    in register instance x = 2 -> SEL7. A parametric name the text does not resolve stays
    parametric (resolved False): it compares by its stem only."""
    name, hi, lo = f["name"], f["hi"], f["lo"]
    n = hi - lo + 1
    m = PARAM_RE.match(name)
    if m:
        stem, var, off, bh, bl = m.groups()
        off = int(off or 0)
        w = int(bh) - int(bl) + 1 if bh is not None else 1
        d = defs.get(var)
        if d and d[0] == "range":
            a, b = sorted(d[1:])
            if n == w * (b - a + 1):
                return [(stem + var, hi, lo, False)] + [
                    (f"{stem}{a + k + off}", lo + k * w + w - 1, lo + k * w, True) for k in range(b - a + 1)]
        elif d and d[0] == "expr" and x is not None and n == w:
            v = _eval_index(d[1], x)
            if v is not None:
                return [(f"{stem}{v + off}", hi, lo, True)]
        return [(_strip_idx(name), hi, lo, False)]
    out = [(_strip_idx(name), hi, lo, True)]
    v = VEC_RE.match(name)
    # per-bit elements only for a vector whose base ends in a letter: SQ1[3:0] is one 4-bit field
    if v and hi > lo and not v.group(1)[-1].isdigit() and abs(int(v.group(2)) - int(v.group(3))) == hi - lo:
        first = min(int(v.group(2)), int(v.group(3)))
        out += [(f"{v.group(1)}{first + k}", lo + k, lo + k, True) for k in range(n)]
    return out


def _split_index(name: str) -> tuple[str, str | None]:
    """`MODER15` -> (MODER, 15); `SEL24_31` -> (SEL, 24_31); `CRCEN` -> (CRCEN, None)."""
    m = re.fullmatch(r"(.*?[A-Za-z_])(\d+(?:_\d+)?)", name)
    return (m.group(1), m.group(2)) if m else (name, None)


def _param_stem(name: str) -> str | None:
    m = PARAM_RE.match(name)
    return m.group(1) if m else None


def _same_name(rm_name: str, resolved: bool, svd_name: str) -> bool:
    if resolved:
        return rm_name == svd_name
    stem = _param_stem(rm_name) or rm_name   # a parametric name: its stem against the SVD name's
    return stem == _split_index(svd_name)[0].rstrip("_")


def name_style(rm_name: str, svd_name: str, cores: set[str], hi: int, lo: int) -> str | None:
    """How a name difference is shaped: `family` (the SVD names each field after its register:
    MODERy for the RM's MODEy, IDRy for IDy), `bit-range` (an indexed field numbered by its bits
    in the SVD, SEL24_31, and by its line in the RM, SEL3), else None."""
    rs, ri = _split_index(rm_name)
    ss, si = _split_index(svd_name)
    if rs == ss and ri is not None and si is not None and "_" in si and "_" not in ri:
        a, b = map(int, si.split("_"))
        if (min(a, b), max(a, b)) == (lo, hi):
            return "bit-range"
    if ri is not None and ri == si and rs != ss and (ss.startswith(rs) or rs.startswith(ss)) and (ss in cores or rs in cores):
        return "family"
    return None


def _style_note(style: str | None, rm_names: list[str], svd_names: list[str]) -> str:
    if style == "family":
        rs, ss = _split_index(rm_names[0])[0], _split_index(svd_names[0])[0]
        return f" (family naming: the SVD names each field after its register, {ss}y for the RM's {rs}y)"
    if style == "bit-range":
        return (f" (indexed-field naming split: the SVD numbers the fields by bit range, {svd_names[0]}, "
                f"the RM by line, {rm_names[0]})")
    return ""


def _svd_bits(reg: dict) -> tuple[list, list]:
    names: list[str | None] = [None] * 32
    acc: list[str | None] = [None] * 32
    for f in reg["fields"].values():
        hi, lo = f["bits"]
        for b in range(lo, hi + 1):
            if 0 <= b < 32:
                names[b] = f["name"]
                acc[b] = SVD_COARSE.get(f.get("access") or reg.get("access") or "", f.get("access"))
    return names, acc


def _rng(bits: list[int]) -> list[int]:
    return [max(bits), min(bits)]


def _svd_reset(sreg: dict) -> tuple[int, int]:
    """(value, defined-bits mask) of an SVD register's reset. A register reset of null means the SVD
    defines none, or leaves some bits to its resetMask; then only the fields that carry a reset count."""
    if sreg.get("reset"):
        return int(sreg["reset"], 16), (1 << 64) - 1
    v = m = 0
    for f in (sreg.get("fields") or {}).values():
        if f.get("reset") is None or not f.get("bits"):
            continue
        hi, lo = max(f["bits"]), min(f["bits"])
        m |= ((1 << (hi - lo + 1)) - 1) << lo
        v |= int(f["reset"], 0) << lo
    return v, m


def diff_register(rmr: dict, sreg: dict) -> list[dict]:
    """Differences between one RM register section (expanded) and its SVD register, grouped."""
    out: list[dict] = []
    s_off = int(sreg["off"], 16)
    s_reset, s_def = _svd_reset(sreg)
    if rmr.get("offset") is not None and rmr["offset"] != s_off:
        out.append({"kind": "offset differs", "detail": f"RM {rmr['offset']:#x} / SVD {s_off:#x}", "bits": None,
                    "rm": f"{rmr['offset']:#x}", "svd": f"{s_off:#x}", "q": rmr.get("q_offset")})
    if rmr.get("reset") is not None and not rmr.get("reset_ambiguous") and s_def:
        m = (rmr.get("reset_mask") or 0xFFFFFFFF) & ((1 << rmr.get("width", 32)) - 1) & s_def
        if (rmr["reset"] & m) != (s_reset & m):
            out.append({"kind": "reset differs", "detail": f"RM {rmr['reset']:#010x} / SVD {s_reset:#010x}", "bits": None,
                        "rm": rmr["reset_text"], "svd": f"{s_reset:#010x}", "q": rmr.get("q_reset")})
    w = rmr.get("width", 32)
    if rmr["coverage"] != w:
        return out
    bn, bd, ba = rmr["bitname"], rmr["bitdecl"], rmr["bitacc"]
    al = rmr.get("bitalias") or [None] * 32
    sn, sa = _svd_bits(sreg)
    strict = _names_by_range(rmr, sreg, bn)
    groups: dict[tuple, list[int]] = {}
    for b in range(w):
        rmn = bn[b]
        rmres = rmn is None or rmn == "Reserved"
        if rmres and sn[b]:
            groups.setdefault(("SVD defines a bit RM reserves", sn[b]), []).append(b)
        elif not rmres and not sn[b]:
            groups.setdefault(("RM defines a bit SVD omits", _strip_idx(bd[b])), []).append(b)
        elif not rmres and sn[b] and b in strict:  # an SVD field whose bit range is an RM field's or element's
            if strict[b] is not None:
                decl = bd[b] or ""
                groups.setdefault(("name differs", _param_stem(_strip_idx(decl)) or _strip_idx(decl)), []).append(b)
        elif not rmres and sn[b] and not _equiv(rmn, sn[b]) and al[b] != sn[b] and not _suffix_equiv(rmr, b, sn[b]):
            groups.setdefault(("name differs", _strip_idx(bd[b])), []).append(b)
        if not rmres and sn[b] and ba[b] and RM_COARSE.get(ba[b], ba[b]) != sa[b]:
            groups.setdefault(("access differs", f"{_strip_idx(rmn)} RM {ba[b]} / SVD {sa[b]}"), []).append(b)
    for (kind, key), bits in groups.items():
        # a name difference found by bit range names the RM's element (MODE5), not its declaration (MODEy)
        rmname = {b: (strict[b][0] if kind == "name differs" and strict.get(b) else _strip_idx(bn[b]))
                  for b in bits if bn[b]}
        rm_names = sorted(set(rmname.values()), key=_natural)
        svd_names = sorted({sn[b] for b in bits if sn[b]}, key=_natural)
        style = None
        if kind == "name differs":
            styles = {strict[b][1] for b in bits if strict.get(b)}
            style = styles.pop() if len(styles) == 1 and all(strict.get(b) for b in bits) else None
            detail = f"RM {', '.join(rm_names)} / SVD {', '.join(svd_names)}" + _style_note(style, rm_names, svd_names)
            extra = (rm_names, svd_names)
        elif kind == "access differs":
            detail, extra = key, (ba[bits[0]], sa[bits[0]])
        else:
            detail, extra = key, None
        out.append({"kind": kind, "detail": detail, "bits": _rng(bits), "bitlist": sorted(bits), "extra": extra,
                    "rm_names": rm_names, "rm_aliases": sorted({al[b] for b in bits if al[b]}),
                    "bit_names": rmname,
                    "bit_aliases": {b: al[b] for b in bits if al[b]},
                    "svd_bit_names": {b: sn[b] for b in bits if sn[b]}, "svd_names": svd_names,
                    **({"style": style} if style else {})})
    return out


def _names_by_range(rmr: dict, sreg: dict, bn: list) -> dict[int, tuple[str, str | None] | None]:
    """bit -> None (same name) or (RM name, style) for every bit of an SVD field whose bit range is
    exactly an RM field's or one element's (rm_elements): those compare by name, strictly, so a
    MODEy/MODERy or ID/IDR difference is a finding. Bits of SVD fields that line up with no RM
    piece are left to the per-bit comparison."""
    x = rmr.get("x")
    defs_all: dict[str, tuple] = {}
    for f in rmr["fields"]:
        for k, v in _var_defs(f.get("text", "")).items():
            defs_all.setdefault(k, v)
    pieces: dict[tuple[int, int], tuple[str, bool]] = {}
    for f in rmr["fields"]:
        if f["name"] == "Reserved":
            continue
        for nm, h, l, ok in rm_elements(f, {**defs_all, **_var_defs(f.get("text", ""))}, x):
            pieces.setdefault((h, l), (nm, ok))
    cores = {_core(rmr["name"]), _core(sreg["name"]), sreg["name"]}
    out: dict[int, tuple[str, str | None] | None] = {}
    for sf in sreg["fields"].values():
        h, l = sf["bits"]
        hit = pieces.get((h, l))
        if hit is None or any(not 0 <= b < 32 or bn[b] in (None, "Reserved") for b in range(l, h + 1)):
            continue
        nm, ok = hit
        whole = _strip_idx(_field_of_bit(rmr, l)["name"])
        # the SVD keeping the RM field's own name on part of its bits is a width question (the
        # per-bit comparison reports the bits it lacks), not a naming one
        same = _same_name(nm, ok, sf["name"]) or sf["name"] == whole
        res = None if same else (nm, name_style(nm, sf["name"], cores, h, l))
        for b in range(l, h + 1):
            out[b] = res
    return out


def _natural(s: str) -> tuple:
    return tuple(int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s))


def _suffix_equiv(rmr: dict, b: int, svd_name: str) -> bool:
    """RM `ID[15:0]` vs SVD per-bit `IDR5` at bit 5: the SVD splits a vector into bits named <prefix><index>."""
    f = _field_of_bit(rmr, b)
    if not f or f["hi"] == f["lo"]:
        return False
    m = re.fullmatch(r"([A-Za-z][A-Za-z0-9_]*?)(?:\[(\d+):(\d+)\])?", f["name"])
    if not m:
        return False
    base, lo_idx = m.group(1), int(m.group(3)) if m.group(3) else 0
    k = b - f["lo"] + lo_idx
    sm = re.fullmatch(r"(.*?)(\d+)", svd_name)
    if not sm or int(sm.group(2)) != k:
        return False
    pre = sm.group(1).rstrip("_")
    return bool(pre) and (pre.startswith(base) or base.startswith(pre))


def _field_of_bit(rmr: dict, bit: int) -> dict | None:
    return next((f for f in rmr["fields"] if f["lo"] <= bit <= f["hi"]), None)


def _covers(hdr: "Header", typ: str, reg: str, name: str, bits: list[int], at_lsb: bool = False) -> tuple | None:
    """(define, line) when the header's TYPE_REG_NAME mask (or position) covers every bit. `at_lsb`
    (a naming vote): a define whose position is the lowest bit is enough, even with a narrower mask
    (a header masking only the valid codes of an 8-bit selector)."""
    mk = hdr.mask(typ, reg, name)
    h = hdr.field(typ, reg, name)
    if mk and all((mk[0] >> b) & 1 for b in bits):
        return (f"{typ}_{reg}_{name}_Msk", mk[1])
    if h and h[1] == min(bits) and (at_lsb or (len(bits) == 1 and not mk)):
        return (h[0] + "_Pos", h[2])
    return None


def adjudicate(d: dict, rmr: dict, sreg: dict, typ: str, hdr: Header, part: str, avail: dict) -> tuple[str, list[dict], str]:
    """-> (verdict, header sources, reason)."""
    reg = _core(rmr["name"])
    kind = d["kind"]
    hsrc: list[dict] = []
    silent = "header silent" if hdr else "no header given"
    if kind == "RM defines a bit SVD omits":
        f = _field_of_bit(rmr, d["bits"][1])
        text = (f or {}).get("text", "") + " " + rmr.get("title", "")
        notes = availability_notes(text, part)
        names = set(d["rm_names"]) | set(d.get("rm_aliases") or [])
        verdict_av = None
        for n in notes:
            if not n["names"] or names & set(n["names"]):
                verdict_av = n
        if verdict_av and verdict_av["available"] is False:
            return "correct-omission", hsrc, f"RM: {verdict_av['q']}"
        absent = [a for a in avail.get("absent_names", []) if any(a in nm for nm in d["rm_names"])]
        if absent and not (verdict_av and verdict_av["available"]):
            return "correct-omission", hsrc, f"RM {avail.get('table')} marks {', '.join(absent)} absent on {part}"
        # the header's field covers the bits (same name, or the per-bit spelling of a vector field)
        by_name: dict[str, list[int]] = {}
        for b, nm in d["bit_names"].items():
            by_name.setdefault(nm, []).append(b)
        by_alias: dict[str, list[int]] = {}
        for b, nm in d.get("bit_aliases", {}).items():
            by_alias.setdefault(nm, []).append(b)
        cov = {nm: _covers(hdr, typ, reg, nm, bs) for nm, bs in by_name.items()}
        if not all(cov.values()) and by_alias:
            cov = {nm: _covers(hdr, typ, reg, nm, bs) for nm, bs in by_alias.items()}
        for nm, c in cov.items():
            if c:
                hsrc.append({"define": c[0], "says": f"{nm} is defined there", "line": c[1]})
        if cov and all(cov.values()):
            return "svd-defect", hsrc, "RM and header define the bits"
        if verdict_av and verdict_av["available"]:
            return "svd-defect-candidate", hsrc, f"RM: {verdict_av['q']}; {silent}"
        return "unresolved", hsrc, silent
    if kind == "name differs":
        rm_names, svd_names = d["extra"]

        spans: dict[str, list[int]] = {}

        def side(bitmap: dict) -> dict:
            by: dict[str, list[int]] = {}
            for bt, nm in bitmap.items():
                by.setdefault(nm, []).append(bt)
            spans.update(by)
            return {nm: _covers(hdr, typ, reg, nm, bs, at_lsb=True) for nm, bs in by.items()}

        ca = side(d["bit_names"])
        if not all(ca.values()) and d.get("bit_aliases"):
            ca = side(d["bit_aliases"])
        cb = side(d["svd_bit_names"])
        rm_ok, svd_ok = bool(ca) and all(ca.values()), bool(cb) and all(cb.values())
        for nm, c in list(ca.items()) + list(cb.items()):
            if c:
                bs = spans.get(nm) or d["bitlist"]
                hsrc.append({"define": c[0], "says": f"{nm} at bit(s) {max(bs)}:{min(bs)}", "line": c[1]})
        if rm_ok and not svd_ok:
            return "svd-naming-defect", hsrc, "header uses the RM name"
        if svd_ok and not rm_ok:
            return "rm-naming-differs", hsrc, "header uses the SVD name"
        if rm_ok and svd_ok:
            return "alternate-view", hsrc, "header defines both names at these bits (two views of one register)"
        a, b = rm_names[0], svd_names[0]
        at = hdr.at_bit(typ, reg, d["bits"][1])
        for nm, line in at:
            hsrc.append({"define": f"{typ}_{reg}_{nm}_Pos", "says": f"{nm} at bit {d['bits'][1]}", "line": line})
        num = lambda x: (re.findall(r"\d+$", x) or [""])[0]
        if len(rm_names) == 1 and len(svd_names) == 1 and at and any(num(nm) == num(a) != num(b) for nm, _ in at):
            return "svd-naming-defect", hsrc, (f"header's name at this bit ({', '.join(n for n, _ in at)}) is also "
                                               f"wrong, but its numbering agrees with the RM's {a}")
        return "unresolved", hsrc, "neither name in the header" if hdr else "no header given"
    if kind == "SVD defines a bit RM reserves":
        nm = d["svd_names"][0]
        mk = hdr.mask(typ, reg, nm)
        if mk:
            hsrc.append({"define": f"{typ}_{reg}_{nm}_Msk", "says": f"mask 0x{mk[0]:08X}", "line": mk[1]})
            if not all((mk[0] >> b) & 1 for b in d["bitlist"]):
                return "svd-defect", hsrc, "header mask excludes the bits"
            return "rm-differs", hsrc, "header mask agrees with the SVD"
        return "unresolved", hsrc, silent
    if kind == "reset differs":
        return "svd-defect-candidate", hsrc, "header carries no reset values; the RM is the only other source"
    if kind == "access differs":
        return "svd-defect-candidate", hsrc, "header carries no access types; the RM is the only other source"
    if kind == "offset differs":
        return "svd-defect-candidate", hsrc, "offsets are not voted by the header"
    return "unresolved", hsrc, ""


# --------------------------------------------------------------------------- constraint sentences -> rule records

def _paragraphs(lines: list[tuple[int, str]], titled: bool = False) -> list[tuple[list[int], str]]:
    """The field's description lines grouped into paragraphs, with the pages each spans."""
    paras: list[tuple[list[int], list[str]]] = []
    for k, (p, t) in enumerate(lines):
        # the field's title (the rest of its `Bit N NAME:` line) stands alone
        if (re.match(r"^(?:\d+|0x[0-9A-Fa-f]+|[01]{2,}|Other|Others)\s*:", t) or t.startswith("Note:") or not paras
                or (k == 1 and titled)):
            paras.append(([p], [t]))
        else:
            paras[-1][0].append(p)
            paras[-1][1].append(t)
    return [(sorted(set(ps)), " ".join(ts)) for ps, ts in paras]


def constraint_sentences(lines: list[tuple[int, str]], titled: bool = False) -> list[tuple[list[int], str]]:
    """Sentences of a field description that constrain use ('only when', 'must', 'Note: ...'), with pages."""
    out = []
    for pages, para in _paragraphs(lines, titled):
        para = re.sub(r"(\w)- (\w)", r"\1-\2", para)
        for sent in re.split(r"(?<=[.;])\s+(?=[A-Z(])", para):
            s = " ".join(sent.split())
            if re.match(r"^(?:\d+|0x[0-9A-Fa-f]+|[01]{2,})\s*:", s) and not CONSTRAINT_RE.search(s):
                continue
            if s.startswith("Note:") or CONSTRAINT_RE.search(s):
                out.append((pages, s))
    return out


def field_lines(lines: list[tuple[int, str]], f: dict) -> tuple[list[tuple[int, str]], bool]:
    """The description lines of one RM field, from its register section's (page, line)s,
    and whether the first one is the field's title (the rest of its `Bit N NAME:` line)."""
    out, on, titled = [], False, False
    for p, ln in lines:
        m = BIT_RE.match(ln)
        if m:
            if on:
                break
            on = m.group(3) == f["name"] and int(m.group(1)) == f["hi"]
            if on and m.group(5).strip():
                out.append((p, m.group(5).strip()))
                titled = True
            continue
        if on and ln.strip():
            out.append((p, ln.strip()))
    return out, titled


# --------------------------------------------------------------------------- the vote

def _replace(path: Path, prefixes: list[str], rows: list[dict]) -> int:
    keep = [r for r in read_jsonl(path) if not any(r.get("id", "").startswith(p) for p in prefixes)] if path.exists() else []
    allr = sorted(keep + rows, key=lambda r: r["id"])
    write_jsonl(path, allr)
    return len(allr)


def run_vote(rm: RmDoc, svd: SvdDoc, hdr: Header, only: list[str] | None = None) -> dict:
    part = svd.device or ""
    av = availability(rm, part)
    rmslug = rm.doc_id.split("@")[0]
    conflicts, quirks, rules, per_periph, parsed = [], [], [], {}, []
    seen_ids: set[str] = set()

    def uid(i: str) -> str:
        n, base = 2, i
        while i in seen_ids:
            i = f"{base}-{n}"
            n += 1
        seen_ids.add(i)
        return i

    chapters = [c["id"] for c in rm.chapters()]
    mapped: dict[str, list[str]] = {c: [] for c in chapters}
    for name in svd.definitions():
        ch, _ = map_chapter(name, rm, svd)
        if ch:
            mapped[ch].append(name)
    for ch in chapters:
        secs = register_sections(rm, ch)
        if not secs:
            continue
        cands = mapped[ch]
        for e in secs:
            rmr, _lines = parse_register_section(rm, e)
            parsed.append({k: v for k, v in rmr.items() if k not in ("bitname", "bitdecl", "bitacc")})
            # which SVD instance holds this register: an instance named by the RM prefix (`<INST>_REG`),
            # else the chapter's definition named in the parent title, else the best bit agreement
            prefix = rmr["name"].split("_")[0]
            if prefix in svd.peripherals and svd.base_of(prefix) in cands:
                pick = [prefix]
            else:
                parent = rm.by_id.get(e["parent"]) or {}
                pick = [c for c in cands if re.search(rf"\b{re.escape(c)}\b", parent.get("title", ""))] or cands
            first = expand_parametric(rmr)[0]["name"]
            hits = [(c, match_svd_reg(svd.regs.get(c, {}), first, rmr)) for c in pick]
            hits = [(c, r) for c, r in hits if r]
            svd_p = max(hits, key=lambda h: agreement(rmr, h[1]))[0] if hits else (
                pick[0] if pick else None)
            svd_base = svd.base_of(svd_p) if svd_p else None
            if only and svd_p not in only and svd_base not in only:
                continue
            stats = per_periph.setdefault(svd_base or f"ch{ch}", {"chapter": ch, "rm_registers": 0, "matched": 0,
                                                                  "parse_failed": [], "missing_in_svd": [], "diffs": 0,
                                                                  "verdicts": {}, "svd_only": []})
            if not rmr["parse_ok"]:
                stats["parse_failed"].append(f"{rmr['name']} p.{rmr['page']} (bits {rmr['coverage']}/{rmr['width']}, "
                                             f"access {'ok' if rmr['access_ok'] else 'fail'})")
            typ_fallback = re.sub(r"x$", "", rmr["name"].split("_")[0])
            typ = hdr.type_of(svd_p, typ_fallback) if svd_p else typ_fallback
            # constraint sentences -> rules (all parsed sections, mapped or not)
            for inst in expand_parametric(rmr)[:1]:
                sreg = match_svd_reg(svd.regs.get(svd_p, {}), inst["name"], inst) if svd_p else None
                for f in rmr["fields"]:
                    if f["name"] == "Reserved":
                        continue
                    fname = _strip_idx(f["name"])
                    sf = None
                    if sreg:
                        sf = sreg["fields"].get(fname) or next(
                            (x for x in sreg["fields"].values() if x["bits"] == [f["hi"], f["lo"]]), None)
                    reg_name = sreg["name"] if sreg else rmr["name"]
                    field_name = sf["name"] if sf else fname
                    for pages, sent in constraint_sentences(*field_lines(_lines, f)):
                        per = svd_p or (rm.by_id[ch].get("abbrevs") or [f"ch{ch}"])[0]
                        rid = uid(f"{rmslug}.rule.reg.{per}.{reg_name}.{field_name}.{slug(' '.join(sent.split()[:7]))[:48]}")
                        rules.append({
                            "id": rid, "kind": "rule", "doc": rm.doc_id, "p": pages[0] if len(pages) == 1 else pages,
                            "q": sent, "src": "text", "prov": "deterministic",
                            "note": f"§{e['id']}; " + (f"SVD {sf['id']}" if sf else "not in the SVD"),
                            "text": sent, "cond": None,
                        })
            for inst in expand_parametric(rmr):
                stats["rm_registers"] += 1
                sreg = match_svd_reg(svd.regs.get(svd_p, {}), inst["name"], inst) if svd_p else None
                if sreg is None:
                    notes = availability_notes(rmr.get("title", "") + " " + " ".join(
                        ln for _, ln in _lines[:12]), part)
                    reason = next((n["q"] for n in notes if n["available"] is False), None)
                    item = f"{inst['name']} p.{rmr['page']}"
                    if reason:
                        quirks.append(_quirk(svd, rm, uid, svd_p or ch, inst["name"], None, rmr, reason))
                        stats["verdicts"]["correct-omission"] = stats["verdicts"].get("correct-omission", 0) + 1
                    else:
                        stats["missing_in_svd"].append(item)
                        if svd_p and hdr.has_reg(typ, _core(inst["name"])):
                            conflicts.append(_conflict(svd, rm, uid, svd_p, inst["name"], None, rmr,
                                                       {"kind": "register missing from SVD", "detail": inst["name"], "bits": None},
                                                       "svd-defect", [{"define": f"{typ}_{_core(inst['name'])}_*", "says": "bit defines exist", "line": None}],
                                                       "header defines its bits", hdr))
                            stats["diffs"] += 1
                            stats["verdicts"]["svd-defect"] = stats["verdicts"].get("svd-defect", 0) + 1
                    continue
                stats["matched"] += 1
                if svd_p == svd_base:
                    stats.setdefault("_seen", set()).add(sreg["name"])
                for d in diff_register(inst, sreg):
                    verdict, hsrc, reason = adjudicate(d, inst, sreg, typ, hdr, part, av)
                    stats["diffs"] += 1
                    stats["verdicts"][verdict] = stats["verdicts"].get(verdict, 0) + 1
                    if verdict == "correct-omission":
                        quirks.append(_quirk(svd, rm, uid, svd_p, sreg["name"], d, inst, reason))
                    else:
                        conflicts.append(_conflict(svd, rm, uid, svd_p, sreg["name"], d.get("rm_names"), inst, d,
                                                   verdict, hsrc, reason, hdr))
    for p, st in per_periph.items():
        seen = st.pop("_seen", set())
        if p in svd.regs:
            st["svd_only"] = sorted(n for n in svd.regs[p] if n not in seen)
    # peripheral level: RM chapters with register sections and no SVD peripheral
    periph_conf = []
    if not only:
        for ch in chapters:
            if mapped[ch] or not register_sections(rm, ch):
                continue
            chap = rm.by_id[ch]
            names = chap.get("abbrevs") or []
            if not names:
                continue
            name = names[0]
            in_absent = name in av.get("absent_names", [])
            in_present = name in av.get("present_names", [])
            hb = hdr.base(name)
            if in_absent:
                quirks.append({
                    "id": uid(f"{svd.part}.quirk.variant.periph.{name}"), "kind": "quirk", "doc": rm.doc_id,
                    "p": av["pages"][0] if av.get("pages") else chap["start"], "q": None, "src": "table",
                    "prov": "deterministic", "note": f"RM chapter {ch} ({chap['title']}) describes it",
                    "class": "variant", "text": f"{name}: in the RM (ch. {ch}) but absent on {part} per {av.get('table')}; "
                                                f"the SVD correctly has no {name}", "refs": []})
                continue
            if in_present or hb:
                srcs = [f"{svd.doc_id}: no {name} peripheral",
                        f"{rm.doc_id} p{chap['start']}: chapter {ch} {chap['title']}, pages {chap['start']}-{chap['end']}"]
                if in_present:
                    srcs.append(f"{rm.doc_id} p{av['pages'][0]}: {av['table']}: {name} present on {part}")
                if hb:
                    srcs.append(f"{hdr.h['name']} L{hb[1]}: {name}_BASE = 0x{hb[0]:08X}")
                verdict = "svd-defect" if (in_present and hb) else "svd-defect-candidate"
                periph_conf.append({
                    "id": uid(f"{svd.part}.conflict.periph.{name}"), "kind": "conflict", "doc": rm.doc_id,
                    "p": chap["start"], "q": None, "src": "text", "prov": "deterministic",
                    "note": f"{verdict}: " + ("RM table and header agree" if verdict == "svd-defect" else "one other source"),
                    "refs": [], "sources": srcs, "_verdict": verdict, "_periph": name, "_diff": "peripheral missing from SVD",
                    "text": f"{name} is missing from the SVD" + (f" (header base 0x{hb[0]:08X})" if hb else ""),
                })
    return {"availability": av, "conflicts": conflicts + periph_conf, "quirks": quirks, "rules": rules,
            "per_periph": per_periph, "parsed": parsed, "only": only}


def _svd_ref(svd: SvdDoc, per: str, reg: str, names: list[str] | None) -> list[str]:
    """SVD record ids a vote record refs: only ids that exist (a register the SVD lacks has none;
    doc check R resolves every ref against docs/)."""
    base = f"{svd.part}.reg.{per}.{reg}"
    r = svd.regs.get(per, {}).get(reg)
    out = [base] if r else []
    for n in names or []:
        if r and n in r["fields"]:
            out.append(f"{base}.{n}")
    return out


def _conflict(svd, rm, uid, per, reg, names, rmr, d, verdict, hsrc, reason, hdr) -> dict:
    f = _field_of_bit(rmr, d["bits"][1]) if d.get("bits") else None
    q = (f or {}).get("q") if d["kind"] not in ("reset differs", "offset differs") else d.get("q")
    page = (f or {}).get("page") or rmr["page"]
    detail = d["detail"]
    dslug = slug({"RM defines a bit SVD omits": "bit-omitted", "SVD defines a bit RM reserves": "bit-extra",
                  "name differs": "name", "access differs": "access", "reset differs": "reset",
                  "offset differs": "offset"}.get(d["kind"], d["kind"]))
    tag = "-".join((names or [])[:3]) or (d.get("svd_names") or [""])[0]
    srcs = [f"{svd.doc_id} {per}.{reg}: {_svd_says(d)}",
            f"{rm.doc_id} p{page} §{rmr['section']}: {_rm_says(d)}"]
    if hdr:
        for h in hsrc:
            at = f" L{h['line']}" if h.get("line") else ""
            srcs.append(f"{hdr.h['name']}{at} {h.get('define', '')}: {h.get('says', '')}")
    return {
        "id": uid(f"{svd.part}.conflict.reg.{per}.{reg}.{dslug}" + (f".{tag}" if tag else "")),
        "kind": "conflict", "doc": rm.doc_id, "p": page, "q": q, "src": "text", "prov": "deterministic",
        "note": f"{verdict}: {reason}" + (f"; {rmr['rm_typo']}" if rmr.get("rm_typo") and d["kind"] == "reset differs" else ""),
        "refs": _svd_ref(svd, per, reg, names or d.get("svd_names")), "sources": srcs,
        "text": f"{per}.{reg}: {d['kind']}: {detail}" + (f" [bits {d['bits'][0]}:{d['bits'][1]}]" if d.get("bits") else ""),
        "_verdict": verdict, "_periph": per, "_reg": reg, "_bits": d.get("bits"), "_diff": d["kind"],
    }


def _svd_says(d: dict) -> str:
    k = d["kind"]
    if k == "RM defines a bit SVD omits":
        return "no field at these bits"
    if k == "SVD defines a bit RM reserves":
        return f"{', '.join(d['svd_names'])} covers the bits"
    if k == "name differs":
        return f"named {', '.join(d['extra'][1])}"
    if k == "access differs":
        return f"access {d['extra'][1]}"
    return d.get("svd", "")


def _rm_says(d: dict) -> str:
    k = d["kind"]
    if k == "RM defines a bit SVD omits":
        return f"defines {', '.join(d['rm_names'])}"
    if k == "SVD defines a bit RM reserves":
        return "reserved"
    if k == "name differs":
        return f"named {', '.join(d['extra'][0])}"
    if k == "access differs":
        return f"access {d['extra'][0]}"
    return d.get("rm", "") or ""


def _quirk(svd, rm, uid, per, reg, d, rmr, reason) -> dict:
    names = (d or {}).get("rm_names") or []
    f = _field_of_bit(rmr, d["bits"][1]) if d and d.get("bits") else None
    tag = "-".join(names[:3]) if names else "register"
    return {
        "id": uid(f"{svd.part}.quirk.variant.{per}.{reg}.{tag}"), "kind": "quirk", "doc": rm.doc_id,
        "p": (f or {}).get("page") or rmr["page"], "q": (f or {}).get("q"), "src": "text", "prov": "deterministic",
        "note": f"§{rmr['section']}", "class": "variant",
        "text": f"{per}.{reg}: the RM (a family superset) defines {', '.join(names) or rmr['name']}; "
                f"not on {svd.device}, so the SVD correctly omits it ({reason})",
        "refs": _svd_ref(svd, per, reg, None),
    }


def rule_view(r: dict) -> dict:
    """periph, reg, field, sec, svd of a `rule` record from `mcu vote`, read back from its id and note."""
    parts = r["id"].split(".")
    m = re.match(r"§(\S+?);", r.get("note") or "")
    return {"periph": parts[3], "reg": parts[4], "field": parts[5], "sec": m.group(1) if m else None,
            "svd": "; SVD " in (r.get("note") or "")}


def render_fields_md(rm: RmDoc, chapter: str, rules: list[dict]) -> str | None:
    rs = [dict(r, **rule_view(r)) for r in rules]
    rs = [r for r in rs if r["sec"] and r["sec"].split(".")[0] == chapter]
    if not rs:
        return None
    ch = rm.by_id[chapter]
    lines = [f"# {ch['title']}: register-field constraints ({rm.doc_id} ch.{chapter})", "",
             "Generated by `mcu vote` from the RM register sections: every constraint sentence as printed, "
             "keyed by SVD `REG.FIELD` (RM name when the SVD has none, marked `not in SVD`). "
             "Deterministic, no paraphrase.", ""]
    for r in rs:
        p = r["p"] if isinstance(r["p"], int) else r["p"][0]
        pr = f"p.{p}" if isinstance(r["p"], int) else f"p.{r['p'][0]}-{r['p'][-1]}"
        lines.append(f"- `{r['reg']}.{r['field']}`{'' if r['svd'] else ' (not in SVD)'} [§{r['sec']} {pr}] {r['text']}")
    lines += ["", f"<!-- {len(rs)} constraint sentences -->"]   # a count, not a claim: doc check M skips comments
    return "\n".join(lines) + "\n"


def render_vote_md(res: dict, svd: SvdDoc, rm: RmDoc, hdr: Header) -> str:
    rows = sorted(res["conflicts"], key=lambda c: (c.get("_periph") or "", c.get("_reg") or "", c["id"]))
    tally: dict[str, int] = {}
    for c in rows:
        tally[c["_verdict"]] = tally.get(c["_verdict"], 0) + 1
    tally["correct-omission"] = len(res["quirks"])
    lines = [f"# Register vote: {svd.doc_id} vs {rm.doc_id}" + (f" vs {hdr.h['name']}" if hdr else ""), "",
             f"Device filter: {res['availability'].get('table')} (column {res['availability'].get('part_column')}), "
             f"absent on {svd.device}: {', '.join(res['availability'].get('absent', [])) or 'none'}; plus per-bit "
             "'only available in' / 'reserved in' notes.", "",
             "Tally: " + ", ".join(f"{k} {v}" for k, v in sorted(tally.items())), "",
             "| Periph | RM regs | matched | diffs | verdicts | parse failures | missing in SVD |", "|---|---|---|---|---|---|---|"]
    for p, st in sorted(res["per_periph"].items()):
        v = ", ".join(f"{k} {n}" for k, n in sorted(st["verdicts"].items())) or "-"
        lines.append(f"| {p} | {st['rm_registers']} | {st['matched']} | {st['diffs']} | {v} | "
                     f"{len(st['parse_failed'])} | {len(st['missing_in_svd'])} |")
    lines += ["", "| Register | Difference | Verdict | Cite | Why |", "|---|---|---|---|---|"]
    for c in rows:
        lines.append(f"| {c.get('_periph')}.{c.get('_reg') or '-'} | {c['text'].split(': ', 1)[-1]} | {c['_verdict']} "
                     f"| {rm.doc_id} p{c['p']} | {(c.get('note') or '').split(': ', 1)[-1]} |")
    lines += ["", "Correct omissions (quirk records, class `variant`):", ""]
    for q in res["quirks"]:
        lines.append(f"- {q['text']} [{rm.doc_id} p{q['p']}]")
    return "\n".join(lines) + "\n"


def cmd_vote(args) -> int:
    root = atlas_dir(args.atlas_dir)
    svd_doc = args.svd_doc or find_svd_doc(root)
    if not svd_doc:
        raise SystemExit("pass --svd-doc")
    svd = SvdDoc.load(root, svd_doc)
    rm = RmDoc.load(root, _rm_doc_arg(root, args.rm_doc))
    hdr = Header(parse_header(Path(args.header)) if args.header else None)
    only = [p for p in (args.periph or "").split(",") if p] or None
    res = run_vote(rm, svd, hdr, only)
    rd = rm.dir
    rmslug = rm.doc_id.split("@")[0]
    if only:
        cpre = [f"{svd.part}.conflict.reg.{p}." for p in only]
        qpre = [f"{svd.part}.quirk.variant.{p}." for p in only]
        rpre = [f"{rmslug}.rule.reg.{p}." for p in only]
    else:
        cpre = [f"{svd.part}.conflict.reg.", f"{svd.part}.conflict.periph."]
        qpre = [f"{svd.part}.quirk.variant."]
        rpre = [f"{rmslug}.rule.reg."]
    public = lambda rows: [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]
    _replace(rd / "records" / "conflict.jsonl", cpre, public(res["conflicts"]))
    _replace(rd / "records" / "quirk.jsonl", qpre, public(res["quirks"]))
    _replace(rd / "records" / "rule.jsonl", rpre, public(res["rules"]))
    write_json(rd / "availability.json", {k: v for k, v in res["availability"].items()})
    write_json(rd / "rm_regs.json", res["parsed"])
    all_rules = list(read_jsonl(rd / "records" / "rule.jsonl"))
    secs = {rule_view(r)["sec"] for r in all_rules if r["id"].startswith(f"{rmslug}.rule.reg.")}
    for ch in sorted({x.split(".")[0] for x in secs if x}, key=lambda c: rm.index.get(c, 0)):
        text = render_fields_md(rm, ch, [r for r in all_rules if r["id"].startswith(f"{rmslug}.rule.reg.")])
        if text:
            write_text(rd / "s" / f"{ch}.fields.md", text)
    tally: dict[str, int] = {}
    for c in res["conflicts"]:
        tally[c["_verdict"]] = tally.get(c["_verdict"], 0) + 1
    tally["correct-omission"] = len(res["quirks"])
    summary = {
        "svd": svd.doc_id, "rm": rm.doc_id, "header": {"file": hdr.h["file"], "sha256": sha256_file(Path(hdr.h["file"]))} if hdr else None,
        "tool": f"embarch-atlas {__version__} mcu vote", "only": only, "tally": tally,
        "per_periph": res["per_periph"], "rules": len(res["rules"]),
        "parsed_sections": len(res["parsed"]), "parse_ok": sum(1 for r in res["parsed"] if r["parse_ok"]),
    }
    write_json(rd / ("vote.json" if not only else f"vote.{'_'.join(only)}.json"), summary)
    write_text(rd / ("vote.md" if not only else f"vote.{'_'.join(only)}.md"), render_vote_md(res, svd, rm, hdr))
    print(f"{svd.doc_id} vs {rm.doc_id}{' vs ' + hdr.h['name'] if hdr else ''}: "
          f"{summary['parsed_sections']} RM register sections parsed ({summary['parse_ok']} self-check ok); "
          f"{len(res['conflicts'])} conflicts, {len(res['quirks'])} correct omissions, {len(res['rules'])} rule records; "
          + ", ".join(f"{k} {v}" for k, v in sorted(tally.items())))
    for p, st in sorted(res["per_periph"].items()):
        if st["diffs"] or st["parse_failed"] or st["missing_in_svd"]:
            print(f"  {p:10} regs {st['rm_registers']:3} matched {st['matched']:3} diffs {st['diffs']:3} "
                  f"{json.dumps(st['verdicts'])} parse-failed {len(st['parse_failed'])} missing {len(st['missing_in_svd'])}")
    return 0


def register(sub, groups) -> None:
    mcu = verb_group(sub, groups, "mcu", "MCU knowledge: SVD, register vote, reference-manual sections")
    p = mcu.add_parser("vote", help="SVD vs RM register sections vs CMSIS header -> conflict/quirk/rule records")
    p.add_argument("--svd-doc", help="SVD doc id (default: the only docs/*/peripherals.json)")
    p.add_argument("--rm-doc", help="RM doc id (default: the only docs/*/outline.json)")
    p.add_argument("--header", help="the CMSIS device header (<device>.h), the third voter")
    p.add_argument("--periph", help="comma list of SVD peripherals to vote (default: all)")
    p.set_defaults(func=cmd_vote)

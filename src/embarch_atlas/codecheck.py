"""`codecheck <atlas>`: register constants in the firmware vs the datasheet's register map.

Which code: every okay DT node whose compatible has a driver TU (`dt.json` `drivers`,
else `drivers_not_built`) and whose part has a translated register map (`register` +
`field` records under `docs/<doc>/records/`). The part comes from the I2C identity
join (`pins.json`: node -> board part -> its docs), else `boards/bindings.json`
`compat_parts`, else the compatible's model token against the docs' part names.
The driver's directory is scanned: every `.c`/`.h` in it, plus the headers they
include with quotes.

What is extracted, deterministically (regex plus a small evaluator for C
preprocessor integer expressions; no libclang):

- every object-like `#define` is evaluated: literals, `BIT(n)`, `GENMASK(h, l)`,
  `(1u << n)`, `FIELD_PREP(mask, v)`, casts, other macros, and the function-like
  macros the scanned files define. The report says how many resolved, and why each
  other one did not.
- each is classified by naming convention: register addresses (`<P>_REG_<X>`,
  `<X>_REG`), masks (`*_MASK`/`*_MSK`/`*_FIELD`, or a `BIT()`/`GENMASK()`/shift body),
  field values (`<FIELD>_<VALUE>` beside `<FIELD>_MASK`), reset constants
  (`*_RESET_VAL`, `*_POR`, `*_DEFAULT`), shifts, and plain values (units, limits).
- a mask is tied to its register by, in order of trust: name prefix (`<X>_<FIELD>`
  beside `<P>_REG_<X>`), a datasheet register name as prefix, the section comment
  above it (`CTRL (05h)`), or position under the last register define. When two
  methods disagree the disagreement is recorded, never resolved by guessing.
- comment claims: reset values (`[reset = 0xE4]`, `default 0x3`, `(POR default)`),
  bit positions of named fields (`bits 5:4 SLEW`), and "these bits are set by a pin
  at power-on".
- register-access call sites in the `.c` files: calls named like a register access,
  and which register macro each one names.

The join to the records is by (address, bit range), never by name: driver
abbreviations make names a bad key. Names only raise questions (a field named like
one that lives in another register). A driver mask wider than the datasheet
register is read as a little-endian word over consecutive registers, then
big-endian; the one used is stated.

Findings, each a question for the engineer (`mismatch.py` ranks them in `checks.md`):
`reg-field-in-other-register`, `reg-address-vs-name`, `reg-mask-vs-field`,
`reg-reset-vs-datasheet`, `reg-address-unknown`, `reg-comment-vs-datasheet`.

Outputs: `atlases/<id>/codecheck.json`, `codecheck.md`. `join` runs the same
analysis (and rewrites both files) and folds its checks and findings into the
mismatch report.
"""
from __future__ import annotations

import collections
import math
import os
import re
import subprocess
from dataclasses import dataclass, field as dfield
from pathlib import Path
from typing import Any, Callable

from embarch_atlas.common import read_json, write_json, write_text

CHECKS = {
    "addr": "register addresses vs the datasheet register map",
    "name": "register macro names vs the datasheet register at that address",
    "mask": "masks vs datasheet field bit ranges",
    "fname": "field names vs the register they sit in",
    "site": "masks at register-access sites belong to that register",
    "reset": "reset/default constants vs datasheet reset",
    "comment": "comment claims vs datasheet (bit positions, pin-set resets)",
}
CHECK_SOURCES = "driver sources (regex + expression evaluator), register/field records"
ORDER = ["reg-field-in-other-register", "reg-address-vs-name", "reg-mask-vs-field", "reg-reset-vs-datasheet",
         "reg-address-unknown", "reg-comment-vs-datasheet"]
SETTLE = {
    "reg-field-in-other-register": "the field tables of both registers (cited pages): which register holds the field; "
                                   "then every call site that writes it",
    "reg-address-vs-name": "the datasheet register map: is the macro's value or its name wrong",
    "reg-mask-vs-field": "the register's field table on the cited page against the driver constant: which bit range is right",
    "reg-reset-vs-datasheet": "the reset column on the cited page, or a register read right after power-on",
    "reg-address-unknown": "the datasheet register map: a register the translation missed, an undocumented one, or a wrong constant",
    "reg-comment-vs-datasheet": "fix the comment, or show the datasheet text that supports it; a read after power-on settles resets",
}
MAX_FILES = 60


def norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def token(compat: str) -> str:
    return re.sub(r"[^a-z0-9]", "_", compat.lower())


def lowbit(m: int) -> int:
    return (m & -m).bit_length() - 1


def runs(m: int) -> list[list[int]]:
    """Contiguous set-bit runs, highest first: 0b1110_0011 -> [[7, 5], [1, 0]]."""
    out, b = [], 0
    while m >> b:
        if (m >> b) & 1:
            lo = b
            while (m >> b) & 1:
                b += 1
            out.append([b - 1, lo])
        else:
            b += 1
    return out[::-1]


def bits_str(m: int) -> str:
    return ",".join(f"{h}:{l}" if h != l else f"{h}" for h, l in runs(m)) or "none"


def fmask(hi: int, lo: int) -> int:
    return ((1 << (hi - lo + 1)) - 1) << lo


def parse_int(s: Any) -> int | None:
    """Record resets and addresses: int, '0x..', '0b..'; None for undefined bits ('0bx1') or null."""
    if s is None or isinstance(s, bool):
        return None
    if isinstance(s, int):
        return s
    try:
        return int(str(s).strip(), 0)
    except ValueError:
        return None


# ---------------------------------------------------------------- C text: comments, defines, calls

@dataclass
class Comment:
    start: int
    end: int
    text: str
    standalone: bool          # nothing but whitespace before it on its first line


def strip_c(text: str) -> tuple[str, list[Comment]]:
    """(code, comments). Code keeps every offset and newline; comments and string contents become spaces."""
    out: list[str] = []
    comments: list[Comment] = []
    i, n, line, code_seen = 0, len(text), 1, False

    def blank(seg: str) -> str:
        return "".join("\n" if ch == "\n" else " " for ch in seg)

    while i < n:
        c = text[i]
        if c == "/" and text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            nl = text.count("\n", i, j)
            comments.append(Comment(line, line + nl, text[i + 2:j - 2 if j - i >= 4 else j], not code_seen))
            out.append(blank(text[i:j]))
            line += nl
            i = j
            continue
        if c == "/" and text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            comments.append(Comment(line, line, text[i + 2:j], not code_seen))
            out.append(" " * (j - i))
            i = j
            continue
        if c in "\"'":
            j = i + 1
            while j < n and text[j] != c and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            j = min(j + 1, n)
            seg = text[i:j]
            out.append(seg[0] + blank(seg[1:-1]) + seg[-1] if len(seg) >= 2 else seg)
            line += seg.count("\n")
            code_seen = True
            i = j
            continue
        if c == "\n":
            line += 1
            code_seen = False
        elif not c.isspace():
            code_seen = True
        out.append(c)
        i += 1
    return "".join(out), comments


@dataclass
class Define:
    name: str
    params: list[str] | None
    body: str
    file: str
    line: int
    end: int
    trail: str = ""           # comments on the define's own lines
    lead: str = ""            # a standalone comment right above
    value: int | None = None
    why: str | None = None
    kind: str = "other"
    short: str = ""
    dup: int = 0
    info: dict = dfield(default_factory=dict)

    @property
    def loc(self) -> str:
        return f"{self.file}:{self.line}"


DEFINE_RE = re.compile(r"^[ \t]*#[ \t]*define[ \t]+(?P<name>[A-Za-z_]\w*)(?P<params>\([^)]*\))?(?P<body>.*)$")


@dataclass
class CFile:
    path: str
    code: str
    comments: list[Comment]
    defines: list[Define]
    includes: list[str]


def scan_file(path: str, text: str) -> CFile:
    code, comments = strip_c(text)
    lines = code.split("\n")
    trail: dict[int, list[str]] = collections.defaultdict(list)
    lead_by_end: dict[int, Comment] = {}
    for c in comments:
        if c.standalone:
            lead_by_end[c.end] = c
        else:
            trail[c.start].append(c.text)
    defines, k = [], 0
    while k < len(lines):
        m = DEFINE_RE.match(lines[k])
        if not m:
            k += 1
            continue
        start, body = k + 1, m["body"]
        while body.rstrip().endswith("\\") and k + 1 < len(lines):
            k += 1
            body = body.rstrip()[:-1] + " " + lines[k]
        end = k + 1
        params = None
        if m["params"] is not None:
            params = [p.strip() for p in m["params"][1:-1].split(",") if p.strip()]
        lc = lead_by_end.get(start - 1)
        defines.append(Define(m["name"], params, body.strip(), path, start, end,
                              trail=" ".join(t.strip() for ln in range(start, end + 1) for t in trail.get(ln, [])),
                              lead=lc.text if lc else ""))
        k += 1
    includes = []
    for m in re.finditer(r'^[ \t]*#[ \t]*include[ \t]*"', code, re.M):
        close = text.find('"', m.end())
        if close > m.end():
            includes.append(text[m.end():close])
    return CFile(path, code, comments, defines, includes)


# ---------------------------------------------------------------- the expression evaluator

class Unresolved(Exception):
    pass


_TOK = re.compile(r"\s*(?:(?P<num>0[xX][0-9a-fA-F]+|0[bB][01]+|\d+)(?P<suf>[uUlL]*)(?![\w.])"
                  r"|(?P<id>[A-Za-z_]\w*)|(?P<op><<|>>|<=|>=|==|!=|&&|\|\||[-+*/%&|^~!()<>?:,]))")
TYPE_WIDTH = {"uint8_t": 8, "u8": 8, "uint16_t": 16, "u16": 16, "uint32_t": 32, "u32": 32, "uint64_t": 64, "u64": 64,
              "uintptr_t": 64, "size_t": 64}
TYPE_WORDS = set(TYPE_WIDTH) | {"unsigned", "signed", "int", "long", "short", "char", "const", "volatile", "bool", "_Bool",
                                "int8_t", "int16_t", "int32_t", "int64_t", "s8", "s16", "s32", "s64", "uint"}
BINARY = {"||": 1, "&&": 2, "|": 3, "^": 4, "&": 5, "==": 6, "!=": 6, "<": 7, ">": 7, "<=": 7, ">=": 7,
          "<<": 8, ">>": 8, "+": 9, "-": 9, "*": 10, "/": 10, "%": 10}
M32, M64 = (1 << 32) - 1, (1 << 64) - 1


def _bit(n: int) -> int:
    if not 0 <= n < 64:
        raise Unresolved(f"BIT({n}) out of range")
    return 1 << n


def _genmask(h: int, lo: int) -> int:
    if not 0 <= lo <= h < 64:
        raise Unresolved(f"GENMASK({h}, {lo}) out of range")
    return fmask(h, lo)


def _field_prep(m: int, v: int) -> int:
    if m <= 0:
        raise Unresolved("FIELD_PREP with an empty mask")
    return (v << lowbit(m)) & m


def _field_get(m: int, v: int) -> int:
    if m <= 0:
        raise Unresolved("FIELD_GET with an empty mask")
    return (v & m) >> lowbit(m)


def _same(x: int) -> int:
    return x


BUILTINS: dict[str, Callable[..., int]] = {
    "BIT": _bit, "BIT_ULL": _bit, "BIT64": _bit, "_BV": _bit, "BIT_U32": _bit,
    "GENMASK": _genmask, "GENMASK_ULL": _genmask, "GENMASK64": _genmask,
    "BIT_MASK": lambda n: (1 << n) - 1, "BIT64_MASK": lambda n: (1 << n) - 1,
    "FIELD_PREP": _field_prep, "FIELD_GET": _field_get,
    **{k: _same for k in ("U", "UL", "ULL", "UINT8_C", "UINT16_C", "UINT32_C", "UINT64_C",
                          "INT8_C", "INT16_C", "INT32_C", "INT64_C")},
}


def _tokens(text: str) -> list[tuple[str, str, int, int]]:
    out, i = [], 0
    while i < len(text):
        if text[i:].strip() == "":
            break
        m = _TOK.match(text, i)
        if not m or m.end() == i:
            raise Unresolved(f"not an integer expression near {text[i:].strip()[:20]!r}")
        kind = "num" if m["num"] else "id" if m["id"] else "op"
        val = m["num"] or m["id"] or m["op"]
        out.append((kind, val, m.start(kind), m.end(kind)))
        i = m.end()
    return out


def _lit(s: str) -> int:
    if s[:2] in ("0x", "0X"):
        return int(s, 16)
    if s[:2] in ("0b", "0B"):
        return int(s[2:], 2)
    if len(s) > 1 and s[0] == "0":
        return int(s, 8)
    return int(s)


class Evaluator:
    """Integer value of object-like macros, over the macro table of the scanned files."""

    def __init__(self, objs: dict[str, str], funcs: dict[str, tuple[list[str], str]]):
        self.objs, self.funcs = objs, funcs
        self.memo: dict[str, int | Unresolved] = {}
        self.active: set[str] = set()

    def value(self, name: str) -> int:
        if name in self.memo:
            v = self.memo[name]
            if isinstance(v, Unresolved):
                raise v
            return v
        if name in self.active:
            raise Unresolved(f"{name} refers to itself")
        self.active.add(name)
        try:
            v = self.eval(self.objs[name])
        except Unresolved as e:
            self.memo[name] = e
            raise
        finally:
            self.active.discard(name)
        self.memo[name] = v
        return v

    def eval(self, text: str, depth: int = 0) -> int:
        if depth > 24:
            raise Unresolved("macro expansion too deep")
        toks = _tokens(text)
        if not toks:
            raise Unresolved("empty expression")
        p = _Parser(self, text, toks, depth)
        try:
            v = p.expr()
        except (ZeroDivisionError, ValueError, OverflowError, RecursionError, IndexError) as e:
            raise Unresolved(f"not evaluable ({type(e).__name__})") from None
        if p.i != len(toks):
            raise Unresolved(f"unexpected {toks[p.i][1]!r}")
        return v

    def call(self, name: str, args: list[str], depth: int) -> int:
        if name in BUILTINS:
            vals = [self.eval(a, depth + 1) for a in args]
            try:
                return BUILTINS[name](*vals)
            except TypeError:
                raise Unresolved(f"{name}() with {len(vals)} arguments") from None
        if name in self.funcs:
            params, body = self.funcs[name]
            if len(params) != len(args) or "#" in body or any(p == "..." for p in params):
                raise Unresolved(f"{name}() not expandable")
            text = body
            if params:
                text = re.sub(r"\b(" + "|".join(map(re.escape, params)) + r")\b",
                              lambda m: "(" + args[params.index(m[1])] + ")", body)
            return self.eval(text, depth + 1)
        raise Unresolved(f"unknown macro {name}()")


class _Parser:
    def __init__(self, ev: Evaluator, text: str, toks: list, depth: int):
        self.ev, self.text, self.toks, self.depth, self.i = ev, text, toks, depth, 0

    def _is(self, op: str, k: int = 0) -> bool:
        j = self.i + k
        return j < len(self.toks) and self.toks[j][0] == "op" and self.toks[j][1] == op

    def _expect(self, op: str) -> None:
        if not self._is(op):
            raise Unresolved(f"expected {op!r}")
        self.i += 1

    def expr(self) -> int:
        c = self.binary(1)
        if self._is("?"):
            self.i += 1
            a = self.expr()
            self._expect(":")
            b = self.expr()
            return a if c else b
        return c

    def binary(self, minp: int) -> int:
        lhs = self.unary()
        while self.i < len(self.toks):
            kind, op, _, _ = self.toks[self.i]
            if kind != "op" or op not in BINARY or BINARY[op] < minp:
                break
            self.i += 1
            rhs = self.binary(BINARY[op] + 1)
            lhs = _apply(op, lhs, rhs)
        return lhs

    def unary(self) -> int:
        if self.i >= len(self.toks):
            raise Unresolved("truncated expression")
        kind, v, _, _ = self.toks[self.i]
        if kind == "op" and v in ("+", "-", "~", "!"):
            self.i += 1
            x = self.unary()
            if v == "-":
                return -x
            if v == "~":
                if x < 0:
                    raise Unresolved("~ of a negative value")
                return ~x & (M64 if x > M32 else M32)
            if v == "!":
                return int(not x)
            return x
        if kind == "op" and v == "(":
            j, words = self.i + 1, []
            while j < len(self.toks) and ((self.toks[j][0] == "id" and self.toks[j][1] in TYPE_WORDS) or self.toks[j][1] == "*"):
                words.append(self.toks[j][1])
                j += 1
            if words and j < len(self.toks) and self.toks[j][1] == ")":
                self.i = j + 1
                x = self.unary()
                w = min((TYPE_WIDTH[t] for t in words if t in TYPE_WIDTH), default=None)
                if w is None and "char" in words and "unsigned" in words:
                    w = 8
                return x & ((1 << w) - 1) if w else x
            self.i += 1
            x = self.expr()
            self._expect(")")
            return x
        return self.primary()

    def primary(self) -> int:
        kind, v, _, _ = self.toks[self.i]
        self.i += 1
        if kind == "num":
            return _lit(v)
        if kind == "id":
            if v in ("sizeof", "defined"):
                raise Unresolved(f"{v} is not evaluated")
            if self._is("("):
                return self.ev.call(v, self._args(), self.depth)
            if v in self.ev.objs:
                return self.ev.value(v)
            raise Unresolved(f"unknown identifier {v}")
        raise Unresolved(f"unexpected {v!r}")

    def _args(self) -> list[str]:
        self._expect("(")
        depth, start, args = 0, self.i, []
        while self.i < len(self.toks):
            kind, v, _, _ = self.toks[self.i]
            if kind == "op" and v == "(":
                depth += 1
            elif kind == "op" and v == ")":
                if depth == 0:
                    if self.i > start or args:
                        args.append(self._slice(start, self.i))
                    self.i += 1
                    return args
                depth -= 1
            elif kind == "op" and v == "," and depth == 0:
                args.append(self._slice(start, self.i))
                start = self.i + 1
            self.i += 1
        raise Unresolved("unbalanced parentheses")

    def _slice(self, a: int, b: int) -> str:
        if a >= b:
            raise Unresolved("empty macro argument")
        return self.text[self.toks[a][2]:self.toks[b - 1][3]]


def _apply(op: str, a: int, b: int) -> int:
    if op in ("<<", ">>"):
        if not 0 <= b <= 64:
            raise Unresolved(f"shift by {b}")
        return a << b if op == "<<" else a >> b
    if op in ("/", "%"):
        if b == 0:
            raise Unresolved("division by zero")
        q = abs(a) // abs(b) * (1 if (a >= 0) == (b >= 0) else -1)
        return q if op == "/" else a - q * b
    return {"+": lambda: a + b, "-": lambda: a - b, "*": lambda: a * b, "&": lambda: a & b, "|": lambda: a | b,
            "^": lambda: a ^ b, "==": lambda: int(a == b), "!=": lambda: int(a != b), "<": lambda: int(a < b),
            ">": lambda: int(a > b), "<=": lambda: int(a <= b), ">=": lambda: int(a >= b),
            "&&": lambda: int(bool(a) and bool(b)), "||": lambda: int(bool(a) or bool(b))}[op]()


# ---------------------------------------------------------------- classification by naming convention

UNIT_TOKENS = {"MV", "UV", "NV", "V", "MA", "UA", "NA", "MW", "UW", "W", "OHM", "OHMS", "MOHM", "UOHM", "KOHM",
               "MS", "US", "NS", "S", "SEC", "HZ", "KHZ", "MHZ", "DEGC", "MDEGC", "PCT", "PERCENT", "MAH", "UAH",
               "MWH", "UWH", "STEP", "STEPS", "MAX", "MIN", "COUNT", "NUM", "LEN", "SIZE", "TIMEOUT", "DELAY",
               "RETRIES", "TRIES", "SCALE", "DIV", "MULT", "FACTOR", "BASE", "OFFSET", "LIMIT", "RANGE", "PPM", "BYTES"}
BOUND_TOKENS = {"MAX", "MIN", "COUNT", "NUM", "FIRST", "LAST", "END", "START", "BASE", "SIZE", "LEN", "CNT"}
ENCODING_TOKENS = {"MASK", "MSK", "BIT", "BITS", "FLAG", "BLOCK", "SHIFT", "POS"}
MASK_SUFFIX = {"MASK", "MSK", "FIELD", "FLD", "BITS", "BIT", "BITMASK"}
RESET_RE = re.compile(r"_(?:RESET_VAL(?:UE)?|RESET_DEFAULT|POR(?:_VAL(?:UE)?)?|DEFAULT(?:_VAL(?:UE)?)?|RST_VAL|INIT_VAL)$", re.I)
SHIFT_TOKENS = {"SHIFT", "POS", "OFS"}
MASKY_BODY = re.compile(r"\b(?:BIT|BIT_ULL|BIT64|_BV|GENMASK|GENMASK_ULL|GENMASK64|FIELD_PREP|BIT_MASK)\s*\(|<<")
GENERIC_FIELDS = {"EN", "ENABLE", "DIS", "DISABLE", "MODE", "STATUS", "RESERVED", "RSVD", "RST", "RESET", "INT", "IRQ",
                  "MASK", "FLAG", "CTRL", "CFG", "CONFIG", "DATA", "VAL", "VALUE", "LSB", "MSB", "SEL", "ON", "OFF",
                  "START", "STOP", "READY", "RDY", "ERR", "ERROR", "FAULT", "ID", "SET", "CLR", "OUT", "IN"}


def is_regish(short: str) -> bool:
    return bool(re.search(r"(?:^|_)REG_", short)) or short.endswith("_REG")


def reg_short(short: str) -> str:
    """`REG_CTRL1` / `CTRL1_REG` / `OTHER_REG_CTRL1` -> `CTRL1`."""
    m = re.search(r"(?:^|_)REG_(.+)$", short)
    if m:
        return m[1]
    return short[:-4] if short.endswith("_REG") else short


def classify(d: Define, reg_names: set[str]) -> str:
    if d.params is not None:
        return "function"
    if d.name == "DT_DRV_COMPAT" or not d.body:
        return "flag"
    if d.value is None:
        return "unresolved"
    toks = d.short.upper().split("_")
    if is_regish(d.short.upper()):
        body_id = d.body.strip("() ")
        if re.fullmatch(r"[A-Za-z_]\w*", body_id) and body_id in reg_names:
            return "alias"
        if toks[-1] in BOUND_TOKENS:
            return "bound"
        if toks[-1] in ENCODING_TOKENS or MASKY_BODY.search(d.body):
            return "encoding"
        return "register"
    if RESET_RE.search(d.short) and not (toks[-1] == "POR" and MASKY_BODY.search(d.body)):
        return "reset"          # a `STATUS_POR BIT(1)` is a power-on-reset flag, not a reset value
    if toks[-1] in SHIFT_TOKENS and 0 <= d.value < 64:
        return "shift"
    if toks[-1] in UNIT_TOKENS or (len(toks) > 1 and toks[-2] in UNIT_TOKENS and toks[-1] in UNIT_TOKENS):
        return "value"
    if d.value < 0:
        return "value"
    if toks[-1] in MASK_SUFFIX or MASKY_BODY.search(d.body):
        return "mask" if d.value > 0 else "value"
    return "value"


# ---------------------------------------------------------------- comment claims

_ADDR_TOK = r"(?:0[xX][0-9A-Fa-f]+|[0-9A-Fa-f]{1,4}[hH]?)"
REG_REF = re.compile(r"(?<![\w.])([A-Za-z][A-Za-z0-9_]*)\s*\(\s*(" + _ADDR_TOK + r"(?:\s*/\s*" + _ADDR_TOK + r")*)\s*\)")
REG_REF2 = re.compile(r"(?<![\w.])(0[xX][0-9A-Fa-f]+|[0-9][0-9A-Fa-f]{0,3}[hH])\s*\(\s*([A-Za-z][A-Za-z0-9_]*)\s*\)")
REF_STOP = {"register", "reg", "regs", "bit", "bits", "byte", "bytes", "high", "low", "offset", "address", "addr",
            "see", "table", "page", "lsb", "msb", "word", "value", "at", "in", "the", "and", "or", "of", "is", "default",
            "reset", "por", "mask", "gpio", "pin", "pins"}
_NUM = r"(0[xX][0-9A-Fa-f]+|0[bB][01]+|[01]+b\b|[0-9][0-9A-Fa-f]*h\b|\d+)"
_UNITS = r"(?:m?V|[mµu]A|A|m?W|[mµun]?s|sec|%|[kM]?Hz|°C|C|m?Ω|ohms?|k|mV/us|bytes?)\b"
RESET_CLAIM = re.compile(r"\b(?:reset|POR|power[- ]on(?:[- ]reset)?|defaults?)\b(?:\s+value)?\s*(?:=|:|is|of|to)?\s*"
                         + _NUM + r"(?!\s*" + _UNITS + ")", re.I)
DEFAULT_MARK = re.compile(r"\((?:POR|reset|power[- ]on)?\s*default\)|\b(?:POR|reset|power[- ]on)\s+default\b", re.I)
BIT_CLAIMS = [
    re.compile(r"\b[Bb]its?\s+(\d{1,2})(?:\s*[:\-]\s*(\d{1,2}))?\s*,?\s+(?:is\s+|are\s+|=\s*)?([A-Z][A-Z0-9_]{2,})\b"),
    re.compile(r"\b([A-Z][A-Z0-9_]{2,})\s*[\[(]\s*(?:[Bb]its?\s*)?(\d{1,2})(?:\s*:\s*(\d{1,2}))?\s*[\])]"),
    re.compile(r"\b([A-Z][A-Z0-9_]{2,})\s+(?:is\s+|are\s+|at\s+|in\s+)?[Bb]its?\s+(\d{1,2})(?:\s*[:\-]\s*(\d{1,2}))?"),
]
PIN_WORD = re.compile(r"\b(?:pin|pins|resistor|strap|straps)\b", re.I)
POWER_WORD = re.compile(r"\b(?:power[- ]?(?:on|up)|POR|at reset|after reset|on reset|start[- ]?up|boot)\b", re.I)
SET_WORD = re.compile(r"\b(?:set|selected|chosen|determined|configured|programmed|latched|sampled|decided|strapped)\b", re.I)


def claim_number(s: str) -> int | None:
    s = s.strip()
    try:
        if s[:2].lower() == "0x":
            return int(s, 16)
        if s[:2].lower() == "0b":
            return int(s[2:], 2)
        if s[-1] in "bB" and set(s[:-1]) <= {"0", "1"}:
            return int(s[:-1], 2)
        if s[-1] in "hH":
            return int(s[:-1], 16)
        return int(s)
    except ValueError:
        return None


def reg_refs(text: str) -> list[tuple[str, list[int]]]:
    """`CTRL (05h)`, `WORD (01/00h)`, `0x06 (MODE)` in a comment -> [(name, [addresses])]."""
    out = []
    for m in REG_REF.finditer(text):
        name, toks = m[1], re.split(r"\s*/\s*", m[2])
        if name.lower() in REF_STOP or not re.search(r"[A-Z0-9]", name):
            continue
        has_h = any(t[-1] in "hH" for t in toks)
        addrs = []
        for t in toks:
            if t[:2].lower() == "0x":
                addrs.append(int(t, 16))
            elif (t[-1] in "hH" or has_h) and re.search(r"\d", t):
                try:
                    addrs.append(int(t.rstrip("hH"), 16))
                except ValueError:
                    addrs = []
                    break
            else:
                addrs = []
                break
        if addrs:
            out.append((name, addrs))
    for m in REG_REF2.finditer(text):
        if m[2].lower() not in REF_STOP and re.search(r"[A-Z]", m[2]):
            out.append((m[2], [int(m[1], 16) if m[1][:2].lower() == "0x" else int(m[1].rstrip("hH"), 16)]))
    return out


def flat_comment(text: str) -> str:
    """A comment's text on one line, without the leading `*` of continuation lines or banner rules."""
    lines = [re.sub(r"^\s*\*+\s?", "", ln) for ln in text.split("\n")]
    lines = [ln for ln in lines if not re.fullmatch(r"\s*[-=*/_~]{3,}\s*", ln)]
    return " ".join(" ".join(lines).split())


def pin_set_sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.;])\s+", flat_comment(text))
            if PIN_WORD.search(s) and POWER_WORD.search(s) and SET_WORD.search(s)]


def bit_claims(text: str) -> list[tuple[str, int, int, str]]:
    """(field name, hi, lo, quoted text) for `bits 5:4 NAME`, `NAME[5:4]`, `NAME at bit 3`."""
    flat = flat_comment(text)
    out, seen = [], set()
    for i, rx in enumerate(BIT_CLAIMS):
        for m in rx.finditer(flat):
            if i == 0:
                hi, lo, name = int(m[1]), int(m[2] if m[2] is not None else m[1]), m[3]
            else:
                name, hi, lo = m[1], int(m[2]), int(m[3] if m[3] is not None else m[2])
            if hi < lo:
                hi, lo = lo, hi
            if (name, hi, lo) not in seen:
                seen.add((name, hi, lo))
                out.append((name, hi, lo, m[0]))
    return out


# ---------------------------------------------------------------- the datasheet side

PROV_RANK = {"deterministic+crosschecked": 0, "deterministic": 1, "engineer-confirmed": 2, "engineer-declared": 3,
             "measured": 4, "llm-verified": 5, "llm-unverified": 6}


def _rank(r: dict) -> int:
    return PROV_RANK.get(r.get("prov") or "llm-unverified", 7)


@dataclass
class Fld:
    id: str
    name: str
    hi: int
    lo: int
    reset: int | None
    rec: dict

    @property
    def mask(self) -> int:
        return fmask(self.hi, self.lo)

    @property
    def reserved(self) -> bool:
        return bool(re.match(r"(?i)(reserved|rsvd|rsv)", self.name))

    @property
    def width(self) -> int:
        return self.hi - self.lo + 1


class RegMap:
    """One document's register/field records, keyed by address; twins merged, deterministic first."""

    def __init__(self, doc: str, regs: list[dict], fields: list[dict]):
        self.doc = doc
        self.regs_at: dict[int, list[dict]] = collections.defaultdict(list)
        for r in sorted(regs, key=_rank):
            a = parse_int(r.get("addr"))
            if a is not None and all(x.get("id") != r.get("id") or _rank(x) != _rank(r) for x in self.regs_at[a]):
                self.regs_at[a].append(r)
        self.regs_at = {a: v for a, v in self.regs_at.items() if v}
        by_reg: dict[str, list[dict]] = collections.defaultdict(list)
        for f in sorted(fields, key=_rank):
            by_reg[f.get("reg")].append(f)
        self.fields_at: dict[int, list[Fld]] = {}
        placed = set()
        for a, rs in self.regs_at.items():
            out, seen = [], set()
            for rid in dict.fromkeys(r.get("id") for r in rs):
                placed.add(rid)
                for f in by_reg.get(rid, []):
                    b = f.get("bits")
                    if not (isinstance(b, list) and len(b) == 2 and all(isinstance(x, int) for x in b)):
                        continue
                    hi, lo = max(b), min(b)
                    key = (hi, lo, norm(f.get("name")))
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(Fld(f.get("id") or "?", f.get("name") or "?", hi, lo, parse_int(f.get("reset")), f))
            self.fields_at[a] = out
        self.orphan_fields = sum(len(v) for k, v in by_reg.items() if k not in placed)
        self._inherit_mirrors()

    def _inherit_mirrors(self) -> None:
        """A non-volatile backup register shares its RAM register's field layout (`mirror_of` /
        `nv_backup` on the register records). Give a field-less side the other side's fields,
        so a driver defining fields under either name is checked against the same bits."""
        addr_of = {r.get("id"): a for a, rs in self.regs_at.items() for r in rs}
        pairs = set()
        for a, rs in self.regs_at.items():
            for r in rs:
                other = r.get("mirror_of") or r.get("nv_backup")
                if other in addr_of and addr_of[other] != a:
                    pairs.add(tuple(sorted((a, addr_of[other]))))
        for x, y in sorted(pairs):
            for dst, src in ((x, y), (y, x)):
                if not self.fields_at.get(dst) and self.fields_at.get(src):
                    self.fields_at[dst] = [Fld(f.id, f.name, f.hi, f.lo, f.reset, dict(f.rec, via_mirror=True))
                                           for f in self.fields_at[src]]

    def reg(self, a: int) -> dict | None:
        rs = self.regs_at.get(a)
        return rs[0] if rs else None

    def width(self, a: int) -> int:
        r = self.reg(a) or {}
        w = r.get("width")
        return w if isinstance(w, int) and w > 0 else 8

    def reset(self, a: int) -> int | None:
        return parse_int((self.reg(a) or {}).get("reset"))

    def fields(self, a: int) -> list[Fld]:
        return self.fields_at.get(a, [])

    def all_fields(self) -> list[tuple[int, Fld]]:
        return [(a, f) for a in sorted(self.fields_at) for f in self.fields_at[a]]

    def regs_named(self, name: str) -> list[int]:
        n = norm(name)
        return sorted(a for a, rs in self.regs_at.items() if any(norm(r.get("name")) == n for r in rs))

    def name_tokens(self) -> list[tuple[str, int]]:
        """Datasheet register names as upper-case `_`-joined tokens, longest first."""
        out = {}
        for a, rs in self.regs_at.items():
            for r in rs:
                nm = re.sub(r"[^A-Za-z0-9]+", "_", r.get("name") or "").strip("_").upper()
                if nm:
                    out.setdefault(nm, a)
        return sorted(out.items(), key=lambda x: -len(x[0]))


def _cite(r: dict, doc: str) -> str:
    p = r.get("p")
    pp = "p" + ",".join(str(x) for x in p) if isinstance(p, list) and p else (f"p{p}" if p else "")
    return f"{r.get('doc') or doc} {pp}".strip()


def reg_ev(rm: RegMap, a: int) -> str:
    r = rm.reg(a)
    if not r:
        return f"{rm.doc}: no register at {a:#04x}"
    return f"{r.get('id')} {r.get('name')} @{a:#04x} reset {r.get('reset')} ({_cite(r, rm.doc)})"


def field_ev(rm: RegMap, a: int | None, f: Fld) -> str:
    where = f" @{a:#04x}" if a is not None else ""
    return f"{f.id} bits {f.hi}:{f.lo}{where} reset {f.rec.get('reset')} ({_cite(f.rec, rm.doc)})"


# ---------------------------------------------------------------- mask vs fields

OK_STATUS = ("exact", "multi")


def classify_piece(rm: RegMap, a: int, m: int) -> dict:
    fields = rm.fields(a)
    if rm.reg(a) is None:
        return {"status": "no-register", "fields": []}
    if not fields:
        return {"status": "no-fields", "fields": []}
    exact = [f for f in fields if f.mask == m and not f.reserved]
    if exact:
        return {"status": "exact", "fields": [exact[0]]}
    over = [f for f in fields if f.mask & m]
    if over and all(not (f.mask & ~m) for f in over) and not any(f.reserved for f in over):
        u = 0
        for f in over:
            u |= f.mask
        if u == m:
            return {"status": "multi", "fields": over}
    inside = [f for f in fields if not (m & ~f.mask) and not f.reserved]
    if inside:
        return {"status": "within", "fields": [inside[0]]}
    covered = 0
    for f in fields:
        covered |= f.mask
    return {"status": "mismatch", "fields": over, "reserved": [f for f in over if f.reserved], "undefined": m & ~covered}


def classify_mask(rm: RegMap, a: int, m: int, order: str | None = None) -> dict:
    """A driver mask at register `a`. Wider than the register, it is a word over a, a+1, ...: in the given byte
    order (`le`/`be`), or, with none given, little-endian first and big-endian when that fails."""
    if rm.reg(a) is None:
        return {"status": "no-register", "addr": a, "pieces": [], "fields": []}
    w = rm.width(a)
    if not m >> w:
        r = classify_piece(rm, a, m)
        return {**r, "addr": a, "pieces": [[a, m]], "order": None, "offsets": {a: 0}}
    n = math.ceil(m.bit_length() / w)
    best = None
    for order in ([order] if order else ["le", "be"]):
        pieces, offsets = [], {}
        for k in range(n):
            pm = (m >> (k * w)) & ((1 << w) - 1)
            if pm:
                pa = a + (k if order == "le" else n - 1 - k)
                pieces.append([pa, pm])
                offsets[pa] = k * w
        res = [classify_piece(rm, pa, pm) for pa, pm in pieces]
        sts = [r["status"] for r in res]
        undefined = 0
        for (pa, _), r in zip(pieces, res):
            k = pa - a if order == "le" else n - 1 - (pa - a)
            undefined |= r.get("undefined", 0) << (k * w)
        if all(s in OK_STATUS for s in sts):
            status = "exact" if all(s == "exact" for s in sts) else "multi"
        elif any(s == "no-register" for s in sts):
            status = "no-register"
        elif all(s in OK_STATUS + ("within",) for s in sts):
            status = "within"
        else:
            status = "mismatch"
        cand = {"status": status, "addr": a, "pieces": pieces, "order": order, "offsets": offsets,
                "fields": [f for r in res for f in r.get("fields", [])],
                "reserved": [f for r in res for f in r.get("reserved", [])],
                "undefined": undefined,
                "piece_status": sts}
        if status in OK_STATUS:
            return cand
        best = best or cand
    return best


# ---------------------------------------------------------------- sources

class Sources:
    """Tagged paths (`fw:drivers/x.c`, `zephyr:...`) as `dt.json` writes them -> file text.

    The atlas's worktree first; when it is gone, the fw repo at the atlas commit (`git show`)."""

    def __init__(self, ad: Path, stamp: dict):
        from embarch_atlas.dt import rel_bases
        self.bases: dict[str, Path] = {}
        for base, tag in rel_bases(stamp, ad):
            self.bases.setdefault(tag, Path(base))
        b = stamp.get("build", {})
        self.fw, self.commit = b.get("fw"), stamp.get("commit")

    def _split(self, p: str) -> tuple[str | None, str]:
        if p.startswith("/"):
            return None, p
        tag, sep, rel = p.partition(":")
        return (tag, rel) if sep else (None, p)

    def _git(self, tag: str | None) -> bool:
        base = self.bases.get(tag or "")
        return tag == "fw" and bool(self.fw and self.commit) and not (base and base.is_dir())

    def _local(self, p: str) -> Path | None:
        tag, rel = self._split(p)
        if tag is None:
            return Path(rel) if rel.startswith("/") else None
        base = self.bases.get(tag)
        return base / rel if base else None

    def read(self, p: str) -> str | None:
        tag, rel = self._split(p)
        if self._git(tag):
            r = subprocess.run(["git", "-C", self.fw, "show", f"{self.commit}:{rel}"], capture_output=True)
            return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None
        lp = self._local(p)
        if lp is None or not lp.is_file():
            return None
        return lp.read_text(encoding="utf-8", errors="replace")

    def listdir(self, d: str) -> list[str]:
        tag, rel = self._split(d)
        if self._git(tag):
            r = subprocess.run(["git", "-C", self.fw, "ls-tree", "--name-only", self.commit, rel.rstrip("/") + "/"],
                               capture_output=True, text=True)
            return sorted(f"fw:{x}" for x in r.stdout.split("\n") if x) if r.returncode == 0 else []
        lp = self._local(d)
        if lp is None or not lp.is_dir():
            return []
        pre = d.rstrip("/") + "/"
        return sorted(pre + x.name for x in lp.iterdir() if x.is_file())


def _join(d: str, rel: str) -> str:
    tag, sep, path = d.partition(":")
    if not sep:
        return os.path.normpath(os.path.join(d, rel))
    return f"{tag}:{os.path.normpath(os.path.join(path, rel))}"


def _dirname(p: str) -> str:
    tag, sep, path = p.partition(":")
    return f"{tag}:{os.path.dirname(path)}" if sep else os.path.dirname(p)


def gather_files(src: Sources, drivers: list[str]) -> list[CFile]:
    """Every .c/.h beside each driver TU, plus quoted includes (transitively)."""
    queue: list[str] = []
    for f in drivers:
        queue += [x for x in src.listdir(_dirname(f)) if x.endswith((".c", ".h"))]
        queue.append(f)
    seen: dict[str, CFile] = {}
    while queue and len(seen) < MAX_FILES:
        p = queue.pop(0)
        if p in seen:
            continue
        text = src.read(p)
        if text is None:
            continue
        cf = scan_file(p, text)
        seen[p] = cf
        for inc in cf.includes:
            for cand in (_join(_dirname(p), inc),) + tuple(_join(_dirname(d), inc) for d in drivers):
                if cand not in seen and src.read(cand) is not None:
                    queue.append(cand)
                    break
    return [seen[k] for k in sorted(seen)]


# ---------------------------------------------------------------- discovery: DT node -> driver -> doc

def _doc_parts(v: dict) -> list[str]:
    parts = v["doc"].get("parts") or v["doc"].get("part") or []
    return [parts] if isinstance(parts, str) else [p for p in parts if p]


def docs_for_node(path: str, compat: str, docs, identity: dict, part_docs: dict, bindings: dict) -> tuple[list[str], str | None]:
    from embarch_atlas.join import _model_pattern
    idr = identity.get(path)
    if idr and part_docs.get(idr.get("part")):
        return list(part_docs[idr["part"]]), f"I2C identity {idr['part']} {idr.get('mpn') or ''}".strip()
    decl = (bindings.get("compat_parts") or {}).get(compat)
    if decl and docs.for_mpn(decl):
        return docs.for_mpn(decl), f"boards/bindings.json {compat} -> {decl}"
    out = set()
    tok = re.split(r"[-_]", compat.split(",", 1)[-1])[0]
    if re.search(r"\d", tok):
        out |= set(docs.for_mpn(tok))
    pat = _model_pattern(compat)
    if pat:
        out |= {d for d, v in docs.docs.items() if any(pat.match(norm(p)) for p in _doc_parts(v))}
    return sorted(out), (f"compatible {compat}" if out else None)


@dataclass
class Unit:
    compat: str
    doc: str
    drivers: list[str]
    built: bool
    via: str
    nodes: list[str] = dfield(default_factory=list)


def discover(dt: dict, docs, pins: dict, bindings: dict) -> tuple[list[Unit], list[dict]]:
    identity = {d["node"]: d for d in ((pins.get("i2c") or {}).get("identity") or []) if d.get("status") == "identified"}
    part_docs = pins.get("part_docs") or {}
    units: dict[tuple, Unit] = {}
    no_map: dict[str, str] = {}
    for path, n in sorted((dt.get("nodes") or {}).items()):
        if not n.get("okay"):
            continue
        for compat in n.get("compats") or []:
            t = token(compat)
            files, built = (dt.get("drivers") or {}).get(t), True
            if not files:
                files, built = (dt.get("drivers_not_built") or {}).get(t), False
            if not files:
                continue
            dids, via = docs_for_node(path, compat, docs, identity, part_docs, bindings)
            dids = [d for d in dids if docs.kind(d) not in ("svd", "rm", "prd")]
            with_regs = [d for d in dids if docs.records(d, "register")]
            if not with_regs and any(f.startswith("fw:") for f in files):
                no_map[compat] = (f"{', '.join(dids)} has no register records yet (`doc regparse`, or translate the "
                                  f"register map)") if dids else "no datasheet in docs/ matches the part"
            for d in with_regs:
                key = (tuple(files), d)
                u = units.setdefault(key, Unit(compat, d, list(files), built, via or ""))
                u.nodes.append(path)
            break
    not_run = [{"check": f"register constants vs datasheet: {c}", "why": w} for c, w in sorted(no_map.items())]
    return list(units.values()), not_run


# ---------------------------------------------------------------- one driver against one register map

ACCESS_RE = re.compile(r"(?i)(?:^|_)(?:reg|regs|read|write|update|modify|rmw|burst|bits)(?:_|$)")
NOT_ACCESS_RE = re.compile(r"^(?:LOG_|printk|printf|shell_|__ASSERT|BUILD_ASSERT|CHECKIF|ARG_UNUSED|IS_ENABLED|COND_CODE|UTIL_|"
                           r"STRINGIFY|DT_|K_|WRITE_BIT$|IS_BIT_SET$)")
REG_PARAM_RE = re.compile(r"^(?:reg|regs|reg_?addr|register|addr|address|start_?reg|first_?reg|r)$", re.I)
C_KEYWORDS = {"if", "while", "for", "switch", "return", "sizeof", "defined", "else", "case", "do"}


@dataclass
class Assoc:
    addr: int | None
    how: str
    addrs: list[int] = dfield(default_factory=list)       # further candidates (multi-address comment)
    note: str = ""
    strong: bool = True
    token: str = ""           # the register name the macro's name starts with, when that is how it was tied
    section: str = ""         # the section comment above the macro, if any


class UnitCheck:
    def __init__(self, unit: Unit, rm: RegMap, files: list[CFile], docs):
        self.u, self.rm, self.files, self.docs = unit, rm, files, docs
        self.findings: list[dict] = []
        self.counts = {k: [0, 0] for k in CHECKS}          # ran, passed
        self.rows: dict[str, list[dict]] = collections.defaultdict(list)
        self.fields_matched: set[str] = set()
        self.regs_matched: set[int] = set()

    # -- bookkeeping
    def ran(self, k: str, ok: bool) -> None:
        self.counts[k][0] += 1
        self.counts[k][1] += int(ok)

    def find(self, cls: str, check: str, key: str, summary: str, evidence: list[str], severity: str = "check") -> None:
        self.findings.append({"class": cls, "check": CHECKS[check], "key": key, "summary": summary,
                              "evidence": [e for e in evidence if e], "settle": SETTLE[cls], "severity": severity,
                              "doc": self.rm.doc})

    # -- the pass
    def run(self) -> dict:
        self.defines = self._collect()
        self._evaluate()
        self._check_registers()
        self._associate()
        self._check_masks()
        self._check_resets()
        self._check_comments()
        self._check_sites()
        return self.report()

    def _collect(self) -> list[Define]:
        seen: dict[str, Define] = {}
        out = []
        for cf in self.files:
            for d in cf.defines:
                if d.name in seen:
                    seen[d.name].dup += 1
                    continue
                seen[d.name] = d
                out.append(d)
        return out

    def _evaluate(self) -> None:
        objs = {d.name: d.body for d in self.defines if d.params is None and d.body}
        funcs = {d.name: (d.params, d.body) for d in self.defines if d.params is not None}
        ev = Evaluator(objs, funcs)
        for d in self.defines:
            if d.params is not None or not d.body or d.name == "DT_DRV_COMPAT":
                continue
            try:
                d.value = ev.value(d.name)
            except Unresolved as e:
                d.why = str(e)
        # the part prefix: the token before _REG_ in most register-looking names
        pre = collections.Counter(m[1] for d in self.defines for m in [re.match(r"(\w+?)_REG_", d.name)] if m)
        self.prefix = pre.most_common(1)[0][0] if pre else ""
        for d in self.defines:
            d.short = d.name[len(self.prefix) + 1:] if self.prefix and d.name.startswith(self.prefix + "_") else d.name
        regish = {d.name for d in self.defines if is_regish(d.short.upper())}
        for d in self.defines:
            d.kind = classify(d, regish)
        self.regs = {reg_short(d.short.upper()): d for d in self.defines if d.kind == "register"}
        self.masks = [d for d in self.defines if d.kind == "mask"]

    # -- registers
    def _check_registers(self) -> None:
        for r, d in sorted(self.regs.items(), key=lambda x: (x[1].value, x[0])):
            a = d.value
            reg = self.rm.reg(a)
            self.ran("addr", reg is not None)
            named = self.rm.regs_named(r)
            row = {"macro": d.name, "loc": d.loc, "addr": f"{a:#04x}", "datasheet": reg.get("name") if reg else None,
                   "record": reg.get("id") if reg else None, "cite": _cite(reg, self.rm.doc) if reg else None}
            if reg is not None:
                self.regs_matched.add(a)
                row["status"] = "match" + ("" if norm(reg.get("name")) == norm(r) else " (names differ)")
            else:
                row["status"] = "no datasheet register"
                self.find("reg-address-unknown", "addr", d.name,
                          f"{d.name} = {a:#04x}, but {self.rm.doc} has no register at {a:#04x}",
                          [f"{d.loc} {d.name} {d.body}", f"{self.rm.doc}: registers at "
                           + ", ".join(f"{x:#04x}" for x in sorted(self.rm.regs_at)[:24])])
            if named:
                ok = a in named
                self.ran("name", ok)
                if not ok:
                    other = named[0]
                    row["status"] = f"name is the datasheet's {other:#04x}"
                    self.find("reg-address-vs-name", "name", d.name,
                              f"{d.name} = {a:#04x}, but the datasheet's {self.rm.reg(other).get('name')} is at {other:#04x}"
                              + (f"; {a:#04x} is {reg.get('name')}" if reg else ""),
                              [f"{d.loc} {d.name} {d.body}", reg_ev(self.rm, other), reg_ev(self.rm, a) if reg else ""])
            self.rows["registers"].append(row)

    # -- association
    def _contexts(self) -> dict[str, list[tuple[int, str, Any]]]:
        """Per file, the events that set a mask's register context: section comments and register defines;
        a blank line after a block of defines ends the context."""
        ev: dict[str, list] = collections.defaultdict(list)
        for cf in self.files:
            for c in cf.comments:
                if c.standalone:
                    refs = reg_refs(c.text)
                    if refs:
                        ev[cf.path].append((c.end, "comment", (refs, c)))
            for d in cf.defines:
                if d.kind == "register" and self.regs.get(reg_short(d.short.upper())) is d:
                    ev[cf.path].append((d.end, "reg", d))
            commented = {ln for c in cf.comments for ln in range(c.start, c.end + 1)}
            define_lines = {ln for d in cf.defines for ln in range(d.line, d.end + 1)}
            prev_define = False
            for ln, text in enumerate(cf.code.split("\n"), 1):
                if not text.strip() and ln not in commented:
                    if prev_define:
                        ev[cf.path].append((ln, "break", None))
                    prev_define = False
                else:
                    prev_define = ln in define_lines
        for v in ev.values():
            v.sort(key=lambda x: x[0])
        return ev

    def context(self, d: Define):
        last = None
        for line, kind, obj in self.ctx.get(d.file, []):
            if line >= d.line:
                break
            last = (kind, obj) if kind != "break" else None
        return last

    def _assoc(self, d: Define) -> Assoc:
        a = self._assoc0(d)
        ctx = self.context(d)
        if ctx and ctx[0] == "comment":
            a.section = ctx[1][1].text
        return a

    def _assoc0(self, d: Define) -> Assoc:
        s = d.short.upper()
        pre = next((r for r in sorted(self.regs, key=len, reverse=True) if s.startswith(r + "_")), None)
        dsn = next(((nm, a) for nm, a in self.ds_names if s.startswith(nm + "_")), None)
        ctx = self.context(d)
        caddrs, cname = [], None
        if ctx and ctx[0] == "comment":
            cname, caddrs = ctx[1][0][0]
        if pre:
            a = self.regs[pre].value
            note = ""
            if caddrs and a not in caddrs:
                note = f"the comment above names {cname} ({', '.join(f'{x:#04x}' for x in caddrs)})"
            return Assoc(a, f"prefix {self.regs[pre].name}", note=note, token=pre)
        if dsn:
            nm, a = dsn
            if caddrs and a not in caddrs:
                return Assoc(a, f"datasheet name {nm}", note=f"the comment above names {cname} "
                             f"({', '.join(f'{x:#04x}' for x in caddrs)})", strong=False, token=nm)
            return Assoc(a, f"datasheet name {nm}" + (" + comment" if caddrs else ""), token=nm)
        if caddrs:
            base = min(caddrs)
            return Assoc(base, f"comment {cname} ({'/'.join(f'{x:#04x}' for x in caddrs)})",
                         addrs=[x for x in sorted(caddrs) if x != base])
        if ctx and ctx[0] == "reg":
            return Assoc(ctx[1].value, f"position (under {ctx[1].name})", strong=False)
        return Assoc(None, "none")

    def _associate(self) -> None:
        self.ctx = self._contexts()
        self.ds_names = self.rm.name_tokens()
        for d in self.defines:
            if d.kind in ("mask", "reset", "shift", "value"):
                d.info["assoc"] = self._assoc(d)
        # a mask's base name: DEMO_CTRL_FREQ_MASK -> CTRL_FREQ; values named <base>_<X> belong to its field
        self.bases: dict[str, Define] = {}
        for d in self.masks:
            toks = d.short.upper().split("_")
            base = "_".join(toks[:-1]) if toks[-1] in MASK_SUFFIX and len(toks) > 1 else "_".join(toks)
            if base not in self.regs:
                self.bases.setdefault(base, d)
        tails: dict[str, list[Define]] = collections.defaultdict(list)
        for base, d in self.bases.items():
            toks = base.split("_")
            for k in range(1, len(toks)):
                tails["_".join(toks[k:])].append(d)
        for d in self.defines:
            if d.kind not in ("mask", "value", "reset"):
                continue
            s = d.short.upper()
            sib = next((m for b, m in sorted(self.bases.items(), key=lambda x: -len(x[0])) if s.startswith(b + "_") and m is not d), None)
            if sib is None:
                sib = next((ms[0] for t, ms in sorted(tails.items(), key=lambda x: -len(x[0]))
                            if len(ms) == 1 and len(t) >= 3 and s.startswith(t + "_") and ms[0] is not d
                            and not ms[0].short.upper().startswith(s)), None)
            if sib is not None:
                d.info["sibling"] = sib

    # -- masks
    def _stated_order(self, d: Define) -> str | None:
        a: Assoc = d.info["assoc"]
        text = " ".join((d.lead, d.trail, a.section))
        if re.search(r"(?i)\blittle[- ]endian\b|\bLE\b", text):
            return "le"
        if re.search(r"(?i)\bbig[- ]endian\b|\bBE\b", text):
            return "be"
        return None

    def _word_orders(self) -> None:
        """One byte order per multi-register word: stated in a comment, else voted by that word's wide masks
        (an exact field match whose name agrees with the macro's), little-endian on a tie."""
        votes: dict[int, collections.Counter] = collections.defaultdict(collections.Counter)
        self.stated: dict[int, str] = {}
        for d in self.masks:
            a: Assoc = d.info["assoc"]
            m = d.info["m"]
            if a.addr is None or self.rm.reg(a.addr) is None or not m >> self.rm.width(a.addr):
                continue
            st = self._stated_order(d)
            if st:
                self.stated.setdefault(a.addr, st)
            for o in ("le", "be"):
                r = classify_mask(self.rm, a.addr, m, o)
                if r["status"] == "exact" and any(norm(f.name) and norm(f.name) in norm(d.short) for f in r["fields"]):
                    votes[a.addr][o] += 1
        self.word_order = {a: self.stated.get(a) or ("be" if v["be"] > v["le"] else "le") for a, v in votes.items()}
        self.word_order.update(self.stated)
        self.order_why = {a: "stated in a comment" if a in self.stated else f"by vote {votes[a]['le']} le / {votes[a]['be']} be"
                          for a in self.word_order}

    def _classify(self, d: Define, m: int | None = None) -> dict:
        a: Assoc = d.info["assoc"]
        m = d.value if m is None else m
        if a.addr is None:
            return {"status": "unassociated", "pieces": [], "fields": []}
        res = classify_mask(self.rm, a.addr, m, self.word_order.get(a.addr, "le"))
        for alt in a.addrs:
            if res["status"] in OK_STATUS:
                break
            r2 = classify_mask(self.rm, alt, m, self.word_order.get(alt, "le"))
            if r2["status"] in OK_STATUS:
                res = {**r2, "note": f"matches at {alt:#04x}, one of the comment's addresses"}
        if res.get("order"):
            res["note"] = "; ".join(x for x in (res.get("note", ""), f"{res['order']} word, "
                                                 f"{self.order_why.get(a.addr, 'little-endian by default')}") if x)
        return res

    def _shift_of(self, d: Define) -> Define | None:
        toks = d.short.upper().split("_")
        base = "_".join(toks[:-1]) if toks[-1] in MASK_SUFFIX else d.short.upper()
        for x in self.defines:
            if x.kind == "shift" and x.short.upper().rsplit("_", 1)[0] == base:
                return x
        return None

    def _check_masks(self) -> None:
        for d in self.masks:
            m = d.value
            sh = self._shift_of(d)
            if sh is not None and lowbit(m) == 0 and sh.value > 0:
                m = m << sh.value
                d.info["shift_note"] = f"unshifted; {sh.name} = {sh.value} applied"
            d.info["m"] = m
        self._word_orders()
        for d in self.masks:
            d.info["res"] = self._classify(d, d.info["m"])
        for d in self.masks:
            m, res, shifted_note = d.info["m"], d.info["res"], d.info.get("shift_note", "")
            a: Assoc = d.info["assoc"]
            row = {"macro": d.name, "loc": d.loc, "value": f"{m:#x}", "bits": bits_str(m),
                   "register": f"{a.addr:#04x}" if a.addr is not None else None, "how": a.how,
                   "fields": [f.id for f in res.get("fields", [])], "order": res.get("order"),
                   "note": "; ".join(x for x in (a.note, res.get("note", ""), shifted_note) if x)}
            st = res["status"]
            sib = d.info.get("sibling")
            if st in OK_STATUS:
                self.ran("mask", True)
                for f in res["fields"]:
                    self.fields_matched.add(f.id)
                names = [f.name for f in res["fields"]]
                row["status"] = "match" if st == "exact" else (
                    "match (" + "+".join(names) + ")" if len(names) <= 3 else f"match ({len(names)} whole fields)")
            elif st == "within" and d.short.upper().split("_")[-1] not in MASK_SUFFIX and (
                    sib is not None and sib.info.get("res", {}).get("status") == "exact"
                    and res["fields"][0].id in [f.id for f in sib.info["res"]["fields"]]
                    or norm(res["fields"][0].name) in norm(d.short)):
                self.ran("mask", True)
                row["status"] = f"value of {res['fields'][0].name}"
                pa = next((pa for pa, pm in res["pieces"] if pm), a.addr)
                d.info["value_of"] = (res["fields"][0], pa, (res.get("offsets") or {}).get(pa, 0))
            elif st in ("unassociated", "no-register", "no-fields"):
                row["status"] = {"unassociated": "no register found", "no-register": "register not in datasheet",
                                 "no-fields": "register has no field records"}[st]
            else:
                self.ran("mask", False)
                row["status"] = "mismatch"
                self._mask_finding(d, m, res)
            self.rows["masks"].append(row)
            self._check_field_name(d, res)

    def _mask_finding(self, d: Define, m: int, res: dict) -> None:
        a: Assoc = d.info["assoc"]
        reg = self.rm.reg(a.addr)
        where = f"{reg.get('name')} ({a.addr:#04x}, by {a.how})" if reg else f"{a.addr:#04x}"
        if res["status"] == "within":
            f = res["fields"][0]
            what = f"covers bits {bits_str(m)} of {f.name}[{f.hi}:{f.lo}], not the whole field"
        else:
            parts = []
            if res.get("reserved"):
                parts.append("reserved bits " + ", ".join(f"{f.name}[{f.hi}:{f.lo}]" for f in res["reserved"]))
            if res.get("undefined"):
                parts.append(f"bits {bits_str(res['undefined'])} that no field defines")
            partial = [f for f in res.get("fields", []) if f not in res.get("reserved", []) and f.mask & ~m]
            if partial:
                parts.append("part of " + ", ".join(f"{f.name}[{f.hi}:{f.lo}]" for f in partial))
            what = "touches " + "; ".join(parts) if parts else "matches no field's bit range"
        word = f" (as a {res['order']} word)" if res.get("order") else ""
        ev = [f"{d.loc} {d.name} {d.body}", reg_ev(self.rm, a.addr)] + [field_ev(self.rm, pa, f)
                                                                         for pa, pm in res.get("pieces", [])
                                                                         for f in self.rm.fields(pa) if f.mask & pm][:4]
        self.find("reg-mask-vs-field", "mask", d.name, f"{d.name} = {m:#x} (bits {bits_str(m)}) in {where}{word} {what}",
                  ev, "check" if a.strong else "info")

    def _field_name_cands(self, d: Define) -> list[str]:
        """`CTRL1_HIZ_MASK` under CTRL1 -> HIZ_MASK, HIZ, MASK: the name with the register stripped, then shorter."""
        a: Assoc = d.info["assoc"]
        s = d.short.upper()
        if a.token and s.startswith(a.token + "_"):
            s = s[len(a.token) + 1:]
        toks = [t for t in s.split("_") if t]
        out = ["_".join(toks[k:]) for k in range(len(toks))]
        if len(toks) > 1 and toks[-1] in MASK_SUFFIX:
            out += ["_".join(toks[k:-1]) for k in range(len(toks) - 1)]
        return sorted(dict.fromkeys(out), key=lambda c: -len(c))

    def _check_field_name(self, d: Define, res: dict) -> None:
        a: Assoc = d.info["assoc"]
        if a.addr is None or self.rm.reg(a.addr) is None:
            return
        here = {pa for pa, _ in res.get("pieces") or [[a.addr, 0]]} | {a.addr}
        for cand in self._field_name_cands(d):
            hits = [(pa, f) for pa, f in self.rm.all_fields() if norm(f.name) == norm(cand)]
            if not hits:
                continue
            if any(pa in here for pa, _ in hits):
                self.ran("fname", True)
                return
            others = sorted({pa for pa, _ in hits})
            if len(cand) < 3 or cand in GENERIC_FIELDS or len(others) != 1:
                return
            self.ran("fname", False)
            oa, of = hits[0]
            bits_there = " (the same bits)" if of.mask == d.value or any(pm == of.mask for pa, pm in res.get("pieces", []) if pa == oa) else ""
            now = res.get("fields") or []
            here_s = (f"; at {a.addr:#04x} those bits are " + ", ".join(f"{f.name}[{f.hi}:{f.lo}]" for f in now)) if now else ""
            reg = self.rm.reg(a.addr) or {}
            self.find("reg-field-in-other-register", "fname", d.name,
                      f"{d.name} sits under {reg.get('name')} ({a.addr:#04x}, by {a.how}) but the datasheet's {of.name} "
                      f"field is in {self.rm.reg(oa).get('name')} ({oa:#04x}) bits {of.hi}:{of.lo}{bits_there}{here_s}",
                      [f"{d.loc} {d.name} {d.body}", field_ev(self.rm, oa, of)]
                      + [field_ev(self.rm, a.addr, f) for f in now[:2]], "check" if a.strong else "info")
            return

    # -- resets and defaults
    def _reg_reset_claim(self, a: int, v: int) -> tuple[bool | None, str]:
        """(agrees, datasheet value) for a claimed reset of the register at `a` (or the word from `a`)."""
        w = self.rm.width(a)
        ds = self.rm.reset(a)
        if v >> w == 0 and ds is not None:
            return v == ds, f"{ds:#0{2 + (w + 3) // 4}x}"
        n = math.ceil(max(v.bit_length(), 1) / w)
        vals = [self.rm.reset(a + k) for k in range(n)]
        if n > 1 and all(x is not None for x in vals):
            le = sum(x << (k * w) for k, x in enumerate(vals))
            be = sum(x << ((n - 1 - k) * w) for k, x in enumerate(vals))
            return v in (le, be), f"{le:#x} (le word)" + ("" if v != be or v == le else f", {be:#x} be")
        return None, "undefined"

    def _reset_finding(self, d_or_loc: str, name: str, claim: str, v: int, a: int, ds: str, fld: Fld | None = None,
                       severity: str = "check") -> None:
        reg = self.rm.reg(a) or {}
        target = f"{reg.get('name')}.{fld.name}" if fld else f"{reg.get('name')} ({a:#04x})"
        self.find("reg-reset-vs-datasheet", "reset", name, f"{claim} gives {target} a reset of {v:#x}; the datasheet says {ds}",
                  [d_or_loc, field_ev(self.rm, a, fld) if fld else reg_ev(self.rm, a)], severity)

    @staticmethod
    def _field_value(f: Fld, v: int, shifted: bool | None) -> int | None:
        """A claimed value as the field's own value: shifted into place (a macro built with BIT/GENMASK), or not
        (a number in a comment); None when it fits neither reading."""
        if shifted is not False and not v & ~f.mask and (shifted or v >> f.width):
            return (v & f.mask) >> f.lo
        if shifted is not True and not v >> f.width:
            return v
        return None

    def _check_resets(self) -> None:
        rows = self.rows["resets"]
        for d in self.defines:
            # a reset constant: <REG>_RESET_VAL (register) or <FIELD>_DEFAULT (field)
            if d.kind == "reset":
                base = RESET_RE.sub("", d.short.upper())
                if base in self.regs:
                    ra = self.regs[base].value
                    ok, ds = self._reg_reset_claim(ra, d.value)
                    self._reset_row(rows, d.loc, d.name, "constant", d.value, ra, None, ok, ds)
                    continue
                fx = self._exact_field(self.bases.get(base))
                if fx:
                    self._field_reset(rows, d.loc, d.name, "constant", d.value, fx, True if MASKY_BODY.search(d.body) else None)
                continue
            # a field mask with `default 0x3` beside it; a value macro marked `(POR default)`
            if d.kind == "mask" and d.trail:
                fx = self._exact_field(d)
                for m in RESET_CLAIM.finditer(d.trail):
                    v = claim_number(m[1])
                    if fx is not None and v is not None:
                        self._field_reset(rows, d.loc, d.name, f"comment '{m[0].strip()}'", v, fx, None)
            if d.kind in ("mask", "value") and DEFAULT_MARK.search(d.trail or ""):
                sib = d.info.get("sibling")
                fx = self._exact_field(sib) if sib is not None else d.info.get("value_of")
                if fx is not None:
                    self._field_reset(rows, d.loc, d.name, f"comment '{DEFAULT_MARK.search(d.trail)[0]}'", d.value, fx,
                                      True if MASKY_BODY.search(d.body) else None)
            # a register define with `reset 0x..` beside it
            if d.kind == "register":
                for m in RESET_CLAIM.finditer(d.trail + " " + ("" if reg_refs(d.lead) else d.lead)):
                    v = claim_number(m[1])
                    if v is not None:
                        ok, ds = self._reg_reset_claim(d.value, v)
                        self._reset_row(rows, d.loc, d.name, f"comment '{m[0].strip()}'", v, d.value, None, ok, ds)
        # section comments: `CTRL (05h) [reset = 0x20]`
        for cf in self.files:
            for c in cf.comments:
                refs = reg_refs(c.text) if c.standalone else []
                if not refs:
                    continue
                name, addrs = refs[0]
                for m in RESET_CLAIM.finditer(c.text):
                    v = claim_number(m[1])
                    if v is None:
                        continue
                    a0 = min(addrs)
                    if self.rm.reg(a0) is None:
                        continue
                    ok, ds = self._reg_reset_claim(a0, v)
                    self._reset_row(rows, f"{cf.path}:{c.start}", name, f"comment '{' '.join(m[0].split())}'", v, a0, None, ok, ds)

    def _exact_field(self, d: Define | None) -> tuple[Fld, int, int] | None:
        """(field, its register, the bit offset of that register in the driver's word) for a one-field mask."""
        if d is None:
            return None
        res = d.info.get("res") or {}
        if res.get("status") != "exact" or len(res.get("pieces", [])) != 1:
            return None
        pa = res["pieces"][0][0]
        return res["fields"][0], pa, (res.get("offsets") or {}).get(pa, 0)

    def _field_reset(self, rows, loc, name, claim, v, fx: tuple[Fld, int, int], shifted: bool | None) -> None:
        f, a, off = fx
        if off and v and not v & ((1 << off) - 1):
            v = v >> off                     # a word-level value -> the field's own register
        fv = self._field_value(f, v, shifted)
        if fv is None or f.reset is None:
            ok, ds = None, "undefined" if f.reset is None else f"{f.rec.get('reset')}"
        else:
            ok, ds = fv == f.reset, f"{f.rec.get('reset')}"
        self._reset_row(rows, loc, name, claim, v if fv is None else fv, a, f, ok, ds)

    def _reset_row(self, rows, loc, name, claim, v, a, f, ok, ds) -> None:
        reg = self.rm.reg(a) or {}
        rows.append({"what": name, "loc": loc, "claim": claim, "fw": f"{v:#x}",
                     "target": f"{reg.get('name')}.{f.name}" if f else f"{reg.get('name')} ({a:#04x})",
                     "datasheet": ds, "status": "match" if ok else ("mismatch" if ok is False else "not comparable")})
        if ok is None:
            return
        self.ran("reset", ok)
        if not ok:
            self._reset_finding(f"{loc} {claim}", name, f"{name} ({claim})" if not claim.startswith("comment") else claim,
                                v, a, ds, f, "check" if claim == "constant" else "info")

    # -- comment claims: bit positions and pin-set resets
    def _related(self, reg_name: str) -> list[str]:
        """Quirk/rule/strap records of the doc that talk about this register (`NAME[`, `NAME.`, `.NAME.` in the id)."""
        pat = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(reg_name) + r"(?:\[|\.\w|\s+register\b)")
        out = []
        for kind in ("quirk", "rule", "strap"):
            for r in self.docs.records(self.rm.doc, kind):
                txt = " ".join(str(r.get(k) or "") for k in ("text", "condition", "why"))
                if f".{reg_name}." in (r.get("id") or "") or pat.search(txt):
                    t = " ".join(str(r.get("text") or r.get("condition") or "").split())
                    t = t if len(t) <= 140 else t[:140].rsplit(" ", 1)[0] + " ..."
                    out.append(f"{r.get('id')} ({_cite(r, self.rm.doc)}): {t}")
        return out[:3]

    def _check_comments(self) -> None:
        rows = self.rows["comments"]
        blocks: list[tuple[str, str, list[int], str]] = []     # loc, register name, addresses, text
        for cf in self.files:
            for c in cf.comments:
                refs = reg_refs(c.text) if c.standalone else []
                if refs:
                    blocks.append((f"{cf.path}:{c.start}", refs[0][0], refs[0][1], c.text))
        for d in self.regs.values():
            txt = " ".join(x for x in ("" if reg_refs(d.lead) else d.lead, d.trail) if x)
            if txt:
                blocks.append((d.loc, d.name, [d.value], txt))
        for loc, name, addrs, text in blocks:
            addrs = [x for x in addrs if self.rm.reg(x) is not None]
            if not addrs:
                continue
            for fname, hi, lo, q in bit_claims(text):
                hits = [(pa, f) for pa in addrs for f in self.rm.fields(pa) if norm(f.name) == norm(fname)]
                if not hits:
                    continue
                w = self.rm.width(min(addrs))
                ok = any((f.hi, f.lo) == (hi, lo) or (f.hi + (pa - min(addrs)) * w, f.lo + (pa - min(addrs)) * w) == (hi, lo)
                         for pa, f in hits)
                self.ran("comment", ok)
                pa, f = hits[0]
                rows.append({"loc": loc, "claim": q, "datasheet": f"{f.name}[{f.hi}:{f.lo}] @{pa:#04x}",
                             "status": "match" if ok else "mismatch"})
                if not ok:
                    self.find("reg-comment-vs-datasheet", "comment", f"{name}.{fname}",
                              f"the comment on {name} says {fname} is bits {hi}:{lo}; the datasheet has "
                              f"{f.name}[{f.hi}:{f.lo}] in {self.rm.reg(pa).get('name')} ({pa:#04x})",
                              [f"{loc} comment: {q}", field_ev(self.rm, pa, f)], "info")
            for sent in pin_set_sentences(text):
                for a in addrs:
                    reg = self.rm.reg(a)
                    fields = self.rm.fields(a)
                    fixed = self.rm.reset(a) is not None and fields and all(f.reset is not None for f in fields)
                    self.ran("comment", not fixed)
                    rows.append({"loc": loc, "claim": sent[:160], "datasheet": f"{reg.get('name')} reset {reg.get('reset')}",
                                 "status": "not supported by the records" if fixed else "datasheet reset not fixed"})
                    if fixed:
                        self.find("reg-comment-vs-datasheet", "comment", f"{reg.get('name')}.pin-set-reset",
                                  f"the comment on {name} says register bits are set by a pin at power-on; the datasheet gives "
                                  f"{reg.get('name')} ({a:#04x}) a fixed reset {reg.get('reset')} with every field's reset defined",
                                  [f"{loc} comment: {sent[:160]}", reg_ev(self.rm, a)] + self._related(reg.get("name") or ""),
                                  "info")

    # -- call sites
    def _check_sites(self) -> None:
        regs_by_name = {d.name: d for d in self.regs.values()}
        masks_by_name = {d.name: d for d in self.masks}
        st = {"calls": 0, "named": 0, "unresolved": [], "several": 0, "wrapper": 0}
        for cf in self.files:
            if not cf.path.endswith(".c"):
                continue
            code = cf.code
            wrappers = _register_wrappers(code)
            for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", code):
                fn = m[1]
                if fn in C_KEYWORDS or fn in BUILTINS or fn in masks_by_name or fn in regs_by_name or NOT_ACCESS_RE.match(fn):
                    continue
                ls = code.rfind("\n", 0, m.start()) + 1
                if code[ls:m.start()].lstrip().startswith("#"):
                    continue
                args, _ = _call_args(code, m.end() - 1)
                if args is None or _is_declaration(code, m.start()):
                    continue
                direct = [x for a in args for x in re.findall(r"\b[A-Za-z_]\w*\b", _strip_calls(a)) if x in regs_by_name]
                named = sorted(set(direct))
                if not ACCESS_RE.search(fn) and not named:
                    continue
                line = code.count("\n", 0, m.start()) + 1
                if not named and any(a <= m.start() < b for a, b in wrappers):
                    st["wrapper"] += 1          # inside a helper that takes the register as a parameter
                    continue
                st["calls"] += 1
                if not named:
                    st["unresolved"].append(f"{cf.path}:{line} {fn}()")
                    continue
                if len(named) > 1:
                    st["several"] += 1
                    continue
                st["named"] += 1
                rd = regs_by_name[named[0]]
                for x in dict.fromkeys(x for a in args for x in re.findall(r"\b[A-Za-z_]\w*\b", a) if x in masks_by_name):
                    md = masks_by_name[x]
                    a: Assoc = md.info["assoc"]
                    if a.addr is None or not a.strong:
                        continue
                    res = md.info.get("res") or {}
                    span = {pa for pa, _ in res.get("pieces") or []} | {a.addr} | set(a.addrs)
                    ok = rd.value in span
                    self.ran("site", ok)
                    if not ok:
                        reg = self.rm.reg(a.addr) or {}
                        here = self.rm.reg(rd.value) or {}
                        fit = classify_mask(self.rm, rd.value, md.value, self.word_order.get(rd.value))
                        fit_s = (" (its bits are " + "+".join(f.name for f in fit["fields"]) + f" in {here.get('name')})"
                                 if fit["status"] in OK_STATUS else "")
                        self.find("reg-field-in-other-register", "site", f"{x}@{cf.path}:{line}",
                                  f"{fn}() at {cf.path}:{line} applies {x} (a field of {reg.get('name') or hex(a.addr)} "
                                  f"{a.addr:#04x}, by {a.how}) to {rd.name} ({rd.value:#04x}){fit_s}",
                                  [f"{cf.path}:{line} {fn}({', '.join(' '.join(s.split()) for s in args)[:160]})",
                                   f"{md.loc} {md.name} {md.body}", f"{rd.loc} {rd.name} {rd.body}",
                                   reg_ev(self.rm, a.addr), reg_ev(self.rm, rd.value)])
        self.sites = st

    # -- the unit's report
    def report(self) -> dict:
        by_kind = collections.Counter(d.kind for d in self.defines)
        found = [d for d in self.defines if d.kind not in ("function", "flag")]
        resolved = [d for d in found if d.value is not None]
        return {
            "compat": self.u.compat, "doc": self.rm.doc, "doc_via": self.u.via, "nodes": sorted(self.u.nodes),
            "drivers": self.u.drivers, "built": self.u.built, "files": [f.path for f in self.files], "prefix": self.prefix,
            "macros": {"found": len(found), "resolved": len(resolved), "by_kind": dict(sorted(by_kind.items())),
                       "function_like": by_kind.get("function", 0),
                       "unresolved": [{"name": d.name, "loc": d.loc, "why": d.why} for d in found if d.value is None]},
            "datasheet": {"registers": len(self.rm.regs_at), "fields": sum(len(v) for v in self.rm.fields_at.values())},
            "registers_matched": self.counts["addr"][1], "register_macros": self.counts["addr"][0],
            "fields_matched": len(self.fields_matched),
            "sites": {"calls": self.sites["calls"], "named": self.sites["named"], "several": self.sites["several"],
                      "inside_wrappers": self.sites["wrapper"], "unresolved": self.sites["unresolved"]},
            "checks": {CHECKS[k]: {"ran": v[0], "passed": v[1]} for k, v in self.counts.items()},
            "registers": self.rows["registers"], "masks": self.rows["masks"], "resets": self.rows["resets"],
            "comments": self.rows["comments"],
        }


def _register_wrappers(code: str) -> list[tuple[int, int]]:
    """Body spans of functions with a register-like parameter (`reg`, `addr`, ...): the helpers every register
    access goes through. A call inside one names no register macro by design; its callers are the sites."""
    out = []
    for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\(([^;{}()]*)\)\s*\{", code):
        if m[1] in C_KEYWORDS or not _is_declaration(code, m.start()):
            continue
        names = [re.findall(r"[A-Za-z_]\w*", p)[-1] for p in m[2].split(",") if re.findall(r"[A-Za-z_]\w*", p)]
        if not any(REG_PARAM_RE.match(n) for n in names):
            continue
        open_i = m.end() - 1
        depth, i = 0, open_i
        while i < len(code):
            depth += {"{": 1, "}": -1}.get(code[i], 0)
            if depth == 0:
                break
            i += 1
        out.append((open_i, i))
    return out


def _call_args(code: str, open_i: int) -> tuple[list[str] | None, int]:
    depth, start, args, i = 0, open_i + 1, [], open_i
    while i < len(code):
        c = code[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                tail = code[start:i]
                if tail.strip() or args:
                    args.append(tail)
                return args, i
        elif c == "," and depth == 1:
            args.append(code[start:i])
            start = i + 1
        elif c == ";" and depth <= 1:
            return None, i
        i += 1
    return None, i


def _strip_calls(arg: str) -> str:
    """An argument with nested calls blanked, so `f(g(REG_X))` credits the register to g, not f."""
    out = arg
    for m in list(re.finditer(r"\b[A-Za-z_]\w*\s*\(", arg))[::-1]:
        if m[0].split("(")[0].strip() in TYPE_WORDS:
            continue
        _, end = _call_args(arg, m.end() - 1)
        out = out[:m.start()] + " " * (end + 1 - m.start()) + out[end + 1:]
    return out


def _is_declaration(code: str, at: int) -> bool:
    k = max(code.rfind(ch, 0, at) for ch in ";{}")
    pre = code[k + 1:at]
    pre = re.sub(r"^\s*#.*$", "", pre, flags=re.M).strip()
    if not pre or re.search(r"[=(,!&|?:+\-<>\[]", pre) or re.search(r"\b(?:return|else|do|case|goto)\b", pre):
        return False
    return bool(re.fullmatch(r"[\w\s\*]+", pre))


# ---------------------------------------------------------------- the whole atlas

def analyze(ad: Path, stamp: dict, dt: dict, docs, pins: dict, bindings: dict) -> dict:
    src = Sources(ad, stamp)
    units, not_run = discover(dt, docs, pins, bindings)
    out_units, findings = [], []
    totals = {k: [0, 0] for k in CHECKS}
    for u in sorted(units, key=lambda x: (x.doc, x.drivers)):
        rm = RegMap(u.doc, docs.records(u.doc, "register"), docs.records(u.doc, "field"))
        files = gather_files(src, u.drivers)
        if not files:
            not_run.append({"check": f"register constants vs datasheet: {u.compat}",
                            "why": f"driver sources not readable ({', '.join(u.drivers)})"})
            continue
        uc = UnitCheck(u, rm, files, docs)
        out_units.append(uc.run())
        findings += uc.findings
        for k, v in uc.counts.items():
            totals[k][0] += v[0]
            totals[k][1] += v[1]
    if not units and not not_run:
        not_run.append({"check": "register constants vs datasheet",
                        "why": "no DT node with a driver in the build maps to a doc with register records"})
    not_run.append({"check": "register constants vs datasheet: MCU peripherals",
                    "why": "the SoC's drivers reach registers through vendor HAL/CMSIS headers; `mcu vote` checks those"})
    rank = {c: i for i, c in enumerate(ORDER)}
    findings.sort(key=lambda f: (rank.get(f["class"], len(ORDER)), f["key"], f["summary"]))
    found = sum(u["macros"]["found"] for u in out_units)
    resolved = sum(u["macros"]["resolved"] for u in out_units)
    calls = sum(u["sites"]["calls"] for u in out_units)
    named = sum(u["sites"]["named"] for u in out_units)
    checks = [{"check": CHECKS[k], "sources": CHECK_SOURCES, "ran": v[0], "passed": v[1],
               "candidates": sum(1 for f in findings if f["check"] == CHECKS[k])} for k, v in totals.items()]
    regs_m = sum(u["registers_matched"] for u in out_units)
    regs_t = sum(u["register_macros"] for u in out_units)
    flds_m = sum(u["fields_matched"] for u in out_units)
    flds_t = sum(u["datasheet"]["fields"] for u in out_units)
    summary = (f"{len(out_units)} driver/register-map pair(s); {found} macros, {resolved} resolved"
               f" ({_pct(resolved, found)}); registers {regs_m}/{regs_t} matched, {flds_m} of {flds_t} datasheet fields "
               f"matched by a mask; {named}/{calls} register-access sites name their register ({_pct(named, calls)}); "
               f"{len(findings)} finding(s)")
    return {"atlas": stamp.get("id"), "summary": summary, "units": out_units, "checks": checks,
            "findings": findings, "not_run": not_run,
            "totals": {"macros_found": found, "macros_resolved": resolved, "registers_matched": regs_m,
                       "register_macros": regs_t, "fields_matched": flds_m, "datasheet_fields": flds_t,
                       "sites": calls, "sites_named": named}}


def _pct(a: int, b: int) -> str:
    return f"{100 * a / b:.1f}%" if b else "n/a"


def render(rep: dict, stamp: dict) -> str:
    L = [f"# Register constants vs datasheet: {rep['atlas']}", "",
         f"Target `{stamp.get('target')}`, commit `{stamp.get('commit')}`. Driver `#define`s evaluated and joined to "
         "the part's register/field records by (address, bit range). Findings are questions for the engineer.", "",
         f"**{rep['summary']}.**", ""]
    for u in rep["units"]:
        mc = u["macros"]
        L += [f"## {u['compat']} -> {u['doc']}", "",
              f"Driver {', '.join(u['drivers'])}{'' if u['built'] else ' (not in this build)'}; part by {u['doc_via']}; "
              f"nodes {', '.join(n.rsplit('/', 1)[-1] for n in u['nodes'])}. Files: {', '.join(u['files'])}.", "",
              f"Macros: {mc['found']} found, {mc['resolved']} resolved ({_pct(mc['resolved'], mc['found'])}); "
              + ", ".join(f"{k} {v}" for k, v in mc["by_kind"].items()) + ". "
              f"Datasheet: {u['datasheet']['registers']} registers, {u['datasheet']['fields']} fields; "
              f"{u['registers_matched']} registers and {u['fields_matched']} fields matched. "
              f"Register-access sites: {u['sites']['calls']}, {u['sites']['named']} name one register macro"
              + (f", {u['sites']['several']} name several" if u['sites']['several'] else "")
              + (f" ({u['sites']['inside_wrappers']} more inside helpers that take the register as a parameter)"
                 if u['sites']['inside_wrappers'] else "") + ".", "",
              "### Registers", "", "| macro | addr | datasheet | cite | status |", "|---|---|---|---|---|"]
        for r in u["registers"]:
            L.append(f"| {r['macro']} | {r['addr']} | {r['datasheet'] or '-'} | {r['cite'] or '-'} | {r['status']} |")
        L += ["", "### Masks", "", "| macro | bits | register (how) | datasheet field | status |", "|---|---|---|---|---|"]
        for r in u["masks"]:
            how = f"{r['register'] or '-'} ({r['how']})"
            note = f"; {r['note']}" if r.get("note") else ""
            fl = [x.split('.', 2)[-1] for x in r["fields"]]
            fs = ", ".join(fl[:3]) + (f", ... ({len(fl)})" if len(fl) > 3 else "")
            L.append(f"| {r['macro']} | {r['bits']} | {how} | {fs or '-'} | {r['status']}{note} |")
        if u["resets"]:
            L += ["", "### Resets and defaults", "", "| claim | where | fw | target | datasheet | status |", "|---|---|---|---|---|---|"]
            for r in u["resets"]:
                L.append(f"| {r['what']}: {r['claim']} | {r['loc']} | {r['fw']} | {r['target']} | {r['datasheet']} | {r['status']} |")
        if u["comments"]:
            L += ["", "### Comment claims", ""]
            for r in u["comments"]:
                L.append(f"- {r['loc']}: \"{r['claim']}\" vs {r['datasheet']}: {r['status']}")
        if mc["unresolved"]:
            L += ["", "### Unresolved macros", ""]
            for x in mc["unresolved"][:20]:
                L.append(f"- {x['name']} ({x['loc']}): {x['why']}")
            if len(mc["unresolved"]) > 20:
                L.append(f"- ... {len(mc['unresolved']) - 20} more in codecheck.json")
        if u["sites"]["unresolved"]:
            L += ["", "Register-access sites with no register macro (the register is a variable or a buffer): "
                  + ", ".join(u["sites"]["unresolved"][:12]) + (" ..." if len(u["sites"]["unresolved"]) > 12 else "")]
        L.append("")
    L += ["## Findings", ""]
    for f in rep["findings"]:
        L.append(f"- [{f['class']}] **{f['key']}**: {f['summary']}  \n  evidence: {' | '.join(f['evidence'][:5])}  \n"
                 f"  settle: {f['settle']}")
    if not rep["findings"]:
        L.append("None.")
    if rep["not_run"]:
        L += ["", "## Not run", ""] + [f"- {n['check']}: {n['why']}" for n in rep["not_run"]]
    return "\n".join(L).rstrip() + "\n"


def write(atlas: Path, rep: dict, stamp: dict) -> None:
    write_json(atlas / "codecheck.json", rep)
    write_text(atlas / "codecheck.md", render(rep, stamp))


def run_join(J, stamp: dict, pins: dict) -> dict:
    """The analysis for `join`: reuses the join's DT, docs and bindings; rewrites codecheck.json/md."""
    rep = analyze(J.ad, stamp, J.dt, J.docs, pins, J.bindings)
    write(J.atlas, rep, stamp)
    return rep


def run(ad: Path, atlas: Path, log=print) -> dict:
    from embarch_atlas.join import Docs
    stamp = read_json(atlas / "atlas.json")
    if not (atlas / "dt.json").is_file():
        raise SystemExit(f"{atlas}: no dt.json (run `join` first)")
    dt = read_json(atlas / "dt.json")
    pins = read_json(atlas / "pins.json") if (atlas / "pins.json").is_file() else {}
    bp = ad / "boards" / "bindings.json"
    bindings = read_json(bp) if bp.is_file() else {}
    rep = analyze(ad, stamp, dt, Docs(ad), pins, bindings)
    write(atlas, rep, stamp)
    log(f"{stamp.get('id')}: {rep['summary']}")
    return rep


def cmd_codecheck(args) -> int:
    from embarch_atlas.build import resolve_atlas, resolve_atlas_dir
    ad = resolve_atlas_dir(args)
    run(ad, resolve_atlas(ad, args.atlas))
    return 0


def register(sub, groups) -> None:
    import argparse
    p = sub.add_parser("codecheck", help="register constants in driver sources vs the datasheet register map")
    p.add_argument("atlas", help="atlas id (<target>@<commit12>), its dir, or a target name (its current atlas)")
    p.add_argument("--fw", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_codecheck)

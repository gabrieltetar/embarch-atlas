"""`doc check <doc_id>`: every deterministic check on one document's records and markdown.

Record checks (per record, against the text layer of the pages it cites):
  V  schema          the record validates against schemas/record.schema.json
  A  quote anchoring every quote occurs on a cited page, in the pdftotext or the PyMuPDF text layer,
                     with Symbol-font private-use glyphs mapped or stripped (Read shows them as nothing);
                     the parts of a quote split with `...` occur in order and near each other (a short
                     part on the same or the adjacent line), so a trailing number is tied to its line
  N  number in quote every numeric min/nom/typ/max is a number inside the quote (requirements: every
                     number of `text`)
  K  columns         for a table row under a MIN/TYP/MAX header (or a single VALUE column), word
                     x-positions give an independent column assignment (and the UNIT column when there
                     is one); it must agree. `±X` in MIN or MAX is min -X, max X; `±X` in TYP is a
                     magnitude (the record keeps the string `"±X"`, or X); `X%` is X with unit %; `a to
                     b` in a VALUE column is min a, max b. A record value kept as an expression string
                     is not compared. A row K cannot find is a warning, counted per document
  C  conditions      every number in `cond` is on a cited page
  U  units           the unit's dimension fits the symbol (V, I, t, R, T, f, C, P, L) or the parameter,
                     and the unit string is on the cited page
  O  order           min <= nom/typ <= max
  R  references      quirk/conflict refs resolve to a record of any document under docs/ (ids are
                     globally unique, `<part>.<kind>...`: an RM vote conflict refs SVD registers); ids
                     are unique (a deterministic record and an LLM record may share an id: they are
                     twins, compared by G)
  X  invariants      `check` records: expr evaluated over R['<id>'].min/nom/typ/max/value/reset/bits/
                     addr/width, result == expect
  G  registers       field widths cover the register, no overlap, field resets compose to the register
                     reset, unique addresses, summary-table coverage, and LLM vs deterministic twins (an
                     LLM `reset: null` with a `note` is a variant- or strap-dependent reset: not compared)
Markdown checks (card.md, s/*.md):
  M  numbers         every number is on a page the line cites (or its heading cites), is a value of a
                     verified record on those pages (a record on another page only warns), or is tagged
                     (derived), (inference), (figure) or (equation). A tag covers the numbers before it in
                     its clause (up to `;`, `:` or a sentence end; in a table, its cell), not the whole
                     line; a Note cell that is only a tag covers its row. An `[SVD]` cite covers its
                     clause the same way (reference-manual prose citing the SVD). Rendered structural
                     cells (Bits, Width, Order, Priority, enum keys) and the expressions in
                     s/invariants.md are not numeric claims, nor are numbered-list markers, `Tier N`
                     labels, token counts (a `tokens` column, `N tokens`) and backticked identifiers
                     and file names (`LINE0_1`, `s/7.3.md`)
  S  table structure the ground-truth-free converter checks of spike 10 on value columns: glued numbers,
                     packed cells, fused MIN/TYP/MAX header, orphaned subscript, min <= typ <= max
  T  budgets         card.md <= 1.5K tokens, each s/*.md <= 4K (o200k)

Then `prov` is set: an LLM record that passes becomes llm-verified, one that fails llm-unverified
(it is never deleted); a figure or equation fact stays llm-unverified, since only a second look can
verify it. A deterministic record whose twin agrees becomes deterministic+crosschecked.
Writes checks.json; exits 1 while anything is failing. Warnings (K rows not found, M numbers anchored
only by a record on another page) are listed, never failing.
"""
from __future__ import annotations

import ast
import json
import math
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from . import __version__, common, records as R
from .pdftext import normalize, page_file, resolve_pdf_path

CARD_BUDGET = 1500
SECTION_BUDGET = 4000

NUM_RE = re.compile(r"(?<![A-Za-z_\d.])(?:(?<=[\s(=/,:|;<>~x*\[])[-+]|(?<=^)[-+])?\d+(?:\.\d+)?")
HDR = {"MIN": "min", "TYP": "typ", "MAX": "max", "NOM": "nom"}
NUMERIC_KEYS = ("min", "nom", "typ", "max")
K_GROUPS = ("abs", "recommended", "ec", "i2c")

UNIT_DIM: dict[str, str] = {}
for _d, _us in {"V": "V mV kV µV nV", "A": "A mA µA nA pA kA", "s": "s ms µs ns ps",
                "Ω": "Ω mΩ kΩ MΩ ohm kohm mohm", "°C": "°C K", "Hz": "Hz kHz MHz GHz",
                "F": "F mF µF nF pF", "W": "W mW µW kW", "H": "H mH µH nH"}.items():
    for _u in _us.split():
        UNIT_DIM[normalize(_u)] = _d
SYM_DIM = {"V": ("V",), "I": ("A",), "t": ("s",), "R": ("Ω",), "T": ("°C", "s"), "f": ("Hz",),
           "C": ("F",), "P": ("W",), "L": ("H",)}
PARAM_DIM = {"V": r"voltage|threshold|drop|droop|rating|clamp", "A": r"current|limit", "s": r"time|delay|period|duration|width|debounce|interval",
             "Hz": r"frequency|rate|clock", "Ω": r"resistance|impedance|on-resistance", "°C": r"temperature|shutdown",
             "F": r"capacitance", "W": r"power|dissipation", "H": r"inductance"}


# ---------------------------------------------------------------- numbers and cites

def numbers(s: str) -> list[float]:
    """Numbers in a normalized string. `10-90%` is 10 and 90, `±2` is 2 and -2, `3..23` is 3 and 23;
    a digit glued to letters (EN1, 1.0A17's 17) is an identifier, not a number."""
    s = re.sub(r"(?<=\d)\.\.(?=[-\d])", " .. ", s)
    out = []
    for m in NUM_RE.finditer(s):
        v = float(m.group())
        out.append(v)
        if m.start() > 0 and s[m.start() - 1] == "±":
            out.append(-v)
    return out


def isnum(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _close(x: float, ys) -> bool:
    return any(math.isclose(x, y, rel_tol=1e-9, abs_tol=1e-12) for y in ys)


# `p.8`, `p.4-5`, `pp. 4-5`, `p8` (the facts.txt form); never `step 5`, never `§7`
PAGE_CITE = re.compile(r"(?<![A-Za-z0-9_])pp?(?:\.\s?|(?=\d))(\d+)(?:\s*[-–]\s*(?:p\.?\s?)?(\d+))?")


def cited_pages(s: str) -> set[int]:
    out: set[int] = set()
    for m in PAGE_CITE.finditer(s):
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        if b >= a and b - a < 400:
            out.update(range(a, b + 1))
    return out


# ---------------------------------------------------------------- quotes split with `...`

SHORT_PART = 24     # a part this short (chars) must sit on the same or the adjacent line as its neighbour
LONG_GAP = 6        # longer parts: at most this many lines apart


def split_quote(q: str) -> list[str]:
    return [x for x in re.split(r"\s*(?:\.\.\.|…)\s*", normalize(q, "map")) if x]


def _parts_near(f: str, starts: list[int], parts: list[str]) -> bool:
    """The parts occur in `f` in order, each near the one before: a short part on the same or the
    adjacent line (so `Title ... 14` ties the 14 to the title's line), a long one within LONG_GAP."""
    from bisect import bisect_right

    def line(pos: int) -> int:
        return bisect_right(starts, pos) - 1

    def walk(k: int, pos: int, prev_end_line: int) -> bool:
        part = parts[k]
        i = f.find(part, pos)
        while i >= 0:
            if k > 0:
                short = len(part) <= SHORT_PART or len(parts[k - 1]) <= SHORT_PART
                if line(i) - prev_end_line > (1 if short else LONG_GAP):
                    return False        # every later occurrence is farther away
            end = i + len(part)
            if k == len(parts) - 1 or walk(k + 1, end, line(end - 1)):
                return True
            i = f.find(part, i + 1)
        return False

    return walk(0, 0, 0)


# ---------------------------------------------------------------- the text layer

class DocText:
    """Text layers of one document: pdftotext -layout (pages/), PyMuPDF (from the PDF, when it is
    still there and unchanged), and word boxes (pages/*.bbox.json, else the PDF)."""

    def __init__(self, docdir: Path, use_pdf: bool = True):
        self.dir = Path(docdir)
        self.meta = common.read_json(self.dir / "doc.json")
        self.doc_id = self.meta.get("doc_id") or self.meta.get("id") or self.dir.name
        # A non-PDF source (an SVD) has no pages: only the record-level checks run on it.
        self.n = int(self.meta.get("pages") or 0)
        if not self.n:
            use_pdf = False
        self.raw = {}
        for p in range(1, self.n + 1):
            f = page_file(self.dir, p)
            self.raw[p] = f.read_text(encoding="utf-8") if f.exists() else ""
        self.pdf = None
        self.pdf_note = "not used"
        if use_pdf:
            path = resolve_pdf_path(self.dir, self.meta)
            if path is None:
                self.pdf_note = "source PDF not found: PyMuPDF layer and K need pages/*.bbox.json"
            elif common.sha256_file(path) != self.meta.get("sha256"):
                self.pdf_note = f"{path} changed since doc prep: re-run doc prep; PDF not used"
            else:
                import pymupdf
                self.pdf = pymupdf.open(path)
                self.pdf_note = str(path)
        self._forms: dict[int, tuple[str, ...]] = {}
        self._nums: dict[int, list[float]] = {}
        self._words: dict[int, list] = {}
        self._lines: dict[int, list[tuple[str, list[int]]]] = {}

    def forms(self, p: int) -> tuple[str, ...]:
        if p not in self._forms:
            texts = [self.raw.get(p, "")]
            if self.pdf is not None and 1 <= p <= self.n:
                texts.append(self.pdf[p - 1].get_text())
            fs = []
            for t in texts:
                for mode in ("map", "strip"):
                    f = normalize(t, mode)
                    if f not in fs:
                        fs.append(f)
            self._forms[p] = tuple(fs)
        return self._forms[p]

    def line_forms(self, p: int) -> list[tuple[str, list[int]]]:
        """(normalized page, offset where each non-blank source line starts in it) per text layer and
        glyph mode. Offsets come from normalizing each prefix of lines, so they follow whatever
        normalize() does across a line break (a rejoined hyphenation shifts them by a character at most)."""
        if p not in self._lines:
            texts = [self.raw.get(p, "")]
            if self.pdf is not None and 1 <= p <= self.n:
                texts.append(self.pdf[p - 1].get_text())
            out: list[tuple[str, list[int]]] = []
            for t in texts:
                lines = t.replace("\f", "").split("\n")
                for mode in ("map", "strip"):
                    f = normalize(t, mode)
                    if any(f == x[0] for x in out):
                        continue
                    starts = []                 # blank lines are no lines: they do not count as a gap
                    for i, ln in enumerate(lines):
                        if ln.strip():
                            n = len(normalize("\n".join(lines[:i]), mode)) if i else 0
                            starts.append(n + 1 if n else 0)
                    out.append((f, starts or [0]))
            self._lines[p] = out
        return self._lines[p]

    def has(self, q: str, p: int) -> bool:
        if not 1 <= p <= self.n:
            return False
        parts = split_quote(q)
        if not parts:
            return False
        if len(parts) == 1:
            return any(parts[0] in f for f in self.forms(p))
        return any(_parts_near(f, starts, parts) for f, starts in self.line_forms(p))

    def page_numbers(self, ps) -> list[float]:
        out: list[float] = []
        for p in ps:
            if p not in self._nums:
                self._nums[p] = [x for f in self.forms(p) for x in numbers(f)] if 1 <= p <= self.n else []
            out += self._nums[p]
        return out

    def page_text(self, ps) -> str:
        return " ".join(" ".join(self.forms(p)) for p in ps if 1 <= p <= self.n)

    def words(self, p: int) -> list:
        if p not in self._words:
            ws: list = []
            bb = page_file(self.dir, p, ".bbox.json")
            if bb.exists():
                ws = [tuple(w[:4]) + (w[4],) for w in common.read_json(bb)["words"]]
            elif self.pdf is not None and 1 <= p <= self.n:
                ws = [w[:5] for w in self.pdf[p - 1].get_text("words")]
            self._words[p] = [(w[0], w[1], w[2], w[3], normalize(w[4])) for w in ws]
        return self._words[p]

    def bands(self, p: int, tol: float = 2.5) -> list[tuple[float, list]]:
        ws = sorted(self.words(p), key=lambda w: ((w[1] + w[3]) / 2, w[0]))
        out, cur, yc = [], [], None
        for w in ws:
            y = (w[1] + w[3]) / 2
            if yc is None or abs(y - yc) <= tol:
                cur.append(w)
                yc = y if yc is None else (yc * (len(cur) - 1) + y) / len(cur)
            else:
                out.append((yc, sorted(cur)))
                cur, yc = [w], y
        if cur:
            out.append((yc, sorted(cur)))
        return out

    def find_row(self, bands: list, q: str) -> tuple[int, int] | None:
        """(first band index, band count) of the table row a quote comes from. The row is the band, or
        up to three stacked bands within ROW_SPAN points (a cell that wraps, a value printed a line
        above or below its condition), whose text holds the quote's last part whole, else its last
        three tokens; punctuation and spacing are ignored, since the two text layers split words
        differently (`1s` vs `1 s`, an overline's `)` on its own line). Whole beats tail, fewer bands
        beat more."""
        parts = [x for x in re.split(r"\s*(?:\.\.\.|…)\s*", q) if x] or [q]
        last = parts[-1]
        full, toks = _compact(last), last.split()
        tail = _compact(" ".join(toks[-3:]))
        qset = {_compact(t) for t in q.split()}
        best = None
        for i in range(len(bands)):
            for n in (1, 2, 3):
                grp = bands[i:i + n]
                if len(grp) < n or grp[-1][0] - grp[0][0] > ROW_SPAN:
                    break
                words = [w for _, ws in grp for w in ws]
                texts = {_compact(" ".join(w[4] for w in words))}
                if n > 1:
                    texts.add(_compact(" ".join(w[4] for w in sorted(words, key=lambda w: w[0]))))
                if full and any(full in t for t in texts):
                    level = 0
                elif tail and any(tail in t for t in texts):
                    level = 1
                else:
                    continue
                score = sum(1 for w in words if _compact(w[4]) in qset)
                cand = (level, n, -score, i)
                if best is None or cand < best:
                    best = cand
        return None if best is None else (best[3], best[1])

    def columns(self, rec: dict) -> dict:
        """Independent min/nom/typ/max (and unit) for a record's row, from word x-positions under the
        nearest MIN/TYP/MAX (or VALUE) header above the row that holds the quote (find_row).

        det values: a float; ("pm", X) for `±X` under TYP/NOM; "expr" for a cell that is not a number
        (`VIN`, `Vs + 0.3`); {"single": X} is a lone number in a VALUE column, whose key (max, typ)
        the column does not say."""
        ps, qs = R.pages(rec), R.quotes(rec)
        if not ps or not qs:
            return {"status": "no-quote"}
        p, qn = ps[0], normalize(qs[0])
        bands = self.bands(p)
        if not bands:
            return {"status": "no-words"}
        headers = []
        for yc, ws in bands:
            h = {HDR[w[4].upper()]: (w[0] + w[2]) / 2 for w in ws if w[4].upper() in HDR}
            if len(h) >= 2:
                headers.append((yc, h, ws))
                continue
            up = [w[4].upper() for w in ws]
            if len(ws) <= 8 and any(u in VALUE_HDR for u in up) and any(u in ("UNIT", "UNITS") for u in up):
                v = next(w for w in ws if w[4].upper() in VALUE_HDR)
                headers.append((yc, {"value": (v[0] + v[2]) / 2}, ws))
        if not headers:
            return {"status": "no-header"}
        row = self.find_row(bands, qn)
        if row is None:
            return {"status": "row-not-found"}
        i0, n = row
        grp = bands[i0:i0 + n]
        yc = grp[0][0]
        words = [w for _, ws in grp for w in ws]
        above = [h for h in headers if h[0] < yc]
        if not above:
            return {"status": "no-header-above"}
        hy, hdr, hws = above[-1]
        det: dict[str, Any] = {}
        pct = False
        if "value" in hdr:
            vx = hdr["value"]
            xs = sorted((w[0] + w[2]) / 2 for w in hws)
            k = xs.index(min(xs, key=lambda x: abs(x - vx)))
            lo = max(vx - 45, (xs[k - 1] + vx) / 2 if k > 0 else vx - 45)
            hi = min(vx + 45, (xs[k + 1] + vx) / 2 if k + 1 < len(xs) else vx + 45)
            cell = [w[4] for w in words if lo <= (w[0] + w[2]) / 2 <= hi]
            det, pct = _value_cell(cell)
            if det is None:
                return {"status": "cell-unparsed"}
        else:
            left = min(hdr.values()) - 30
            seen: dict[str, list] = {}
            for w in words:
                xc = (w[0] + w[2]) / 2
                c = _cell_num(w[4]) if xc >= left else None
                if c is None:
                    continue
                col = min(hdr, key=lambda k: abs(hdr[k] - xc))
                if abs(hdr[col] - xc) < 28:
                    seen.setdefault(col, []).append(c[0])
                    pct |= c[1]
            for col, vs in seen.items():
                if n > 1 and len({repr(v) for v in vs}) > 1:
                    return {"status": "row-ambiguous"}   # two stacked lines put two values in one column
                v = vs[0]
                if isinstance(v, tuple) and col in ("min", "max"):
                    det.setdefault("min", -v[1])
                    det.setdefault("max", v[1])
                else:
                    det[col] = v
        unit = None
        uh = [w for w in hws if w[4].upper() in ("UNIT", "UNITS")]
        if uh:
            ux = (uh[0][0] + uh[0][2]) / 2

            def uwords(ws):
                return [w[4] for w in ws if abs((w[0] + w[2]) / 2 - ux) < 25 and _cell_num(w[4]) is None]

            own = [x for _, ws in grp for x in uwords(ws)]
            if own:
                unit = " ".join(own)
                want = _unit_forms(rec["unit"]) if rec.get("unit") else []
                if want and unit not in want:
                    # a unit cell that wraps over the neighbouring lines ('% of' / 'Reading')
                    near = [(j, uwords(bands[j][1])) for j in range(max(0, i0 - 3), min(len(bands), i0 + n + 3))
                            if not i0 <= j < i0 + n and abs(bands[j][0] - yc) <= UNIT_SPAN]
                    before = [x for j, x in near if j < i0 and x][::-1]
                    after = [x for j, x in near if j >= i0 + n and x]
                    for kb in range(len(before) + 1):
                        for ka in range(len(after) + 1):
                            joined = " ".join(sum(before[:kb][::-1], []) + own + sum(after[:ka], []))
                            if joined in want:
                                unit = joined
        if unit is None and pct:
            unit = "%"
        return {"status": "ok", "det": det, "unit": unit}


ROW_SPAN = 12.0      # points: stacked lines of one table row
# K statuses where the row is on the page but K could not read it: warnings, counted per document
K_WARN = {"row-not-found": "row not found", "row-ambiguous": "row ambiguous (two stacked lines, one column)",
          "cell-unparsed": "row with an unreadable VALUE cell"}
UNIT_SPAN = 16.0     # points: a unit cell wrapped over the row's neighbouring lines
VALUE_HDR = ("VALUE", "VALUES")
# A value cell; a footnote marker glued to it (`3000(4)`, `1.7(1)`) is not part of the number.
_CELL_NUM = re.compile(r"^([±+-]?)(\d+(?:\.\d+)?)(%?)(?:\(\d{1,2}\))?$")
_COMPACT = re.compile(r"[\s()\[\],;:'\"]+")


def _compact(s: str) -> str:
    return _COMPACT.sub("", s)


def _cell_num(w: str) -> tuple[Any, bool] | None:
    """A value word: (float, has %) or (("pm", X), has %) for `±X`; None when it is not a number."""
    m = _CELL_NUM.match(w)
    if not m:
        return None
    v = float(m.group(2))
    if m.group(1) == "±":
        return ("pm", v), bool(m.group(3))
    return (-v if m.group(1) == "-" else v), bool(m.group(3))


_GLUED_UNIT = re.compile(r"^([±+-]?\d+(?:\.\d+)?)(\D+)$")


def _side_num(side: list[str]) -> tuple[Any, bool] | None:
    """One side of a VALUE cell as a number: unit words dropped (`-0.3 V`), a glued unit cut (`6V`)."""
    toks = [t for t in side if normalize(t).replace("ohm", "ω") not in UNIT_DIM]
    if len(toks) != 1:
        return None
    c = _cell_num(toks[0])
    if c is None:
        m = _GLUED_UNIT.match(toks[0])
        if m and normalize(m.group(2)).replace("ohm", "ω") in UNIT_DIM:
            c = _cell_num(m.group(1))
    return c


def _value_cell(toks: list[str]) -> tuple[dict | None, bool]:
    """A single VALUE column's cell: `a to b` is min/max, `±X` is -X/X, a lone number is {"single"}.
    A side that is not one number (`VIN`, `Vs + 0.3`) is "expr"."""
    if not toks:
        return {}, False
    pct = False
    if "to" in toks:
        i = toks.index("to")
        out: dict[str, Any] = {}
        for key, side in (("min", toks[:i]), ("max", toks[i + 1:])):
            c = _side_num(side)
            if c is not None and not isinstance(c[0], tuple):
                out[key] = c[0]
                pct |= c[1]
            elif side:
                out[key] = "expr"
        return out, pct
    c = _side_num(toks)
    if c is None:
        return ({"single": "expr"}, False) if len(toks) == 1 else (None, False)
    if isinstance(c[0], tuple):
        return {"min": -c[0][1], "max": c[0][1]}, c[1]
    return {"single": c[0]}, c[1]


def _pm_string(v: str) -> float | None:
    """`"±0.48"` (a typical kept as printed) -> 0.48; None for any other string."""
    c = _cell_num(normalize(v).replace(" ", ""))
    return c[0][1] if c is not None and isinstance(c[0], tuple) else None


def compare_columns(rec: dict, det: dict) -> dict:
    """{key: {"record", "columns"}} where the record and the independent column reading disagree."""
    diff: dict[str, Any] = {}
    if "single" in det:
        nums = [float(rec[k]) for k in NUMERIC_KEYS if isnum(rec.get(k))]
        pms = [x for x in (_pm_string(rec[k]) for k in NUMERIC_KEYS if isinstance(rec.get(k), str)) if x is not None]
        dv = det["single"]
        if dv == "expr":
            if nums:
                diff["value"] = {"record": nums, "columns": "expression"}
        elif pms or len(nums) != 1 or not math.isclose(nums[0], dv, rel_tol=1e-9):
            diff["value"] = {"record": nums + pms, "columns": dv}
        return diff
    for k in NUMERIC_KEYS:
        lv, dv = rec.get(k), det.get(k)
        if isinstance(lv, str):
            pm = _pm_string(lv)
            if pm is None:
                continue                      # an expression kept as printed: nothing to compare
            if isinstance(dv, tuple) and math.isclose(pm, dv[1], rel_tol=1e-9):
                continue
            diff[k] = {"record": lv, "columns": _show(dv)}
            continue
        if lv is None and dv is None:
            continue
        if isinstance(dv, tuple):             # `±X` under TYP: a magnitude
            if lv is not None and math.isclose(float(lv), dv[1], rel_tol=1e-9):
                continue
        elif dv != "expr" and lv is not None and dv is not None and math.isclose(float(lv), dv, rel_tol=1e-9):
            continue
        diff[k] = {"record": lv, "columns": _show(dv)}
    return diff


def _show(dv: Any) -> Any:
    if isinstance(dv, tuple):
        return f"±{dv[1]:g}"
    return "expression" if dv == "expr" else dv


# ---------------------------------------------------------------- per-record checks

def _unit_forms(u: str) -> list[str]:
    n = normalize(u)
    alts = {n, n.replace("ohm", "ω"), n.replace("ω", "ohm"), re.sub(r"^u(?=[avsfhw])", "µ", n),
            re.sub(r"^µ", "u", n), n.replace("°c", "c"), n.replace("°", "")}
    return sorted(a for a in alts if a)


def unit_dim(u: str | None) -> str | None:
    if not u:
        return None
    return UNIT_DIM.get(normalize(u).replace("ohm", "ω"))


def check_unit(rec: dict) -> str | None:
    """None when the unit fits the symbol or the parameter; a reason otherwise."""
    u, sym, param = rec.get("unit"), str(rec.get("sym") or ""), str(rec.get("param") or "")
    dim = unit_dim(u)
    if not dim or not sym or "lsb" in normalize(u or "") or "/" in (u or ""):
        return None
    if not re.match(r"[A-Za-z]", sym) or sym.startswith(("Rθ", "RΘ", "Ψ", "Θ", "Δ")) or "θ" in sym:
        return None
    exp = SYM_DIM.get(sym[0])
    if exp is None or dim in exp:
        return None
    if re.search(PARAM_DIM.get(dim, r"$^"), param, re.I):
        return None
    return f"unit {u} ({dim}) does not fit symbol {sym} ({'/'.join(exp)})"


def record_checks(doc: DocText, rec: dict) -> dict:
    """{check letter: result} for the checks that apply to one record. Results: 'ok' or a finding."""
    out: dict[str, Any] = {}
    ps, qs = R.pages(rec), R.quotes(rec)
    kind = rec.get("kind")
    if kind == "check":
        return out
    # A
    if qs:
        missing, adjacent = [], []
        for q in qs:
            if any(doc.has(q, p) for p in ps):
                continue
            missing.append(q)
            near = sorted({p + d for p in ps for d in (-1, 1) if doc.has(q, p + d)})
            if near:
                adjacent.append(near)
        if not missing:
            out["A"] = "ok"
        elif adjacent and len(adjacent) == len(missing):
            out["A"] = f"wrong-page: quote found on p{','.join(str(x) for x in adjacent[0])}"
        else:
            out["A"] = f"not-found: {missing[0][:80]!r}"
    elif kind not in ("figure",) and rec.get("src") not in ("figure", "equation", "svd", "header"):
        out["A"] = "no-quote"
    # N
    numv = {k: rec[k] for k in NUMERIC_KEYS if isnum(rec.get(k))}
    if qs and numv:
        qn = [x for q in qs for x in numbers(normalize(q))]
        miss = [k for k, v in numv.items() if not _close(float(v), qn)]
        out["N"] = "ok" if not miss else f"not in quote: {', '.join(f'{k}={numv[k]}' for k in miss)}"
    if kind == "requirement" and qs:
        qn = [x for q in qs for x in numbers(normalize(q))]
        miss = [x for x in numbers(normalize(rec.get("text") or "")) if not _close(x, qn)]
        out["N"] = "ok" if not miss else f"text numbers not in quote: {miss}"
    # O
    if numv:
        order = [float(numv[k]) for k in ("min", "nom", "typ", "max") if k in numv]
        lo, hi = numv.get("min"), numv.get("max")
        mids = [float(numv[k]) for k in ("nom", "typ") if k in numv]
        bad = (lo is not None and hi is not None and lo > hi) or \
              any((lo is not None and m < lo) or (hi is not None and m > hi) for m in mids)
        out["O"] = "ok" if not bad else f"order violated: {order}"
    # K
    pm_strings = [k for k in NUMERIC_KEYS if isinstance(rec.get(k), str) and _pm_string(rec[k]) is not None]
    if kind in ("limit", "timing") and rec.get("group") in K_GROUPS and rec.get("src") == "table" and qs \
            and (numv or pm_strings):
        c = doc.columns(rec)
        if c["status"] == "ok":
            diff = compare_columns(rec, c["det"])
            if c.get("unit") and rec.get("unit") and c["unit"] not in _unit_forms(rec["unit"]):
                diff["unit"] = {"record": rec["unit"], "columns": c["unit"]}
            out["K"] = "ok" if not diff else {"disagree": diff}
            out["K_unit_seen"] = c.get("unit") is not None
        else:
            out["K"] = f"not checkable: {c['status']}"
    # C
    cond = rec.get("cond")
    if isinstance(cond, str) and numbers(normalize(cond)):
        pn = doc.page_numbers(ps)
        miss = [x for x in numbers(normalize(cond)) if not _close(x, pn)]
        out["C"] = "ok" if not miss else f"condition numbers not on page: {miss}"
    # U
    if rec.get("unit") and kind in ("limit", "timing"):
        why = check_unit(rec)
        page = doc.page_text(ps)
        on_page = any(f in page for f in _unit_forms(rec["unit"]))
        if why:
            out["U"] = why
        elif not on_page and rec.get("src") not in ("figure", "equation"):
            out["U"] = f"unit {rec['unit']!r} not on the cited page"
        else:
            out["U"] = "ok"
    return out


FAILING = {"V", "A", "N", "C", "U", "O", "R", "G", "X"}


def _is_fail(letter: str, result: Any) -> bool:
    if result == "ok" or letter.endswith("_unit_seen"):
        return False
    if letter == "K":
        return isinstance(result, dict)
    return letter in FAILING


# ---------------------------------------------------------------- invariants (X)

_SAFE_NODES = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div,
               ast.FloorDiv, ast.Mod, ast.Pow, ast.BitAnd, ast.BitOr, ast.BitXor, ast.LShift, ast.RShift,
               ast.Invert, ast.UnaryOp, ast.USub, ast.UAdd, ast.Not, ast.Compare, ast.Eq,
               ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.Is, ast.IsNot, ast.Call,
               ast.Name, ast.Load, ast.Store, ast.Constant, ast.Subscript, ast.Attribute, ast.List, ast.Tuple,
               ast.GeneratorExp, ast.ListComp, ast.comprehension, ast.IfExp)
_SAFE_FUNCS = {"abs": abs, "all": all, "any": any, "min": min, "max": max, "round": round, "sum": sum, "len": len}
_ATTRS = set(NUMERIC_KEYS) | {"value", "reset", "bits", "addr", "width"}


def safe_eval(expr: str, env: dict) -> Any:
    """Evaluate an LLM-proposed invariant: arithmetic, comparisons, comprehensions, R['id'].typ, and a
    few pure functions. Anything else (imports, dunders, other calls) is refused."""
    tree = ast.parse(expr, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, _SAFE_NODES):
            raise ValueError(f"not allowed in an invariant: {type(node).__name__}")
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            raise ValueError(f"name {node.id} not allowed")
        if isinstance(node, ast.Attribute) and node.attr not in _ATTRS:
            raise ValueError(f"attribute .{node.attr} not allowed (use .min/.nom/.typ/.max/.value/.reset/"
                             ".bits/.addr/.width)")
        if isinstance(node, ast.Call) and not (isinstance(node.func, ast.Name) and node.func.id in _SAFE_FUNCS):
            raise ValueError("only abs/all/any/min/max/round/sum/len may be called")
    # env goes into globals: a generator expression cannot see eval's locals
    return eval(compile(tree, "<invariant>", "eval"), {"__builtins__": {}, **_SAFE_FUNCS, **env})


def _attr(rec: dict, a: str) -> Any:
    """An invariant's view of a record field: register/field `reset` and `addr` as integers."""
    v = rec.get(a)
    if a in ("reset", "addr") and isinstance(v, str):
        try:
            return R.to_int(v)
        except ValueError:
            return v
    return v


def check_invariants(by_id: dict[str, dict], checks: list[dict]) -> list[dict]:
    ns = {k: SimpleNamespace(**{a: _attr(v, a) for a in _ATTRS}) for k, v in by_id.items()}
    out = []
    for c in checks:
        try:
            got = "pass" if bool(safe_eval(c["expr"], {"R": ns})) else "fail"
        except Exception as e:  # noqa: BLE001 - any failure of an LLM expression is a finding
            got = f"error: {type(e).__name__}: {e}"
        out.append({"id": c["id"], "expect": c.get("expect"), "got": got, "ok": got == c.get("expect")})
    return out


# ---------------------------------------------------------------- registers (G)

def _is_reserved(name: str) -> bool:
    return name.upper().startswith(("RESERVED", "RSVD"))


def merge_reserved(fields: list[dict]) -> list[dict]:
    """Adjacent reserved fields with equal access merged, so a split granularity (TI prints STATUS[4:2]
    as three rows) is not a difference."""
    out: list[dict] = []
    for f in sorted(fields, key=lambda f: -f["bits"][0]):
        if out and _is_reserved(f["name"]) and _is_reserved(out[-1]["name"]) \
                and out[-1].get("access") == f.get("access") and out[-1]["bits"][1] == f["bits"][0] + 1:
            prev = out[-1]
            reset = None
            if prev.get("reset") is not None and f.get("reset") is not None:
                w = f["bits"][0] - f["bits"][1] + 1
                reset = bin((R.to_int(prev["reset"]) << w) | R.to_int(f["reset"]))
            out[-1] = dict(prev, name=f"RESERVED_{prev['bits'][0]}_{f['bits'][1]}",
                           bits=[prev["bits"][0], f["bits"][1]], reset=reset, _ids=prev["_ids"] + [f["id"]])
        else:
            out.append(dict(f, _ids=[f["id"]]))
    return out


def _structure(regs: list[dict], fields_by_reg: dict[str, list[dict]]) -> list[dict]:
    issues = []
    seen: dict[int, str] = {}
    for r in regs:
        w = r.get("width") or 0
        cover: list[str | None] = [None] * w
        comp, defined = 0, True
        for f in fields_by_reg.get(r["id"], []):
            hi, lo = f["bits"]
            for b in range(lo, hi + 1):
                if b >= w:
                    issues.append({"id": f["id"], "issue": f"bit {b} >= register width {w}"})
                elif cover[b] is not None:
                    issues.append({"id": f["id"], "issue": f"overlaps {cover[b]} at bit {b}"})
                else:
                    cover[b] = f["name"]
            if f.get("reset") is None:
                defined = False
            else:
                comp |= R.to_int(f["reset"]) << lo
        if fields_by_reg.get(r["id"]):
            gaps = [i for i, c in enumerate(cover) if c is None]
            # An SVD leaves reserved bits out by convention; a datasheet table lists them.
            if gaps and r.get("src") not in ("svd", "header"):
                issues.append({"id": r["id"], "issue": f"bits {gaps} covered by no field"})
            if defined and r.get("reset") is not None and comp != R.to_int(r["reset"]):
                issues.append({"id": r["id"], "issue": f"field resets compose to {comp:#x}, register reset {r['reset']}"})
        a = R.to_int(r.get("addr"))
        if a is not None:
            if a in seen and seen[a] != r["name"]:
                issues.append({"id": r["id"], "issue": f"address {a:#x} is also {seen[a]}"})
            seen[a] = r["name"]
    return issues


def field_key(name: str | None) -> str:
    """A field name as G compares it: case and separators dropped, so a parser's `FILT_EMPTY` or
    `Filt_Empty` and the printed `Filt Empty` are one field. Different words stay different."""
    return re.sub(r"[^0-9a-z]", "", (name or "").casefold())


# G compares an attribute only where the parser read one: a template with no access column or a
# heading with no reset does not contradict the LLM's value. An LLM `reset: null` is a claim (the
# document defines none) and is compared; an access left out is not. The access a parser reads from a
# printed type column, and the SystemRDL refinements an LLM may add from the description (`R` plus
# "cleared when read" is rclr), do not disagree.
ACCESS_REFINES = {"ro": {"rclr"}, "rw": {"woclr", "w1s"}}


def access_agrees(det: str | None, llm: str | None) -> bool:
    if det is None or llm is None or det == llm:
        return True
    return llm in ACCESS_REFINES.get(det, set())


def _variant_reset(rec: dict) -> bool:
    """`reset: null` with a `note`: the reset depends on a variant or a strap, and the note gives the
    values (schema, register.reset). A parser that read one of them is not contradicted."""
    return "reset" in rec and rec["reset"] is None and bool(rec.get("note"))


def check_registers(recs: list[dict]) -> dict:
    """Structure per source (deterministic, LLM), then the twin diff. Returns the G section of the report
    and per-record verdicts ('agree' | 'disagree: ...')."""
    regs = [r for r in recs if r.get("kind") == "register"]
    fields = [r for r in recs if r.get("kind") == "field"]
    if not regs and not fields:
        return {}
    groups = {"deterministic": lambda r: r.get("prov") in R.DETERMINISTIC,
              "llm": lambda r: r.get("prov") in R.LLM}
    rep: dict[str, Any] = {"structure": [], "diff": [], "verdict": {}}
    by_src: dict[str, tuple[dict, dict]] = {}
    for name, pred in groups.items():
        rg = [r for r in regs if pred(r)]
        fb: dict[str, list[dict]] = {}
        for f in fields:
            if pred(f):
                fb.setdefault(f["reg"], []).append(f)
        rep["structure"] += [dict(x, source=name) for x in _structure(rg, fb)]
        by_src[name] = ({r["id"]: r for r in rg}, fb)
        orphans = [f["id"] for k, fs in fb.items() if k not in {r["id"] for r in rg} for f in fs]
        rep["structure"] += [{"id": i, "issue": "field's reg is no register record", "source": name} for i in orphans]
    det_regs, det_fields = by_src["deterministic"]
    llm_regs, llm_fields = by_src["llm"]
    rep["summary"] = {"registers": {"deterministic": len(det_regs), "llm": len(llm_regs)},
                      "fields": {"deterministic": sum(len(v) for v in det_fields.values()),
                                 "llm": sum(len(v) for v in llm_fields.values())}}
    if det_regs:
        cov = [r for r in det_regs.values() if r.get("summary")]
        rep["summary"]["in_summary_table"] = f"{len(cov)}/{len(det_regs)}"
    for rid in sorted(set(det_regs) | set(llm_regs)):
        d, l = det_regs.get(rid), llm_regs.get(rid)
        if d is None or l is None:
            if d is not None and llm_regs:
                rep["diff"].append({"id": rid, "issue": "in the deterministic parse only"})
            if l is not None and det_regs:
                rep["diff"].append({"id": rid, "issue": "in the LLM records only"})
            continue
        diffs = []
        for k in ("addr", "width", "reset"):
            if k == "reset" and _variant_reset(l):
                continue
            dv = R.to_int(d.get(k)) if k != "width" else d.get(k)
            lv = R.to_int(l.get(k)) if k != "width" else l.get(k)
            if dv is not None and dv != lv:          # a value the parser could not read is not compared
                diffs.append(f"{k}: llm {l.get(k)} det {d.get(k)}")
        dm = {field_key(f["name"]): f for f in merge_reserved(det_fields.get(rid, []))}
        lm = {field_key(f["name"]): f for f in merge_reserved(llm_fields.get(rid, []))}
        field_verdict: dict[str, str] = {}
        for key in sorted(set(dm) | set(lm)):
            df, lf = dm.get(key), lm.get(key)
            if df is None or lf is None:
                name = (lf or df)["name"]
                msg = f"field {name} only in {'llm' if df is None else 'deterministic'}"
                diffs.append(msg)
                for i in (lf or df)["_ids"]:
                    field_verdict[i] = f"disagree: {msg}"
                continue
            name = lf["name"]
            fd = []
            if df["bits"] != lf["bits"]:
                fd.append(f"bits llm {lf['bits']} det {df['bits']}")
            if not access_agrees(df.get("access"), lf.get("access")):
                fd.append(f"access llm {lf.get('access')} det {df.get('access')}")
            if df.get("reset") is not None and R.to_int(df.get("reset")) != R.to_int(lf.get("reset")) \
                    and not _variant_reset(lf):
                fd.append(f"reset llm {lf.get('reset')} det {df.get('reset')}")
            v = "agree" if not fd else "disagree: " + "; ".join(fd)
            diffs += [f"{name}: {x}" for x in fd]
            for i in df["_ids"] + lf["_ids"]:
                field_verdict[i] = v
        for x in diffs:
            rep["diff"].append({"id": rid, "issue": x})
        rep["verdict"][rid] = "agree" if not diffs else "disagree: " + "; ".join(diffs[:3])
        rep["verdict"].update(field_verdict)
    return rep


# ---------------------------------------------------------------- markdown (M, S, T)

# Prose that carries numbers which are not claims about the document: a numbered-list marker, an
# atlas tier label, a token count (index lines), a backticked identifier or file name (`LINE0_1`,
# `s/9.4.16-9.8.md`). rm-check blanks these too (rm.number_claims).
NOT_CLAIMS = [
    re.compile(r"^\s*(?:[-*+]\s+)?\d+[.)](?=\s)"),                              # 3. / 3) at line start
    re.compile(r"\bTier\s+\d+\b"),
    re.compile(r"\b\d{1,3}(?:,\d{3})+(?:\s+raw)?\s+tokens?\b|\b\d+(?:\s+raw)?\s+tokens?\b", re.I),
    re.compile(r"`[^`\s]*[A-Za-z][^`\s]*`"),                                  # no whitespace, a letter
]


def blank_not_claims(s: str) -> str:
    for rx in NOT_CLAIMS:
        s = rx.sub(lambda m: " " * len(m.group()), s)
    return s


# Section references: `§7.3`, `§6.3-§6.5`; section numbers 7.3.4, and a range of sections with one
# such end (9.4.16-9.8, 6.2-6.2.1). rm-check blanks these too.
SECTION_REF = re.compile(r"§\s*[\d.]+(?:\s*[-–]\s*§?\s*\d+(?:\.\d+)*)?")
SECTION_NUM = re.compile(r"\b\d+(?:\.\d+)+\s*[-–]\s*\d+(?:\.\d+){2,}\b|\b\d+(?:\.\d+){2,}(?:\s*[-–]\s*\d+(?:\.\d+)+)?\b")

_STRIP_BEFORE_NUMBERS = NOT_CLAIMS + [
    re.compile(r"<!--.*?-->"),
    PAGE_CITE,
    SECTION_REF,
    re.compile(r"\b[a-z0-9][a-z0-9._+-]*@[A-Za-z0-9._-]+"),                   # doc ids
    re.compile(r"\bfn\s*\d+(?:\s*(?:,|and)\s*(?:fn\s*)?\d+)*", re.I),              # footnote refs: fn 13, 18
    re.compile(r"(?:figure|fig\.|table|eq\.?|equation|footnote|q|note|step|section)\s*\d+(?:[-.]\d+)*", re.I),
    SECTION_NUM,
    # hex and binary; an `h`/`b` suffix only on a whole token, so the fraction of 102.3984h stays
    re.compile(r"\b0x[0-9a-f]+\b|\b0b[01]+\b|(?<![\w.])[01]+b\b|(?<![\w.])(?=[0-9a-f]*\d)[0-9a-f]+h\b", re.I),
    re.compile(r"(?<![\w.])0[01]{7,}(?![\w.])"),         # a bare bit string (a reset printed as 0000000000010000)
    re.compile(r"\b[A-Za-z_]+[A-Za-z_./]*\d[\w./]*(?:-\d[\w./]*)*"),            # identifiers: EN1, I2C, A5
    re.compile(r"\[\s*\d+\s*:\s*\d+\s*\]|\[\s*\d+\s*\]"),                       # bit ranges [7:5], [0]
]
# A tag: `(derived)`, `(inference: ...)`, `(figure, read at 100 dpi)`; never a reference `(Figure 7-1)`.
# The whole parenthetical is the tag, so the numbers of `(derived: 24 - 0.8)` are covered by it.
_TAG = re.compile(r"\((derived|inference|figure|equation)(?=\)|[:,;]|\s+[^\d\s])[^()]*\)?", re.I)
_ROW_TAG = re.compile(r"^\((derived|inference|figure|equation)\)$", re.I)
TAG_CLASS = {"derived": "derived", "inference": "derived", "figure": "figure", "equation": "figure"}
# An `[SVD]` cite (reference-manual prose): the numbers of its clause come from the SVD, not a page.
_SVD_CITE = re.compile(r"\[SVD\b[^\]]*\]")
_TAG_OR_SVD = re.compile(f"{_TAG.pattern}|{_SVD_CITE.pattern}", re.I)
# Clause ends, outside parentheses: `;`, a sentence end, a spaced dash, the quirk renderer's `*Why it matters:*`.
_CLAUSE = re.compile(r";|\.(?=\s|$)|\s[—–]\s|\*why it matters:\*", re.I)
# Rendered cells that are structure, not numeric claims (DESIGN.md: tables are rendered from records)
STRUCTURAL = {"bits", "width", "order", "priority"}
_BLOCK_START = re.compile(r"^<!--\s*atlas:table\s+(.*?)\s*-->\s*$")
_BLOCK_END = re.compile(r"^<!--\s*atlas:end\s*-->\s*$")
_ENUM_KEY = re.compile(r"(?:^|;)\s*[^;=]*?=")


def _blank(s: str) -> str:
    """The strip patterns replaced by spaces of the same length, so positions survive."""
    for rx in _STRIP_BEFORE_NUMBERS:
        s = rx.sub(lambda m: " " * len(m.group()), s)
    return s


def _md_numbers(line: str) -> list[float]:
    return numbers(normalize(_blank(line)))


def _split_clauses(s: str) -> list[str]:
    """Clauses of a piece of prose; a boundary inside parentheses does not split (an aside belongs to
    the clause around it)."""
    out, start, depth, i = [], 0, 0, 0
    while i < len(s):
        c = s[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            m = _CLAUSE.match(s, i)
            if m:
                out.append(s[start:i])
                start = i = m.end()
                continue
        i += 1
    out.append(s[start:])
    return out


def tagged_pieces(body: str, row_tag: str | None = None) -> list[tuple[str, str | None]]:
    """Prose cut into (piece, tag class). A tag (`(derived)`, `(figure)`, ...) or an `[SVD]` cite
    (class `svd`) covers the piece before it in its clause and its own text; every other piece
    carries `row_tag` (a table row's Note tag). Shared by M and rm-check."""
    out: list[tuple[str, str | None]] = []
    pos = 0
    for m in _TAG_OR_SVD.finditer(body):
        cls = TAG_CLASS[m.group(1).lower()] if m.group(1) else "svd"
        clauses = _split_clauses(body[pos:m.start()])
        out += [(c, row_tag) for c in clauses[:-1]]
        out += [(clauses[-1], cls), (m.group(), cls)]
        pos = m.end()
    out.append((body[pos:], row_tag))
    return out


def _clause_claims(raw: str, row_tag: str | None = None) -> list[tuple[float, str | None]]:
    """(number, tag class) for a piece of prose. A tag covers its own parenthetical and the numbers
    before it in its clause; `row_tag` (a table row's Note tag) covers everything else."""
    return [(x, cls) for piece, cls in tagged_pieces(_blank(raw), row_tag) for x in numbers(normalize(piece))]


def _cells(line: str) -> list[str]:
    return [c.strip() for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def md_claims(line: str, cols: list[str] | None = None, rendered: str | None = None) -> list[tuple[float, str | None]]:
    """(number, tag class or None) for every numeric claim of one markdown line. A table row (`cols`
    is its header) is read cell by cell; a Note cell item that is only a tag covers the whole row, as
    the renderer writes a figure or equation record's `(figure)` there. `rendered` is the selector
    kind of the atlas:table block the row sits in: its structural cells are skipped."""
    if rendered == "quirk" and line.startswith("- **"):
        # render.t_quirk: `- **id** (meta): text (figure) *Why it matters:* why`. The src tag it puts
        # after the text is the record's own: it covers the text; the why is read as prose.
        head, sep, why = line.partition(" *Why it matters:* ")
        m = re.search(r" \((figure|equation)\)$", head)
        if m:
            return [(x, "figure") for x, _ in _clause_claims(head)] + (_clause_claims(why) if sep else [])
        return _clause_claims(line)
    if cols is None or not line.lstrip().startswith("|"):
        return _clause_claims(line)
    cells = _cells(line)
    row_tag = None
    for c, name in zip(cells, cols):
        if name == "note":
            for item in c.split(";"):
                m = _ROW_TAG.match(item.strip())
                if m:
                    row_tag = TAG_CLASS[m.group(1).lower()]
    out: list[tuple[float, str | None]] = []
    for i, c in enumerate(cells):
        name = cols[i] if i < len(cols) else ""
        if "token" in name or name in ("section", "sections"):   # an index table: budgets and section ids
            continue
        if rendered is not None:
            if name in STRUCTURAL or (rendered == "check" and name == "expression"):
                continue
            if name == "enum":
                c = _ENUM_KEY.sub(";", c)
        out += _clause_claims(c, row_tag)
    return out


def _values_by_page(recs: list[dict]) -> dict[int, list[float]]:
    """page -> numeric values of the records citing it; page 0 holds the records with no page (an
    SVD's), which a line cannot cite and which anchor on every line."""
    out: dict[int, list[float]] = {}
    for r in recs:
        vals = [float(r[k]) for k in NUMERIC_KEYS if isnum(r.get(k))]
        for p in R.pages(r) or [0]:
            out.setdefault(p, []).extend(vals)
    return out


def check_markdown_file(doc: DocText, path: Path, rec_values: dict[int, list[float]] | list[float]) -> dict:
    """M on one file. `rec_values`: page -> values of the verified records citing it (a plain list
    anchors on any page, the old behaviour). A number held only by a record on a page the line does
    not cite is a warning, not an anchor."""
    by_page = rec_values if isinstance(rec_values, dict) else {}
    everywhere = [x for vs in by_page.values() for x in vs] if by_page else list(rec_values)
    heads: list[tuple[int, set[int]]] = []
    stats = {"numbers": 0, "anchored_page": 0, "anchored_record": 0, "derived": 0, "figure": 0, "svd": 0,
             "record_elsewhere": 0, "unanchored": 0}
    bad, warn = [], []
    lines = path.read_text(encoding="utf-8").splitlines()
    cols: list[str] | None = None
    rendered: str | None = None
    for n, ln in enumerate(lines, 1):
        s = ln.rstrip()
        if _BLOCK_START.match(s):
            sel = _BLOCK_START.match(s).group(1).split()
            kinds = [t[5:] for t in sel if t.startswith("kind=")] or [t for t in sel[:1] if "=" not in t] or [""]
            rendered = kinds[0].split(",")[0]
            continue
        if _BLOCK_END.match(s):
            rendered = None
            continue
        m = re.match(r"^(#+)\s", s)
        if m:
            lvl = len(m.group(1))
            heads = [h for h in heads if h[0] < lvl]
            own = cited_pages(s)
            heads.append((lvl, own or (heads[-1][1] if heads else set())))
            continue
        if s.lstrip().startswith("|"):
            if n < len(lines) and re.match(r"^\s*\|?\s*:?-{3,}", lines[n]):
                cols = [c.lower() for c in _cells(s)]        # a header row: its separator follows
                continue
            if re.match(r"^\s*\|?\s*:?-{3,}", s):
                continue
        else:
            cols = None
        if s.startswith(("aka:", "Source:")) or s.strip().startswith("<!--"):
            continue
        claims = md_claims(s, cols, rendered)
        if not claims:
            continue
        pages = cited_pages(s) or (heads[-1][1] if heads else set())
        pnums = doc.page_numbers(sorted(pages))
        cited_vals = [x for p in sorted(pages) + [0] for x in by_page.get(p, [])] if by_page else everywhere
        for x, tag in claims:
            stats["numbers"] += 1
            if _close(x, pnums):
                stats["anchored_page"] += 1
            elif _close(x, cited_vals):
                stats["anchored_record"] += 1
            elif tag == "derived":
                stats["derived"] += 1
            elif tag == "figure":
                stats["figure"] += 1
            elif tag == "svd":
                stats["svd"] += 1
            elif _close(x, everywhere):
                stats["record_elsewhere"] += 1
                warn.append({"line": n, "number": x, "pages": sorted(pages), "text": s[:140],
                             "record_pages": sorted(p for p, vs in by_page.items() if _close(x, vs))[:6]})
            else:
                stats["unanchored"] += 1
                bad.append({"line": n, "number": x, "pages": sorted(pages), "text": s[:140]})
    return {"file": str(path.relative_to(doc.dir)), "stats": stats, "unanchored": bad[:60],
            "record_elsewhere": warn[:60]}


def _md_tables(text: str):
    """(header cells, [row cells]) for every pipe table."""
    lines = text.splitlines()
    i = 0
    while i < len(lines) - 1:
        if lines[i].lstrip().startswith("|") and re.match(r"^\s*\|?\s*:?-{3,}", lines[i + 1]):
            hdr = [c.strip() for c in lines[i].strip().strip("|").split("|")]
            rows, j = [], i + 2
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                rows.append([c.strip() for c in lines[j].strip().strip("|").split("|")])
                j += 1
            yield hdr, rows
            i = j
        else:
            i += 1


def check_tables(path: Path, rel: str) -> list[dict]:
    """Spike 10's ground-truth-free mangling checks, on value columns of markdown tables."""
    out = []
    for hdr, rows in _md_tables(path.read_text(encoding="utf-8")):
        low = [h.lower() for h in hdr]
        for h in low:
            if re.search(r"min\s*(<br>|/)?\s*typ\s*(<br>|/)?\s*max", h):
                out.append({"file": rel, "check": "fused-header", "cell": h})
        vcols = [i for i, h in enumerate(low) if h in ("min", "typ", "max", "nom", "value", "values")]
        scol = next((i for i, h in enumerate(low) if h in ("symbol", "sym")), None)
        idx = {h: i for i, h in enumerate(low)}
        for r in rows:
            for i in vcols:
                if i >= len(r):
                    continue
                c = PAGE_CITE.sub(" ", r[i].replace("–", "-")).strip()
                plain = re.sub(r"<[^>]+>", " ", c)
                if re.search(r"\d+\.\d+\.\d+|\d-\d+(?:\.\d+)?%?$", plain) and not re.search(r"[A-Za-z]{2,}", plain):
                    out.append({"file": rel, "check": "glued", "cell": r[i]})
                if "<br>" not in c and len(NUM_RE.findall(plain)) > 1 and not re.search(r"[A-Za-z=x]", plain):
                    out.append({"file": rel, "check": "packed", "cell": r[i]})
            if scol is not None and scol < len(r) and re.match(r"^\s*[VIRCTP]\s*(<br>|\s+\d)", r[scol]):
                out.append({"file": rel, "check": "orphan-subscript", "cell": r[scol]})
            if all(k in idx for k in ("min", "typ", "max")):
                try:
                    v = [float(r[idx[k]].replace("–", "-").replace("−", "-")) for k in ("min", "typ", "max")]
                    if not v[0] <= v[1] <= v[2]:
                        out.append({"file": rel, "check": "min<=typ<=max", "cell": " | ".join(r)})
                except (ValueError, IndexError):
                    pass
    return out


# ---------------------------------------------------------------- the run

def _status(results: dict, rec: dict, extra: list[str]) -> tuple[str, list[str]]:
    issues = [f"{k}: {v}" if not isinstance(v, dict) else f"{k}: {json.dumps(v, sort_keys=True)}"
              for k, v in results.items() if _is_fail(k, v)] + extra
    if issues:
        hard = [i for i in issues if not i.startswith("A:")]
        if rec.get("src") in ("figure", "equation") and not hard:
            return "unverifiable", issues
        return "failed", issues
    if rec.get("src") in ("figure", "equation") or rec.get("kind") == "figure":
        return "unverifiable", issues
    return "verified", issues


def new_prov(rec: dict, status: str, twin: str | None) -> str | None:
    prov = rec.get("prov")
    if prov in R.DETERMINISTIC:
        return "deterministic+crosschecked" if twin == "agree" else "deterministic"
    if prov in R.LLM:
        return "llm-verified" if status == "verified" else "llm-unverified"
    return prov


def atlas_ids(docdir: Path) -> set[str]:
    """Every record id of the atlas's other documents (the siblings of `docdir` under docs/).
    Record ids are globally unique by construction (`<part>.<kind>...`), so a ref resolves in
    whichever document holds it: an RM vote conflict refs SVD registers, an RM quirk an SVD field."""
    out: set[str] = set()
    docdir = Path(docdir).resolve()
    root = docdir.parent
    for d in sorted(root.iterdir()) if root.is_dir() else []:
        if d.is_dir() and d.resolve() != docdir:
            out.update(r.get("id") for _, r in R.load_records(d) if r.get("id"))
    return out


def run_check(docdir: Path, write: bool = True, render: bool = True, use_pdf: bool = True) -> dict:
    docdir = Path(docdir)
    doc = DocText(docdir, use_pdf=use_pdf)
    pairs = R.load_records(docdir)
    recs = [r for _, r in pairs]
    results: list[dict] = []
    # R: ids unique, except deterministic/LLM twins
    seen: dict[str, list[dict]] = {}
    for r in recs:
        seen.setdefault(r.get("id"), []).append(r)
    dup = {i for i, rs in seen.items() if len(rs) > 1 and not (
        len(rs) == 2 and sum(1 for x in rs if x.get("prov") in R.DETERMINISTIC) == 1)}
    ids = set(seen)
    if any(ref not in ids for r in recs for ref in r.get("refs") or []):
        ids |= atlas_ids(docdir)       # only read the other documents when a ref needs them
    greg = check_registers(recs)
    verdict = greg.get("verdict", {})
    struct_issue: dict[tuple[str, str], list[str]] = {}
    documented = {ref: r["id"] for r in recs if r.get("kind") == "conflict" for ref in r.get("refs") or []}
    documented_resets: list[str] = []
    for x in greg.get("structure", []):
        if x["issue"].startswith("field resets compose") and x["id"] in documented:
            # the document disagrees with itself and a conflict record keeps both sides (principle 4)
            documented_resets.append(f"{x['id']} ({x['source']}, {documented[x['id']]})")
            continue
        struct_issue.setdefault((x["id"], x["source"]), []).append(f"G: {x['issue']} ({x['source']})")
    checks = [r for r in recs if r.get("kind") == "check"]
    by_id: dict[str, dict] = {}
    for r in recs:  # a twin pair resolves to the LLM record, the one invariants were written against
        if r.get("id") not in by_id or r.get("prov") in R.LLM:
            by_id[r.get("id")] = r
    xs = check_invariants(by_id, checks)
    xmap = {x["id"]: x for x in xs}
    prov_changed = 0
    for (fname, r) in pairs:
        res = record_checks(doc, r) if r.get("kind") else {}
        extra = [f"V: {e}" for e in R.validate(r)]
        if r.get("id") in dup:
            extra.append(f"R: duplicate id {r.get('id')}")
        for ref in r.get("refs") or []:
            if ref not in ids:
                extra.append(f"R: ref {ref} resolves to no record in docs/")
        src = "deterministic" if r.get("prov") in R.DETERMINISTIC else "llm"
        extra += struct_issue.get((r.get("id"), src), [])
        twin = verdict.get(r.get("id")) if r.get("kind") in ("register", "field") else None
        if twin and twin.startswith("disagree"):
            extra.append(f"G: {twin}")
        if twin == "agree" and res.get("A") == "no-quote":
            res["A"] = "ok"  # a deterministic twin that agrees verifies structure without a quote
            res["A_by"] = "twin"
        if r.get("kind") == "check":
            x = xmap.get(r["id"])
            if x and not x["ok"]:
                extra.append(f"X: expect {x['expect']} got {x['got']}")
        status, issues = _status({k: v for k, v in res.items() if k != "A_by"}, r, extra)
        np_ = new_prov(r, status, twin)
        if np_ != r.get("prov"):
            prov_changed += 1
            if np_ is None:
                r.pop("prov", None)
            else:
                r["prov"] = np_
        warns = []
        kst = res.get("K") if isinstance(res.get("K"), str) else ""
        if kst.removeprefix("not checkable: ") in K_WARN:
            warns.append(f"K: {K_WARN[kst.removeprefix('not checkable: ')]} on p{R.pages(r)[0]}: "
                         "the column check did not run")
        results.append({"id": r.get("id"), "file": fname, "kind": r.get("kind"), "status": status,
                        "prov": r.get("prov"), "checks": res, "issues": issues,
                        **({"warnings": warns} if warns else {})})
    written = R.save_records(docdir, pairs) if write and prov_changed else []
    rendered = False
    if render and write and prov_changed and ((docdir / "s").is_dir() or (docdir / "facts.txt").exists()):
        from . import render as rnd
        rnd.render_doc(docdir)
        rendered = True
    good = {"llm-verified", "deterministic", "deterministic+crosschecked", "engineer-declared",
            "engineer-confirmed", "measured"}
    values = _values_by_page([r for r in recs if r.get("prov") in good])
    md_files = ([docdir / "card.md"] if (docdir / "card.md").exists() else []) + sorted((docdir / "s").glob("*.md"))
    # A non-PDF source has no pages to anchor prose to: its markdown is generated, not written.
    M = [check_markdown_file(doc, f, values) for f in md_files] if doc.n else []
    S = [x for f in md_files for x in check_tables(f, str(f.relative_to(docdir)))]
    T: dict[str, Any] = {}
    for f in md_files:
        tok = common.count_tokens(f.read_text(encoding="utf-8"))
        budget = CARD_BUDGET if f.name == "card.md" and f.parent == docdir else SECTION_BUDGET
        T[str(f.relative_to(docdir))] = {"tokens": tok, "budget": budget,
                                          "over": tok is not None and tok > budget}
    # summary
    count = lambda st: sum(1 for x in results if x["status"] == st)  # noqa: E731
    letter = lambda L, val="ok": sum(1 for x in results if x["checks"].get(L) == val)  # noqa: E731
    ran = lambda L: sum(1 for x in results if L in x["checks"])  # noqa: E731
    Kd = sum(1 for x in results if isinstance(x["checks"].get("K"), dict))
    summary = {
        "records": len(results), "verified": count("verified"), "failed": count("failed"),
        "unverifiable": count("unverifiable"),
        "A": {"anchored": sum(1 for x in results if x["checks"].get("A") == "ok" and not x["checks"].get("A_by")),
              "of": sum(1 for x in results if "A" in x["checks"] and not x["checks"].get("A_by")),
              "by_twin": sum(1 for x in results if x["checks"].get("A_by"))},
        "N": {"ok": letter("N"), "of": ran("N")},
        "K": {"agree": letter("K"), "disagree": Kd, "not_checkable": ran("K") - letter("K") - Kd,
              "unit_seen": sum(1 for x in results if x["checks"].get("K_unit_seen")),
              "row_not_found": letter("K", "not checkable: row-not-found"),
              "unreadable": sum(letter("K", f"not checkable: {st}") for st in K_WARN if st != "row-not-found")},
        "C": {"ok": letter("C"), "of": ran("C")},
        "U": {"ok": letter("U"), "of": ran("U")},
        "O": {"ok": letter("O"), "of": ran("O")},
        "X": {"as_expected": sum(1 for x in xs if x["ok"]), "of": len(xs)},
        "G": {"diffs": len(greg.get("diff", [])), "structure_issues": len(greg.get("structure", [])),
              **greg.get("summary", {})} if greg else {},
        "M": {"numbers": sum(m["stats"]["numbers"] for m in M),
              "unanchored": sum(m["stats"]["unanchored"] for m in M),
              "record_elsewhere": sum(m["stats"]["record_elsewhere"] for m in M)},
        "S": len(S),
        "T_over": [k for k, v in T.items() if v["over"]],
        "prov_changed": prov_changed, "rendered": rendered,
    }
    failing = summary["failed"] or summary["M"]["unanchored"] or S or summary["T_over"]
    summary["exit"] = 1 if failing else 0
    warnings = []
    if documented_resets:
        warnings.append(f"G: {len(documented_resets)} register resets disagree with their fields' resets inside the "
                        f"document, kept as conflict records: {', '.join(documented_resets[:6])}")
    for st, what in K_WARN.items():
        ids = [x["id"] for x in results if x["checks"].get("K") == f"not checkable: {st}"]
        if ids:
            warnings.append(f"K: {len(ids)} table rows {what.split(' ', 1)[1]} on their page, so their columns were "
                            f"not checked (they are verified by A/N/C/U/O only): {', '.join(ids[:8])}"
                            + (" ..." if len(ids) > 8 else ""))
    if summary["M"]["record_elsewhere"]:
        warnings.append(f"M: {summary['M']['record_elsewhere']} numbers are on no page their line cites and "
                        f"match only a verified record on another page: cite that page")
    summary["warnings"] = len(warnings)
    report = {"doc": doc.doc_id, "tool": f"embarch-atlas {__version__}",
              "pdf": doc.pdf_note, "summary": summary, "warnings": warnings, "records": results, "X": xs,
              "G": {k: v for k, v in greg.items() if k != "verdict"}, "M": M, "S": S, "T": T,
              "files_written": written}
    if write:
        common.write_json(docdir / "checks.json", report)
    return report


def print_summary(rep: dict, out=None, limit: int = 12) -> None:
    out = out or sys.stdout
    s = rep["summary"]
    w = lambda *a: print(*a, file=out)  # noqa: E731
    w(f"== {rep['doc']}  ({s['records']} records: {s['verified']} verified, {s['failed']} failed, "
      f"{s['unverifiable']} unverifiable figure/equation facts)")
    w(f"A quotes anchored {s['A']['anchored']}/{s['A']['of']} | N {s['N']['ok']}/{s['N']['of']} | "
      f"K agree {s['K']['agree']}, disagree {s['K']['disagree']}, not checkable {s['K']['not_checkable']} "
      f"(unit column seen {s['K']['unit_seen']}) | C {s['C']['ok']}/{s['C']['of']} | U {s['U']['ok']}/{s['U']['of']} | "
      f"O {s['O']['ok']}/{s['O']['of']} | X {s['X']['as_expected']}/{s['X']['of']}")
    if s["G"]:
        w(f"G registers: {s['G']}")
    shown = 0
    for x in rep["records"]:
        if x["status"] == "failed" and shown < limit:
            w(f"   {x['id']} ({x['file']}): {'; '.join(x['issues'])[:200]}")
            shown += 1
    if s["failed"] > shown:
        w(f"   ... {s['failed'] - shown} more failed records in checks.json")
    for d in rep["G"].get("diff", [])[:limit]:
        w(f"   G diff {d['id']}: {d['issue']}")
    for m in rep["M"]:
        st = m["stats"]
        flag = "" if not st["unanchored"] else f"  <- {st['unanchored']} unanchored"
        elsewhere = f", {st['record_elsewhere']} only in a record on another page (warning)" \
            if st.get("record_elsewhere") else ""
        svd = f", {st['svd']} [SVD]" if st.get("svd") else ""
        w(f"M {m['file']}: {st['numbers']} numbers, {st['anchored_page']} on a cited page, "
          f"{st['anchored_record']} in a record, {st['derived']} derived, {st['figure']} figure{svd}{elsewhere}{flag}")
        for b in m["unanchored"][:4]:
            w(f"   line {b['line']}: {b['number']:g} not on p{b['pages']}: {b['text'][:90]}")
        for b in m.get("record_elsewhere", [])[:2]:
            w(f"   line {b['line']}: {b['number']:g} not on p{b['pages']}, in a record on p{b['record_pages']}: "
              f"{b['text'][:70]}")
    for x in rep["S"][:limit]:
        w(f"S {x['file']}: {x['check']}: {x['cell'][:80]}")
    for k, v in rep["T"].items():
        if v["over"]:
            w(f"T {k}: {v['tokens']} tokens > budget {v['budget']}")
    for x in rep.get("warnings", []):
        w(f"warning: {x}")
    w(f"prov changed on {s['prov_changed']} records{'; tables re-rendered' if s['rendered'] else ''}; "
      f"exit {s['exit']}")


def cmd_check(args) -> int:
    atlas = common.atlas_dir(args.atlas_dir)
    docdir = atlas / "docs" / args.doc_id
    if not (docdir / "doc.json").exists():
        print(f"doc check: {docdir} has no doc.json; run doc prep first", file=sys.stderr)
        return 2
    rep = run_check(docdir, write=True, render=not args.no_render, use_pdf=not args.no_pdf)
    print_summary(rep)
    return rep["summary"]["exit"]


def register(sub, groups) -> None:
    g = common.verb_group(sub, groups, "doc", "per-document steps: prep, check, render, regparse")
    p = g.add_parser("check", help="checks A-T on records and markdown; writes checks.json, sets prov")
    p.add_argument("doc_id")
    p.add_argument("--no-render", action="store_true", help="do not re-render tables after prov changes")
    p.add_argument("--no-pdf", action="store_true", help="use only pages/ (and bbox files), not the source PDF")
    p.set_defaults(func=cmd_check)

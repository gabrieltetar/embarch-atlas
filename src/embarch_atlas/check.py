"""`doc check <doc_id>`: every deterministic check on one document's records and markdown.

Record checks (per record, against the text layer of the pages it cites):
  V  schema          the record validates against schemas/record.schema.json
  A  quote anchoring every quote occurs on a cited page, in the pdftotext or the PyMuPDF text layer,
                     with Symbol-font private-use glyphs mapped or stripped (Read shows them as nothing)
  N  number in quote every numeric min/nom/typ/max is a number inside the quote (requirements: every
                     number of `text`)
  K  columns         for a table row under a MIN/TYP/MAX header, word x-positions give an independent
                     column assignment (and the UNIT column when there is one); it must agree
  C  conditions      every number in `cond` is on a cited page
  U  units           the unit's dimension fits the symbol (V, I, t, R, T, f, C, P, L) or the parameter,
                     and the unit string is on the cited page
  O  order           min <= nom/typ <= max
  R  references      quirk/conflict refs resolve; ids are unique (a deterministic record and an LLM
                     record may share an id: they are twins, compared by G)
  X  invariants      `check` records: expr evaluated over R['<id>'].min/nom/typ/max, result == expect
  G  registers       field widths cover the register, no overlap, field resets compose to the register
                     reset, unique addresses, summary-table coverage, and LLM vs deterministic twins
Markdown checks (card.md, s/*.md):
  M  numbers         every number is on a page the line cites (or its heading cites), is a value of a
                     verified record, or the line is tagged (derived), (figure) or (inference)
  S  table structure the ground-truth-free converter checks of spike 10 on value columns: glued numbers,
                     packed cells, fused MIN/TYP/MAX header, orphaned subscript, min <= typ <= max
  T  budgets         card.md <= 1.5K tokens, each s/*.md <= 4K (o200k)

Then `prov` is set: an LLM record that passes becomes llm-verified, one that fails llm-unverified
(it is never deleted); a figure or equation fact stays llm-unverified, since only a second look can
verify it. A deterministic record whose twin agrees becomes deterministic+crosschecked.
Writes checks.json; exits 1 while anything is failing.
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


# ---------------------------------------------------------------- the text layer

class DocText:
    """Text layers of one document: pdftotext -layout (pages/), PyMuPDF (from the PDF, when it is
    still there and unchanged), and word boxes (pages/*.bbox.json, else the PDF)."""

    def __init__(self, docdir: Path, use_pdf: bool = True):
        self.dir = Path(docdir)
        self.meta = common.read_json(self.dir / "doc.json")
        self.doc_id = self.meta["doc_id"]
        self.n = int(self.meta["pages"])
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

    def has(self, q: str, p: int) -> bool:
        if not 1 <= p <= self.n:
            return False
        parts = [x for x in re.split(r"\s*(?:\.\.\.|…)\s*", normalize(q, "map")) if x]
        if not parts:
            return False
        for f in self.forms(p):
            pos, ok = 0, True
            for part in parts:
                i = f.find(part, pos)
                if i < 0:
                    ok = False
                    break
                pos = i + len(part)
            if ok:
                return True
        return False

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

    def columns(self, rec: dict) -> dict:
        """Independent min/nom/typ/max (and unit) for a record's row, from word x-positions under the
        nearest MIN/TYP/MAX header above the row that holds the tail of the quote."""
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
                headers.append((yc, h))
        if not headers:
            return {"status": "no-header"}
        qtok = qn.split()
        tail = " ".join(qtok[-3:]) if len(qtok) >= 3 else qn
        best = None
        for yc, ws in bands:
            txt = " ".join(w[4] for w in ws)
            if tail in txt or qn in txt:
                toks = txt.split()
                score = sum(1 for t in qtok if t in toks)
                if best is None or score > best[0]:
                    best = (score, yc, ws)
        if best is None:
            return {"status": "row-not-found"}
        _, yc, ws = best
        above = [h for h in headers if h[0] < yc]
        if not above:
            return {"status": "no-header-above"}
        hy, hdr = above[-1]
        left = min(hdr.values()) - 30
        det: dict[str, float] = {}
        for w in ws:
            xc = (w[0] + w[2]) / 2
            if xc < left or not NUM_RE.fullmatch(w[4]):
                continue
            col = min(hdr, key=lambda k: abs(hdr[k] - xc))
            if abs(hdr[col] - xc) < 28:
                det.setdefault(col, float(w[4]))
        unit = None
        uh = [w for y2, ws2 in bands if abs(y2 - hy) < 1 for w in ws2 if w[4].upper() in ("UNIT", "UNITS")]
        if uh:
            ux = (uh[0][0] + uh[0][2]) / 2
            cand = [w[4] for w in ws if abs((w[0] + w[2]) / 2 - ux) < 25 and not NUM_RE.fullmatch(w[4])]
            unit = cand[0] if cand else None
        return {"status": "ok", "det": det, "unit": unit}


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
    elif kind not in ("figure",) and rec.get("src") not in ("figure", "equation"):
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
    if kind in ("limit", "timing") and rec.get("group") in K_GROUPS and rec.get("src") == "table" and qs and numv:
        c = doc.columns(rec)
        if c["status"] == "ok":
            det, diff = c["det"], {}
            for k in NUMERIC_KEYS:
                lv, dv = numv.get(k), det.get(k)
                if lv is None and dv is None:
                    continue
                if lv is None or dv is None or not math.isclose(float(lv), dv, rel_tol=1e-9):
                    diff[k] = {"record": lv, "columns": dv}
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
_ATTRS = set(NUMERIC_KEYS) | {"value"}


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
            raise ValueError(f"attribute .{node.attr} not allowed (use .min/.nom/.typ/.max)")
        if isinstance(node, ast.Call) and not (isinstance(node.func, ast.Name) and node.func.id in _SAFE_FUNCS):
            raise ValueError("only abs/all/any/min/max/round/sum/len may be called")
    # env goes into globals: a generator expression cannot see eval's locals
    return eval(compile(tree, "<invariant>", "eval"), {"__builtins__": {}, **_SAFE_FUNCS, **env})


def check_invariants(by_id: dict[str, dict], checks: list[dict]) -> list[dict]:
    ns = {k: SimpleNamespace(**{a: v.get(a) for a in _ATTRS}) for k, v in by_id.items()}
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
            if gaps:
                issues.append({"id": r["id"], "issue": f"bits {gaps} covered by no field"})
            if defined and r.get("reset") is not None and comp != R.to_int(r["reset"]):
                issues.append({"id": r["id"], "issue": f"field resets compose to {comp:#x}, register reset {r['reset']}"})
        a = R.to_int(r.get("addr"))
        if a is not None:
            if a in seen and seen[a] != r["name"]:
                issues.append({"id": r["id"], "issue": f"address {a:#x} is also {seen[a]}"})
            seen[a] = r["name"]
    return issues


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
            dv = R.to_int(d.get(k)) if k != "width" else d.get(k)
            lv = R.to_int(l.get(k)) if k != "width" else l.get(k)
            if dv != lv:
                diffs.append(f"{k}: llm {l.get(k)} det {d.get(k)}")
        dm = {f["name"]: f for f in merge_reserved(det_fields.get(rid, []))}
        lm = {f["name"]: f for f in merge_reserved(llm_fields.get(rid, []))}
        field_verdict: dict[str, str] = {}
        for name in sorted(set(dm) | set(lm)):
            df, lf = dm.get(name), lm.get(name)
            if df is None or lf is None:
                msg = f"field {name} only in {'llm' if df is None else 'deterministic'}"
                diffs.append(msg)
                for i in (lf or df)["_ids"]:
                    field_verdict[i] = f"disagree: {msg}"
                continue
            fd = []
            if df["bits"] != lf["bits"]:
                fd.append(f"bits llm {lf['bits']} det {df['bits']}")
            if df.get("access") != lf.get("access"):
                fd.append(f"access llm {lf.get('access')} det {df.get('access')}")
            if R.to_int(df.get("reset")) != R.to_int(lf.get("reset")):
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

_STRIP_BEFORE_NUMBERS = [
    re.compile(r"<!--.*?-->"),
    PAGE_CITE,
    re.compile(r"§\s*[\d.]+"),
    re.compile(r"\b[a-z0-9][a-z0-9._+-]*@[A-Za-z0-9._-]+"),                   # doc ids
    re.compile(r"\bfn\s*\d+(?:\s*(?:,|and)\s*(?:fn\s*)?\d+)*", re.I),              # footnote refs: fn 13, 18
    re.compile(r"(?:figure|fig\.|table|eq\.?|equation|footnote|q|note|step|section)\s*\d+(?:[-.]\d+)*", re.I),
    re.compile(r"\b\d+(?:\.\d+){2,}\b"),                                       # section numbers 7.3.4
    re.compile(r"\b0x[0-9a-f]+\b|\b0b[01]+\b|\b[01]+b\b|\b[0-9a-f]+h\b", re.I),  # hex, binary
    re.compile(r"\b[A-Za-z_]+[A-Za-z_./]*\d[\w./]*(?:-\d[\w./]*)*"),            # identifiers: EN1, I2C, A5
    re.compile(r"\[\s*\d+\s*:\s*\d+\s*\]|\[\s*\d+\s*\]"),                       # bit ranges [7:5], [0]
]
_TAGS = ("(derived", "(inference", "(figure")


def _md_numbers(line: str) -> list[float]:
    body = line
    for rx in _STRIP_BEFORE_NUMBERS:
        body = rx.sub(" ", body)
    return numbers(normalize(body))


def check_markdown_file(doc: DocText, path: Path, rec_values: list[float]) -> dict:
    heads: list[tuple[int, set[int]]] = []
    stats = {"numbers": 0, "anchored_page": 0, "anchored_record": 0, "derived": 0, "figure": 0, "unanchored": 0}
    bad = []
    in_code = False
    for n, ln in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        s = ln.rstrip()
        if s.startswith("```"):
            in_code = not in_code
        m = re.match(r"^(#+)\s", s)
        if m:
            lvl = len(m.group(1))
            heads = [h for h in heads if h[0] < lvl]
            own = cited_pages(s)
            heads.append((lvl, own or (heads[-1][1] if heads else set())))
            continue
        if s.startswith(("aka:", "Source:")) or s.strip().startswith("<!--"):
            continue
        ns = _md_numbers(s)
        if not ns:
            continue
        pages = cited_pages(s) or (heads[-1][1] if heads else set())
        tagged = next((t for t in _TAGS if t in s.lower()), None)
        pnums = doc.page_numbers(sorted(pages))
        for x in ns:
            stats["numbers"] += 1
            if _close(x, pnums):
                stats["anchored_page"] += 1
            elif _close(x, rec_values):
                stats["anchored_record"] += 1
            elif tagged in ("(derived", "(inference"):
                stats["derived"] += 1
            elif tagged == "(figure":
                stats["figure"] += 1
            else:
                stats["unanchored"] += 1
                bad.append({"line": n, "number": x, "pages": sorted(pages), "text": s[:140]})
    return {"file": str(path.relative_to(doc.dir)), "stats": stats, "unanchored": bad[:60]}


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
    greg = check_registers(recs)
    verdict = greg.get("verdict", {})
    struct_issue: dict[tuple[str, str], list[str]] = {}
    for x in greg.get("structure", []):
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
                extra.append(f"R: ref {ref} resolves to no record")
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
        results.append({"id": r.get("id"), "file": fname, "kind": r.get("kind"), "status": status,
                        "prov": r.get("prov"), "checks": res, "issues": issues})
    written = R.save_records(docdir, pairs) if write and prov_changed else []
    rendered = False
    if render and write and prov_changed and ((docdir / "s").is_dir() or (docdir / "facts.txt").exists()):
        from . import render as rnd
        rnd.render_doc(docdir)
        rendered = True
    good = {"llm-verified", "deterministic", "deterministic+crosschecked", "engineer-declared",
            "engineer-confirmed", "measured"}
    values = [float(r[k]) for r in recs if r.get("prov") in good for k in NUMERIC_KEYS if isnum(r.get(k))]
    md_files = ([docdir / "card.md"] if (docdir / "card.md").exists() else []) + sorted((docdir / "s").glob("*.md"))
    M = [check_markdown_file(doc, f, values) for f in md_files]
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
              "unit_seen": sum(1 for x in results if x["checks"].get("K_unit_seen"))},
        "C": {"ok": letter("C"), "of": ran("C")},
        "U": {"ok": letter("U"), "of": ran("U")},
        "O": {"ok": letter("O"), "of": ran("O")},
        "X": {"as_expected": sum(1 for x in xs if x["ok"]), "of": len(xs)},
        "G": {"diffs": len(greg.get("diff", [])), "structure_issues": len(greg.get("structure", [])),
              **greg.get("summary", {})} if greg else {},
        "M": {"numbers": sum(m["stats"]["numbers"] for m in M),
              "unanchored": sum(m["stats"]["unanchored"] for m in M)},
        "S": len(S),
        "T_over": [k for k, v in T.items() if v["over"]],
        "prov_changed": prov_changed, "rendered": rendered,
    }
    failing = summary["failed"] or summary["M"]["unanchored"] or S or summary["T_over"]
    summary["exit"] = 1 if failing else 0
    report = {"doc": doc.doc_id, "tool": f"embarch-atlas {__version__}",
              "pdf": doc.pdf_note, "summary": summary, "records": results, "X": xs,
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
        w(f"M {m['file']}: {st['numbers']} numbers, {st['anchored_page']} on a cited page, "
          f"{st['anchored_record']} in a record, {st['derived']} derived, {st['figure']} figure{flag}")
        for b in m["unanchored"][:4]:
            w(f"   line {b['line']}: {b['number']:g} not on p{b['pages']}: {b['text'][:90]}")
    for x in rep["S"][:limit]:
        w(f"S {x['file']}: {x['check']}: {x['cell'][:80]}")
    for k, v in rep["T"].items():
        if v["over"]:
            w(f"T {k}: {v['tokens']} tokens > budget {v['budget']}")
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

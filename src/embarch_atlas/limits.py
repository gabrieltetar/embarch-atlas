"""DT limit properties vs the part's datasheet limits: `limits.json`, and classes in the mismatch report.

Which properties: every numeric property with a unit suffix in the Zephyr convention
(`-microvolt`, `-millivolt`, `-microamp`, `-milliamp`, `-ohms`, `-milliohm`, `-celsius`,
`-millicelsius`, `-ms`, `-us`, `-hz`, `-percent`, `-microamp-hours`, ...; `<a>-per-<b>`
is a rate) on an okay DT node that resolves to a part: the I2C identity join (`pins.json`:
node -> board part -> `part_docs`), else `boards/bindings.json` `compat_parts`, else the
compatible's model token (the same resolution as `codecheck`). Values go to SI; an array
is compared element by element. Unit properties on nodes with no part are counted only.

Which records: the part's `limit` and `timing` records, mapped to a property in this order:

1. declared, in `boards/bindings.json`:
   `"prop_limits": {"<compatible>": {"<prop>": <spec>}}`. <spec> is a record id, a list of
   ids, `{"records": [...], "times": "<prop>"}` (or `"per"`: the engineer's own scaling by
   another property of the node), or `null` (no datasheet limit applies). A declared
   mapping wins, is compared without the filters below, and is marked `declared`.
2. proposed, by name: records whose unit has the property's dimension, scored on the
   property's words (vendor prefix, unit and dimension words dropped: `output-voltage-max`
   gives `output` and the bound `max`) against the record's `sym` (each comma/slash
   alternative on its own; `VOUT` reads as `output`) and the first clause of its `param`.
   A shared word scores 1, a record word the property lacks costs 0.5, a matching bound
   0.5; a symbol built from the property (`VOUT`, `VOUTMAX`) equal to the record's scores
   2, a prefix of it 1. Opposite bounds (`min` vs `max`) exclude. Per group, the rows with
   the best score at or above 1 are kept.

Proposed rows are not compared when: the condition names part numbers other than the
board's (a variant row); a current row is stated at another sense-resistor value than the
node's; an EC row sits at a stated register setting (a point, not a range); no bound is a
number (typ only, or an expression such as `VOUT+0.3`). Each is counted with its reason.

Scaling by a board component: a current property on a node that also carries a
sense-resistor property (`sense-resistor-milliohm`, `*-shunt-*`) is compared as a sense
voltage only when a record states the relation: a `rule` text, an equation `figure` or a
limit row's `param`/`note` of the form `<lhs> = V<x> / R<sense>` (or `V<x> = <lhs> x
R<sense>`) whose left side names the property. The scaled value (I x R) is compared with the part's `V<x>` rows and with
bounds stated in that record (`up to 63.5 mV`, `60 mV max`). With no such record and no
row stated at the node's own sense resistor, the property is reported as needing a
declared formula: nothing is guessed.

Checks and weights: the value must lie inside each applicable row's [min, max]. Outside an
`abs` row is severity `error` (`dt-limit-over-abs-max`); outside a `recommended`,
`register`, `application` row or a rule-stated bound is `warn`
(`dt-limit-outside-recommended`); outside an EC range is `info` (`dt-limit-outside-ec`;
EC rows of one symbol under different conditions are pooled, inside any of them passes).
Also `dt-limit-needs-formula` (info) and `dt-limit-declared-unknown` (warn).

Output: `atlases/<id>/limits.json`. `join` runs this through `mismatch.run`, which folds
its checks, findings and a summary section into `checks.md` and `mismatches.json`.
"""
from __future__ import annotations

import collections
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from embarch_atlas.common import read_json, write_json

CHECK = "DT limit properties inside datasheet limits"
CHECK_SENSE = "DT currents set through a sense resistor have a stated relation"
SOURCES = "dt.json properties, part limit/timing records, boards/bindings.json prop_limits"
SOURCES_SENSE = "dt.json properties, rule/equation records"
ORDER = ["dt-limit-over-abs-max", "dt-limit-outside-recommended", "dt-limit-declared-unknown",
         "dt-limit-needs-formula", "dt-limit-outside-ec"]
SETTLE = {
    "dt-limit-over-abs-max": "the abs-max row on the cited page against the DT value: if the row applies to this pin and "
                             "condition, the DT value must come down (beyond abs max the part may be damaged)",
    "dt-limit-outside-recommended": "the cited row's condition: does it apply to this board; if so, bring the DT value "
                                    "inside the recommended or programmable range",
    "dt-limit-outside-ec": "the EC row's conditions against this board's operating point (an EC row is a guaranteed "
                           "behaviour, not a limit)",
    "dt-limit-needs-formula": "a rule or equation record that states how the limit scales with the sense resistor "
                              "(`I = VSNS / RSNS`), or a declared mapping in boards/bindings.json prop_limits: "
                              "{\"records\": [\"<id>\"], \"times\": \"<sense property>\"}",
    "dt-limit-declared-unknown": "fix the record id in boards/bindings.json prop_limits, or translate that row",
}
WEIGHT = {"abs": "error", "recommended": "warn", "register": "warn", "application": "warn", "rule": "warn"}
CLASS_OF = {"error": "dt-limit-over-abs-max", "warn": "dt-limit-outside-recommended", "info": "dt-limit-outside-ec"}
SEV_RANK = {"error": 0, "warn": 1, "info": 2}
STRICT = {"abs", "recommended", "register", "application", "esd", "rule"}   # each row on its own
THRESHOLD = 1.0
FOLD_NO_LIMIT = "DT limits: no datasheet limit for this property"
FOLD_NOT_COMPARABLE = ("DT limits: mapped, but no row is comparable (typ only, expression bound, register-setting "
                       "point, other variant or sense resistor)")
FOLD_NO_PART = "DT limits: unit properties on nodes with no part datasheet"
FOLD_DECLARED_NONE = "DT limits: no datasheet limit applies (declared)"
NOT_PART_DOCS = ("svd", "rm", "reference-manual", "prd")


# ---------------------------------------------------------------- units

_PREFIX_WORDS = {"": 1.0, "kilo": 1e3, "mega": 1e6, "milli": 1e-3, "micro": 1e-6, "nano": 1e-9, "pico": 1e-12,
                 "deci": 0.1}
_BASE_WORDS = {"volt": "V", "amp": "A", "ampere": "A", "ohm": "ohm", "watt": "W", "celsius": "degC", "farad": "F",
               "henry": "H", "second": "s", "hertz": "Hz", "amphour": "Ah", "watthour": "Wh"}
_SHORT = {"ms": ("s", 1e-3), "us": ("s", 1e-6), "ns": ("s", 1e-9), "ps": ("s", 1e-12), "sec": ("s", 1.0),
          "msec": ("s", 1e-3), "usec": ("s", 1e-6), "nsec": ("s", 1e-9), "psec": ("s", 1e-12),
          "hz": ("Hz", 1.0), "khz": ("Hz", 1e3), "mhz": ("Hz", 1e6), "percent": ("%", 1.0), "mv": ("V", 1e-3),
          "kohm": ("ohm", 1e3), "kohms": ("ohm", 1e3)}


def unit_word(w: str) -> tuple[str, float] | None:
    """A DT unit suffix -> (dimension, scale to SI): `microamp-hours` -> ('Ah', 1e-6), `ohms` -> ('ohm', 1)."""
    w = w.lower()
    if w in _SHORT:
        return _SHORT[w]
    flat = w.replace("-", "")
    for cand in (flat, flat[:-1] if flat.endswith("s") else None):
        if not cand:
            continue
        for pfx, sc in sorted(_PREFIX_WORDS.items(), key=lambda x: -len(x[0])):
            if cand.startswith(pfx) and cand[len(pfx):] in _BASE_WORDS:
                return _BASE_WORDS[cand[len(pfx):]], sc
    return None


def _split_unit(name: str) -> tuple[str, str, float] | None:
    parts = name.split("-")
    for k in (2, 1):
        if len(parts) > k:
            u = unit_word("-".join(parts[-k:]))
            if u:
                return "-".join(parts[:-k]), u[0], u[1]
    return None


def prop_unit(prop: str) -> tuple[str, str, float] | None:
    """`vendor,output-current-max-microamp` -> ('output-current-max', 'A', 1e-6); None without a unit suffix.

    `<a>-per-<b>` is a rate: `slew-rate-microvolt-per-microsecond` -> ('slew-rate', 'V/s', 1.0)."""
    base = prop.split(",", 1)[-1].lower()
    if "-per-" in base:
        head, _, tail = base.rpartition("-per-")
        a, b = _split_unit(head), unit_word(tail)
        if a and b:
            return a[0], f"{a[1]}/{b[0]}", a[2] / b[1]
        return None
    return _split_unit(base)


_R_BASE = [("Ah", "Ah"), ("Wh", "Wh"), ("Hz", "Hz"), ("ohm", "ohm"), ("degC", "degC"), ("V", "V"), ("A", "A"),
           ("W", "W"), ("s", "s"), ("F", "F"), ("H", "H")]
_R_PFX = {"": 1.0, "G": 1e9, "M": 1e6, "k": 1e3, "K": 1e3, "m": 1e-3, "µ": 1e-6, "u": 1e-6, "n": 1e-9, "p": 1e-12}


def _runit(s: str) -> tuple[str, float] | None:
    if s == "%":
        return "%", 1.0
    if s in ("sec", "secs"):
        return "s", 1.0
    for sfx, dim in _R_BASE:
        if s.endswith(sfx) and s[:-len(sfx)] in _R_PFX:
            return dim, _R_PFX[s[:-len(sfx)]]
    return None


def record_unit(u: Any) -> tuple[str, float] | None:
    """A record's `unit` -> (dimension, scale to SI): 'mV' -> ('V', 1e-3), 'kΩ' -> ('ohm', 1e3), 'mV/µs' -> ('V/s', 1e3).
    Relative units ('% of reading'), counts ('bits', 'LSB') and anything else give None."""
    if not isinstance(u, str) or not u.strip():
        return None
    s = (u.strip().replace("μ", "µ").replace("Ω", "ohm").replace("Ohm", "ohm").replace("°C", "degC")
         .replace("℃", "degC").replace(" ", ""))
    if s.lower() in ("ohm", "ohms"):
        s = "ohm"
    if "/" in s:
        a, _, b = s.partition("/")
        ua, ub = _runit(a), _runit(b)
        return (f"{ua[0]}/{ub[0]}", ua[1] / ub[1]) if ua and ub else None
    return _runit(s)


def num(v: Any, side: str | None = None) -> float | None:
    """A record bound as a number: int/float, '5.5', '−0.3', '±51.2' (the side picks the sign); None for an expression."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    s = str(v).strip().replace("−", "-").replace("–", "-").replace(" ", "")
    m = re.fullmatch(r"±(\d+(?:\.\d+)?(?:e[-+]?\d+)?)", s, re.I)
    if m:
        return -float(m[1]) if side == "min" else float(m[1])
    try:
        return float(s)
    except ValueError:
        return None


def fmt_si(v: float, dim: str) -> str:
    unit = {"ohm": "Ω", "degC": "°C"}.get(dim, dim)
    if dim in ("%", "degC") or v == 0:
        return f"{v:g} {unit}"
    for p, f in (("G", 1e9), ("M", 1e6), ("k", 1e3), ("", 1.0), ("m", 1e-3), ("µ", 1e-6), ("n", 1e-9), ("p", 1e-12)):
        if abs(v) >= f * 0.999999:
            return f"{v / f:.4g} {p}{unit}"
    return f"{v:g} {unit}"


def fmt_range(lo: float | None, hi: float | None, dim: str) -> str:
    a = fmt_si(lo, dim) if lo is not None else "-"
    b = fmt_si(hi, dim) if hi is not None else "-"
    return f"[{a}, {b}]"


# ---------------------------------------------------------------- words

STOP = {"of", "the", "a", "an", "at", "to", "for", "with", "and", "or", "in", "on", "by", "per", "into", "from", "as",
        "is", "be", "when", "while", "during", "under", "vs", "versus", "each", "all", "any", "its", "this", "that",
        "not", "no", "only", "than", "then", "if"}
GENERIC = {"pin", "pins", "mode", "setting", "settings", "set", "value", "values", "range", "level", "regulation",
           "regulated", "regulate", "reg", "rng", "op", "operating", "measured", "typical", "typ", "nom", "spec",
           "specification", "register", "host", "programmable", "programmed", "circuit", "total", "design", "rated",
           "rating", "control", "target"}
DIM_WORDS = {"voltage", "volt", "volts", "current", "currents", "amp", "amps", "resistance", "resistor", "resistors",
             "impedance", "ohm", "ohms", "frequency", "freq", "temperature", "temp", "power", "capacitance",
             "inductance", "time", "duration", "rate", "percent", "percentage", "capacity"}
BOUND = {"max": "max", "maximum": "max", "limit": "max", "limits": "max", "lim": "max", "clamp": "max",
         "upper": "max", "ceiling": "max", "min": "min", "minimum": "min", "lower": "min", "floor": "min"}
NEUTRAL = {"default", "initial", "init", "nominal"}
COMPOUND = {("soft", "start"): "softstart", ("short", "circuit"): "short", ("over", "voltage"): "overvoltage",
            ("under", "voltage"): "undervoltage", ("over", "current"): "overcurrent", ("pre", "charge"): "precharge"}
SYN = {
    "out": "output", "outp": "output", "output": "output", "in": "input", "inp": "input", "input": "input",
    "sys": "system", "system": "system", "bat": "battery", "batt": "battery", "battery": "battery",
    "chg": "charge", "chrg": "charge", "charge": "charge", "charging": "charge", "charger": "charge",
    "dchg": "discharge", "dischg": "discharge", "dschg": "discharge", "dsg": "discharge", "discharge": "discharge",
    "discharging": "discharge", "prechg": "precharge", "prechrg": "precharge", "precharge": "precharge",
    "term": "termination", "termination": "termination", "terminate": "termination",
    "sns": "sense", "sen": "sense", "sense": "sense", "sensing": "sense", "shunt": "sense",
    "ref": "reference", "reference": "reference", "ss": "softstart", "softstart": "softstart",
    "ovp": "overvoltage", "ov": "overvoltage", "overvoltage": "overvoltage",
    "uvlo": "undervoltage", "uv": "undervoltage", "undervoltage": "undervoltage",
    "ocp": "overcurrent", "oc": "overcurrent", "overcurrent": "overcurrent",
    "sc": "short", "scp": "short", "short": "short", "sd": "shutdown", "shdn": "shutdown", "shutdown": "shutdown",
    "q": "quiescent", "quiescent": "quiescent", "lkg": "leakage", "leak": "leakage", "leakage": "leakage",
    "avg": "average", "average": "average", "lim": "limit", "limit": "limit",
    "dly": "delay", "delay": "delay", "delays": "delay",
    "deb": "debounce", "debounce": "debounce", "comp": "compensation", "compensation": "compensation",
    "droop": "drop", "drop": "drop", "hys": "hysteresis", "hyst": "hysteresis", "hysteresis": "hysteresis",
    "acc": "accuracy", "accuracy": "accuracy", "thr": "threshold", "th": "threshold", "thresh": "threshold",
    "threshold": "threshold", "thresholds": "threshold", "en": "enable", "enable": "enable", "fb": "feedback",
    "feedback": "feedback", "bus": "bus", "sw": "switching", "switching": "switching", "slew": "slew",
    "lsb": "lsb", "res": "resolution", "resolution": "resolution", "err": "error", "error": "error",
    "cell": "cell", "cells": "cell", "full": "full", "empty": "empty", "sink": "sink", "source": "source",
}
ABBR: dict[str, list[str]] = collections.defaultdict(list)
for _k, _v in SYN.items():
    if _k != _v and len(_k) >= 2:
        ABBR[_v].append(_k)
for _v in set(SYN.values()):
    ABBR[_v].append(_v)
LETTER = {"V": "v", "A": "i", "ohm": "r", "s": "t", "Hz": "f", "degC": "t", "W": "p", "Ah": "q"}


def _raw_tokens(text: str) -> list[str]:
    out = []
    for t in re.split(r"[^A-Za-z0-9]+", text or ""):
        if not t:
            continue
        if re.search(r"[A-Z][a-z]{2,}", t) and not re.match(r"[a-z]", t):     # CamelCase: InputVoltage
            out += re.findall(r"[A-Z][a-z]+|[A-Z]+(?![a-z])|\d+", t)
        else:
            out.append(t)
    return out


def words(text: str, symbolic: bool = False) -> list[str]:
    """Canonical words: `VOUT` -> output, `ILIM_AVG` -> limit, average, `soft-start` -> softstart; stop words dropped."""
    out: list[str] = []
    toks = _raw_tokens(text)
    i = 0
    while i + 1 < len(toks):
        pair = (toks[i].lower(), toks[i + 1].lower())
        if pair in COMPOUND:
            toks[i:i + 2] = [COMPOUND[pair]]
        i += 1
    for t in toks:
        low = t.lower()
        is_sym = symbolic or (t.isupper() and len(t) >= 3) or bool(re.match(r"[a-z][A-Z]", t))
        if not is_sym and low in STOP:
            continue
        if is_sym:
            m = re.fullmatch(r"(.{3,}?)(max|min)", low)
            if m and not low.startswith(("max", "min")):
                out += words(m[1], True) + [m[2]]
                continue
            if len(low) >= 3 and low[0] in "virtf" and not low[1:].isdigit():      # VOUT, IIN, RSNS, tSS, fSW
                rest = low[1:]
                if rest in SYN:
                    out.append(SYN[rest])
                    continue
        out.append(SYN.get(low, low))
    return out


def wordset(text: str, symbolic: bool = False) -> tuple[set[str], str | None]:
    """(distinctive words, bound 'max'|'min'|None) of a name or a phrase."""
    ws = words(text, symbolic)
    b = {BOUND[w] for w in ws if w in BOUND}
    keep = {w for w in ws if w not in BOUND and w not in GENERIC and w not in DIM_WORDS and w not in NEUTRAL
            and w not in STOP and not w.isdigit() and len(w) > 1}
    return keep, (b.pop() if len(b) == 1 else None)


def norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def prop_syms(qual: list[str], dim: str, bound: str | None) -> set[str]:
    """Symbols a property's name suggests: output-voltage-max -> {vout, voutmax, voutput, ...}."""
    L = LETTER.get(dim.split("/")[0])
    if not L or not qual:
        return set()
    firsts = [L + a for a in ABBR.get(qual[0], [qual[0]])]
    out = set(firsts)
    if len(qual) > 1:
        out |= {f + a for f in firsts for a in ABBR.get(qual[1], [qual[1]])}
    if bound:
        out |= {s + bound for s in list(out)}
    return out


# ---------------------------------------------------------------- records

PARTNO_RE = re.compile(r"\b[A-Z]{2,6}\d{3,}[A-Z0-9]*\b")
POINT_RE = re.compile(r"=\s*(?:0x[0-9a-f]+h?|[0-9a-f]+h|[01]+b)(?![0-9a-z])|\breg\s*0x|\[\d+(?::\d+)?\]\s*="
                      r"|\b[a-z_][a-z0-9_]*\s*=\s*[01]{3,}(?![0-9a-z.])|\bcode\b", re.I)
SENSE_IN_TEXT = re.compile(r"(\d+(?:\.\d+)?)\s*-?\s*(m|µ|μ|u)?\s*(?:Ω|[Oo]hms?\b)")
VERIFIED = {"llm-verified", "deterministic", "deterministic+crosschecked", "engineer-declared", "engineer-confirmed",
            "measured"}


def _cite(rec: dict, doc: str) -> str:
    p = rec.get("p")
    pp = "p" + "-".join(str(x) for x in p) if isinstance(p, list) and p else (f"p{p}" if p else "")
    return f"{rec.get('doc') or doc} {pp}".strip()


@dataclass
class Row:
    """One limit/timing record, in SI, with the words its name offers."""
    doc: str
    rec: dict
    id: str
    group: str
    dim: str | None
    lo: float | None
    hi: float | None
    cite: str
    alts: list[tuple[set, str | None, str]] = field(default_factory=list)   # (words, bound, norm sym) per sym alternative
    pwords: set = field(default_factory=set)
    pbound: str | None = None

    @property
    def text(self) -> str:
        return f"{self.rec.get('cond') or ''} {self.rec.get('param') or ''}"

    def range_str(self) -> str:
        return fmt_range(self.lo, self.hi, self.dim or "")


def make_row(doc: str, rec: dict) -> Row:
    u = record_unit(rec.get("unit"))
    dim, sc = u if u else (None, 1.0)
    lo, hi = num(rec.get("min"), "min"), num(rec.get("max"), "max")
    row = Row(doc, rec, str(rec.get("id") or ""), str(rec.get("group") or "other"), dim,
              lo * sc if lo is not None else None, hi * sc if hi is not None else None, _cite(rec, doc))
    sym = str(rec.get("sym") or "")
    for alt in [a for a in re.split(r"[,/]", sym) if a.strip()] or [""]:
        ws, b = wordset(alt, symbolic=True)
        row.alts.append((ws, b, norm(alt)))
    first = re.split(r"[,;(]", str(rec.get("param") or ""))[0]
    row.pwords, row.pbound = wordset(first)
    return row


def sense_values(text: str) -> list[float]:
    """Sense-resistor values a row's condition states: '5-mΩ RAC' -> [0.005]; resistors above 1 Ω are not sense resistors."""
    out = []
    for m in SENSE_IN_TEXT.finditer(text or ""):
        v = float(m[1]) * {"m": 1e-3, "µ": 1e-6, "μ": 1e-6, "u": 1e-6, None: 1.0}[m[2]]
        if v <= 1.0:
            out.append(v)
    return out


# ---------------------------------------------------------------- formulas stated in records

F_DIV = re.compile(r"(?P<lhs>[A-Za-z][A-Za-z0-9_ ()'\-]{0,60}?)\s*=\s*(?P<v>V[A-Za-z0-9_()]*)"
                   r"\s*/\s*(?P<r>R[A-Za-z0-9_()]*)")                                   # I = VSNS / RSNS
F_MUL = re.compile(r"(?P<v>V[A-Za-z0-9_()]*)\s*=\s*(?P<lhs>[A-Za-z][A-Za-z0-9_ ()]{0,40}?)"
                   r"\s*[x×*·]\s*(?P<r>R[A-Za-z0-9_()]*)")                              # VSNS = I x RSNS
_Q = r"(\d+(?:\.\d+)?)\s*(m|µ|μ|u|k|M|n|p)?(V|A|Ω|ohm|s|Hz)(?![A-Za-z])"
B_MAX = [re.compile(r"(?<![A-Za-z])(?i:up\s+to|max(?:imum)?(?:\s+of)?|≤|<=|below)\s*" + _Q),
         re.compile(_Q + r"\s*(?i:max(?:imum)?)\b")]
B_MIN = [re.compile(r"(?<![A-Za-z])(?i:at\s+least|min(?:imum)?(?:\s+of)?|≥|>=)\s*" + _Q),
         re.compile(_Q + r"\s*(?i:min(?:imum)?)\b")]


def is_sense_sym(r: str) -> bool:
    n = norm(r)
    return n.startswith("r") and (n in {"rs", "rsr", "rac", "rcs", "rsh"} or bool(re.search(r"sns|sense|shunt|snse", n)))


def formula_text(rec: dict) -> str:
    """The text of a record that may state a relation: a rule's text, an equation figure, a limit row's param/note."""
    k = rec.get("kind")
    if k == "rule":
        return f"{rec.get('text') or ''}; {rec.get('cond') or ''}"
    if k == "figure":
        return str(rec.get("what") or "")
    if k in ("limit", "timing"):
        return f"{rec.get('param') or ''}; {rec.get('note') or ''}"
    return ""


def stated_bounds(text: str, dim: str) -> tuple[float | None, float | None]:
    """`up to 63.5 mV`, `60 mV max`, `at least 1 V` in a formula record's own text, in SI, for one dimension."""
    def vals(pats):
        out = []
        for pat in pats:
            for m in pat.finditer(text):
                u = record_unit((m[2] or "") + m[3])
                if u and u[0] == dim:
                    out.append(float(m[1]) * u[1])
        return out
    hi, lo = vals(B_MAX), vals(B_MIN)
    return (max(lo) if lo else None), (min(hi) if hi else None)


@dataclass
class Formula:
    doc: str
    rec: dict
    lhs: str
    v: str          # the sense-voltage symbol
    r: str          # the sense-resistor symbol
    snippet: str

    @property
    def id(self) -> str:
        return str(self.rec.get("id") or "")


def formulas(docs, dids: list[str]) -> list[Formula]:
    out = []
    for d in dids:
        for kind in ("rule", "figure", "limit", "timing"):
            for rec in docs.records(d, kind):
                if kind == "figure" and rec.get("src") != "equation":
                    continue
                t = formula_text(rec)
                for pat in (F_DIV, F_MUL):
                    for m in pat.finditer(t):
                        if is_sense_sym(m["r"]):
                            out.append(Formula(d, rec, m["lhs"].strip(), m["v"], m["r"], m[0].strip()))
    return out


# ---------------------------------------------------------------- the property side

SENSE_PROP = re.compile(r"(^|-)(sense|shunt|rsense|r-sense|sns)(-|$)")


@dataclass
class Prop:
    node: str
    label: str
    compat: str
    prop: str
    loc: str | None
    raw: Any
    values: list[float]
    dim: str
    stem: str
    qual: list[str]
    qset: set
    bound: str | None
    syms: set

    @property
    def key(self) -> str:
        return f"{self.label}.{self.prop}"

    def dt_ev(self) -> str:
        si = ", ".join(fmt_si(v, self.dim) for v in self.values[:4]) + (", ..." if len(self.values) > 4 else "")
        return f"DT {self.loc or self.node}: {self.prop} = {self.raw} ({si})"


def make_prop(node: str, label: str, compat: str, name: str, pv: dict) -> Prop | None:
    u = prop_unit(name)
    if not u:
        return None
    stem, dim, sc = u
    val = pv.get("val")
    vals = val if isinstance(val, list) else [val]
    if not vals or not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in vals):
        return None
    ws = words(stem.replace("-", " "))
    b = {BOUND[w] for w in ws if w in BOUND}
    qual = [w for w in ws if w not in BOUND and w not in GENERIC and w not in DIM_WORDS and w not in NEUTRAL
            and w not in STOP and not w.isdigit() and len(w) > 1]
    bound = b.pop() if len(b) == 1 else None
    return Prop(node, label, compat, name, pv.get("loc"), val, [float(x) * sc for x in vals], dim, stem,
                list(dict.fromkeys(qual)), set(qual), bound, prop_syms(list(dict.fromkeys(qual)), dim, bound))


def sense_props(node_props: dict) -> list[dict]:
    out = []
    for name, pv in sorted(node_props.items()):
        u = prop_unit(name)
        val = pv.get("val")
        if u and u[1] == "ohm" and SENSE_PROP.search(u[0]) and isinstance(val, (int, float)) and not isinstance(val, bool):
            out.append({"prop": name, "value": float(val) * u[2], "loc": pv.get("loc"), "words": wordset(u[0])[0]})
    return out


def pick_sense(p: Prop, senses: list[dict]) -> dict | None:
    if not senses:
        return None
    return max(senses, key=lambda s: (len(s["words"] & p.qset), -senses.index(s)))


def score(p: Prop, row: Row) -> tuple[float, str] | None:
    best = None
    for aw, ab, asym in row.alts:
        rw = aw | row.pwords
        rb = {b for b in (ab, row.pbound) if b}
        rbound = rb.pop() if len(rb) == 1 else None
        if p.bound and rbound and p.bound != rbound:
            continue
        ov = p.qset & rw
        bonus, how = 0, ""
        for s in sorted(p.syms):
            if asym and asym == s:
                bonus, how = 2, f"symbol {row.rec.get('sym')} = {s.upper()}"
                break
            if len(s) >= 3 and asym.startswith(s) and bonus < 1:
                bonus, how = 1, f"symbol {s.upper()}... in {row.rec.get('sym')}"
        if not ov and bonus < 2:
            continue
        sc = len(ov) - 0.5 * len(rw - p.qset) + bonus + (0.5 if p.bound and p.bound == rbound else 0)
        why = "; ".join(x for x in (how, f"words {', '.join(sorted(ov))}" if ov else "") if x)
        if best is None or sc > best[0]:
            best = (sc, why)
    return best


# ---------------------------------------------------------------- comparing

def _inside(v: float, lo: float | None, hi: float | None) -> bool:
    eps = 1e-9 * max(abs(v), abs(lo or 0), abs(hi or 0), 1e-12)
    return (lo is None or v >= lo - eps) and (hi is None or v <= hi + eps)


def compare(values: list[float], rows: list[Row], dim: str, mapping: dict[str, str], via: str,
            scaled: dict | None = None) -> list[dict]:
    """Strict groups row by row; EC-like rows of one symbol pooled (inside any passes)."""
    pools: dict[tuple, list[Row]] = {}
    for r in rows:
        key = (r.id,) if r.group in STRICT else (r.doc, r.group, norm(r.rec.get("sym")))
        pools.setdefault(key, []).append(r)
    out = []
    for rs in pools.values():
        bad = [v for v in values if not any(_inside(v, r.lo, r.hi) for r in rs)]
        g = rs[0].group
        c = {"records": [r.id for r in rs], "group": g, "weight": WEIGHT.get(g, "info"),
             "mapping": sorted({mapping.get(r.id, "proposed") for r in rs}),
             "ranges": [r.range_str() for r in rs], "cites": sorted({r.cite for r in rs}),
             "status": "outside" if bad else "inside", "value": bad[0] if bad else None, "via": via,
             "dim": dim, "prov": sorted({r.rec.get("prov") or "llm-unverified" for r in rs})}
        if scaled:
            c["scaled"] = scaled
        out.append(c)
    return out


def row_ev(c: dict, i: int = 0) -> str:
    m = "/".join(c["mapping"])
    pv = [x for x in c["prov"] if x not in VERIFIED]
    return (f"{c['records'][i]} {c['ranges'][i]} ({c['group']}, {m}{', ' + '/'.join(pv) if pv else ''}) {c['cites'][0]}"
            + (f" (+{len(c['records']) - 1} pooled row(s))" if len(c["records"]) > 1 else ""))


# ---------------------------------------------------------------- the analysis

def resolve_nodes(dt: dict, docs, pins: dict, bindings: dict) -> dict[str, dict]:
    """Okay DT node -> {compat, docs, via, part, mpn}; a node with no compatible inherits its parent's part."""
    from embarch_atlas.codecheck import docs_for_node
    identity = {d["node"]: d for d in ((pins.get("i2c") or {}).get("identity") or []) if d.get("status") == "identified"}
    part_docs = pins.get("part_docs") or {}
    nodes = dt.get("nodes") or {}
    out: dict[str, dict] = {}
    for path, n in sorted(nodes.items()):
        if not n.get("okay"):
            continue
        for compat in n.get("compats") or []:
            dids, via = docs_for_node(path, compat, docs, identity, part_docs, bindings)
            dids = [d for d in dids if docs.kind(d) not in NOT_PART_DOCS]
            if dids:
                idr = identity.get(path) or {}
                out[path] = {"compat": compat, "docs": dids, "via": via, "part": idr.get("part"),
                             "mpn": idr.get("mpn") or (bindings.get("compat_parts") or {}).get(compat)}
                break
    for path, n in sorted(nodes.items()):
        if path in out or not n.get("okay") or n.get("compats"):
            continue
        par, hops = n.get("parent"), 0
        while par and hops < 2 and par not in out:
            par, hops = (nodes.get(par) or {}).get("parent"), hops + 1
        if par in out:
            out[path] = {**out[par], "via": f"{out[par]['via']} (parent {par})"}
    return out


def declared_spec(bindings: dict, compats: list[str], prop: str) -> tuple[bool, Any]:
    pl = bindings.get("prop_limits") or {}
    for c in compats:
        if prop in (pl.get(c) or {}):
            return True, pl[c][prop]
    return False, None


def _spec(spec: Any) -> tuple[list[str], str | None, str | None]:
    if isinstance(spec, str):
        return [spec], None, None
    if isinstance(spec, list):
        return [str(x) for x in spec], None, None
    if isinstance(spec, dict):
        ids = spec.get("records") or ([spec["record"]] if spec.get("record") else [])
        return [str(x) for x in ids], spec.get("times"), spec.get("per")
    return [], None, None


_PRODUCT = {("A", "ohm"): "V", ("ohm", "A"): "V"}
_QUOTIENT = {("V", "ohm"): "A", ("V", "A"): "ohm"}


class Analysis:
    def __init__(self, dt: dict, docs, pins: dict, bindings: dict):
        self.dt, self.docs, self.pins, self.bindings = dt, docs, pins, bindings
        self.findings: list[dict] = []
        self.props: list[dict] = []
        self.unresolved: list[dict] = []
        self.folded: dict[str, list[str]] = collections.defaultdict(list)
        self.not_run: list[dict] = []
        self._all_rows: dict[str, Row] | None = None

    def all_rows(self) -> dict[str, Row]:
        if self._all_rows is None:
            self._all_rows = {}
            for kind in ("limit", "timing"):
                for d, r in self.docs.all_records(kind):
                    if r.get("id"):
                        self._all_rows.setdefault(str(r["id"]), make_row(d, r))
        return self._all_rows

    def find(self, cls: str, key: str, summary: str, evidence: list[str], check: str, severity: str, settle: str) -> None:
        self.findings.append({"class": cls, "key": key, "summary": summary, "evidence": [e for e in evidence if e],
                              "check": check, "severity": severity, "settle": settle})

    def run(self) -> None:
        nodes = self.dt.get("nodes") or {}
        res = resolve_nodes(self.dt, self.docs, self.pins, self.bindings)
        rows_by_docs: dict[tuple, list[Row]] = {}
        form_by_docs: dict[tuple, list[Formula]] = {}
        empty_docs = set()
        for path, n in sorted(nodes.items()):
            if not n.get("okay"):
                continue
            label = (n.get("labels") or [path.rsplit("/", 1)[-1]])[0]
            info = res.get(path)
            props = n.get("props") or {}
            if not info:
                for name, pv in sorted(props.items()):
                    if make_prop(path, label, "", name, pv):
                        self.unresolved.append({"node": path, "prop": name, "loc": pv.get("loc")})
                        self.folded[FOLD_NO_PART].append(f"{label}.{name}")
                continue
            dk = tuple(info["docs"])
            if dk not in rows_by_docs:
                seen, rows = set(), []
                for d in info["docs"]:
                    for kind in ("limit", "timing"):
                        for r in self.docs.records(d, kind):
                            rid = str(r.get("id") or "")
                            if rid and rid not in seen:
                                seen.add(rid)
                                rows.append(make_row(d, r))
                rows_by_docs[dk] = rows
                form_by_docs[dk] = formulas(self.docs, info["docs"])
                if not rows:
                    empty_docs |= set(info["docs"])
            senses = sense_props(props)
            compats = list(dict.fromkeys((n.get("compats") or []) + [info["compat"]]))
            for name, pv in sorted(props.items()):
                p = make_prop(path, label, info["compat"], name, pv)
                if p:
                    self.one(p, info, rows_by_docs[dk], form_by_docs[dk], senses, props, compats)
        for d in sorted(empty_docs):
            self.not_run.append({"check": f"DT limits vs datasheet: {d}",
                                 "why": "no limit/timing records yet (translate the datasheet's limit tables)"})
        if not res:
            self.not_run.append({"check": "DT limits vs datasheet",
                                 "why": "no okay DT node resolves to a part with a datasheet in docs/"})

    # -- one property
    def one(self, p: Prop, info: dict, rows: list[Row], forms: list[Formula], senses: list[dict], props: dict,
            compats: list[str]) -> None:
        sense = pick_sense(p, senses) if p.dim == "A" else None
        rec: dict[str, Any] = {"node": p.node, "label": p.label, "compat": p.compat, "prop": p.prop, "loc": p.loc,
                               "raw": p.raw, "dim": p.dim, "si": p.values, "words": sorted(p.qset), "bound": p.bound,
                               "part": info.get("part"), "mpn": info.get("mpn"), "docs": info["docs"],
                               "part_via": info["via"], "sense": sense and {k: v for k, v in sense.items() if k != "words"},
                               "mapping": None, "mapped": [], "skipped": [], "comparisons": [], "formula": None}
        declared, spec = declared_spec(self.bindings, compats, p.prop)
        if declared:
            self.declared(p, rec, spec, rows, forms, props, info)
        else:
            self.proposed(p, rec, rows, forms, sense, info)
        comps = rec["comparisons"]
        if comps:
            rec["status"] = "outside" if any(c["status"] == "outside" for c in comps) else "inside"
        elif rec.get("status") is None:
            rec["status"] = "not-comparable" if rec["mapped"] else "no-limit"
        if rec["status"] == "outside":
            self.report_outside(p, rec, sense)
        elif rec["status"] == "no-limit":
            self.folded[FOLD_NO_LIMIT].append(p.key)
        elif rec["status"] == "not-comparable":
            why = sorted({s["why"].split(" (")[0] for s in rec["skipped"]}) or ["no numeric bound"]
            self.folded[FOLD_NOT_COMPARABLE].append(f"{p.key} ({'; '.join(why)})")
        elif rec["status"] == "declared-none":
            self.folded[FOLD_DECLARED_NONE].append(p.key)
        self.props.append(rec)

    def _skip_reason(self, p: Prop, row: Row, info: dict, sense: dict | None) -> str | None:
        cond = str(row.rec.get("cond") or "")
        toks = PARTNO_RE.findall(cond)
        mpn = norm(info.get("mpn"))
        if toks and mpn and all(not (mpn.startswith(norm(t)) or norm(t).startswith(mpn)) for t in toks):
            return f"variant row ({', '.join(toks)})"
        if p.dim == "A" and sense:
            rs = sense_values(row.text)
            if rs and not any(abs(r - sense["value"]) <= 0.011 * sense["value"] for r in rs):
                return (f"stated at another sense resistor ({fmt_si(rs[0], 'ohm')}; the node has "
                        f"{fmt_si(sense['value'], 'ohm')})")
        if row.group not in STRICT and POINT_RE.search(cond):
            return "EC row at a stated register setting (a point, not a range)"
        if row.lo is None and row.hi is None:
            return "no numeric bound (typ only, or an expression)"
        return None

    def _usable(self, p: Prop, rows: list[Row], rec: dict, info: dict, sense: dict | None,
                filters: bool = True) -> list[Row]:
        out = []
        for r in rows:
            why = self._skip_reason(p, r, info, sense) if filters else (
                "no numeric bound (typ only, or an expression)" if r.lo is None and r.hi is None else None)
            if why:
                rec["skipped"].append({"record": r.id, "why": why, "cite": r.cite})
            else:
                out.append(r)
        return out

    def proposed(self, p: Prop, rec: dict, rows: list[Row], forms: list[Formula], sense: dict | None, info: dict) -> None:
        best: dict[str, list[tuple[float, Row, str]]] = collections.defaultdict(list)
        for r in rows:
            if r.dim != p.dim:
                continue
            s = score(p, r)
            if s and s[0] >= THRESHOLD:
                best[r.group].append((s[0], r, s[1]))
        picked: list[tuple[float, Row, str]] = []
        for g, lst in sorted(best.items()):
            top = max(x[0] for x in lst)
            picked += [x for x in lst if x[0] >= top - 1e-9]
        for sc, r, why in picked:
            rec["mapped"].append({"record": r.id, "group": r.group, "score": sc, "why": why, "mapping": "proposed"})
        usable = self._usable(p, [r for _, r, _ in picked], rec, info, sense)
        if usable:
            rec["comparisons"] += compare(p.values, usable, p.dim, {}, "direct")
        if p.dim == "A" and sense:
            self.scaled(p, rec, rows, forms, sense, info, "proposed")
            if not rec["comparisons"] and not rec.get("formula"):
                self.needs_formula(p, rec, sense, info)
        if rec["mapped"]:
            rec["mapping"] = "proposed"

    def scaled(self, p: Prop, rec: dict, rows: list[Row], forms: list[Formula], sense: dict, info: dict,
               mapping: str, want_v: str | None = None) -> bool:
        """Compare I x R_sense against the V rows a stated relation names; True when a relation was found."""
        for f in forms:
            lw, lb = wordset(f.lhs)
            if want_v is None and (not (lw & p.qset) or (p.bound and lb and p.bound != lb)):
                continue
            if want_v is not None and norm(f.v) != norm(want_v):
                continue
            vals = [v * sense["value"] for v in p.values]
            rec["formula"] = {"record": f.id, "cite": _cite(f.rec, f.doc), "text": f.snippet, "v": f.v, "r": f.r,
                              "prov": f.rec.get("prov") or "llm-unverified"}
            scaled = {"formula": f.id, "text": f.snippet, "cite": _cite(f.rec, f.doc), "sense": sense["prop"],
                      "sense_si": sense["value"], "values_si": vals}
            vrows = [r for r in rows if r.dim == "V" and norm(r.rec.get("sym")) == norm(f.v)]
            for r in vrows:
                rec["mapped"].append({"record": r.id, "group": r.group, "score": None,
                                      "why": f"relation {f.id}: {f.v}", "mapping": mapping})
            usable = self._usable(p, vrows, rec, info, None)
            lo, hi = stated_bounds(formula_text(f.rec), "V")
            if lo is not None or hi is not None:
                usable.append(Row(f.doc, f.rec, f.id, "rule", "V", lo, hi, _cite(f.rec, f.doc)))
                rec["mapped"].append({"record": f.id, "group": "rule", "score": None,
                                      "why": f"bound stated with the relation for {f.v}", "mapping": mapping})
            if usable:
                rec["comparisons"] += compare(vals, usable, "V", {r.id: mapping for r in usable}, "scaled", scaled)
            return True
        return False

    def needs_formula(self, p: Prop, rec: dict, sense: dict, info: dict, declared: str = "") -> None:
        rec["status"] = "needs-formula"
        other = [s for s in rec["skipped"] if s["why"].startswith("stated at another sense")]
        ev = [p.dt_ev(), f"sense: DT {sense['loc'] or p.node}: {sense['prop']} ({fmt_si(sense['value'], 'ohm')})"]
        ev += [f"{s['record']} {s['cite']}: {s['why']}" for s in other[:2]]
        ev.append(f"searched: rule/equation records of {', '.join(info['docs'])} for `<{p.stem}> = V<x> / R<sense>`")
        summ = (f"{p.key} = {', '.join(fmt_si(v, p.dim) for v in p.values[:3])} on a node that sets its current through "
                f"{sense['prop']} = {fmt_si(sense['value'], 'ohm')}{declared}: no record states the relation, so it is "
                "not compared")
        if other:
            summ += f"; {len(other)} current row(s) are stated at another sense resistor"
        self.find("dt-limit-needs-formula", p.key, summ, ev, CHECK_SENSE, "info", SETTLE["dt-limit-needs-formula"])

    def declared(self, p: Prop, rec: dict, spec: Any, rows: list[Row], forms: list[Formula], props: dict,
                 info: dict) -> None:
        rec["mapping"] = "declared"
        if spec is None:
            rec["status"] = "declared-none"
            return
        ids, times, per = _spec(spec)
        local = {r.id: r for r in rows}
        for rid in ids:
            r = local.get(rid) or self.all_rows().get(rid)
            if r is None:
                if not rec["mapped"]:
                    rec["status"] = "declared-unknown"
                self.find("dt-limit-declared-unknown", p.key,
                          f"boards/bindings.json prop_limits maps {p.compat} {p.prop} to {rid}, which no doc's records have",
                          [p.dt_ev(), f"boards/bindings.json prop_limits.{p.compat}.{p.prop}"], CHECK, "warn",
                          SETTLE["dt-limit-declared-unknown"])
                continue
            rec["mapped"].append({"record": r.id, "group": r.group, "score": None, "why": "boards/bindings.json",
                                  "mapping": "declared"})
            if rec.get("status") == "declared-unknown":
                rec["status"] = None
            other_name = times or per
            if other_name:
                opv = props.get(other_name) or {}
                ou = prop_unit(other_name)
                ov = opv.get("val")
                if not ou or not isinstance(ov, (int, float)) or isinstance(ov, bool):
                    rec["skipped"].append({"record": r.id, "why": f"declared scaling by {other_name}: not a unit property "
                                           "of this node", "cite": r.cite})
                    continue
                osi = float(ov) * ou[2]
                dim = (_PRODUCT if times else _QUOTIENT).get((p.dim, ou[1]))
                if dim != r.dim:
                    rec["skipped"].append({"record": r.id, "why": f"declared scaling gives {dim or '?'}, the row is "
                                           f"{r.dim or '?'}", "cite": r.cite})
                    continue
                vals = [v * osi if times else v / osi for v in p.values]
                scaled = {"formula": f"declared: {'times' if times else 'per'} {other_name}", "text": "",
                          "cite": "boards/bindings.json", "sense": other_name, "sense_si": osi, "values_si": vals}
                usable = self._usable(p, [r], rec, info, None, filters=False)
                rec["comparisons"] += compare(vals, usable, r.dim, {r.id: "declared"}, "scaled", scaled)
            elif r.dim == p.dim:
                usable = self._usable(p, [r], rec, info, None, filters=False)
                rec["comparisons"] += compare(p.values, usable, p.dim, {r.id: "declared"}, "direct")
            else:
                sense = pick_sense(p, sense_props(props)) if p.dim == "A" and r.dim == "V" else None
                if sense and self.scaled(p, rec, [r], forms, sense, info, "declared", want_v=str(r.rec.get("sym") or "")):
                    continue
                if sense:
                    self.needs_formula(p, rec, sense, info, f" (declared: {r.id}, a {r.dim} row)")
                else:
                    rec["skipped"].append({"record": r.id, "why": f"declared row is {r.dim or 'unitless'}, the property "
                                           f"is {p.dim}; no scaling declared", "cite": r.cite})

    def report_outside(self, p: Prop, rec: dict, sense: dict | None) -> None:
        bad = sorted((c for c in rec["comparisons"] if c["status"] == "outside"), key=lambda c: SEV_RANK[c["weight"]])
        worst = bad[0]
        cls = CLASS_OF[worst["weight"]]
        v = worst["value"]
        dim = worst["dim"]
        what = f"{fmt_si(v, dim)}"
        if worst.get("scaled"):
            sc = worst["scaled"]
            i = sc["values_si"].index(v) if v in sc["values_si"] else 0
            what = (f"{fmt_si(p.values[i], p.dim)} x {sc['sense']} {fmt_si(sc['sense_si'], 'ohm')} = {fmt_si(v, dim)}")
        summ = (f"{p.key} = {what} is outside {worst['records'][0]} {worst['ranges'][0]} ({worst['group']}, "
                f"{'/'.join(worst['mapping'])})")
        if len(bad) > 1:
            summ += f"; also outside {len(bad) - 1} more row(s)"
        ev = [p.dt_ev()] + [row_ev(c) for c in bad[:2]]
        if worst.get("scaled"):
            sc = worst["scaled"]
            ev.append(f"relation {sc['formula']} ({sc['cite']})" + (f": \"{sc['text']}\"" if sc["text"] else ""))
            if sense:
                ev.append(f"sense: DT {sense['loc'] or p.node}: {sense['prop']} ({fmt_si(sense['value'], 'ohm')})")
        settle = SETTLE[cls]
        if "proposed" in worst["mapping"]:
            settle += "; the mapping is proposed (by name): confirm or correct it in boards/bindings.json prop_limits"
        self.find(cls, p.key, summ, ev, CHECK, worst["weight"], settle)

    # -- the summary
    def report(self, stamp: dict) -> dict:
        P = self.props
        cnt = collections.Counter(x["status"] for x in P)
        compared = [x for x in P if x["comparisons"]]
        inside = [x for x in compared if x["status"] == "inside"]
        mapped = [x for x in P if x["mapped"] or x["status"] == "declared-none"]
        declared = [x for x in mapped if x["mapping"] == "declared"]
        sev = collections.Counter(f["severity"] for f in self.findings if f["class"] in CLASS_OF.values())
        sense_cur = [x for x in P if x["dim"] == "A" and x["sense"] and x["mapping"] != "declared"]
        sense_ok = [x for x in sense_cur if x["status"] != "needs-formula"]
        nodes = {x["node"] for x in P}
        un_nodes = {x["node"] for x in self.unresolved}
        counts = {"properties": len(P), "nodes": len(nodes), "unresolved_properties": len(self.unresolved),
                  "unresolved_nodes": len(un_nodes), "mapped": len(mapped), "declared": len(declared),
                  "proposed": len(mapped) - len(declared), "compared": len(compared), "inside": len(inside),
                  "outside": cnt["outside"], "needs_formula": cnt["needs-formula"],
                  "not_comparable": cnt["not-comparable"], "no_limit": cnt["no-limit"],
                  "declared_none": cnt["declared-none"], "severity": dict(sev),
                  "sense_currents": len(sense_cur), "sense_currents_related": len(sense_ok)}
        summary = (f"{len(P)} unit properties on {len(nodes)} node(s) resolved to a part"
                   f" ({len(self.unresolved)} more on {len(un_nodes)} node(s) with no part datasheet); "
                   f"{len(mapped)} mapped ({len(declared)} declared, {len(mapped) - len(declared)} proposed); "
                   f"{len(compared)} compared, {len(inside)} inside; {cnt['outside']} outside"
                   + (f" ({', '.join(f'{v} {k}' for k, v in sorted(sev.items(), key=lambda x: SEV_RANK[x[0]]))})"
                      if sev else "")
                   + f"; {cnt['needs-formula']} need a declared formula; {cnt['not-comparable']} mapped but not "
                   f"comparable; {cnt['no-limit']} with no datasheet limit")
        checks = [{"check": CHECK, "sources": SOURCES, "ran": len(compared), "passed": len(inside),
                   "candidates": sum(1 for f in self.findings if f["check"] == CHECK)},
                  {"check": CHECK_SENSE, "sources": SOURCES_SENSE, "ran": len(sense_cur), "passed": len(sense_ok),
                   "candidates": sum(1 for f in self.findings if f["check"] == CHECK_SENSE)}]
        rank = {c: i for i, c in enumerate(ORDER)}
        self.findings.sort(key=lambda f: (rank.get(f["class"], len(ORDER)), f["key"], f["summary"]))
        lines = [summary + ". Details: limits.json.", ""]
        if inside:
            lines.append("Inside (compared; every applicable row passed):")
            for x in inside[:30]:
                bits = []
                for c in x["comparisons"][:3]:
                    tag = ""
                    if c.get("scaled"):
                        sc = c["scaled"]
                        tag = (f"x {sc['sense']} {fmt_si(sc['sense_si'], 'ohm')} = "
                               f"{', '.join(fmt_si(v, c['dim']) for v in sc['values_si'][:3])} vs ")
                    bits.append(f"{tag}{c['records'][0]} {c['ranges'][0]} ({c['group']}, {'/'.join(c['mapping'])})")
                more = f" +{len(x['comparisons']) - 3}" if len(x["comparisons"]) > 3 else ""
                val = ", ".join(fmt_si(v, x["dim"]) for v in x["si"][:3])
                lines.append(f"- {x['label']}.{x['prop']} = {val}: " + "; ".join(bits) + more)
            if len(inside) > 30:
                lines.append(f"- ... {len(inside) - 30} more in limits.json")
        section = {"title": "DT limits vs datasheet", "lines": lines}
        return {"atlas": stamp.get("id"), "summary": summary, "counts": counts, "properties": P,
                "unresolved": self.unresolved, "findings": self.findings, "checks": checks, "not_run": self.not_run,
                "folded": {k: sorted(set(v)) for k, v in sorted(self.folded.items())}, "section": section}


def analyze(dt: dict, docs, pins: dict, bindings: dict, stamp: dict) -> dict:
    a = Analysis(dt, docs, pins, bindings)
    a.run()
    return a.report(stamp)


def run_join(J, stamp: dict, pins: dict) -> dict:
    """The analysis for `join`: reuses the join's DT, docs and bindings; writes limits.json."""
    rep = analyze(J.dt, J.docs, pins, J.bindings, stamp)
    write_json(J.atlas / "limits.json", rep)
    return rep


def run(ad: Path, atlas: Path, log=print) -> dict:
    """Standalone: read the atlas's dt.json/pins.json and boards/bindings.json, write limits.json."""
    from embarch_atlas.join import Docs
    stamp = read_json(atlas / "atlas.json")
    if not (atlas / "dt.json").is_file():
        raise SystemExit(f"{atlas}: no dt.json (run `join` first)")
    dt = read_json(atlas / "dt.json")
    pins = read_json(atlas / "pins.json") if (atlas / "pins.json").is_file() else {}
    bp = ad / "boards" / "bindings.json"
    bindings = read_json(bp) if bp.is_file() else {}
    rep = analyze(dt, Docs(ad), pins, bindings, stamp)
    write_json(atlas / "limits.json", rep)
    log(f"{stamp.get('id')}: {rep['summary']}")
    return rep

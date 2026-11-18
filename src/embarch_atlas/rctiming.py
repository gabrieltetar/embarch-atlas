"""`q rc_timing`: the worst-case time for one board node to cross the receiving gate's input threshold.

The handled network is one series R and one C on the node and a receiving pin:

- **charging**: the R's far end is a supply rail, the C's far end is ground; the node starts at 0 V and must reach
  the receiving pin's guaranteed VIH: `t = tau * ln(V / (V - Vth))`.
- **discharging**: the R's far end is ground, the C's far end is ground; the node starts at the rail (`rail=` names
  it) and must fall to the guaranteed VIL: `t = tau * ln(V / Vth)`.

Every input is read from a record or a BOM line and cited in the answer, or named as **assumed** with why (a
tolerance the caller passed, a capacitor's temperature characteristic from its EIA code). The corners are the
product of R and C extremes and both ends of the rail's range; the minimum and maximum of the four are reported.
Anything outside that network, and any missing tolerance, regulator fact or threshold, is a named gap: nothing is
approximated and no typical value is substituted.
"""
from __future__ import annotations

import math
import re
from typing import Any, Callable, Iterable

from . import bomvals

_NUM = r"(\d+(?:\.\d+)?)"
_GROUND = re.compile(r"^(?:[ADPS]?GND\w*|VSS\w*|0V)$", re.I)
_R_UNITS = {"": 1.0, "R": 1.0, "K": 1e3, "M": 1e6, "G": 1e9}
_C_UNITS = {"": 1.0, "P": 1e-12, "N": 1e-9, "U": 1e-6, "µ": 1e-6, "M": 1e-3}
# EIA temperature characteristic -> (negative, positive) fractional change of capacitance over the code's range.
# A table of the code's published class limits, not a datasheet fact: the answer calls it assumed.
EIA_TC = {"C0G": (0.0054, 0.0054), "NP0": (0.0054, 0.0054), "X7R": (0.15, 0.15), "X5R": (0.15, 0.15),
          "X7S": (0.22, 0.22), "X6S": (0.22, 0.22), "X7T": (0.33, 0.22), "Y5V": (0.82, 0.22), "Z5U": (0.56, 0.22)}
_TC_RE = re.compile(r"(?<![A-Z0-9])(" + "|".join(EIA_TC) + r")(?![A-Z0-9])", re.I)
_FRAC = re.compile(r"^(\d*\.?\d+)\s*[*x×]?\s*V(?:DD|CC|DDIO)\w*$", re.I)


def parse_resistance(text: Any) -> float | None:
    """`10k`, `4k7`, `86.6K OHM`, `0R1` or `100` as ohms; None when it is not a plain value."""
    t = re.sub(r"\s*(?:ohms?|Ω)\s*$", "", str(text or "").strip(), flags=re.I)
    m = re.fullmatch(_NUM + r"\s*([kKmMgGrR]?)", t)
    if m:
        return float(m.group(1)) * _R_UNITS[m.group(2).upper()]
    m = re.fullmatch(r"(\d+)([kKmMgGrR])(\d+)", t)
    if m:
        return float(f"{m.group(1)}.{m.group(3)}") * _R_UNITS[m.group(2).upper()]
    return None


def parse_capacitance(text: Any) -> float | None:
    """`100nF`, `0.1uF`, `4.7 µF` or `10p` as farads; None when it is not a plain value."""
    m = re.fullmatch(_NUM + r"\s*([pnuµmPNUM]?)\s*F?", str(text or "").strip(), flags=re.I)
    if not m:
        return None
    return float(m.group(1)) * _C_UNITS[m.group(2).upper() if m.group(2) != "µ" else "µ"]


def tolerance_fraction(tol: str | None, nominal: float, unit_scale: Callable[[str], float | None]) -> float | None:
    """`1%` as 0.01; an absolute tolerance (`0.25pF`) as its share of the nominal; None when unreadable."""
    if not tol:
        return None
    m = re.fullmatch(r"[±]?\s*" + _NUM + r"\s*%", tol.strip())
    if m:
        return float(m.group(1)) / 100
    m = re.fullmatch(r"[±]?\s*" + _NUM + r"\s*([pnuµm]?F)", tol.strip())
    if m and nominal:
        a = unit_scale(m.group(2))
        return None if a is None else float(m.group(1)) * a / nominal
    return None


def _cap_scale(unit: str) -> float | None:
    return _C_UNITS.get(unit[:-1].upper() if unit[:-1] != "µ" else "µ")


def fmt_time(t: float) -> str:
    """Seconds with an engineering prefix and four significant figures."""
    if t == 0:
        return "0 s"
    for scale, unit in ((1.0, "s"), (1e-3, "ms"), (1e-6, "us"), (1e-9, "ns")):
        if abs(t) >= scale or unit == "ns":
            v = t / scale
            return f"{v:.4g} {unit}"
    return f"{t:.4g} s"


def _fmt_v(v: float) -> str:
    return f"{v:.4g} V"


def _num(x: Any) -> float | None:
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        try:
            return float(x)
        except ValueError:
            return None
    return None


def _pin_of(entry: str) -> tuple[str, str]:
    ref, _, pin = entry.rpartition("-")
    return ref, pin


def _norm(s: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())


def _cond_volts(cond: Any) -> list[float]:
    return [float(x) for x in re.findall(_NUM + r"\s*V(?![A-Za-z])", str(cond or ""))]


def _covers(cond: Any, v: float) -> bool | None:
    """Whether a record's condition names a supply that includes `v`; None when it names none."""
    vs = _cond_volts(cond)
    if not vs:
        return None
    lo, hi = min(vs), max(vs)
    return lo - 0.05 * lo <= v <= hi + 0.05 * hi if lo == hi else lo <= v <= hi


class Gap(Exception):
    """A named reason the answer cannot be computed; `parts` names what was found."""


def _pin_doc_records(a: Any, doc_ids: Iterable[str]) -> list[tuple[Any, dict]]:
    out = []
    for d in a.docs:
        if d.name in doc_ids:
            out += [(d, r) for r in a_records(d)]
    return out


def a_records(d: Any) -> Iterable[dict]:
    from .query import _records
    return _records(d)


def _pp(r: dict) -> str:
    from .query import _pp as pp
    return pp(r)


def _pin_matches(r: dict, pin: str, name: str | None) -> bool:
    pv = r.get("pin")
    pins = [str(x) for x in (pv if isinstance(pv, list) else [pv])]
    return pin in pins or (bool(name) and _norm(r.get("name")) == _norm(name))


def _regulators(a: Any, board: str, comps: dict, rail: str) -> list[tuple[str, str, Any, list[dict]]]:
    """(ref, pin, doc, records) of each part whose datasheet says the pin on `rail` sources power."""
    out = []
    docs = a.part_docs()
    for ref, c in sorted(comps.items()):
        for num, p in (c.get("pins") or {}).items():
            if p.get("net") != rail:
                continue
            ids = docs.get(f"{board}:{ref}") or []
            for d in a.docs:
                if d.name not in ids:
                    continue
                recs = list(a_records(d))
                if any(r.get("kind") == "pin" and r.get("flow") == "source" and _pin_matches(r, num, p.get("name"))
                       for r in recs):
                    out.append((ref, num, d, recs))
    return out


def _rail_range(regs: list, rail: str) -> tuple[float, float, float, list[str], list[str]]:
    """(vmin, vmax, nominal, citations, notes) for the rail from its regulator's datasheet records."""
    if not regs:
        raise Gap(f"no part on {rail} is a source in its datasheet (a `pin` record with flow=source), so the rail's accuracy is "
                  f"not known: translate the regulator's datasheet, or `trace` the net with `net_trace`")
    if len(regs) > 1:
        raise Gap(f"{rail} has several sources ({', '.join(f'{r[0]}-{r[1]}' for r in regs)}); none is picked")
    ref, pin, d, recs = regs[0]
    lims = [r for r in recs if r.get("kind") == "limit"]
    vout = [r for r in lims if re.fullmatch(r"V_?(?:OUT|O)\d*", str(r.get("sym") or ""), re.I)
            or "output voltage" in str(r.get("param") or "").lower()]
    acc = [r for r in lims if re.search(r"accura|toler|regulation", f"{r.get('sym')} {r.get('param')}", re.I)
           and "%" in str(r.get("unit") or "")]
    if len(vout) > 1:
        raise Gap(f"{ref}'s datasheet has several output-voltage records ({', '.join(str(r.get('id')) for r in vout)}); "
                  f"none is picked")
    if not vout:
        raise Gap(f"{ref}'s datasheet ({d.name}) has no output-voltage `limit` record (sym VOUT)")
    r = vout[0]
    lo, hi = _num(r.get("min")), _num(r.get("max"))
    nom = _num(r.get("nom")) if _num(r.get("nom")) is not None else _num(r.get("typ"))
    cite = f"{d.name} limit {r.get('id')} {_pp(r)}".strip()
    if lo is not None and hi is not None:
        return lo, hi, nom if nom is not None else (lo + hi) / 2, [cite], []
    if nom is not None and acc:
        ar = acc[0]
        pct = next((v for v in (_num(ar.get("max")), _num(ar.get("typ")), _num(ar.get("nom"))) if v is not None), None)
        if pct is not None:
            return (nom * (1 - abs(pct) / 100), nom * (1 + abs(pct) / 100), nom,
                    [cite, f"{d.name} limit {ar.get('id')} {_pp(ar)}".strip()], [])
    raise Gap(f"{ref}'s output-voltage record {r.get('id')} gives no min and max, and no accuracy record (% unit) to apply to "
              f"its nominal")


def _threshold(a: Any, board: str, gate: str, gate_pin: str, gate_name: str | None, which: str,
               nominal: float) -> tuple[tuple[float, bool], str]:
    """((value, is_fraction_of_supply), citation) of the guaranteed VIH (`min`) or VIL (`max`) at this supply."""
    ids = a.part_docs().get(f"{board}:{gate}") or []
    if not ids:
        raise Gap(f"{gate} has no datasheet joined to it, so its guaranteed {which} is not known")
    cands = []
    for d, r in _pin_doc_records(a, ids):
        if r.get("kind") == "limit" and re.match(which, str(r.get("sym") or ""), re.I):
            cands.append((d, r))
    if not cands:
        raise Gap(f"{gate}'s datasheet ({', '.join(ids)}) has no `limit` record for {which}")
    pinned = [c for c in cands if gate_name and _norm(gate_name) in _norm(f"{c[1].get('sym')} {c[1].get('param')} {c[1].get('cond')}")]
    cands = pinned or cands
    covering = [c for c in cands if _covers(c[1].get("cond"), nominal) is True]
    cands = covering or [c for c in cands if _covers(c[1].get("cond"), nominal) is None] or cands
    if len(cands) > 1:
        raise Gap(f"{gate}'s datasheet has several {which} records ({', '.join(str(c[1].get('id')) for c in cands)}) "
                  f"and none is picked for {_fmt_v(nominal)}")
    d, r = cands[0]
    field = "min" if which == "VIH" else "max"
    raw = r.get(field)
    cite = f"{d.name} limit {r.get('id')} {_pp(r)}".strip()
    v = _num(raw)
    if v is not None:
        return (v, False), cite
    m = _FRAC.match(str(raw or "").strip())
    if m:
        return (float(m.group(1)), True), cite
    raise Gap(f"{gate}'s {which} record {r.get('id')} has no `{field}` value I can read (got {raw!r}; only a number of volts "
              f"or a fraction of the supply such as `0.7*VDD`)")


def times(r_nom: float, r_tol: float, c_lo: float, c_hi: float, vlo: float, vhi: float, thr: tuple[float, bool],
          charging: bool) -> tuple[float, float, list[str]]:
    """(t_min, t_max, notes) over the R extremes, the C extremes and both ends of the supply range."""
    taus = [r_nom * (1 - r_tol) * c_lo, r_nom * (1 + r_tol) * c_hi]
    out, notes = [], []
    for v in (vlo, vhi):
        vth = thr[0] * v if thr[1] else thr[0]
        if charging and vth >= v:
            raise Gap(f"at {_fmt_v(v)} the guaranteed VIH ({_fmt_v(vth)}) is not below the supply: the node never crosses it")
        if not charging and vth >= v:
            notes.append(f"at {_fmt_v(v)} the node already starts below the guaranteed VIL ({_fmt_v(vth)}): t = 0")
            out += [0.0, 0.0]
            continue
        k = math.log(v / (v - vth)) if charging else math.log(v / vth)
        out += [t * k for t in taus]
    return min(out), max(out), notes


def _comp_line(board: str, ref: str, comps: dict, bom: dict) -> str:
    c, e = comps.get(ref) or {}, bom.get(ref) or {}
    return f"{board}:{ref} value={bomvals._clean(e.get('value') or c.get('value')) or bomvals.UNKNOWN}"


def compute(a: Any, node: str, margin: float | None = None, rail: str | None = None, gate: str | None = None,
            r_tol: float | None = None, c_tol: float | None = None) -> tuple[str, str]:
    """(src, body) for `rc_timing`; src is empty when the board or node is not found."""
    from .query import _json
    boards = sorted(p.parent.name for p in (a.dir / "boards").glob("*/netlist.json"))
    board, _, net = node.partition(":")
    if not net:
        if len(boards) != 1:
            return "", f"give the node as <board>:<net>. Boards: {', '.join(boards) or 'none'}."
        board, net = boards[0], node
    if board not in boards:
        return "", f"board {board!r} not found. Boards: {', '.join(boards) or 'none'}."
    bdir = a.dir / "boards" / board
    nl = _json(bdir / "netlist.json", {}) or {}
    comps = nl.get("components") or {}
    bom = {e["ref"]: e for e in (_json(bdir / "bom.json", []) or []) if isinstance(e, dict) and e.get("ref")}
    if net not in (nl.get("nets") or {}):
        return "", f"net {net!r} not found on {board}."
    src = f"boards/{board} netlist.json + bom.json"
    pins = (nl["nets"][net] or {}).get("pins") or []

    def not_handled(why: str, found: str = "") -> tuple[str, str]:
        return src, (f"node={board}:{net}\n\nNot handled: {why}.\n" + (f"\nOn the node: {found}\n" if found else "")
                     + "\nNothing was approximated; no typical value was substituted.\n")

    def dni(ref: str) -> bool:
        e, c = bom.get(ref) or {}, comps.get(ref) or {}
        return bool(e.get("dni") if "dni" in e else c.get("dni"))

    rs, cs, others, ignored = [], [], [], []
    for entry in pins:
        ref, pin = _pin_of(entry)
        e = bom.get(ref) or {}
        c = comps.get(ref) or {}
        if dni(ref):
            ignored.append(ref)
            continue
        val = e.get("value") or c.get("value")
        if re.match(r"R\d", ref, re.I) or str(e.get("description") or "").upper().startswith("RES"):
            rs.append((ref, pin, val))
        elif re.match(r"C\d", ref, re.I) or str(e.get("description") or "").upper().startswith("CAP"):
            cs.append((ref, pin, val))
        else:
            others.append((ref, pin, c.get("mpn") or e.get("mpn")))
    found = ", ".join(sorted({f"{board}:{r} ({_comp_line(board, r, comps, bom).split(' ', 1)[1]})" for r, _, _ in rs + cs}
                             | {f"{board}:{r} ({m or 'no mpn'})" for r, _, m in others}))
    found += (f"; DNI, not counted: {', '.join(ignored)}" if ignored else "")
    if gate:
        want_ref, want_pin = _pin_of(gate if "-" in gate else gate + "-")
        g = [o for o in others if o[0] == want_ref and (not want_pin or o[1] == want_pin)]
        if not g:
            return not_handled(f"{gate} is not a pin of a non-R/C part on {net}", found)
        others = g
    if len(rs) != 1 or len(cs) != 1:
        return not_handled(f"the node has {len(rs)} resistor(s) and {len(cs)} capacitor(s); only one series R and one C is handled",
                           found)
    if len(others) != 1:
        return not_handled(("no receiving pin on the node" if not others else
                            f"{len(others)} candidate receiving pins ({', '.join(f'{r}-{p}' for r, p, _ in others)}): pass `gate=<ref>-<pin>`"),
                           found)
    (rref, rpin, rval), (cref, cpin, cval), (gref, gpin, gmpn) = rs[0], cs[0], others[0]

    def far(ref: str, pin: str) -> str | None:
        pp = ((comps.get(ref) or {}).get("pins") or {})
        other = [p for n, p in pp.items() if n != pin]
        return other[0].get("net") if len(pp) == 2 and len(other) == 1 else None

    rnet, cnet = far(rref, rpin), far(cref, cpin)
    if rnet is None or cnet is None:
        return not_handled(f"{rref} or {cref} is not a two-pin part with a net on its far end", found)
    if not _GROUND.match(cnet):
        return not_handled(f"{cref} goes to {cnet}, not ground: a capacitor to a rail is not handled", found)
    charging = not _GROUND.match(rnet)
    if charging:
        rail_net = rnet
    else:
        rail_net = rail
        if not rail_net:
            return not_handled(f"{rref} goes to ground, so the node discharges from a supply the netlist does not name here: "
                               f"pass `rail=<net>`", found)
        if rail_net not in (nl.get("nets") or {}):
            return not_handled(f"rail {rail_net!r} is not a net on {board}", found)
    if charging and rail and rail != rnet:
        return not_handled(f"rail={rail} given but {rref} goes to {rnet}", found)

    # ---- inputs
    r_nom, c_nom = parse_resistance(rval), parse_capacitance(cval)
    if r_nom is None or c_nom is None:
        return not_handled(f"the value of {rref if r_nom is None else cref} ({(rval if r_nom is None else cval) or 'none'}) "
                           f"is not a plain number", found)
    lines, cites, assumed, gaps = [], [src], [], []

    def tol_for(ref: str, nominal: float, given: float | None, scale: Callable[[str], float | None]) -> tuple[float | None, str]:
        e = bom.get(ref) or {}
        f = bomvals._of(e)
        if given is not None:
            assumed.append(f"{ref} tolerance {given:g}% given by the caller")
            return given / 100, f"{given:g}% (ASSUMED: given by the caller)"
        t = f["tolerance"]
        frac = tolerance_fraction(t, nominal, scale)
        if frac is None:
            gaps.append(f"{ref} has no tolerance in bom.json ({'unreadable: ' + t if t else 'neither a schematic parameter nor its description carries one'}); "
                        f"pass it as r_tol or c_tol (percent) to compute with an assumed value")
            return None, "unknown"
        return frac, f"{t} (bom.json tolerance)"

    rt, rtxt = tol_for(rref, r_nom, r_tol, lambda u: None)
    ct, ctxt = tol_for(cref, c_nom, c_tol, _cap_scale)
    desc = " ".join(str((bom.get(cref) or {}).get(k) or "") for k in ("description", "comment")) + " " + str(cval or "")
    tcm = _TC_RE.search(desc)
    tc_lo, tc_hi = EIA_TC[tcm.group(1).upper()] if tcm else (0.0, 0.0)
    if tcm:
        assumed.append(f"{cref} temperature characteristic {tcm.group(1).upper()}: -{tc_lo * 100:g}% / +{tc_hi * 100:g}% from the EIA class table")

    try:
        regs = _regulators(a, board, comps, rail_net)
        vlo, vhi, vnom, rcites, _ = _rail_range(regs, rail_net)
    except Gap as g:
        gaps.append(str(g))
        vlo = vhi = vnom = None
        rcites = []
    thr = None
    if vnom is not None:
        try:
            gname = ((comps.get(gref) or {}).get("pins") or {}).get(gpin, {}).get("name")
            thr, tcite = _threshold(a, board, gref, gpin, gname, "VIH" if charging else "VIL", vnom)
            cites.append(tcite)
        except Gap as g:
            gaps.append(str(g))
    else:
        gaps.append(f"{gref}'s guaranteed {'VIH' if charging else 'VIL'} is looked up at the rail's nominal, which is not known")
    if gaps:
        return src, (f"node={board}:{net}\n\nNot computed: {len(gaps)} input(s) missing.\n"
                     + "\n".join(f"- {g}" for g in gaps) + f"\n\nFound: {rref} {rval}, {cref} {cval}, receiving pin {gref}-{gpin} "
                     f"({gmpn or 'no mpn'}), rail {rail_net}.\nNo value is substituted; nothing is computed from what is known.\n")
    cites += rcites
    c_lo, c_hi = c_nom * (1 - ct - tc_lo), c_nom * (1 + ct + tc_hi)
    tmin, tmax, notes = times(r_nom, rt, c_lo, c_hi, vlo, vhi, thr, charging)
    vthtxt = (f"{thr[0]:g} x supply" if thr[1] else _fmt_v(thr[0]))
    form = ("t = tau * ln(V / (V - Vth)), tau = R * C" if charging else "t = tau * ln(V / Vth), tau = R * C")
    which = "VIH" if charging else "VIL"
    out = [f"node={board}:{net}  {'charging from 0 V to' if charging else 'discharging from the rail to'} the guaranteed {which} "
           f"at {gref}-{gpin}", ""]
    out += [f"result (derived): t = {fmt_time(tmin)} .. {fmt_time(tmax)}", f"formula: {form}; corners = R and C extremes x both ends of the rail range, "
            f"min and max of the four"]
    if margin is not None:
        out.append(f"margin {margin:g}% passed by the caller (not part of the result above): "
                   f"{fmt_time(tmin * (1 - margin / 100))} .. {fmt_time(tmax * (1 + margin / 100))}")
    out += ["", "inputs:",
            f"- R {board}:{rref} value={rval} = {r_nom:.6g} ohm, tol {rtxt} -> {r_nom * (1 - rt):.6g} .. {r_nom * (1 + rt):.6g} ohm; far end {rnet}",
            f"- C {board}:{cref} value={cval} = {c_nom:.6g} F, tol {ctxt}"
            + (f", temperature {tcm.group(1).upper()} (ASSUMED, see below)" if tcm else ", no temperature characteristic in its description: none applied")
            + f" -> {c_lo:.6g} .. {c_hi:.6g} F; far end {cnet}",
            f"- rail {rail_net}: {_fmt_v(vlo)} .. {_fmt_v(vhi)} (nominal {_fmt_v(vnom)}) from {'; '.join(rcites)}",
            f"- threshold: guaranteed {which} {'min' if charging else 'max'} = {vthtxt} from {cites[1]}"]
    if assumed:
        out += ["", "assumed:"] + [f"- {x}" for x in assumed]
    out += ["", "not modelled: resistor temperature coefficient, capacitor DC-bias derating and ageing, leakage and the gate's input "
                "current; the gate may switch earlier than its guaranteed level, so this is the time to the guaranteed one."]
    out += notes
    return "; ".join(dict.fromkeys(cites)), "\n".join(out) + "\n"

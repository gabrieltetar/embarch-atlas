"""`derive strain-budget <file.toml|json>`: a strain or load sensor's signal budget from loads and geometry.

A standalone helper outside the atlas build: it reads no atlas, no document and no netlist, only the
input file. Every input carries its SI-convertible unit as text (`"10 mm"`, `"200 GPa"`); a number with
no unit is refused, except the dimensionless gauge factor. Every output row is tagged `(derived)` and names
its formula, the textbook one, so a report cites this table and not ad hoc arithmetic.

Beam model: a cantilever loaded at its tip by `force` at `lever_arm` (moment M = F*L), or a pure `moment`.
Strains are at the outer surface unless the row says inner wall. Not modelled (refused or named):
torque on a rectangular flexure (no closed form that is textbook-short), stress concentration, shear
deflection, Poisson-ratio bridge gains, gauge-factor nonlinearity, temperature, bridge imbalance.
"""
from __future__ import annotations

import json
import math
import re
import sys
import tomllib
from pathlib import Path

SECTIONS = ("solid_round", "round_tube", "rectangular")
BRIDGES = {  # active-arm gain k in Vout/Vex = k * GF * strain
    "quarter": (0.25, "1 active gauge"),
    "half": (0.5, "2 active gauges, equal and opposite strain (bending, top and bottom)"),
    "full": (1.0, "4 active gauges, pairs of equal and opposite strain"),
}

_LEN = {"m": 1.0, "cm": 1e-2, "mm": 1e-3, "um": 1e-6, "in": 0.0254}
_FORCE = {"N": 1.0, "kN": 1e3, "mN": 1e-3, "lbf": 4.4482216152605}
_MOMENT = {"N*m": 1.0, "N*mm": 1e-3, "N*cm": 1e-2, "kN*m": 1e3, "mN*m": 1e-3}
_PRESS = {"Pa": 1.0, "kPa": 1e3, "MPa": 1e6, "GPa": 1e9, "psi": 6894.757293168}
_VOLT = {"V": 1.0, "mV": 1e-3, "uV": 1e-6, "nV": 1e-9}
_FREQ = {"Hz": 1.0, "kHz": 1e3, "MHz": 1e6}
_DENS = {"V/sqrt(Hz)": 1.0, "mV/sqrt(Hz)": 1e-3, "uV/sqrt(Hz)": 1e-6, "nV/sqrt(Hz)": 1e-9}
KINDS = {"length": _LEN, "force": _FORCE, "moment": _MOMENT, "pressure": _PRESS, "voltage": _VOLT,
         "frequency": _FREQ, "noise density": _DENS}


def _unit(raw, kind: str, where: str) -> float:
    """`"10 mm"` as SI; ValueError names the field when the unit is missing or not a `kind`."""
    table = KINDS[kind]
    if isinstance(raw, bool) or not isinstance(raw, str):
        raise ValueError(f"{where}: no unit (write a string such as \"10 {next(iter(table))}\")")
    m = re.fullmatch(r"\s*([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s*([A-Za-z]\S*)\s*", raw.replace("·", "*").replace("µ", "u"))
    if not m and re.fullmatch(r"\s*[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?\s*", raw):
        raise ValueError(f"{where}: no unit (write e.g. \"{raw.strip()} {next(iter(table))}\")")
    if not m:
        raise ValueError(f"{where}: {raw!r} is not a number and a unit")
    if m.group(2) not in table:
        raise ValueError(f"{where}: unit {m.group(2)!r} is not a {kind} unit ({', '.join(table)})")
    return float(m.group(1)) * table[m.group(2)]


def _get(d: dict, key: str, kind: str, where: str, required: bool = False) -> float | None:
    if key not in d:
        if required:
            raise ValueError(f"{where}.{key}: missing")
        return None
    v = _unit(d[key], kind, f"{where}.{key}")
    if v <= 0 and key not in ("moment", "axial_force", "torque", "force"):
        raise ValueError(f"{where}.{key}: must be positive")
    return v


class Section:
    """Section properties: second moment I, bending extreme-fibre distance c, area A, polar J (None if not handled)."""

    def __init__(self, spec: dict):
        t = spec.get("type")
        if t not in SECTIONS:
            raise ValueError(f"section.type: {t!r} is not one of {', '.join(SECTIONS)}")
        self.type = t
        w = "section"
        if t == "rectangular":
            self.b = _get(spec, "width", "length", w, True)
            self.h = _get(spec, "thickness", "length", w, True)
            self.I = self.b * self.h ** 3 / 12
            self.c_out = self.h / 2
            self.c_in = None
            self.A = self.b * self.h
            self.J = None
            self.S = self.b * self.h ** 2 / 6
            self.formulas = {"I": "I = b*h^3/12", "S": "S = I/c = b*h^2/6", "A": "A = b*h", "J": None}
            return
        self.D = _get(spec, "outer_diameter", "length", w, True)
        if t == "solid_round":
            self.d_i = 0.0
        else:
            tw = _get(spec, "wall_thickness", "length", w, True)
            if 2 * tw >= self.D:
                raise ValueError("section.wall_thickness: 2*wall must be less than outer_diameter")
            self.d_i = self.D - 2 * tw
        self.I = math.pi * (self.D ** 4 - self.d_i ** 4) / 64
        self.J = math.pi * (self.D ** 4 - self.d_i ** 4) / 32
        self.A = math.pi * (self.D ** 2 - self.d_i ** 2) / 4
        self.c_out = self.D / 2
        self.c_in = self.d_i / 2 if t == "round_tube" else None
        self.S = self.I / self.c_out
        if t == "solid_round":
            self.formulas = {"I": "I = pi*d^4/64", "S": "S = I/c = pi*d^3/32", "A": "A = pi*d^2/4", "J": "J = pi*d^4/32"}
        else:
            self.formulas = {"I": "I = pi*(D^4 - d_i^4)/64, d_i = D - 2*t", "S": "S = I/(D/2)",
                             "A": "A = pi*(D^2 - d_i^2)/4", "J": "J = pi*(D^4 - d_i^4)/32"}


def _load_input(path: Path) -> dict:
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise ValueError(f"cannot read {path}: {e.strerror}")
    try:
        if path.suffix.lower() == ".json":
            return json.loads(raw)
        return tomllib.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:  # tomllib.TOMLDecodeError and JSONDecodeError are ValueErrors
        raise ValueError(f"{path.name}: {e}")


def budget(spec: dict) -> tuple[list[str], list[tuple[str, float, str, str]]]:
    """(notes, rows of (quantity, SI value, unit, formula)). ValueError names the first bad input in one line."""
    for k in ("section", "material", "load", "readout"):
        if not isinstance(spec.get(k), dict):
            raise ValueError(f"{k}: missing table")
    sec = Section(spec["section"])
    mat, load, ro = spec["material"], spec["load"], spec["readout"]
    E = _get(mat, "youngs_modulus", "pressure", "material", True)
    G = _get(mat, "shear_modulus", "pressure", "material")
    F = _get(load, "force", "force", "load")
    L = _get(load, "lever_arm", "length", "load")
    M = _get(load, "moment", "moment", "load")
    P = _get(load, "axial_force", "force", "load")
    T = _get(load, "torque", "moment", "load")
    if F is not None and L is None:
        raise ValueError("load.lever_arm: missing (a force needs its lever arm)")
    if F is None and L is not None:
        raise ValueError("load.force: missing (a lever arm needs its force)")
    if F is not None and M is not None:
        raise ValueError("load: give force with lever_arm, or moment, not both")
    if F is None and M is None and P is None and T is None:
        raise ValueError("load: no load given (force + lever_arm, moment, axial_force or torque)")
    if T is not None and sec.J is None:
        raise ValueError("load.torque: not handled for a rectangular section (no textbook closed form here)")
    if T is not None and G is None:
        raise ValueError("material.shear_modulus: missing (torque needs it)")
    if "gauge_factor" not in ro or isinstance(ro["gauge_factor"], bool) or not isinstance(ro["gauge_factor"], (int, float)) or ro["gauge_factor"] <= 0:
        raise ValueError("readout.gauge_factor: a positive number (dimensionless)")
    gf = float(ro["gauge_factor"])
    bridge = ro.get("bridge")
    if bridge not in BRIDGES:
        raise ValueError(f"readout.bridge: {bridge!r} is not one of {', '.join(BRIDGES)}")
    k, bnote = BRIDGES[bridge]
    vex = _get(ro, "excitation", "voltage", "readout", True)
    if "adc_noise_density" in ro and "adc_noise_rms" in ro:
        raise ValueError("readout: give adc_noise_density with bandwidth, or adc_noise_rms, not both")
    if "adc_noise_density" in ro:
        dens = _get(ro, "adc_noise_density", "noise density", "readout")
        bw = _get(ro, "bandwidth", "frequency", "readout", True)
        v_n = dens * math.sqrt(bw)
        noise_row = ("ADC input noise, rms", v_n, "V", "v_n = density*sqrt(BW)")
    elif "adc_noise_rms" in ro:
        v_n = _get(ro, "adc_noise_rms", "voltage", "readout")
        rate = _get(ro, "data_rate", "frequency", "readout")
        noise_row = ("ADC input noise, rms" + (f" at {rate:.4g} Hz data rate" if rate else ""), v_n, "V", "as given")
    else:
        raise ValueError("readout: adc_noise_density (with bandwidth) or adc_noise_rms is missing")

    notes = [f"section: {sec.type}; bridge: {bridge} ({bnote}), Vout/Vex = {k:g}*GF*strain",
             "beam model: cantilever, tip load at the lever arm; strain is at the outer surface unless the row says inner wall; "
             "not modelled: stress concentration, shear deflection, Poisson-ratio bridge gain, gauge nonlinearity, temperature"]
    rows: list[tuple[str, float, str, str]] = [
        ("second moment of area I", sec.I, "m^4", sec.formulas["I"]),
        ("section modulus S (outer fibre)", sec.S, "m^3", sec.formulas["S"]),
        ("area A", sec.A, "m^2", sec.formulas["A"]),
    ]
    if sec.J is not None:
        rows.append(("polar moment J", sec.J, "m^4", sec.formulas["J"]))
    if F is not None:
        M = F * L
        rows.append(("bending moment M", M, "N*m", "M = F*L"))
    elif M is not None:
        rows.append(("bending moment M", M, "N*m", "as given"))
    signals: list[tuple[str, float]] = []   # (name, strain driving the bridge)
    if M is not None:
        eb = M / (E * sec.S)
        rows.append(("bending strain, outer surface", eb, "m/m", "eps = M*c/(E*I) = M/(E*S)"))
        if sec.c_in is not None:
            rows.append(("bending strain, inner wall", M * sec.c_in / (E * sec.I), "m/m", "eps_i = M*(d_i/2)/(E*I)"))
        signals.append(("bending", eb))
        if F is not None:
            rows.append(("tip deflection", F * L ** 3 / (3 * E * sec.I), "m", "delta = F*L^3/(3*E*I)"))
        else:
            notes.append("tip deflection: not computed for a pure moment (needs force and lever arm)")
    if P is not None:
        ea = P / (E * sec.A)
        rows.append(("axial strain", ea, "m/m", "eps = P/(E*A)"))
        signals.append(("axial", ea))
    if T is not None:
        gam = T * sec.c_out / (sec.J * G)
        rows.append(("torsional shear strain, outer surface", gam, "m/m", "gamma = T*r/(J*G)"))
        if sec.c_in is not None:
            rows.append(("torsional shear strain, inner wall", T * sec.c_in / (sec.J * G), "m/m", "gamma_i = T*(d_i/2)/(J*G)"))
        rows.append(("torsional principal strain (gauge at 45 deg)", gam / 2, "m/m", "eps_45 = gamma/2"))
        signals.append(("torsion, 45 deg gauge", gam / 2))
    rows.append(("bridge gain k", k, "", f"k = {k:g} for a {bridge} bridge"))
    per_ue = vex * k * gf * 1e-6
    rows.append(("bridge output per microstrain", per_ue, "V/ue", "Vout = k*GF*eps*Vex, per 1e-6"))
    rows.append(noise_row)
    eres = v_n / (vex * k * gf)
    rows.append(("noise-limited resolvable strain (1 sigma)", eres, "m/m", "eps_res = v_n/(k*GF*Vex)"))
    if M is not None:
        rows.append(("noise-limited resolvable bending moment", eres * E * sec.S, "N*m", "M_res = eps_res*E*S"))
    for name, e in signals:
        vout = vex * k * gf * e
        rows.append((f"bridge output, {name} strain", vout, "V", "Vout = k*GF*eps*Vex"))
        rows.append((f"signal-to-noise ratio, {name}", vout / v_n, "", "SNR = Vout/v_n"))
    return notes, rows


def render(notes: list[str], rows: list[tuple[str, float, str, str]]) -> str:
    out = ["src: none (derived from the input file only; no atlas, document or netlist read)", ""]
    out += [f"- {n}" for n in notes] + [""]
    out += ["| quantity | value | unit | formula |", "|---|---|---|---|"]
    for name, v, unit, f in rows:
        out.append(f"| {name} | {v:.4g} (derived) | {unit or '-'} | {f} |")
    return "\n".join(out) + "\n"


def cmd_strain_budget(args) -> int:
    try:
        notes, rows = budget(_load_input(Path(args.file)))
    except ValueError as e:
        print(f"derive strain-budget: {e}", file=sys.stderr)
        return 2
    print(render(notes, rows), end="")
    return 0


def register(sub, groups) -> None:
    p = sub.add_parser("derive", help="standalone derive helpers outside the atlas build (no atlas read)")
    dsub = p.add_subparsers(dest="derive_verb", required=True)
    s = dsub.add_parser("strain-budget", help="section modulus, strain, deflection, bridge output and noise-limited resolution from loads and geometry")
    s.add_argument("file", help="TOML or JSON: [section] [material] [load] [readout], every value a string with its unit")
    s.set_defaults(func=cmd_strain_budget)

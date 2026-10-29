"""`inventory/answers.toml`: the engineer's answers to `missing.md`, and the generic function cards.

One table per part number the engineer answered:

    [parts."FETZZ9"]
    answer = "standard-function"          # or "supplied" or "not-needed"
    function = "n-mosfet"
    pins = { "1" = "G", "2" = "S", "3" = "D" }   # the part's pin number or name -> the card's pin

    [parts."SENS55"]
    answer = "not-needed"
    why = "firmware never touches it"

A **standard-function card** is a function template, not a part: it names each pin's role and `flow` the
way a datasheet pin record does (`prov = engineer-declared`) and states no limit, timing or part-specific
fact. A card applies only to an MPN the engineer answered, only through the engineer's pin map (the atlas
never reads a function or a pin map out of netlist names), and never when a datasheet covers the part.
"""
from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

from . import common

ANSWERS_FILE = "answers.toml"
KINDS = ("supplied", "not-needed", "standard-function")
PROV = "engineer-declared"

# pin -> (dir, type, flow, control, optional). `flow` source/sink/bidir is what net_trace walks on;
# `control` marks a pin that gates the path (an enable), never walked as an input.
_P = lambda d, t=None, f=None, c=False, o=False: {"dir": d, "type": t, "flow": f, "control": c, "optional": o}  # noqa: E731
_VCC, _GND = _P("power", "supply", None, False, True), _P("power", "ground", None, False, True)

CARDS: dict[str, dict[str, Any]] = {
    "n-mosfet": {"about": "n-channel MOSFET: gate controls the drain-source path; the path passes current both ways",
                 "pins": {"G": _P("in", "gate", "sink", True), "D": _P("passive", "channel", "bidir"),
                          "S": _P("passive", "channel", "bidir")}},
    "p-mosfet": {"about": "p-channel MOSFET: gate controls the source-drain path; the path passes current both ways",
                 "pins": {"G": _P("in", "gate", "sink", True), "D": _P("passive", "channel", "bidir"),
                          "S": _P("passive", "channel", "bidir")}},
    "diode": {"about": "diode: current from anode to cathode", "pins": {"A": _P("in", "anode", "sink"),
                                                                       "K": _P("out", "cathode", "source")}},
    "schottky": {"about": "Schottky diode: current from anode to cathode", "pins": {"A": _P("in", "anode", "sink"),
                                                                                    "K": _P("out", "cathode", "source")}},
    "tvs": {"about": "TVS clamp: no signal flow", "pins": {"1": _P("passive", "protection"),
                                                           "2": _P("passive", "protection")}},
    "thermistor": {"about": "thermistor: a two-terminal sensor, no signal flow",
                   "pins": {"1": _P("passive", "sensor"), "2": _P("passive", "sensor")}},
    "inverter": {"about": "inverter: Y is the logical NOT of A",
                 "pins": {"A": _P("in", "logic", "sink"), "Y": _P("out", "logic", "source"), "VCC": _VCC, "GND": _GND}},
    "and": {"about": "2-input AND: Y = A and B",
            "pins": {"A": _P("in", "logic", "sink"), "B": _P("in", "logic", "sink"),
                     "Y": _P("out", "logic", "source"), "VCC": _VCC, "GND": _GND}},
    "nand": {"about": "2-input NAND: Y = not (A and B)",
             "pins": {"A": _P("in", "logic", "sink"), "B": _P("in", "logic", "sink"),
                      "Y": _P("out", "logic", "source"), "VCC": _VCC, "GND": _GND}},
    "buffer-oe": {"about": "buffer with output enable: Y follows A while OE allows it",
                  "pins": {"A": _P("in", "logic", "sink"), "OE": _P("in", "enable", "sink", True),
                           "Y": _P("out", "logic", "source"), "VCC": _VCC, "GND": _GND}},
    "decoder-1of2": {"about": "1-of-2 decoder: A selects Y0 or Y1 while E allows it",
                     "pins": {"A": _P("in", "logic", "sink"), "E": _P("in", "enable", "sink", True),
                              "Y0": _P("out", "logic", "source"), "Y1": _P("out", "logic", "source"),
                              "VCC": _VCC, "GND": _GND}},
}


def norm(s: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def path_of(atlas_dir: Path) -> Path:
    return Path(atlas_dir) / "inventory" / ANSWERS_FILE


def load(atlas_dir: Path) -> dict[str, dict[str, Any]]:
    """MPN -> answer, as written. A missing file is no answers; a malformed entry raises ValueError."""
    p = path_of(atlas_dir)
    if not p.is_file():
        return {}
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{p}: not valid TOML: {e}") from e
    out = {}
    for mpn, a in (raw.get("parts") or {}).items():
        problem = check_answer(mpn, a)
        if problem:
            raise ValueError(f"{p}: {mpn}: {problem}")
        out[mpn] = a
    return out


def check_answer(mpn: str, a: Any) -> str | None:
    if not isinstance(a, dict) or a.get("answer") not in KINDS:
        return f"`answer` must be one of {', '.join(KINDS)}"
    if a["answer"] == "not-needed" and not str(a.get("why") or "").strip():
        return "`not-needed` needs a `why`"
    if a["answer"] == "standard-function":
        card = CARDS.get(a.get("function"))
        if card is None:
            return f"unknown function {a.get('function')!r}; known: {', '.join(CARDS)}"
        pins = a.get("pins")
        if not isinstance(pins, dict) or not pins:
            return "`standard-function` needs a `pins` map (the part's pin -> the card's pin)"
        bad = sorted({v for v in pins.values() if v not in card["pins"]})
        if bad:
            return f"card {a['function']} has no pin {', '.join(map(str, bad))}; its pins: {', '.join(card['pins'])}"
        need = [n for n, p in card["pins"].items() if not p["optional"] and n not in pins.values()]
        if need:
            return f"the pin map leaves out {', '.join(need)} of card {a['function']}"
    return None


def parse_pins(text: str) -> dict[str, str]:
    """`1=G,2=S,3=D` -> {"1": "G", ...}. Several part pins may name one card pin (a MOSFET's drain tab)."""
    out = {}
    for item in re.split(r"[,;]", text):
        if not item.strip():
            continue
        k, sep, v = item.partition("=")
        if not sep or not k.strip() or not v.strip():
            raise ValueError(f"pin map entry {item.strip()!r} is not PART_PIN=CARD_PIN")
        out[k.strip()] = v.strip()
    return out


def _q(s: str) -> str:
    return json.dumps(str(s), ensure_ascii=False)


def dump(answers: dict[str, dict[str, Any]]) -> str:
    L = ["# The engineer's answers to missing.md (embarch-atlas missing --answer). Hand edits are kept except comments.", ""]
    for mpn in sorted(answers):
        a = answers[mpn]
        L.append(f"[parts.{_q(mpn)}]")
        L.append(f"answer = {_q(a['answer'])}")
        if a.get("why"):
            L.append(f"why = {_q(a['why'])}")
        if a["answer"] == "standard-function":
            L.append(f"function = {_q(a['function'])}")
            L.append("pins = { " + ", ".join(f"{_q(k)} = {_q(v)}" for k, v in a["pins"].items()) + " }")
        L.append("")
    return "\n".join(L)


def record(atlas_dir: Path, mpn: str, answer: dict[str, Any]) -> None:
    problem = check_answer(mpn, answer)
    if problem:
        raise ValueError(problem)
    cur = load(atlas_dir)
    for k in [k for k in cur if norm(k) == norm(mpn)]:
        del cur[k]
    cur[mpn] = answer
    common.write_text(path_of(atlas_dir), dump(cur))


def find(answers: dict[str, dict[str, Any]], mpn: str | None) -> dict[str, Any] | None:
    n = norm(mpn)
    return next((a for k, a in answers.items() if norm(k) == n), None) if n else None


def _pin_matches(want: str, num: str, name: str | None) -> bool:
    return norm(want) == norm(num) or (bool(name) and norm(want) == norm(name))


def pin_records(answer: dict[str, Any], comp: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Pin number -> a datasheet-shaped pin record for one component, through the engineer's pin map."""
    fn = answer["function"]
    card = CARDS[fn]
    doc = f"generic card, {PROV}: {fn}"
    out = {}
    for num, P in (comp.get("pins") or {}).items():
        for want, role in (answer.get("pins") or {}).items():
            if _pin_matches(want, num, P.get("name")):
                spec = card["pins"][role]
                rec = {"kind": "pin", "doc": doc, "id": f"generic.{fn}.pin.{role}", "pin": num, "name": role,
                       "prov": PROV, "generic": True}
                rec.update({k: spec[k] for k in ("dir", "type", "flow") if spec.get(k)})
                if spec["control"]:
                    rec["control"] = True
                out[num] = rec
                break
    return out


def unmatched_pins(answer: dict[str, Any], comps: list[dict[str, Any]]) -> list[str]:
    """Keys of the pin map that match no pin of any given component."""
    return [w for w in (answer.get("pins") or {})
            if not any(_pin_matches(w, n, P.get("name")) for c in comps for n, P in (c.get("pins") or {}).items())]


def _cmd_answer(args, adir: Path) -> int:
    from . import interfaces
    mpn = args.answer[0]
    kind = args.answer[1] if len(args.answer) > 1 else ""
    rest = args.answer[2:]
    if kind not in KINDS or rest and kind != "standard-function":
        print(f"missing --answer: MPN supplied | not-needed --why TEXT | standard-function FUNCTION --pins MAP "
              f"(got {' '.join(args.answer)!r})", file=sys.stderr)
        return 2
    a: dict[str, Any] = {"answer": kind}
    if args.why:
        a["why"] = args.why
    if kind == "standard-function":
        if len(rest) != 1 or not args.pins:
            print(f"missing --answer: standard-function needs one FUNCTION ({', '.join(CARDS)}) and --pins MAP",
                  file=sys.stderr)
            return 2
        try:
            a.update(function=rest[0], pins=parse_pins(args.pins))
        except ValueError as e:
            print(f"missing --answer: {e}", file=sys.stderr)
            return 2
        nls = interfaces.load_netlists(adir)
        comps = [c for nl in nls.values() for c in nl["components"].values() if norm(c.get("mpn")) == norm(mpn)]
        if nls and not comps:
            print(f"missing --answer: no part on the boards has MPN {mpn!r}", file=sys.stderr)
            return 2
        bad = unmatched_pins(a, comps) if comps else []
        if bad:
            print(f"missing --answer: pin(s) {', '.join(bad)} match no pin number or name of {mpn}", file=sys.stderr)
            return 2
    try:
        record(adir, mpn, a)
    except ValueError as e:
        print(f"missing --answer: {e}", file=sys.stderr)
        return 2
    print(f"recorded: {mpn} {kind}" + (f" ({a['function']})" if kind == "standard-function" else "")
          + f" -> {path_of(adir)}")
    return 0

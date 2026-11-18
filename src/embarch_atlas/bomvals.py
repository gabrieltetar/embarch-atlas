"""Tolerance and rating of a BOM line, and the `parts` lookup that returns them by reference.

`schematic` stores `tolerance`, `voltage` and `power` on every `bom.json` record: from the schematic's
own parameter when it has one, otherwise read out of the description (`RES SMD 86.6K OHM 1% 1/16W
0402`). A value neither carries is `None`, shown as `unknown`; nothing is guessed from the part kind
or the footprint. The lookup reads `boards/<B>/bom.json` and `netlist.json` directly, so it needs no atlas.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

FIELDS = ("tolerance", "voltage", "power")
UNKNOWN = "unknown"

_NUM = r"(\d+(?:\.\d+)?)"
_PCT = re.compile(r"(?:±|\+/-|\+-)?\s*" + _NUM + r"\s*%")
_ABS = re.compile(r"(?:±|\+/-|\+-)\s*" + _NUM + r"\s*([pnuµm]?[FHf])\b")
_VOLT = re.compile(r"(?<![\w.])" + _NUM + r"\s*(?:V|VDC|VOLTS?)(?![\w])", re.I)
_PWR_FRAC = re.compile(r"(?<![\w.])(\d+)\s*/\s*(\d+)\s*W(?![\w])", re.I)
_PWR = re.compile(r"(?<![\w./])" + _NUM + r"\s*(m?W)(?![\w])", re.I)

# dedicated schematic parameters, first present wins
PARAM_KEYS = {
    "tolerance": ("Tolerance", "Value Tolerance", "Tol"),
    "voltage": ("Voltage", "Voltage Rating", "Rated Voltage", "Voltage - Rated", "Working Voltage"),
    "power": ("Power", "Power Rating", "Power (Watts)", "Power Rated"),
}


def _clean(s: Any) -> str | None:
    s = " ".join(str(s).split()) if s is not None else ""
    return s or None


def parse_description(text: str | None) -> dict[str, str | None]:
    """Tolerance, voltage and power written in free text; `None` for each one it does not carry."""
    t = text or ""
    pct = _PCT.search(t)
    out: dict[str, str | None] = dict.fromkeys(FIELDS)
    if pct:
        out["tolerance"] = f"{pct.group(1)}%"
    elif (ab := _ABS.search(t)):
        out["tolerance"] = f"{ab.group(1)}{ab.group(2)}"
    if (v := _VOLT.search(t)):
        out["voltage"] = f"{v.group(1)}V"
    if (w := _PWR_FRAC.search(t)):
        out["power"] = f"{w.group(1)}/{w.group(2)}W"
    elif (w := _PWR.search(t)):
        out["power"] = f"{w.group(1)}{w.group(2)[:-1].lower()}W"
    return out


def from_props(props: dict[str, str], description: str | None, comment: str | None = None) -> dict[str, str | None]:
    """A dedicated parameter wins; the description, then the Comment, fills what it left."""
    out: dict[str, str | None] = dict.fromkeys(FIELDS)
    for f, keys in PARAM_KEYS.items():
        for k in keys:
            if (v := _clean(props.get(k))):
                out[f] = v
                break
    for text in (description, comment):
        if all(out.values()):
            break
        got = parse_description(text)
        for f in FIELDS:
            out[f] = out[f] or got[f]
    return out


def rating(e: dict[str, Any]) -> str:
    """`50V 1/16W`, or `unknown` when the record carries neither."""
    return " ".join(x for x in (e.get("voltage"), e.get("power")) if x) or UNKNOWN


def _of(e: dict[str, Any]) -> dict[str, str | None]:
    """The record's stored fields; a bom.json written before they existed is read from its description."""
    if any(f in e for f in FIELDS):
        return {f: e.get(f) for f in FIELDS}
    return parse_description(e.get("description"))


def lookup(atlas_dir: Path, board: str, refs: list[str], read_json: Callable[[Path], Any]) -> tuple[str, str]:
    """(src header text, body) for `parts`: one line per reference, then the references not found."""
    boards = sorted(p.parent.name for p in (atlas_dir / "boards").glob("*/netlist.json"))
    if board not in boards:
        return "", f"board {board!r} not found. Boards: {', '.join(boards) or 'none'}."
    want = [r.strip() for r in refs if r and r.strip()]
    want = [w.split(":", 1)[1] if w.startswith(board + ":") else w for w in want]
    if not want:
        return "", "parts needs one or more references (R12 R13 ...)."
    bdir = atlas_dir / "boards" / board
    nl = read_json(bdir / "netlist.json") or {}
    comps = nl.get("components") or {}
    bom = {e["ref"]: e for e in (read_json(bdir / "bom.json") or []) if isinstance(e, dict) and e.get("ref")}
    lines, missing = [], []
    for ref in dict.fromkeys(want):
        c, e = comps.get(ref), bom.get(ref)
        if c is None and e is None:
            missing.append(ref)
            continue
        c, e = c or {}, e or {}
        f = _of(e)
        bits = [f"{board}:{ref}", f"mpn={_clean(e.get('mpn') or c.get('mpn')) or UNKNOWN}",
                f"value={_clean(e.get('value') or c.get('value')) or UNKNOWN}",
                f"tol={f['tolerance'] or UNKNOWN}",
                f"rating={rating(f)}",
                f"dni={'yes' if (e.get('dni') if 'dni' in e else c.get('dni')) else 'no'}"]
        pins = [f"{p}{('/' + P['name']) if P.get('name') and P['name'] != p else ''}={P.get('net') or 'NC'}"
                for p, P in (c.get("pins") or {}).items()]
        lines.append(" ".join(bits) + (" | " + " ".join(pins) if pins else ""))
    body = (f"{len(lines)} of {len(dict.fromkeys(want))} references found on {board}.\n"
            f"Line: `<board>:<ref> mpn= value= tol= rating=<voltage> <power> dni= | <pin>[/<name>]=<net> ...`; "
            f"`unknown` = neither a schematic parameter nor the description carries it.\n\n")
    body += "\n".join(lines) + ("\n" if lines else "")
    if missing:
        body += f"\nNot found on {board}: {', '.join(missing)}.\n"
    return f"boards/{board} bom.json + netlist.json", body

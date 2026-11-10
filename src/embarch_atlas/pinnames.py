"""`pinnames`: names for pins the schematic PDF's text left unnamed, each saying where it came from.

A pin's name is the visible word the PDF prints at the end of its anchor (`smartpdf.pin_name_for`); a module
whose pad names are missing from that text keeps `name: null`. This module is the engineer-owned half:

- `boards/<label>/pin_names.json` is the overlay, beside `netlist.json` and never written by `schematic`
  (as `boards/system.json`'s `interfaces` are never overwritten). Shape:
  `{"board": "B", "pins": {"U3": {"A4": {"name": "VDD", "prov": "llm-unverified", "note": "..."}}}}`,
  keyed by board (the directory), ref and pin number. `prov` is `engineer-declared`, `engineer-confirmed`, or
  `llm-unverified` for a name read off a rendered crop, until the engineer confirms it.
- `apply` merges the overlay into a netlist: every named pin gets `name_source`, `pdf-text` for a name the PDF
  printed, `overlay:<prov>` for one from the overlay. `netlist.json` is that merged view, so `join`, `pin_trace`
  and every later step read the overlay's names without knowing the overlay exists. A PDF-text name always
  wins: an overlay entry for a pin the PDF now names is left unused.
- `declare-pin-name` writes one entry and re-applies it to `netlist.json` (and `nets.md`, `parts.md`).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from . import common

OVERLAY = "pin_names.json"
PROV = ("engineer-declared", "engineer-confirmed", "llm-unverified")
PDF_SOURCE = "pdf-text"
OVERLAY_PREFIX = "overlay:"


def overlay_path(atlas_dir: Path, board: str) -> Path:
    return Path(atlas_dir) / "boards" / board / OVERLAY


def load(path: Path) -> dict[str, dict[str, dict[str, Any]]]:
    """The overlay's `pins` ({ref: {pin: entry}}), empty when the file is missing."""
    return (common.read_json(path) or {}).get("pins") or {} if Path(path).is_file() else {}


def apply(nl: dict[str, Any], overlay: dict[str, dict[str, dict[str, Any]]]) -> dict[str, Any]:
    """Merge the overlay into the netlist in place and refresh the name counts. Idempotent: names an earlier
    merge took from the overlay are dropped first, so a changed or removed entry does not linger."""
    from .smartpdf import MIN_NAMED_PINS

    for c in nl.get("components", {}).values():
        for p in (c.get("pins") or {}).values():
            if str(p.get("name_source", "")).startswith(OVERLAY_PREFIX):
                p["name"] = None
                p.pop("name_source")
    for ref, pins in overlay.items():
        c = nl.get("components", {}).get(ref)
        for num, e in pins.items():
            p = ((c or {}).get("pins") or {}).get(num)
            if p is not None and not p.get("name") and e.get("name"):
                p["name"], p["name_source"] = e["name"], OVERLAY_PREFIX + str(e.get("prov"))
    pins_all = [p for c in nl.get("components", {}).values() for p in (c.get("pins") or {}).values()]
    for p in pins_all:
        if p.get("name"):
            p.setdefault("name_source", PDF_SOURCE)
    on3 = [p for c in nl.get("components", {}).values() if len(c.get("pins") or {}) >= MIN_NAMED_PINS
           for p in c["pins"].values()]
    counts = nl.setdefault("counts", {})
    counts["pins_named"] = sum(1 for p in pins_all if p.get("name"))
    counts["pins_named_from_overlay"] = sum(1 for p in pins_all if str(p.get("name_source", "")).startswith(OVERLAY_PREFIX))
    counts["pins_unnamed_on_3plus_pin_parts"] = sum(1 for p in on3 if not p.get("name"))
    return nl


def declare(atlas_dir: Path, board: str, ref: str, pin: str, name: str, prov: str = "llm-unverified",
            note: str | None = None) -> dict[str, Any]:
    """Record one pin name in `boards/<board>/pin_names.json` and merge it into that board's `netlist.json`.

    The same board/ref/pin again replaces its entry. Raises ValueError for a board with no netlist, a ref the
    netlist lacks, a pin number the part lacks, a pin the PDF's text already named, an empty name or a `prov`
    outside PROV."""
    from . import smartpdf

    atlas_dir = Path(atlas_dir)
    if prov not in PROV:
        raise ValueError(f"prov must be one of {', '.join(PROV)}")
    name = (name or "").strip()
    if not name:
        raise ValueError("the name is empty")
    bdir = atlas_dir / "boards" / board
    nlp = bdir / "netlist.json"
    if not nlp.is_file():
        known = sorted(p.parent.name for p in (atlas_dir / "boards").glob("*/netlist.json"))
        raise ValueError(f"board {board!r} has no netlist.json ({', '.join(known) or 'none found'}); "
                         f"run `embarch-atlas schematic` for it first")
    nl = common.read_json(nlp)
    comps = nl.get("components") or {}
    if ref not in comps:
        raise ValueError(f"{ref!r} is not a part on board {board}")
    pins = comps[ref].get("pins") or {}
    if pin not in pins:
        raise ValueError(f"{ref} has no pin {pin!r} on board {board} (pins: {', '.join(sorted(pins, key=smartpdf.natural_key))})")
    cur = pins[pin]
    if cur.get("name") and not str(cur.get("name_source", "")).startswith(OVERLAY_PREFIX):
        raise ValueError(f"{ref}-{pin} is already named {cur['name']!r} by the PDF's text; "
                         f"an overlay does not overwrite a printed name")
    entry: dict[str, Any] = {"name": name, "prov": prov}
    if note:
        entry["note"] = note
    path = overlay_path(atlas_dir, board)
    ov = load(path)
    ov.setdefault(ref, {})[pin] = entry
    ov = {r: dict(sorted(v.items(), key=lambda kv: smartpdf.natural_key(kv[0]))) for r, v in
          sorted(ov.items(), key=lambda kv: smartpdf.natural_key(kv[0]))}
    common.write_json(path, {"board": board, "pins": ov})
    apply(nl, ov)
    common.write_json(nlp, nl)
    common.write_text(bdir / "nets.md", smartpdf.render_nets_md(nl))
    if (bdir / "bom.json").is_file():
        common.write_text(bdir / "parts.md", smartpdf.render_parts_md(nl, common.read_json(bdir / "bom.json")))
    return {"board": board, "ref": ref, "pin": pin, **entry}


def _declare_cmd(args) -> int:
    adir = common.atlas_dir(args.atlas_dir)
    try:
        e = declare(adir, args.board, args.ref, args.pin, args.name, prov=args.prov, note=args.note)
    except (ValueError, FileNotFoundError) as ex:
        print(f"declare-pin-name: {ex}", file=sys.stderr)
        return 2
    print(f"{e['board']}:{e['ref']}-{e['pin']} = {e['name']}  [{e['prov']}]")
    print(f"-> {overlay_path(adir, e['board'])}  (merged into netlist.json; `schematic` keeps it)")
    return 0


def register(sub, groups) -> None:
    d = sub.add_parser("declare-pin-name", help="record a name for a pin the schematic PDF left unnamed "
                       "(boards/<board>/pin_names.json)")
    d.add_argument("board", help="board label")
    d.add_argument("ref", help="the part's reference designator, e.g. U3")
    d.add_argument("pin", help="pin number or ball, as in netlist.json, e.g. A4")
    d.add_argument("name", help="the pin's name as the symbol shows it")
    d.add_argument("--prov", default="llm-unverified", choices=PROV,
                   help="llm-unverified (default) for a name read off a rendered crop; the engineer's word otherwise")
    d.add_argument("--note", help="where the name was read (sheet, crop) or who said it")
    d.set_defaults(func=_declare_cmd)

"""`schematic <pdf> --board <label>`: the netlist an Altium Smart PDF already embeds.

Rung 2 of the schematic ingest ladder (DESIGN.md). An Altium "Smart PDF"
exported with *net information* and *component parameters* on carries, read here:

- **the outline** (bookmarks): per sheet, `Components > REF > REF-PIN` and
  `Nets > NET > Pins / Ports / NetLabels > item`. This is the netlist, exactly:
  every pin, every net, auto-named nets (`NetR5_1`) and power nets included.
- **invisible anchor words** (text render mode 3): `PI<ref>0<pin>` on each pin,
  `CO<ref>` on each designator, `NL<net>` on each net label, `PO<net>` on each
  port, with every non-alphanumeric written as `0`. A pin anchor spans its pin
  line, so it gives the pin's position and direction. The decoding
  `PI` + enc(ref) + `0` + enc(pin) is ambiguous in principle (`U1-01` and
  `U10-1` both give `PIU1001`), so tokens are resolved against the outline's own
  pin list, never parsed on their own.
- **component parameters**: one Link annotation per placed part whose JavaScript
  action calls `ShowCompProps_<GUID>()`; the document-level JavaScript defines
  that function as an `app.popUpMenu("Key: value", ...)` listing every parameter
  (MPN, value, description, footprint, datasheet links). A popup is mapped to a
  part by which part's pin anchors its rectangle holds.

Derived here, with the rule stated where it is applied:

- multi-part symbols (`U4A`, `U4B`) fold into one physical part (`U4`), because
  their pin bookmarks and every net already use the physical designator;
- pin names are the visible word just past the body end of a pin's anchor, on
  the pin's axis, for parts with 3 or more pins (the outline has no pin names);
- a part is not fitted (`dni`) when a parameter says so (`DNI`, `DNP`, ...) or the
  print draws it crossed out; the evidence is kept;
- a 1-pin part (test point, spring-contact pad) never takes a popup whose rectangle holds
  another part's pins: that was the one assignment error class the spikes found.

A PDF without net bookmarks is refused: the geometric and vision rungs are not
built yet, and a re-export with net information on turns the problem into a parse.
"""
from __future__ import annotations

import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from . import common, project

METHOD = "smartpdf-outline"
SECTIONS = ("Components", "Nets", "Ports")
NET_KINDS = {"Pins": "pins", "Ports": "ports", "NetLabels": "labels"}
MPN_KEYS = ("Manufacturer Part Number 1", "Manufacturer Part Number", "MPN", "Mfr Part Number",
            "Manufacturer Part #", "Part Number")
MFR_KEYS = ("Manufacturer 1", "Manufacturer", "Mfr")
VALUE_KEYS = ("Value", "Resistance", "Capacitance", "Inductance")
EMPTY_VALUES = {"", "n/a", "na", "-", "none", "~", "*"}
DNI_VALUES = {"dni", "dnp", "dnf", "nf", "np", "nopop", "no pop", "not fitted", "not placed", "do not populate",
              "do not install", "no stuff"}
RED_MIN_WIDTH = 1.0      # pt; Altium's not-fitted cross is ~2 pt, a no-ERC mark ~0.25 pt
PIN_NAME_GAP = 6.0       # pt past the anchor's body end where a pin name may start
PIN_NAME_AXIS = 3.2      # pt off the pin axis a pin-name word may sit
RECT_SLACK = 3.0         # pt of slack when testing a point against a popup rectangle
MIN_NAMED_PINS = 3       # parts with fewer pins get no pin names (their text is values, not names)
BIG_NET = 40             # nets.md lists only 3+-pin parts' pins for nets at least this big


class NoNetInfo(RuntimeError):
    """The PDF has no net bookmarks, so this rung cannot read it."""


NO_NET_INFO_MSG = (
    "{pdf}: no net bookmarks in the PDF outline, so the embedded-netlist rung cannot read it. "
    "The geometric and vision rungs of the schematic ladder are not built yet. "
    "Ask the EE to re-export the schematic PDF with \"net information\" and \"component parameters\" "
    "turned on (Altium Smart PDF wizard; OrCAD: \"Create Net and Part Bookmarks\")."
)


# ---------------------------------------------------------------- naming helpers

def enc(s: str) -> str:
    """Altium's anchor encoding: every non-alphanumeric becomes `0`."""
    return re.sub(r"[^A-Za-z0-9]", "0", s)


def pin_token(ref: str, pin: str) -> str:
    return "PI" + enc(ref) + "0" + enc(pin)


def natural_key(s: str) -> tuple:
    """Designators and pin numbers in human order: C2 < C10 < R1; 2 < 10 < A1."""
    return tuple((0, int(t), "") if t.isdigit() else (1, 0, t) for t in re.split(r"(\d+)", s) if t != "")


def ref_prefix(ref: str) -> str:
    m = re.match(r"[A-Za-z_]+", ref)
    return m.group(0).upper() if m else ""


def split_pin_id(pid: str, refs: Iterable[str] | None = None) -> tuple[str, str]:
    """`U1-12` -> (`U1`, `12`); with `refs`, prefer the split whose left side is a known designator."""
    if refs is not None:
        idx = [i for i, c in enumerate(pid) if c == "-"]
        for i in reversed(idx):
            if pid[:i] in refs:
                return pid[:i], pid[i + 1:]
    if "-" in pid:
        a, b = pid.rsplit("-", 1)
        return a, b
    return pid, ""


def physical_ref(comp_title: str, pin_title: str) -> tuple[str, str]:
    """The physical designator and pin a pin bookmark names under a (possibly multi-part) component.

    `U4A` / `U4-3` -> (`U4`, `3`): the longest prefix of the component title
    that the pin bookmark starts with, followed by `-`.
    """
    for k in range(len(comp_title), 0, -1):
        if pin_title.startswith(comp_title[:k] + "-"):
            return comp_title[:k], pin_title[k + 1:]
    return split_pin_id(pin_title)


def sheet_name(title: str) -> str:
    m = re.match(r"^(.*?)\.SchDoc\((.*)\)$", title, re.I)
    if m:
        return m.group(2) or m.group(1)
    return re.sub(r"\.SchDoc$", "", title, flags=re.I)


def is_auto_net(name: str, pins: list[str]) -> bool:
    """Altium names an unlabelled net after one of its pins: `Net` + ref + `_` + pin."""
    if not name.startswith("Net"):
        return False
    rest = name[3:]
    if pins:
        return any(rest == f"{r}_{p}" for r, p in (split_pin_id(x) for x in pins))
    return bool(re.fullmatch(r"[A-Za-z0-9_]+_[A-Za-z0-9]+", rest))


# ---------------------------------------------------------------- outline

def outline_tree(toc: list) -> list[dict[str, Any]]:
    """PyMuPDF `get_toc(simple=True)` rows -> nested nodes {title, page, children}."""
    root: list[dict[str, Any]] = []
    stack: list[tuple[int, list]] = [(0, root)]
    for lvl, title, page, *_ in toc:
        node = {"title": title, "page": page if page and page > 0 else None, "children": []}
        while stack and stack[-1][0] >= lvl:
            stack.pop()
        stack[-1][1].append(node)
        stack.append((lvl, node["children"]))
    return root


def read_outline(toc: list) -> dict[str, dict[str, Any]]:
    """Sheets -> {page, parent, components {sheet_ref: [(phys_ref, pin)]}, nets {net: {pins, labels, ports}}, ports}.

    A sheet is any outline node with a `Components`, `Nets` or `Ports` child;
    its parent is the nearest ancestor sheet.
    """
    sheets: dict[str, dict[str, Any]] = {}

    def visit(nodes, parent_sheet):
        for node in nodes:
            secs = {c["title"]: c for c in node["children"] if c["title"] in SECTIONS}
            this = parent_sheet
            if secs:
                name = sheet_name(node["title"])
                base, n = name, 2
                while name in sheets:
                    name, n = f"{base}#{n}", n + 1
                S = {"page": node["page"], "parent": parent_sheet, "components": {}, "nets": {}, "ports": []}
                sheets[name] = S
                for comp in (secs.get("Components") or {"children": []})["children"]:
                    S["components"][comp["title"]] = [physical_ref(comp["title"], p["title"]) for p in comp["children"]]
                for net in (secs.get("Nets") or {"children": []})["children"]:
                    N = S["nets"].setdefault(net["title"], {"pins": [], "labels": 0, "ports": 0})
                    for kind in net["children"]:
                        key = NET_KINDS.get(kind["title"])
                        if key == "pins":
                            N["pins"] += [c["title"] for c in kind["children"]]
                        elif key:
                            N[key] += len(kind["children"])
                S["ports"] = sorted(p["title"] for p in (secs.get("Ports") or {"children": []})["children"])
                this = name
            visit([c for c in node["children"] if c["title"] not in SECTIONS or not secs], this)

    visit(outline_tree(toc), None)
    return sheets


# ---------------------------------------------------------------- page layer

def page_text_layer(page) -> tuple[dict[str, list[tuple]], list[tuple]]:
    """(invisible anchor words -> boxes, visible words as (x0, y0, x1, y1, text, rgb)).

    Boxes come from `rawdict`, whose character boxes follow the full glyph
    matrix: a squashed anchor on a vertical pin gets a tall thin box that spans
    the pin line (`texttrace` boxes do not). Invisibility (render mode 3) comes
    from `texttrace`, matched to `rawdict` characters by glyph origin.
    """
    hidden = set()
    for sp in page.get_texttrace():
        if sp.get("type") == 3:
            for ch in sp["chars"]:
                hidden.add((ch[0], round(ch[2][0], 1), round(ch[2][1], 1)))
    anchors: dict[str, list[tuple]] = defaultdict(list)
    words = []
    for blk in page.get_text("rawdict")["blocks"]:
        for line in blk.get("lines", []):
            for sp in line["spans"]:
                col = sp.get("color", 0)
                rgb = (round(((col >> 16) & 255) / 255, 3), round(((col >> 8) & 255) / 255, 3), round((col & 255) / 255, 3))
                cur, box, inv = "", None, 0
                for ch in list(sp["chars"]) + [{"c": " ", "bbox": (0, 0, 0, 0), "origin": (0, 0)}]:
                    c = ch["c"]
                    if c.isspace():
                        if cur:
                            b = tuple(round(v, 2) for v in box)
                            if inv * 2 > len(cur):
                                if b not in anchors[cur]:
                                    anchors[cur].append(b)
                            else:
                                words.append((*b, cur, rgb))
                        cur, box, inv = "", None, 0
                        continue
                    r = ch["bbox"]
                    cur += c
                    inv += (ord(c), round(ch["origin"][0], 1), round(ch["origin"][1], 1)) in hidden
                    box = tuple(r) if box is None else (min(box[0], r[0]), min(box[1], r[1]), max(box[2], r[2]), max(box[3], r[3]))
    return anchors, words


def js_unescape(s: str) -> str:
    out, i = [], 0
    simple = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
    while i < len(s):
        c = s[i]
        if c != "\\" or i + 1 >= len(s):
            out.append(c)
            i += 1
            continue
        n = s[i + 1]
        if n == "u" and re.fullmatch(r"[0-9A-Fa-f]{4}", s[i + 2:i + 6]):
            out.append(chr(int(s[i + 2:i + 6], 16)))
            i += 6
        elif n == "x" and re.fullmatch(r"[0-9A-Fa-f]{2}", s[i + 2:i + 4]):
            out.append(chr(int(s[i + 2:i + 4], 16)))
            i += 4
        else:
            out.append(simple.get(n, n))
            i += 2
    return "".join(out)


def js_string_args(js: str, call: str = "popUpMenu") -> list[str]:
    """The string literals passed to `call(...)`, in order (nested arrays flattened)."""
    at = js.find(call + "(")
    if at < 0:
        return []
    i, depth, out = at + len(call) + 1, 1, []
    while i < len(js) and depth:
        c = js[i]
        if c in "\"'":
            j, buf = i + 1, []
            while j < len(js) and js[j] != c:
                if js[j] == "\\" and j + 1 < len(js):
                    buf.append(js[j:j + 2])
                    j += 2
                    continue
                buf.append(js[j])
                j += 1
            out.append(js_unescape("".join(buf)))
            i = j + 1
            continue
        depth += {"(": 1, "[": 1, ")": -1, "]": -1}.get(c, 0)
        i += 1
    return out


def parse_props(js_body: str) -> dict[str, str]:
    props: dict[str, str] = {}
    for s in js_string_args(js_body):
        if ": " in s:
            k, v = s.split(": ", 1)
        elif s.endswith(":"):
            k, v = s[:-1], ""
        else:
            continue
        props.setdefault(k.strip(), v.strip())
    return props


def _xref_of(val: tuple[str, str]) -> int | None:
    return int(val[1].split()[0]) if val[0] == "xref" else None


def _js_text(doc, xref: int, key: str = "JS") -> str:
    t, v = doc.xref_get_key(xref, key)
    if t == "xref":
        try:
            return doc.xref_stream(int(v.split()[0])).decode("latin-1")
        except Exception:  # noqa: BLE001 - not a stream: no JS
            return ""
    return v if t == "string" else ""


def document_js(doc) -> dict[str, str]:
    """Document-level JavaScript (the catalog's Names > JavaScript name tree): name -> source."""
    out: dict[str, str] = {}
    names = _xref_of(doc.xref_get_key(doc.pdf_catalog(), "Names"))
    if names is None:
        return out
    tree = _xref_of(doc.xref_get_key(names, "JavaScript"))
    seen: set[int] = set()
    todo = [tree] if tree is not None else []
    while todo:
        x = todo.pop()
        if x in seen:
            continue
        seen.add(x)
        t, v = doc.xref_get_key(x, "Names")
        if t == "array":
            for nm, ref in re.findall(r"\(((?:[^()\\]|\\.)*)\)\s*(\d+) 0 R", v):
                out[nm] = _js_text(doc, int(ref))
        t, v = doc.xref_get_key(x, "Kids")
        if t == "array":
            todo += [int(r) for r in re.findall(r"(\d+) 0 R", v)]
    return out


def page_popups(doc, page, funcs: dict[str, str]) -> list[tuple[tuple, dict[str, str]]]:
    """Link annotations calling ShowCompProps_<GUID>() -> [(rect in page coords, parameters)]."""
    import pymupdf

    res = []
    t, v = doc.xref_get_key(page.xref, "Annots")
    if t != "array":
        return res
    mat = page.transformation_matrix
    for x in map(int, re.findall(r"(\d+) 0 R", v)):
        js = _js_text(doc, x, "A/JS")
        m = re.search(r"(ShowCompProps_\w+)\s*\(", js)
        if not m or m.group(1) not in funcs:
            continue
        rt, rv = doc.xref_get_key(x, "Rect")
        nums = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", rv)] if rt == "array" else []
        if len(nums) != 4:
            continue
        r = pymupdf.Rect(nums) * mat
        r.normalize()
        res.append(((round(r.x0, 2), round(r.y0, 2), round(r.x1, 2), round(r.y1, 2)), parse_props(funcs[m.group(1)])))
    return res


def red_diagonals(page) -> list[tuple[float, float, float, float]]:
    """Red stroked diagonal segments at least RED_MIN_WIDTH wide: the strokes of a not-fitted cross."""
    segs = []
    for g in page.get_drawings():
        col = g.get("color")
        if not col or g.get("type") not in ("s", "fs") or (g.get("width") or 0) < RED_MIN_WIDTH:
            continue
        if not (col[0] >= 0.8 and col[1] <= 0.25 and col[2] <= 0.25):
            continue
        for it in g["items"]:
            if it[0] == "l":
                a, b = it[1], it[2]
                if abs(a.x - b.x) > 0.5 and abs(a.y - b.y) > 0.5:
                    segs.append((a.x, a.y, b.x, b.y))
    return segs


# ---------------------------------------------------------------- geometry rules

def centre(b: tuple) -> tuple[float, float]:
    return (b[0] + b[2]) / 2, (b[1] + b[3]) / 2


def in_rect(pt: tuple[float, float], r: tuple, slack: float = RECT_SLACK) -> bool:
    return r[0] - slack <= pt[0] <= r[2] + slack and r[1] - slack <= pt[1] <= r[3] + slack


def resolve_pin_boxes(parts: dict[str, list[tuple[str, str]]], anchors: dict[str, list[tuple]]) -> tuple[dict, list[str]]:
    """(sheet_ref, pin) -> anchor box, resolving tokens that several outline pins encode to.

    A token shared by k pins with k boxes gives each box to the pin whose part's
    other (unambiguous) pins lie nearest; anything left over has no box.
    """
    want: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for sref, pins in parts.items():
        for phys, pin in pins:
            want[pin_token(phys, pin)].append((sref, pin))
    boxes: dict[tuple[str, str], tuple] = {}
    issues = []
    ambiguous = []
    for tok, owners in want.items():
        found = anchors.get(tok) or []
        if len(owners) == 1 and found:
            boxes[owners[0]] = found[0]
        elif len(owners) > 1:
            ambiguous.append((tok, owners, found))
    for tok, owners, found in ambiguous:
        pts = {}
        for o in owners:
            mine = [centre(b) for (s, p), b in boxes.items() if s == o[0]]
            if mine:
                pts[o] = (sum(x for x, _ in mine) / len(mine), sum(y for _, y in mine) / len(mine))
        pairs = sorted(((abs(centre(b)[0] - pts[o][0]) + abs(centre(b)[1] - pts[o][1]), i, o)
                        for o in owners if o in pts for i, b in enumerate(found)), key=lambda t: (t[0], t[1], t[2]))
        used_b, used_o = set(), set()
        for _, i, o in pairs:
            if i not in used_b and o not in used_o:
                boxes[o] = found[i]
                used_b.add(i)
                used_o.add(o)
        left = [f"{s}:{p}" for s, p in owners if (s, p) not in boxes]
        if left:
            issues.append(f"anchor {tok} is shared by pins {', '.join(f'{s}-{p}' for s, p in owners)}; "
                          f"no position for {', '.join(left)}")
    return boxes, issues


def plausible(npins: int, inside: Counter, ref: str) -> bool:
    """The pin-count rule: a 1-pin part (test point, spring-contact pad) cannot own a popup whose rectangle
    holds 2+ pins of another part; that rectangle is the other part's body.

    The popup's own "Number of Pins" is no evidence either way: the outline lists
    only a part's connected pins, so a 2-pin resistor with one pin open shows 1.
    """
    return npins != 1 or not any(n >= 2 for r, n in inside.items() if r != ref)


def match_popups(parts: dict[str, list[tuple[str, str]]], pin_boxes: dict, co_boxes: dict[str, tuple],
                 popups: list[tuple[tuple, dict]]) -> dict[str, int]:
    """sheet_ref -> index into `popups`, one-to-one.

    Greedy on (pins of the part inside the rectangle, desc; rectangle area, asc):
    a part's own body rectangle holds all its pins and is the smallest that does.
    A part with no pin inside any free rectangle falls back to the rectangle
    holding its designator anchor. Every assignment passes `plausible`.
    """
    npins = {s: len(p) for s, p in parts.items()}
    inside_by_rect: list[Counter] = []
    for rect, _ in popups:
        c: Counter = Counter()
        for (sref, _pin), b in pin_boxes.items():
            if in_rect(centre(b), rect):
                c[sref] += 1
        inside_by_rect.append(c)
    cands = []
    for i, (rect, _) in enumerate(popups):
        area = (rect[2] - rect[0]) * (rect[3] - rect[1])
        for sref, n in inside_by_rect[i].items():
            cands.append((-n, area, sref, i))
    cands.sort()
    got: dict[str, int] = {}
    used: set[int] = set()
    for _, _, sref, i in cands:
        if sref in got or i in used:
            continue
        if not plausible(npins.get(sref, 0), inside_by_rect[i], sref):
            continue
        got[sref] = i
        used.add(i)
    for sref in sorted(parts, key=natural_key):
        if sref in got or sref not in co_boxes:
            continue
        pt = centre(co_boxes[sref])
        free = sorted(((popups[i][0][2] - popups[i][0][0]) * (popups[i][0][3] - popups[i][0][1]), i)
                      for i in range(len(popups)) if i not in used and in_rect(pt, popups[i][0], 1.0))
        for _, i in free:
            if plausible(npins.get(sref, 0), inside_by_rect[i], sref):
                got[sref] = i
                used.add(i)
                break
    return got


def pin_name_for(box: tuple, words: list[tuple], body: tuple[float, float] | None, pin: str,
                 exclude: set[str] = frozenset()) -> str | None:
    """The visible word just past the body end of a pin anchor, on the pin's axis, toward the body.

    Numbers (neighbouring pins' numbers) and `exclude` (designators, the part's own
    comment, MPN and value, all printed inside or beside the body) are never names.
    """
    if body is None:
        return None
    x0, y0, x1, y1 = box
    horiz = (x1 - x0) >= (y1 - y0)
    cx, cy = centre(box)
    bx, by = body
    cands = []
    for w in words:
        wx0, wy0, wx1, wy1, t = w[:5]
        if t == pin or t.isdigit() or t in exclude:
            continue
        wcx, wcy = (wx0 + wx1) / 2, (wy0 + wy1) / 2
        if horiz:
            if abs(wcy - cy) > PIN_NAME_AXIS:
                continue
            if bx > cx and 0 <= wx0 - x1 <= PIN_NAME_GAP:
                cands.append((wx0 - x1, t))
            elif bx < cx and 0 <= x0 - wx1 <= PIN_NAME_GAP:
                cands.append((x0 - wx1, t))
        else:
            if abs(wcx - cx) > PIN_NAME_AXIS:
                continue
            if by > cy and 0 <= wy0 - y1 <= PIN_NAME_GAP:
                cands.append((wy0 - y1, t))
            elif by < cy and 0 <= y0 - wy1 <= PIN_NAME_GAP:
                cands.append((y0 - wy1, t))
    cands.sort()
    return cands[0][1] if cands else None


def _is_grey(rgb: tuple) -> bool:
    return len(rgb) == 3 and all(0.55 <= c <= 0.85 for c in rgb) and max(rgb) - min(rgb) <= 0.05


def drawn_dni(sref: str, body: tuple | None, co_box: tuple | None, words: list[tuple], reds: list[tuple]) -> list[str]:
    ev = []
    if body:
        inside = [s for s in reds if in_rect((s[0], s[1]), body, 2.0) and in_rect((s[2], s[3]), body, 2.0)]
        slopes = {((s[3] - s[1]) * (s[2] - s[0])) > 0 for s in inside}
        if len(inside) >= 2 and len(slopes) == 2:
            ev.append("drawn-cross")
    if co_box:
        for w in words:
            if w[4] == sref and abs(w[0] - co_box[0]) < 4 and abs(w[1] - co_box[1]) < 6:
                if _is_grey(w[5]):
                    ev.append("drawn-grey")
                break
    return ev


# ---------------------------------------------------------------- component parameters

def _first(props: dict[str, str], keys: Iterable[str]) -> str | None:
    for k in keys:
        v = (props.get(k) or "").strip()
        if v and v.lower() not in EMPTY_VALUES:
            return v
    return None


def datasheet_link(props: dict[str, str]) -> str | None:
    links = []
    for k, v in props.items():
        m = re.fullmatch(r"ComponentLink(\d+)URL", k)
        if m and v.strip():
            desc = props.get(f"ComponentLink{m.group(1)}Description", "")
            links.append((0 if "datasheet" in desc.lower() else 1, int(m.group(1)), v.strip()))
    if links:
        return sorted(links)[0][2]
    for k in ("Datasheet", "Datasheet URL", "HelpURL"):
        v = (props.get(k) or "").strip()
        if v.lower().startswith(("http://", "https://")):
            return v
    return None


def summarize_props(props: dict[str, str]) -> dict[str, Any]:
    value = _first(props, VALUE_KEYS)
    dni_ev = []
    for k in ("Value", "Comment", "Fitted", "Variant", "Populate"):
        v = (props.get(k) or "").strip().lower()
        if v in DNI_VALUES:
            dni_ev.append(f"parameter {k}={props[k].strip()}")
    if value and value.lower() in DNI_VALUES:
        value = _first({k: v for k, v in props.items() if k != "Value"}, VALUE_KEYS)
    kind = props.get("Component Kind", "")
    return {
        "mpn": _first(props, MPN_KEYS),
        "manufacturer": _first(props, MFR_KEYS),
        "value": value,
        "comment": _first(props, ("Comment",)),
        "description": _first(props, ("Description",)),
        "footprint": _first(props, ("Footprint",)),
        "datasheet_link": datasheet_link(props),
        "in_bom": "no bom" not in kind.lower(),
        "dni_evidence": dni_ev,
    }


# ---------------------------------------------------------------- the parse

def parse(pdf: Path, board: str, source_file: str | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """-> (netlist.json, bom.json) in DESIGN.md's shape. Raises NoNetInfo when the outline has no nets."""
    import pymupdf

    pdf = Path(pdf)
    doc = pymupdf.open(pdf)
    sheets = read_outline(doc.get_toc(simple=True))
    if not any(S["nets"] for S in sheets.values()):
        raise NoNetInfo(NO_NET_INFO_MSG.format(pdf=pdf.name))
    funcs = {k: v for k, v in document_js(doc).items() if k.startswith("ShowCompProps_")}
    issues: list[str] = []

    # symbol parts (sheet-local, possibly U4A) -> physical parts (U4)
    phys: dict[str, dict[str, Any]] = {}
    sym_count = 0
    for sname in sorted(sheets, key=natural_key):
        S = sheets[sname]
        for sref, pins in S["components"].items():
            sym_count += 1
            prefs = sorted({p for p, _ in pins})
            pref = prefs[0] if prefs else sref
            if len(prefs) > 1:
                issues.append(f"{sref} on {sname}: pin bookmarks name several designators {prefs}")
            P = phys.setdefault(pref, {"parts": set(), "pins": {}, "pages": set(), "sheets": set(), "sym": []})
            P["parts"].add(sref)
            P["sheets"].add(sname)
            if S["page"]:
                P["pages"].add(S["page"])
            P["sym"].append((sname, sref))
            for _, pin in pins:
                P["pins"].setdefault(pin, {"name": None, "net": None})

    # board nets: one per name (Altium joins same-named nets through ports, sheet entries and power ports)
    refs = set(phys)
    nets: dict[str, dict[str, Any]] = {}
    label_only: dict[str, list[str]] = defaultdict(list)
    for sname in sorted(sheets, key=natural_key):
        for net, N in sheets[sname]["nets"].items():
            B = nets.setdefault(net, {"pins": set(), "sheets": set(), "labels": 0, "ports": 0})
            B["pins"] |= set(N["pins"])
            B["sheets"].add(sname)
            B["labels"] += N["labels"]
            B["ports"] += N["ports"]
            if N["labels"] and not N["ports"]:
                label_only[net].append(sname)
    for net, shs in sorted(label_only.items()):
        if len(nets[net]["sheets"]) > 1 and shs:
            issues.append(f"net {net} reaches sheet(s) {', '.join(sorted(shs, key=natural_key))} only through a net label "
                          "(no port): joined by name, which holds only for a global net-identifier scope")
    for net, B in nets.items():
        for pid in B["pins"]:
            ref, pin = split_pin_id(pid, refs)
            if ref in phys and pin in phys[ref]["pins"]:
                cur = phys[ref]["pins"][pin]["net"]
                if cur and cur != net:
                    issues.append(f"pin {ref}-{pin} is on two nets: {cur}, {net}")
                    continue
                phys[ref]["pins"][pin]["net"] = net
            else:
                issues.append(f"net {net} lists pin {pid}, which no component lists")

    # page layer: anchors, visible words, popups, not-fitted strokes
    props_of: dict[tuple[str, str], dict[str, Any]] = {}
    body_of: dict[tuple[str, str], tuple] = {}
    names_of: dict[tuple[str, str, str], str] = {}
    dni_of: dict[tuple[str, str], list[str]] = {}
    popups_total = popups_matched = 0
    for sname in sorted(sheets, key=natural_key):
        S = sheets[sname]
        if not S["page"] or not S["components"] or S["page"] > doc.page_count:
            continue
        page = doc[S["page"] - 1]
        anchors, words = page_text_layer(page)
        pin_boxes, amb = resolve_pin_boxes(S["components"], anchors)
        issues += [f"{sname}: {a}" for a in amb]
        co_boxes = {sref: anchors["CO" + enc(sref)][0] for sref in S["components"] if anchors.get("CO" + enc(sref))}
        pops = page_popups(doc, page, funcs) if funcs else []
        popups_total += len(pops)
        got = match_popups(S["components"], pin_boxes, co_boxes, pops)
        popups_matched += len(got)
        reds = red_diagonals(page)
        designators = set(S["components"]) | {p for pins in S["components"].values() for p, _ in pins}
        for sref, pins in S["components"].items():
            rect = pops[got[sref]][0] if sref in got else None
            if sref in got:
                props_of[(sname, sref)] = summarize_props(pops[got[sref]][1])
                body_of[(sname, sref)] = rect
            mine = [pin_boxes[(sref, p)] for _, p in pins if (sref, p) in pin_boxes]
            body = centre(rect) if rect else (
                (sum(centre(b)[0] for b in mine) / len(mine), sum(centre(b)[1] for b in mine) / len(mine))
                if len(mine) >= 2 else None)
            pref = sorted({p for p, _ in pins})[0] if pins else sref
            total_pins = len(phys[pref]["pins"]) if pref in phys else len(pins)
            own = props_of.get((sname, sref)) or {}
            exclude = designators | {v for v in (own.get("comment"), own.get("mpn"), own.get("value")) if v}
            for ph, pin in pins:
                b = pin_boxes.get((sref, pin))
                if b is not None and total_pins >= MIN_NAMED_PINS:
                    nm = pin_name_for(b, words, body, pin, exclude)
                    if nm:
                        names_of[(ph, pin, sref)] = nm
            dni_of[(sname, sref)] = drawn_dni(sref, rect, co_boxes.get(sref), words, reds)

    # fold parameters and evidence onto physical parts
    components: dict[str, Any] = {}
    bom: list[dict[str, Any]] = []
    for ref in sorted(phys, key=natural_key):
        P = phys[ref]
        props = None
        ev: list[str] = []
        for key in sorted(P["sym"], key=lambda t: (natural_key(t[1]), natural_key(t[0]))):
            pr = props_of.get(key)
            if pr and props is None:
                props = pr
            elif pr and props and pr["mpn"] != props["mpn"]:
                issues.append(f"{ref}: its symbol parts disagree on the MPN ({props['mpn']} vs {pr['mpn']})")
            if pr:
                ev += pr["dni_evidence"]
            ev += dni_of.get(key, [])
        for (ph, pin, _sref), nm in names_of.items():
            if ph == ref and pin in P["pins"]:
                P["pins"][pin]["name"] = nm
        props = props or {}
        ev = sorted(set(ev))
        components[ref] = {
            "mpn": props.get("mpn"),
            "value": props.get("value"),
            "dni": bool(ev),
            "pins": {p: P["pins"][p] for p in sorted(P["pins"], key=natural_key)},
            "pages": sorted(P["pages"]),
            "parts": sorted(P["parts"], key=natural_key),
            "sheets": sorted(P["sheets"], key=natural_key),
        }
        if ev:
            components[ref]["dni_evidence"] = ev
        bom.append({
            "ref": ref, "mpn": props.get("mpn"), "value": props.get("value"), "dni": bool(ev),
            "description": props.get("description"), "datasheet_link": props.get("datasheet_link"),
            "comment": props.get("comment"), "manufacturer": props.get("manufacturer"),
            "footprint": props.get("footprint"), "in_bom": props.get("in_bom", True) if props else None,
            "pins": len(P["pins"]),
        })
        if props and comment_conflicts(props.get("comment"), props.get("mpn")):
            issues.append(f"{ref}: printed Comment {props['comment']} differs from its MPN parameter {props['mpn']}")

    net_out = {}
    for net in sorted(nets, key=natural_key):
        B = nets[net]
        pins = sorted(B["pins"], key=natural_key)
        net_out[net] = {"pins": pins, "sheets": sorted(B["sheets"], key=natural_key),
                        "labels": B["labels"], "ports": B["ports"], "auto": is_auto_net(net, pins)}

    all_pins = [(r, p) for r, c in components.items() for p in c["pins"]]
    counts = {
        "sheets": len(sheets),
        "symbol_parts": sym_count,
        "parts": len(components),
        "pins": len(all_pins),
        "pins_on_net": sum(1 for r, p in all_pins if components[r]["pins"][p]["net"]),
        "nets": len(net_out),
        "nets_auto_named": sum(1 for n in net_out.values() if n["auto"]),
        "nets_single_pin": sum(1 for n in net_out.values() if len(n["pins"]) == 1),
        "parts_with_mpn": sum(1 for c in components.values() if c["mpn"]),
        "parts_dni": sum(1 for c in components.values() if c["dni"]),
        "pins_named": sum(1 for r, p in all_pins if components[r]["pins"][p]["name"]),
        "pins_on_3plus_pin_parts": sum(1 for r, p in all_pins if len(components[r]["pins"]) >= MIN_NAMED_PINS),
        "popups": len(funcs),
        "popups_on_pages": popups_total,
        "popups_matched": popups_matched,
    }
    if not funcs:
        issues.append("no component-parameter popups (ShowCompProps): MPN, value and DNI are unknown; "
                      "ask the EE to re-export with \"component parameters\" on")
    netlist = {
        "board": board,
        "source": {"file": source_file or pdf.name, "sha256": common.sha256_file(pdf)},
        "method": METHOD,
        "sheets": {s: {"page": sheets[s]["page"], "parent": sheets[s]["parent"]} for s in sorted(sheets, key=natural_key)},
        "components": components,
        "nets": net_out,
        "counts": counts,
        "issues": sorted(set(issues), key=natural_key),
    }
    doc.close()
    return netlist, bom


def _norm(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def _looks_like_mpn(s: str) -> bool:
    """A part number, not a description: 6+ characters, 2+ digits, a letter, no plain word of 5+ letters."""
    n = _norm(s)
    words = re.split(r"[^A-Za-z0-9]+", s)
    return (len(n) >= 6 and sum(c.isdigit() for c in n) >= 2 and any(c.isalpha() for c in n)
            and not any(len(w) >= 5 and w.isalpha() for w in words))


def comment_conflicts(comment: str | None, mpn: str | None) -> bool:
    """The printed Comment names a different part than the MPN parameter (neither is a prefix of the other)."""
    if not comment or not mpn or not _looks_like_mpn(comment):
        return False
    a, b = _norm(comment), _norm(mpn)
    return not (a.startswith(b) or b.startswith(a))


# ---------------------------------------------------------------- line formats for the LLM

def _pin_ref(board: str, ref: str, pin: str, comp: dict[str, Any] | None) -> str:
    s = f"{board}:{ref}-{pin}"
    name = (comp or {}).get("pins", {}).get(pin, {}).get("name")
    return s + (f"/{name}" if name and name != pin else "")


def _net_str(board: str, net: str | None, nets: dict[str, Any]) -> str:
    if not net:
        return "NC"
    return ("~" if nets.get(net, {}).get("auto") else "") + net


def render_nets_md(nl: dict[str, Any]) -> str:
    b, comps, nets = nl["board"], nl["components"], nl["nets"]
    c = nl["counts"]
    L = [f"# Nets, board {b}", "",
         f"From `{nl['source']['file']}` ({nl['method']}, sha256 {nl['source']['sha256'][:12]}): "
         f"{c['nets']} nets, {c['pins']} pins, {c['pins_on_net']} on a net.",
         f"Line: `{b}:<net> <pin> <pin> ...`, pin = `{b}:<ref>-<pin>[/<pin name>]`. `~` marks a net Altium auto-named "
         f"after one of its pins. Nets with {BIG_NET}+ pins list only pins on parts with 3+ pins (every membership is in "
         "parts.md and netlist.json).", ""]
    for net, N in nets.items():
        pins = N["pins"]
        head = f"{b}:{'~' if N['auto'] else ''}{net}"
        if len(pins) >= BIG_NET:
            keep = [p for p in pins if len(comps.get(split_pin_id(p, comps)[0], {}).get("pins", {})) >= MIN_NAMED_PINS]
            head += f" ({len(pins)} pins, {len(pins) - len(keep)} on 1-2-pin parts not listed)"
            pins = keep
        L.append(head + "".join(" " + _pin_ref(b, *split_pin_id(p, comps), comps.get(split_pin_id(p, comps)[0])) for p in pins))
    return "\n".join(L) + "\n"


def render_parts_md(nl: dict[str, Any], bom: list[dict[str, Any]]) -> str:
    b, comps, nets = nl["board"], nl["components"], nl["nets"]
    by_ref = {e["ref"]: e for e in bom}
    c = nl["counts"]
    L = [f"# Parts, board {b}", "",
         f"From `{nl['source']['file']}`: {c['parts']} physical parts ({c['symbol_parts']} symbol parts), "
         f"{c['parts_with_mpn']} with an MPN, {c['parts_dni']} not fitted.",
         f"Line: `{b}:<ref> <mpn or comment> [<value>] [DNI] [no-BOM] p<page> | <pin>[/<name>]=<net> ...`; "
         "`~` = auto-named net, `NC` = no net.", ""]
    for ref, C in comps.items():
        e = by_ref.get(ref, {})
        ident = C["mpn"] or e.get("comment") or "-"
        bits = [f"{b}:{ref}", ident.replace(" ", "_")]
        if C["value"] and C["value"] != ident:
            bits.append(C["value"].replace(" ", ""))
        if C["dni"]:
            bits.append("DNI")
        if e.get("in_bom") is False:
            bits.append("no-BOM")
        bits.append("p" + ",".join(map(str, C["pages"])) if C["pages"] else "p?")
        pins = [f"{p}{('/' + P['name']) if P['name'] and P['name'] != p else ''}={_net_str(b, P['net'], nets)}"
                for p, P in C["pins"].items()]
        L.append(" ".join(bits) + (" | " + " ".join(pins) if pins else ""))
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- CLI

def run(atlas_dir: Path, pdf: Path, board: str) -> dict[str, Any]:
    atlas_dir = Path(atlas_dir)
    cfg = project.load_config(atlas_dir) if (atlas_dir / project.CONFIG_NAME).is_file() else {}
    src = project.sources_dir(atlas_dir, cfg) if cfg else None
    try:
        rel = Path(pdf).resolve().relative_to(src.resolve()).as_posix() if src else None
    except ValueError:
        rel = None
    nl, bom = parse(pdf, board, rel or str(Path(pdf).resolve()))
    out = atlas_dir / "boards" / board
    common.write_json(out / "netlist.json", nl)
    common.write_json(out / "bom.json", bom)
    common.write_text(out / "nets.md", render_nets_md(nl))
    common.write_text(out / "parts.md", render_parts_md(nl, bom))
    return nl


def _resolve_pdf(adir: Path, pdf: str | None, board: str) -> Path:
    cfg = project.load_config(adir) if (adir / project.CONFIG_NAME).is_file() else {}
    if pdf is None:
        for b in project.board_entries(cfg):
            if b["label"] == board and b.get("pdf"):
                pdf = b["pdf"]
                break
        else:
            raise FileNotFoundError(f"no PDF given and no [[boards]] entry with label {board!r} in atlas.toml")
    p = Path(pdf)
    if not p.is_file() and cfg and not p.is_absolute():
        p = project.sources_dir(adir, cfg) / pdf
    if not p.is_file():
        raise FileNotFoundError(f"{pdf}: no such file")
    return p


def _cmd(args) -> int:
    adir = common.atlas_dir(args.atlas_dir)
    t0 = time.time()
    try:
        pdf = _resolve_pdf(adir, args.pdf, args.board)
        nl = run(adir, pdf, args.board)
    except (FileNotFoundError, NoNetInfo) as e:
        print(f"schematic: {e}", file=sys.stderr)
        return 2
    c = nl["counts"]
    print(f"{args.board}: {c['parts']} parts ({c['symbol_parts']} symbol parts), {c['pins']} pins "
          f"({c['pins_on_net']} on a net), {c['nets']} nets ({c['nets_auto_named']} auto-named), "
          f"{c['parts_with_mpn']} with MPN, {c['parts_dni']} DNI, popups {c['popups_matched']}/{c['popups_on_pages']}, "
          f"{c['pins_named']}/{c['pins_on_3plus_pin_parts']} pin names, {len(nl['issues'])} issues "
          f"-> {adir / 'boards' / args.board} in {time.time() - t0:.1f} s")
    return 0


def register(sub, groups) -> None:
    p = sub.add_parser("schematic", help="netlist.json, bom.json, nets.md, parts.md from an Altium Smart PDF")
    p.add_argument("pdf", nargs="?", help="the board's schematic PDF (default: its [[boards]] entry in atlas.toml)")
    p.add_argument("--board", required=True, help="board label, as in atlas.toml [[boards]]")
    p.set_defaults(func=_cmd)

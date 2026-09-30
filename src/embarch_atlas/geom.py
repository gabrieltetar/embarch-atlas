"""Rung 3 of the schematic ingest ladder: nets traced from a vector schematic PDF's drawn geometry.

Today this rung is the independent cross-check of rung 2 (the embedded Smart-PDF
netlist, `smartpdf.py`): `schematic` runs both and marks every net and pin
`crosscheck: agree | disagree | geometry-missing`. The embedded netlist stays
the source of truth; geometry never changes it. Later this rung is the
fallback for a PDF that has no embedded netlist.

What connects, and why. Each rule is the EDA tool's own connectivity rule as it
shows in the drawing:

- **wires** are stroked line paths in the wire style. Two wires connect only
  where an endpoint of one lies on the other (end to end, or a T). A plain
  crossing never connects; a junction dot on a crossing does, because the dot
  is the drawn connection.
- **pins**: a pin is the thin segment inside its invisible anchor word
  (`PI<ref>0<pin>`, render mode 3). Its hot end is the end that touches a wire,
  port or power port, else the end away from the part's body. The anchor gives
  the pin's *identity* (which part, which pin), resolved against the outline's
  component pin list when there is one: that, and nothing about nets, is what
  geometry takes from rung 2. A pin with no anchor cannot be named, and is
  reported as unanchored.
- a **net label** names the wire (or pin end) its hotspot lies on: the bottom
  corner where its text starts;
- a **port** joins everything touching any edge of its flag, and names it;
- a **power port** is a small group of power-coloured strokes with an end on a
  wire end or a pin end; its name is the nearest free word of the power colour;
- **the same name on one sheet is one net** (labels, ports and power ports alike).

Every colour, width and distance is a `Style` field. `ALTIUM` holds the defaults,
measured on one house style (one design team's Altium Smart PDF exports, two
boards); `detect_style` re-measures what it can on the PDF at hand (the wire
style from where wires meet pin anchors, label colours from the net-label
anchors, ...) and says which fields it measured and which it left at default.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from typing import Any, Iterable

from . import smartpdf as sp

RGB = tuple[float, float, float]
Pt = tuple[float, float]
Seg = tuple[Pt, Pt]

GEOMETRY = "geometry"  # the suffix the netlist's `method` gains when the cross-check agrees
AGREE, DISAGREE, MISSING = "agree", "disagree", "geometry-missing"


@dataclass(frozen=True)
class Style:
    """One EDA tool's drawing conventions in its PDF export (a "house style").

    The defaults below were measured on one house style: Altium Designer Smart PDF
    exports from one design team (two boards, 17 pages). Another tool, or the same
    tool with another template, may differ in any of them; `detect_style` re-measures
    the ones marked "detected".
    """
    name: str = "altium-default"
    wire_rgb: RGB = (0.0, 0.0, 0.502)         # detected; measured: navy
    wire_width: float = 0.72                  # detected; measured
    junction_rgb: RGB = (0.0, 0.0, 0.502)     # detected; measured: a filled navy dot
    port_stroke_rgb: RGB = (0.502, 0.0, 0.0)  # detected; measured: maroon border
    port_fill_rgb: RGB = (1.0, 1.0, 0.502)    # detected; measured: pale yellow
    power_rgb: RGB = (0.502, 0.0, 0.0)        # detected; measured: power-port strokes and their names, maroon
    label_rgbs: tuple[RGB, ...] = ()          # detected from net-label anchors; () = any colour but the
                                              # non-label ones (black/grey text, the wire colour, red marks)
    colour_tol: float = 0.03
    width_tol: float = 0.05
    # distances in pt, measured on the same style (a 7.2 pt snap grid, so all sit well below a grid step)
    snap: float = 0.8              # a wire endpoint on another wire; two pin ends meeting
    pin_snap: float = 1.0          # a pin's hot end on a wire; a pin end on a port edge or power stroke
    label_snap: float = 1.3        # a label's hotspot on a wire or pin end
    port_snap: float = 1.0         # a wire endpoint on a port flag's edge
    power_snap: float = 1.0        # a power-port stroke end on a wire end or pin end
    pin_len: tuple[float, float] = (0.5, 40.0)  # a pin line's length (test-point pins are very short)
    pin_max_width: float = 1.5     # pin lines are thin; the net-colour highlight strokes (3.6) are not
    anchor_slack: float = 2.0      # a pin line may overhang its anchor box by this much
    power_gap: float = 2.6         # strokes whose boxes are this close are one power symbol (ground bars do not touch)
    power_max: float = 40.0        # a stroke group bigger than this is a symbol body, not a power port
    power_name_radius: float = 12.0
    dot_max: float = 6.0           # a junction dot's diameter at most


ALTIUM = Style()


def style_dict(st: Style) -> dict[str, Any]:
    d = asdict(st)
    return {k: (list(v) if isinstance(v, tuple) else v) for k, v in d.items()}


# ---------------------------------------------------------------- small geometry

def _rgb(c) -> RGB | None:
    return tuple(round(float(x), 3) for x in c[:3]) if c else None  # type: ignore[return-value]


def col_eq(c: Iterable[float] | None, ref: Iterable[float], tol: float) -> bool:
    return c is not None and all(abs(a - b) <= tol for a, b in zip(c, ref))


def near(a: Pt, b: Pt, tol: float) -> bool:
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


def on_segment(p: Pt, s: Seg, tol: float) -> bool:
    """`p` within `tol` of segment `s` (its ends included)."""
    (x1, y1), (x2, y2) = s
    dx, dy = x2 - x1, y2 - y1
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return math.hypot(p[0] - x1, p[1] - y1) <= tol
    t = max(0.0, min(1.0, ((p[0] - x1) * dx + (p[1] - y1) * dy) / L2))
    return math.hypot(p[0] - x1 - t * dx, p[1] - y1 - t * dy) <= tol


def axis_aligned(s: Seg, tol: float = 0.1) -> bool:
    return abs(s[0][0] - s[1][0]) <= tol or abs(s[0][1] - s[1][1]) <= tol


def seg_len(s: Seg) -> float:
    return math.hypot(s[1][0] - s[0][0], s[1][1] - s[0][1])


def box_gap(a: tuple, b: tuple) -> float:
    """Distance between two boxes (x0, y0, x1, y1); 0 when they overlap."""
    dx = max(a[0] - b[2], b[0] - a[2], 0.0)
    dy = max(a[1] - b[3], b[1] - a[3], 0.0)
    return math.hypot(dx, dy)


class Grid:
    """Bucket index for points and segments: a query looks in one cell, so `pad` must cover every tolerance."""

    def __init__(self, cell: float = 8.0, pad: float = 1.5):
        self.cell, self.pad = cell, pad
        self.cells: dict[tuple[int, int], list[int]] = defaultdict(list)

    def add(self, i: int, x0: float, y0: float, x1: float, y1: float) -> None:
        c, p = self.cell, self.pad
        for gx in range(math.floor((min(x0, x1) - p) / c), math.floor((max(x0, x1) + p) / c) + 1):
            for gy in range(math.floor((min(y0, y1) - p) / c), math.floor((max(y0, y1) + p) / c) + 1):
                self.cells[(gx, gy)].append(i)

    def near(self, p: Pt) -> list[int]:
        return self.cells.get((math.floor(p[0] / self.cell), math.floor(p[1] / self.cell)), [])

    def in_box(self, x0: float, y0: float, x1: float, y1: float) -> set[int]:
        c = self.cell
        return {i for gx in range(math.floor(x0 / c), math.floor(x1 / c) + 1)
                for gy in range(math.floor(y0 / c), math.floor(y1 / c) + 1) for i in self.cells.get((gx, gy), ())}


class UF:
    def __init__(self):
        self.p: dict = {}

    def f(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def u(self, a, b) -> None:
        ra, rb = self.f(a), self.f(b)
        if ra != rb:
            self.p[ra] = rb


# ---------------------------------------------------------------- page reading

@dataclass
class Path:
    kind: str                 # "s" stroke, "f" fill, "fs" both
    stroke: RGB | None
    fill: RGB | None
    width: float
    segs: list[Seg]           # the line items
    curves: int               # how many curve items
    other: int                # rectangles, quads
    rect: tuple

    @property
    def only_lines(self) -> bool:
        return bool(self.segs) and not self.curves and not self.other


@dataclass
class PageData:
    number: int                              # 1-based
    paths: list[Path]
    anchors: dict[str, list[tuple]]          # invisible words -> boxes
    words: list[tuple]                       # (x0, y0, x1, y1, text, rgb, dir)


def read_page(page) -> PageData:
    paths = []
    for g in page.get_drawings():
        segs, curves, other = [], 0, 0
        for it in g["items"]:
            if it[0] == "l":
                segs.append(((it[1].x, it[1].y), (it[2].x, it[2].y)))
            elif it[0] == "c":
                curves += 1
            else:
                other += 1
        r = g["rect"]
        paths.append(Path(g.get("type") or "", _rgb(g.get("color")), _rgb(g.get("fill")), float(g.get("width") or 0),
                          segs, curves, other, (r.x0, r.y0, r.x1, r.y1)))
    anchors, words = sp.page_text_layer(page)
    return PageData(page.number + 1, paths, dict(anchors), words)


def anchor_ends(box: tuple) -> tuple[Pt, Pt]:
    """The two ends of the pin line an anchor box spans, along its long axis."""
    x0, y0, x1, y1 = box
    if (x1 - x0) >= (y1 - y0):
        return (x0, (y0 + y1) / 2), (x1, (y0 + y1) / 2)
    return ((x0 + x1) / 2, y0), ((x0 + x1) / 2, y1)


def label_hotspot(w: tuple) -> Pt:
    """Where a net label connects: the bottom corner of its text box on the side the text starts.

    Measured on one house style: the box's bottom edge (descent included) sits on the
    wire within 0.1 pt, the glyph baseline 1.4 pt above it. `dir` (1, 0) is normal
    text, (0, -1) reads upward, (0, 1) downward, (-1, 0) upside down.
    """
    x0, y0, x1, y1 = w[:4]
    d = w[6] if len(w) > 6 else (1.0, 0.0)
    if abs(d[0]) >= abs(d[1]):
        return (x0, y1) if d[0] >= 0 else (x1, y0)
    return (x1, y1) if d[1] < 0 else (x0, y0)


def _box_contains(box: tuple, p: Pt, slack: float) -> bool:
    return box[0] - slack <= p[0] <= box[2] + slack and box[1] - slack <= p[1] <= box[3] + slack


def _greyish(rgb: RGB) -> bool:
    return max(rgb) - min(rgb) <= 0.08


def _reddish(rgb: RGB) -> bool:
    return rgb[0] >= 0.8 and rgb[1] <= 0.45 and rgb[2] <= 0.45


def label_colour(rgb: RGB | None, st: Style) -> bool:
    if rgb is None:
        return False
    if st.label_rgbs:
        return any(col_eq(rgb, c, st.colour_tol) for c in st.label_rgbs)
    return not (_greyish(rgb) or _reddish(rgb) or col_eq(rgb, st.wire_rgb, st.colour_tol))


LABEL_TEXT = re.compile(r"(?=.*[A-Za-z0-9])\S+")  # a one-letter net name is legal


# ---------------------------------------------------------------- drawing classes

def is_wire(p: Path, st: Style) -> bool:
    return (p.kind == "s" and p.only_lines and col_eq(p.stroke, st.wire_rgb, st.colour_tol)
            and abs(p.width - st.wire_width) <= st.width_tol)


def is_dot(p: Path, st: Style, rgb: RGB | None = None) -> bool:
    return ("f" in p.kind and p.curves >= 2 and not p.segs and not p.other
            and (rgb is None or col_eq(p.fill, rgb, st.colour_tol))
            and (p.rect[2] - p.rect[0]) <= st.dot_max and (p.rect[3] - p.rect[1]) <= st.dot_max)


def is_port(p: Path, st: Style) -> bool:
    return (p.kind == "fs" and p.only_lines and 3 <= len(p.segs) <= 8
            and col_eq(p.fill, st.port_fill_rgb, st.colour_tol) and col_eq(p.stroke, st.port_stroke_rgb, st.colour_tol))


def is_power_stroke(p: Path, st: Style) -> bool:
    return p.kind == "s" and p.only_lines and col_eq(p.stroke, st.power_rgb, st.colour_tol) and p.width <= st.pin_max_width


def is_pin_line(p: Path, st: Style) -> bool:
    """Any thin single segment not in the wire style: pin lines take the symbol's own colour
    (black, blue on passives, grey when not fitted), so the anchor decides which is a pin."""
    return (p.kind == "s" and len(p.segs) == 1 and not p.curves and not p.other and p.width <= st.pin_max_width
            and not is_wire(p, st))


# ---------------------------------------------------------------- style detection

def detect_style(pages: list[PageData], base: Style = ALTIUM) -> tuple[Style, dict[str, str]]:
    """Re-measure the house style on this PDF. -> (style, field -> how it was set).

    - wire colour and width: the thin stroke class that links the most pins. A segment
      reaches a pin when one end lies in the outer half of the pin's anchor (the hot
      end sits there: on the measured style 0.6-6.4 pt in from the anchor's outer end,
      never outside it) and the other end outside the anchor; the outer half is the
      one away from the other pins of the part. A class's connected segments vote once
      per pin reached, and only when they reach two or more pins. Pin lines lie wholly
      inside their anchor, bodies meet the inner half, and a stray pin line reaches one
      pin: on the measured style, other stroke classes took about 1 vote in 5 without
      the last two rules and under 1 in 60 with them. With no anchors, the class whose
      ends land on junction dots;
    - junction dot colour: the fill of small filled circles lying on a wire end;
    - port flag border and fill: filled polygons with a wire end on an edge;
    - power-port colour: the non-wire stroke colour whose segment ends meet wire ends
      outside any pin anchor (needs anchors; pin lines would vote too without them);
    - label colours: the colours of visible words sitting on net-label anchors (`NL...`).
    Any field without enough evidence keeps the default.
    """
    basis = {k: f"default ({base.name})" for k in
             ("wire", "junction", "port", "power", "labels")}
    upd: dict[str, Any] = {}

    def cls(p: Path) -> tuple:
        return (p.stroke, round(p.width, 2))

    # wire class: votes from segments ending in the outer half of a pin anchor
    votes: Counter = Counter()
    for pd in pages:
        pins, _ = decode_pins(pd.anchors)
        by_part: dict[str, list[Pt]] = defaultdict(list)
        for pid, b in pins.items():
            by_part[sp.split_pin_id(pid)[0]].append(sp.centre(b))
        halves: list[tuple] = []      # (outer-half box, whole box)
        for pid, b in pins.items():
            ref = sp.split_pin_id(pid)[0]
            cs, co = by_part[ref], pd.anchors.get("CO" + ref)
            body = ((sum(x for x, _ in cs) / len(cs), sum(y for _, y in cs) / len(cs)) if len(cs) >= 2
                    else sp.centre(co[0]) if co else None)
            if body is None:
                continue
            x0, y0, x1, y1 = b
            cx, cy = sp.centre(b)
            if (x1 - x0) >= (y1 - y0):
                half = (x0, y0, cx, y1) if math.dist((x0, cy), body) > math.dist((x1, cy), body) else (cx, y0, x1, y1)
            else:
                half = (x0, y0, x1, cy) if math.dist((cx, y0), body) > math.dist((cx, y1), body) else (x0, cy, x1, y1)
            halves.append((half, b))
        if not halves:
            continue
        hg = Grid(cell=16.0, pad=1.0)
        for i, (_h, b) in enumerate(halves):
            hg.add(i, *b)
        by_cls: dict[tuple, list[Seg]] = defaultdict(list)
        for p in pd.paths:
            if p.kind == "s" and p.only_lines and p.width <= 2.0:
                by_cls[cls(p)] += [s for s in p.segs if axis_aligned(s)]
        for key, segs in by_cls.items():
            # a class's segments joined end-on-segment; a component votes once per pin it reaches, and only
            # when it reaches two or more: a wire links pins, a stray pin line or body stroke touches one
            sg = Grid(pad=1.5)
            for i, (a, b) in enumerate(segs):
                sg.add(i, a[0], a[1], b[0], b[1])
            uf = UF()
            for i, s in enumerate(segs):
                uf.f(i)
                for q in s:
                    for j in sg.near(q):
                        if j != i and on_segment(q, segs[j], 0.8):
                            uf.u(i, j)
            reach: dict[int, set[int]] = defaultdict(set)
            for i, s in enumerate(segs):
                for q, other in ((s[0], s[1]), (s[1], s[0])):
                    for k in hg.near(q):
                        if _box_contains(halves[k][0], q, 1.0) and not _box_contains(halves[k][1], other, 1.0):
                            reach[uf.f(i)].add(k)
            votes[key] += sum(len(ks) for ks in reach.values() if len(ks) >= 2)
    how = "pins linked"
    if sum(votes.values()) < 3:
        votes, how = Counter(), "junction dots"
        for pd in pages:
            dots = [((p.rect[0] + p.rect[2]) / 2, (p.rect[1] + p.rect[3]) / 2) for p in pd.paths if is_dot(p, base)]
            if not dots:
                continue
            g = Grid(pad=1.5)
            for i, d in enumerate(dots):
                g.add(i, d[0], d[1], d[0], d[1])
            for p in (q for q in pd.paths if q.kind == "s" and q.only_lines and q.width <= 2.0):
                for s in p.segs:
                    for q in s:
                        if any(near(q, dots[i], 1.0) for i in g.near(q)):
                            votes[cls(p)] += 1
    if sum(votes.values()) >= 3:
        (rgb, w), n = votes.most_common(1)[0]
        upd.update(wire_rgb=rgb, wire_width=w)
        basis["wire"] = f"detected from {how} ({n} of {sum(votes.values())} votes)"
    st = replace(base, **upd)

    # junction dots, ports, power colour: all measured against the wire class just found
    jv: Counter = Counter()
    pv: Counter = Counter()
    wv: Counter = Counter()
    for pd in pages:
        wires = [s for p in pd.paths if is_wire(p, st) for s in p.segs]
        if not wires:
            continue
        wends = [q for s in wires for q in s]
        eg = Grid(pad=1.5)
        for i, q in enumerate(wends):
            eg.add(i, q[0], q[1], q[0], q[1])

        def at_wire_end(q: Pt, tol: float = 1.0) -> bool:
            return any(near(q, wends[i], tol) for i in eg.near(q))

        for p in pd.paths:
            if is_dot(p, st):
                c = ((p.rect[0] + p.rect[2]) / 2, (p.rect[1] + p.rect[3]) / 2)
                if at_wire_end(c):
                    jv[p.fill] += 1
            elif p.kind == "fs" and p.only_lines and 3 <= len(p.segs) <= 8 and (p.rect[3] - p.rect[1]) <= 25:
                edges = p.segs
                if any(on_segment(q, e, st.port_snap) for q in wends if _box_contains(p.rect, q, 1.5) for e in edges):
                    pv[(p.stroke, p.fill)] += 1
        pin_boxes = [b for t, bs in pd.anchors.items() if t.startswith("PI") for b in bs]
        if pin_boxes:
            bg = Grid(cell=16.0, pad=1.0)
            for i, b in enumerate(pin_boxes):
                bg.add(i, *b)
            for p in pd.paths:
                if p.kind != "s" or not p.only_lines or p.width > 2.0 or is_wire(p, st):
                    continue
                for s in p.segs:
                    for q in s:
                        if at_wire_end(q) and not any(_box_contains(pin_boxes[i], q, 1.0) for i in bg.near(q)):
                            wv[p.stroke] += 1
    if jv:
        rgb, n = jv.most_common(1)[0]
        upd["junction_rgb"] = rgb
        basis["junction"] = f"detected from dots on wire ends ({n})"
    if sum(pv.values()) >= 2:
        (stroke, fill), n = pv.most_common(1)[0]
        upd.update(port_stroke_rgb=stroke, port_fill_rgb=fill)
        basis["port"] = f"detected from flags touching wires ({n})"
    if sum(wv.values()) >= 3:
        rgb, n = wv.most_common(1)[0]
        upd["power_rgb"] = rgb
        basis["power"] = f"detected from strokes meeting wire ends outside pins ({n})"

    # label colours from net-label anchors
    lv: Counter = Counter()
    for pd in pages:
        nls = [b for t, bs in pd.anchors.items() if t.startswith("NL") for b in bs]
        for b in nls:
            c = ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
            for w in pd.words:
                if _box_contains(w, c, 1.0) and w[5] is not None:
                    lv[w[5]] += 1
                    break
    if lv:
        upd["label_rgbs"] = tuple(sorted(lv))
        basis["labels"] = f"detected from net-label anchors ({sum(lv.values())} labels, {len(lv)} colours)"
    return replace(base, **upd), basis


# ---------------------------------------------------------------- pin identity

def decode_pins(anchors: dict[str, list[tuple]]) -> tuple[dict[str, tuple], set[str]]:
    """No outline: pin id -> anchor box from the tokens alone. -> (boxes, ids whose split was a guess).

    `PI` + enc(ref) + `0` + enc(pin) is split at the longest designator a `CO` anchor
    on the page names; ids use the encoded spelling (`_` and `-` read as `0`).
    """
    refs = {t[2:] for t in anchors if t.startswith("CO") and len(t) > 2}
    boxes: dict[str, tuple] = {}
    guessed: set[str] = set()
    for tok in sorted(anchors):
        if not tok.startswith("PI"):
            continue
        body = tok[2:]
        cands = sorted((r for r in refs if body.startswith(r + "0") and len(body) > len(r) + 1), key=len)
        if cands:
            ref, pin = cands[-1], body[len(cands[-1]) + 1:]
            doubt = len(cands) > 1
        else:
            m = re.fullmatch(r"([A-Za-z]+\d+?)0(.+)", body)
            if not m:
                continue
            ref, pin, doubt = m.group(1), m.group(2), True
        for k, b in enumerate(anchors[tok]):
            pid = f"{ref}-{pin}" if k == 0 else f"{ref}-{pin}#{k + 1}"
            boxes[pid] = b
            if doubt or len(anchors[tok]) > 1:
                guessed.add(pid)
    return boxes, guessed


def hinted_pins(parts: dict[str, list[tuple[str, str]]], anchors: dict[str, list[tuple]]
                ) -> tuple[dict[str, tuple], dict[str, str], set[str], list[str]]:
    """Outline pin list -> (pin id -> box, pin id -> symbol part, ids resolved among shared tokens, unanchored ids)."""
    boxes, _issues = sp.resolve_pin_boxes(parts, anchors)
    counts = Counter(sp.pin_token(ph, pin) for pins in parts.values() for ph, pin in pins)
    out: dict[str, tuple] = {}
    owner: dict[str, str] = {}
    shared: set[str] = set()
    missing: list[str] = []
    for sref, pins in parts.items():
        for ph, pin in pins:
            pid = f"{ph}-{pin}"
            owner[pid] = sref
            if (sref, pin) in boxes:
                out[pid] = boxes[(sref, pin)]
                if counts[sp.pin_token(ph, pin)] > 1:
                    shared.add(pid)
            else:
                missing.append(pid)
    return out, owner, shared, sorted(set(missing), key=sp.natural_key)


# ---------------------------------------------------------------- one page

def trace_page(pd: PageData, st: Style, parts: dict[str, list[tuple[str, str]]] | None = None) -> dict[str, Any]:
    """Nets of one page from its geometry. `parts` (sheet ref -> [(physical ref, pin)]) names the pins."""
    wires: list[Seg] = [s for p in pd.paths if is_wire(p, st) for s in p.segs]
    wg = Grid(pad=1.5)
    for i, (a, b) in enumerate(wires):
        wg.add(i, a[0], a[1], b[0], b[1])

    def wires_at(q: Pt, tol: float) -> list[int]:
        return [i for i in wg.near(q) if on_segment(q, wires[i], tol)]

    wends = [q for s in wires for q in s]
    eg = Grid(pad=1.5)
    for i, q in enumerate(wends):
        eg.add(i, q[0], q[1], q[0], q[1])
    dots = [((p.rect[0] + p.rect[2]) / 2, (p.rect[1] + p.rect[3]) / 2) for p in pd.paths if is_dot(p, st, st.junction_rgb)]
    ports = [p for p in pd.paths if is_port(p, st)]
    pstrokes: list[Seg] = [s for p in pd.paths if is_power_stroke(p, st) for s in p.segs]
    pends = [q for s in pstrokes for q in s]
    pin_lines: list[Seg] = [p.segs[0] for p in pd.paths if is_pin_line(p, st)]
    plg = Grid(cell=16.0, pad=0.5)
    for i, (a, b) in enumerate(pin_lines):
        plg.add(i, a[0], a[1], b[0], b[1])

    def touches(q: Pt) -> bool:
        """Something that carries a connection is drawn at `q`: a wire, a port edge or a power-port stroke end."""
        if wires_at(q, st.pin_snap):
            return True
        if any(near(q, e, st.power_snap) for e in pends):
            return True
        return any(on_segment(q, e, st.port_snap) for p in ports if _box_contains(p.rect, q, 1.5) for e in p.segs)

    # ---- pin identity and hot ends
    if parts is not None:
        boxes, owner, doubtful, unanchored = hinted_pins(parts, pd.anchors)
        ident = "anchor"
    else:
        boxes, doubtful = decode_pins(pd.anchors)
        owner = {pid: sp.split_pin_id(pid)[0] for pid in boxes}
        unanchored, ident = [], "anchor-decoded"
    by_part: dict[str, list[Pt]] = defaultdict(list)
    for pid, b in boxes.items():
        by_part[owner[pid]].append(sp.centre(b))
    body: dict[str, Pt] = {}
    for sref, cs in by_part.items():
        if len(cs) >= 2:
            body[sref] = (sum(x for x, _ in cs) / len(cs), sum(y for _, y in cs) / len(cs))
        elif pd.anchors.get("CO" + sp.enc(sref)):
            body[sref] = sp.centre(pd.anchors["CO" + sp.enc(sref)][0])
    pins: dict[str, dict[str, Any]] = {}
    claimed: set[int] = set()
    for pid in sorted(boxes, key=sp.natural_key):
        x0, y0, x1, y1 = boxes[pid]
        long_x = (x1 - x0) > 2 * (y1 - y0)
        long_y = (y1 - y0) > 2 * (x1 - x0)
        c = sp.centre(boxes[pid])
        k = st.anchor_slack
        best, best_sc = None, None
        for i in sorted(plg.in_box(x0 - k, y0 - k, x1 + k, y1 + k)):
            s = pin_lines[i]
            if not (st.pin_len[0] <= seg_len(s) <= st.pin_len[1]) or not axis_aligned(s):
                continue
            (ax, ay), (bx, by) = s
            lx0, lx1 = sorted((ax, bx))
            ly0, ly1 = sorted((ay, by))
            if lx1 < x0 - k or lx0 > x1 + k or ly1 < y0 - k or ly0 > y1 + k:
                continue
            if (long_x and abs(ay - by) > 0.1) or (long_y and abs(ax - bx) > 0.1):
                continue
            ov = (min(lx1, x1 + k) - max(lx0, x0 - k)) + (min(ly1, y1 + k) - max(ly0, y0 - k))
            sc = (1 if (wires_at(s[0], st.pin_snap) or wires_at(s[1], st.pin_snap)) else 0, ov)
            if best_sc is None or sc > best_sc:
                best, best_sc = i, sc
        basis = [ident if pid not in doubtful else ident + "-shared"]
        if best is not None:
            claimed.add(best)
            a, b = pin_lines[best]
            basis.append("pin-line")
        else:
            # no pin line drawn where the anchor is: the anchor's own ends overhang the hot end (by up to
            # 6.4 pt on the measured style), so a wire end inside the anchor box, if any, is the hot end
            a, b = anchor_ends(boxes[pid])
            ins = [wends[i] for i in sorted(eg.in_box(*boxes[pid])) if _box_contains(boxes[pid], wends[i], 1.0)]
            if ins:
                bc = body.get(owner[pid], c)
                a = b = max(ins, key=lambda q: (math.dist(q, bc), q))
                basis.append("anchor-box-wire-end")
            else:
                basis.append("anchor-box")
        ta, tb = (True, True) if a == b else (touches(a), touches(b))
        if a == b:
            hot = a                         # the wire end found inside the anchor box
        elif ta != tb:
            hot = a if ta else b
            basis.append("hot-touch")
        else:
            bc = body.get(owner[pid], c)
            hot = a if math.dist(a, bc) > math.dist(b, bc) else b
            basis.append("hot-body")
        if pid in doubtful:
            conf = "low"
        elif basis[1:] == ["pin-line", "hot-touch"]:
            conf = "high"
        else:
            conf = "medium"
        pins[pid] = {"at": [round(hot[0], 2), round(hot[1], 2)], "conf": conf, "basis": basis, "part": owner[pid]}
    hot_of = {pid: (P["at"][0], P["at"][1]) for pid, P in pins.items()}
    pg = Grid(pad=1.5)
    plist = list(hot_of.items())
    for i, (_, q) in enumerate(plist):
        pg.add(i, q[0], q[1], q[0], q[1])

    def pins_at(q: Pt, tol: float) -> list[str]:
        return [plist[i][0] for i in pg.near(q) if near(q, plist[i][1], tol)]

    # ---- union-find over wires and pins
    uf = UF()
    for i in range(len(wires)):
        uf.f(("w", i))
    for i, s in enumerate(wires):          # an endpoint on another wire: end to end or a T; crossings do not connect
        for q in s:
            for j in wires_at(q, st.snap):
                if j != i:
                    uf.u(("w", i), ("w", j))
    for d in dots:                          # a junction dot connects every wire through it
        through = wires_at(d, st.snap)
        for j in through[1:]:
            uf.u(("w", through[0]), ("w", j))
    for pid, q in hot_of.items():
        uf.f(("p", pid))
        for j in wires_at(q, st.pin_snap):
            uf.u(("p", pid), ("w", j))
        for other in pins_at(q, st.snap):
            if other != pid:
                uf.u(("p", pid), ("p", other))

    names: list[tuple[Any, str, str]] = []  # (node, kind, name)

    # ---- ports: everything on the flag's edges joins; the text inside names it
    port_rects = [p.rect for p in ports]
    n_ports_connected = 0
    for p in ports:
        r = p.rect
        txt = " ".join(w[4] for w in sorted(pd.words, key=lambda w: (w[0], w[1]))
                       if _box_contains(r, sp.centre(w), 0.5))
        hits: list[Any] = []
        for i, q in enumerate(wends):
            if _box_contains(r, q, 1.5) and any(on_segment(q, e, st.port_snap) for e in p.segs):
                hits.append(("w", i // 2))
        for pid, q in hot_of.items():
            if _box_contains(r, q, 1.5) and any(on_segment(q, e, st.port_snap) for e in p.segs):
                hits.append(("p", pid))
        if hits:
            n_ports_connected += 1
            for h in hits[1:]:
                uf.u(hits[0], h)
            if txt:
                names.append((hits[0], "port", txt))

    # ---- net labels: a label-coloured word whose hotspot lies on a wire or a pin end
    free_words: list[tuple] = []
    n_labels = 0
    for w in pd.words:
        if any(_box_contains(r, sp.centre(w), 0.5) for r in port_rects):
            continue
        if not label_colour(w[5], st) or not LABEL_TEXT.fullmatch(w[4]):
            if col_eq(w[5], st.power_rgb, st.colour_tol):
                free_words.append(w)
            continue
        h = label_hotspot(w)
        hit = wires_at(h, st.label_snap)
        node: Any = ("w", hit[0]) if hit else None
        if node is None:
            ps = pins_at(h, st.label_snap)
            node = ("p", ps[0]) if ps else None
        if node is not None:
            names.append((node, "label", w[4]))
            n_labels += 1
        elif col_eq(w[5], st.power_rgb, st.colour_tol):
            free_words.append(w)

    # ---- power ports: groups of power strokes with an end on a wire end or a pin end
    guf = UF()
    for i in range(len(pstrokes)):
        guf.f(i)
    sboxes = [(min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])) for a, b in pstrokes]
    for i in range(len(pstrokes)):
        for j in range(i):
            if box_gap(sboxes[i], sboxes[j]) <= st.power_gap:
                guf.u(i, j)
    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(pstrokes)):
        groups[guf.f(i)].append(i)
    power: list[dict[str, Any]] = []
    for idx in groups.values():
        pts = [q for i in idx for q in pstrokes[i]]
        gb = (min(q[0] for q in pts), min(q[1] for q in pts), max(q[0] for q in pts), max(q[1] for q in pts))
        if gb[2] - gb[0] > st.power_max or gb[3] - gb[1] > st.power_max:
            continue  # a symbol body drawn in the power colour
        hits = []
        for q in pts:
            hits += [("w", i // 2) for i in eg.near(q) if near(q, wends[i], st.power_snap)]
            hits += [("p", pid) for pid in pins_at(q, st.power_snap)]
        if not hits:
            continue
        horiz = sum(1 for i in idx if abs(pstrokes[i][0][1] - pstrokes[i][1][1]) < 0.1)
        power.append({"box": gb, "hits": sorted(set(hits), key=str),
                      "shape": "bars" if horiz >= 3 else ("arrow" if len(idx) >= 3 else "other"), "name": None})
    used: set[int] = set()
    for P in sorted(power, key=lambda P: (P["box"][1], P["box"][0])):
        best = None
        for wi, w in enumerate(free_words):
            if wi in used:
                continue
            dd = box_gap(P["box"], w[:4])
            if dd <= st.power_name_radius and (best is None or dd < best[0]):
                best = (dd, wi)
        if best:
            used.add(best[1])
            P["name"] = free_words[best[1]][4]
    # A bar-style ground whose name is hidden takes the name of the page's one named bar-style
    # ground [inference, measured on one house style: correct on every such symbol seen there].
    bars = {P["name"] for P in power if P["name"] and P["shape"] == "bars"}
    n_inferred = 0
    for P in power:
        if P["name"] is None and P["shape"] == "bars" and len(bars) == 1:
            P["name"], P["inferred"] = next(iter(bars)), True
            n_inferred += 1
    for P in power:
        for h in P["hits"][1:]:
            uf.u(P["hits"][0], h)
        if P["name"]:
            names.append((P["hits"][0], "power", P["name"]))

    # ---- the same name on one page is one net
    first: dict[str, Any] = {}
    for node, _kind, nm in names:
        if nm in first:
            uf.u(first[nm], node)
        else:
            first[nm] = node
    by_root: dict[Any, dict[str, set]] = defaultdict(lambda: {"label": set(), "port": set(), "power": set()})
    for node, kind, nm in names:
        by_root[uf.f(node)][kind].add(nm)
    clusters: dict[Any, list[str]] = defaultdict(list)
    for pid in pins:
        clusters[uf.f(("p", pid))].append(pid)
    nets = []
    for root, ps in clusters.items():
        nm = by_root.get(root, {"label": set(), "port": set(), "power": set()})
        nets.append({"pins": sorted(ps, key=sp.natural_key), "labels": sorted(nm["label"]),
                     "ports": sorted(nm["port"]), "power": sorted(nm["power"])})
    nets.sort(key=lambda n: sp.natural_key(n["pins"][0]))
    confs = Counter(P["conf"] for P in pins.values())
    counts = {
        "wire_segments": len(wires), "junction_dots": len(dots), "pin_lines": len(pin_lines),
        "pin_lines_claimed": len(claimed), "pins": len(pins), "pins_unanchored": len(unanchored),
        "pins_high": confs["high"], "pins_medium": confs["medium"], "pins_low": confs["low"],
        "labels_on_wire": n_labels, "ports": len(ports), "ports_connected": n_ports_connected,
        "power_ports": sum(1 for P in power if P["name"]), "power_ports_inferred_name": n_inferred,
        "power_ports_unnamed": sum(1 for P in power if not P["name"]), "nets": len(nets),
    }
    return {"page": pd.number, "traced": bool(wires), "pins": pins, "unanchored": unanchored, "nets": nets,
            "counts": counts}


# ---------------------------------------------------------------- the whole PDF

def trace(pdf, outline: dict[str, dict[str, Any]] | None = None, style: Style | None = None,
          detect: bool = True) -> dict[str, Any]:
    """Nets per sheet from the drawn geometry of a vector schematic PDF.

    `outline` is `smartpdf.read_outline`'s result; only its sheet -> page map and each
    sheet's component pin list are used (to name the pins), never its nets. Without
    it every page is a sheet and pins are named from the anchor tokens alone.
    -> {"style", "style_basis", "sheets" {sheet: page}, "pages" {page: trace_page result}}.
    """
    import pymupdf

    own = not hasattr(pdf, "page_count")  # a path, or an open document the caller keeps
    doc = pymupdf.open(pdf) if own else pdf
    try:
        if outline:
            sheets = {s: S["page"] for s, S in outline.items() if S.get("page") and S["page"] <= doc.page_count}
        else:
            sheets = {f"page{n}": n for n in range(1, doc.page_count + 1)}
        parts_by_page: dict[int, dict[str, list[tuple[str, str]]]] = defaultdict(dict)
        if outline:
            for s, pg in sheets.items():
                parts_by_page[pg].update(outline[s]["components"])
        pages = {pg: read_page(doc[pg - 1]) for pg in sorted(set(sheets.values()))}
    finally:
        if own:
            doc.close()
    st, basis = detect_style(list(pages.values()), style or ALTIUM) if detect else (style or ALTIUM, {})
    out = {pg: trace_page(pd, st, parts_by_page[pg] if outline else None) for pg, pd in pages.items()}
    return {"style": style_dict(st), "style_basis": basis, "sheets": sheets, "pages": out}


# ---------------------------------------------------------------- cross-check against the embedded netlist

def _pairs(n: int) -> int:
    return n * (n - 1) // 2


def _fmt_at(at: list[float] | None) -> str:
    return f"({at[0]:.1f}, {at[1]:.1f})" if at else "(?)"


def crosscheck(outline: dict[str, dict[str, Any]], tr: dict[str, Any]) -> dict[str, Any]:
    """Per sheet net and per pin pair: does the geometry partition the located pins as the outline does?

    A sheet net (the outline's net on one sheet, with at least one pin there):
    - `agree`: every pin located, all in one geometry net that holds no other pin;
    - `disagree`: its located pins are split over several geometry nets, or their
      geometry net also holds pins of another outline net (a false pin pair);
    - `geometry-missing`: no disagreement, but a pin was not located (no anchor), or the
      page has no wires to trace.
    A board net rolls up its sheet nets: any disagree -> disagree, else any missing -> missing.
    Pairs count pins both located on one page: `false_pairs` joined by geometry but not by
    the outline, `missed_pairs` the reverse.
    """
    sheet_rows: list[dict[str, Any]] = []
    pin_status: dict[str, str] = {}
    issues: list[str] = []
    tot = Counter()
    by_page: dict[int, list[str]] = defaultdict(list)
    for s, pg in tr["sheets"].items():
        by_page[pg].append(s)
    for pg in sorted(by_page):
        T = tr["pages"].get(pg) or tr["pages"].get(str(pg))
        if T is None:
            continue
        truth: dict[str, set[str]] = defaultdict(set)
        sheet_of: dict[str, str] = {}
        for s in sorted(by_page[pg], key=sp.natural_key):
            for net, N in outline[s]["nets"].items():
                if N["pins"]:
                    truth[net] |= set(N["pins"])
                    sheet_of.setdefault(net, s)
        if not truth:
            continue
        g_of: dict[str, int] = {}
        if T["traced"]:
            for gi, n in enumerate(T["nets"]):
                for p in n["pins"]:
                    g_of[p] = gi
        t_of = {p: net for net, ps in truth.items() for p in ps}
        located = {p for p in t_of if p in g_of}
        cell: Counter = Counter((t_of[p], g_of[p]) for p in located)
        t_n: Counter = Counter(t_of[p] for p in located)
        g_n: Counter = Counter(g_of[p] for p in located)
        agree_pairs = sum(_pairs(n) for n in cell.values())
        tot["pairs"] += sum(_pairs(n) for n in t_n.values())
        tot["geometry_pairs"] += sum(_pairs(n) for n in g_n.values())
        tot["false_pairs"] += sum(_pairs(n) for n in g_n.values()) - agree_pairs
        tot["missed_pairs"] += sum(_pairs(n) for n in t_n.values()) - agree_pairs
        at = {p: T["pins"][p]["at"] for p in located}
        for net in sorted(truth, key=sp.natural_key):
            ps = truth[net]
            loc = sorted((p for p in ps if p in located), key=sp.natural_key)
            gs = sorted({g_of[p] for p in loc})
            extra = sorted({q for g in gs for q in T["nets"][g]["pins"] if q in t_of and t_of[q] != net},
                           key=sp.natural_key)
            if not loc:
                status = MISSING
            elif len(gs) > 1 or extra:
                status = DISAGREE
            elif len(loc) < len(ps):
                status = MISSING
            else:
                status = AGREE
            gnames = sorted({x for g in gs for k in ("labels", "ports", "power") for x in T["nets"][g][k]})
            row = {"sheet": sheet_of[net], "page": pg, "net": net, "status": status, "pins": len(ps),
                   "located": len(loc), "geometry_nets": len(gs), "named": net in gnames}
            if status == DISAGREE:
                parts = []
                if len(gs) > 1:
                    groups = [[p for p in loc if g_of[p] == g] for g in gs]
                    parts.append("split into " + " | ".join(
                        " ".join(f"{p}{_fmt_at(at[p])}" for p in grp[:4]) + (" ..." if len(grp) > 4 else "")
                        for grp in groups))
                    row["split"] = [grp for grp in groups]
                if extra:
                    others = sorted({t_of[q] for q in extra}, key=sp.natural_key)
                    parts.append("joined with " + ", ".join(f"{q}{_fmt_at(at[q])} of {t_of[q]}" for q in extra[:4])
                                 + (" ..." if len(extra) > 4 else "") + f" (net{'s' if len(others) > 1 else ''} "
                                 + ", ".join(others) + ")")
                    row["joined_with"] = extra
                issues.append(f"geometry disagrees on net {net}, sheet {sheet_of[net]} page {pg}: " + "; ".join(parts))
            sheet_rows.append(row)
            tot["sheet_nets"] += 1
            tot[f"sheet_nets_{status}"] += 1
            tot["sheet_nets_named"] += row["named"] and status == AGREE
        for p, net in t_of.items():
            if p not in located:
                pin_status[p] = MISSING
                continue
            same_t = {q for q in truth[net] if q in located}
            same_g = {q for q in T["nets"][g_of[p]]["pins"] if q in located}
            pin_status[p] = AGREE if same_t == same_g else DISAGREE
        if T["unanchored"]:
            issues.append(f"geometry: {len(T['unanchored'])} pin(s) on page {pg} have no anchor word, so no position: "
                          + ", ".join(T["unanchored"][:12]) + (" ..." if len(T["unanchored"]) > 12 else ""))
    net_status: dict[str, str] = {}
    for row in sheet_rows:
        cur = net_status.get(row["net"])
        rank = {AGREE: 0, MISSING: 1, DISAGREE: 2}
        if cur is None or rank[row["status"]] > rank[cur]:
            net_status[row["net"]] = row["status"]
    for st_ in (AGREE, DISAGREE, MISSING):
        tot[f"nets_{st_}"] = sum(1 for v in net_status.values() if v == st_)
        tot[f"pins_{st_}"] = sum(1 for v in pin_status.values() if v == st_)
    counts = {k.replace("-", "_"): int(v) for k, v in tot.items()}
    for k in ("sheet_nets", "sheet_nets_agree", "sheet_nets_disagree", "sheet_nets_geometry_missing",
              "sheet_nets_named", "pairs", "geometry_pairs", "false_pairs", "missed_pairs"):
        counts.setdefault(k, 0)
    return {"sheet_nets": sheet_rows, "nets": net_status, "pins": pin_status, "counts": counts, "issues": issues}


def apply(nl: dict[str, Any], xc: dict[str, Any], tr: dict[str, Any]) -> None:
    """Mark the netlist (in place) with the cross-check; never change its membership.

    `method` gains `+geometry` when at least one net agrees and none disagrees.
    """
    for net, N in nl["nets"].items():
        if net in xc["nets"]:
            N["crosscheck"] = xc["nets"][net]
    for ref, C in nl["components"].items():
        for pin, P in C["pins"].items():
            s = xc["pins"].get(f"{ref}-{pin}")
            if s:
                P["crosscheck"] = s
    c = dict(xc["counts"])
    pages = list(tr["pages"].values())
    c["pages_traced"] = sum(1 for T in pages if T["traced"])
    c["pins_unanchored"] = sum(T["counts"]["pins_unanchored"] for T in pages)
    for conf in ("high", "medium", "low"):
        c[f"pin_conf_{conf}"] = sum(T["counts"][f"pins_{conf}"] for T in pages)
    nl["counts"]["geometry"] = dict(sorted(c.items()))
    nl["issues"] = sorted(set(nl["issues"]) | set(xc["issues"]), key=sp.natural_key)
    if c.get("nets_agree", 0) and not c.get("nets_disagree", 0):
        if not nl["method"].endswith("+" + GEOMETRY):
            nl["method"] = nl["method"] + "+" + GEOMETRY


def report(tr: dict[str, Any], xc: dict[str, Any] | None, board: str, source: dict[str, Any]) -> dict[str, Any]:
    """`boards/<label>/geometry.json`: the trace, per-pin confidence, unanchored pins, the cross-check rows."""
    out = {"board": board, "source": source, "style": tr["style"], "style_basis": tr["style_basis"],
           "sheets": dict(sorted(tr["sheets"].items(), key=lambda kv: sp.natural_key(kv[0]))),
           "pages": {str(pg): T for pg, T in sorted(tr["pages"].items())}}
    if xc is not None:
        out["crosscheck"] = {"counts": xc["counts"], "sheet_nets": xc["sheet_nets"]}
    return out

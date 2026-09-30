"""geom: rung 3, nets traced from the drawn geometry of a synthetic vector schematic.

The fixture is drawn here with PyMuPDF in an Altium-like house style: wires as
stroked paths, pins as thin segments each spanned by an invisible (render mode 3)
`PI<ref>0<pin>` anchor, `CO<ref>` and `NL<net>` anchors, net labels whose text
bottom sits on the wire, a port flag, bar-style ground symbols (one with its name
hidden), a T-junction, a plain crossing that must not connect, a crossing with a
junction dot that must, and a net that spans two sheets through a port. The
outline (bookmarks) carries the truth netlist, so `smartpdf.parse` cross-checks.
"""
from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass, replace
from pathlib import Path

import pymupdf
import pytest

from embarch_atlas import cli, geom, smartpdf as sp

COUR, HELV = pymupdf.Font("cour"), pymupdf.Font("helv")
FS = 5.0


@dataclass(frozen=True)
class House:
    """The colours and widths a synthetic export draws with."""
    wire: tuple = (0.0, 0.0, 0.502)
    wire_w: float = 0.72
    dot: tuple = (0.0, 0.0, 0.502)
    label: tuple = (0.502, 0.0, 0.0)
    power: tuple = (0.502, 0.0, 0.0)
    port_stroke: tuple = (0.502, 0.0, 0.0)
    port_fill: tuple = (1.0, 1.0, 0.502)


ALT = House()
OTHER = House(wire=(0.0, 0.4, 0.0), wire_w=1.0, dot=(0.0, 0.4, 0.0), label=(0.0, 0.0, 0.8), power=(0.6, 0.3, 0.0),
              port_stroke=(0.0, 0.3, 0.3), port_fill=(0.8, 1.0, 1.0))


class Sheet:
    def __init__(self, page, house: House, drop: set[str], bare: set[str]):
        self.page, self.h, self.drop, self.bare = page, house, drop, bare

    def wire(self, *pts):
        self.page.draw_polyline(pts, color=self.h.wire, width=self.h.wire_w)

    def dot(self, c):
        self.page.draw_circle(c, 1.5, color=self.h.dot, fill=self.h.dot, width=0.24)

    def pin(self, ref: str, num: str, hot, body):
        """A pin line from its hot end to the body, spanned exactly by its invisible anchor."""
        tok = sp.pin_token(ref, num)
        if tok not in self.bare:
            self.page.draw_line(hot, body, color=(0, 0, 0), width=0.72)
        if tok in self.drop:
            return
        L = math.dist(hot, body)
        fs = L / pymupdf.get_text_length(tok, fontname="cour", fontsize=1)
        off = fs * (COUR.ascender + COUR.descender) / 2
        if hot[1] == body[1]:
            self.page.insert_text((min(hot[0], body[0]), hot[1] + off), tok, fontname="cour", fontsize=fs, render_mode=3)
        else:
            self.page.insert_text((hot[0] + off, max(hot[1], body[1])), tok, fontname="cour", fontsize=fs,
                                  render_mode=3, rotate=90)

    def resistor(self, ref: str, a, vertical: bool = False):
        """Two-pin part: pin 1's hot end at `a`, 10 pt pins, a 20 pt body; returns pin 2's hot end."""
        x, y = a
        if vertical:
            self.pin(ref, "1", (x, y), (x, y + 10))
            self.page.draw_rect((x - 4, y + 10, x + 4, y + 30), color=(0, 0, 0), width=0.72)
            self.pin(ref, "2", (x, y + 40), (x, y + 30))
            self.designator(ref, (x + 6, y + 22))
            return (x, y + 40)
        self.pin(ref, "1", (x, y), (x + 10, y))
        self.page.draw_rect((x + 10, y - 4, x + 30, y + 4), color=(0, 0, 0), width=0.72)
        self.pin(ref, "2", (x + 40, y), (x + 30, y))
        self.designator(ref, (x + 14, y - 6))
        return (x + 40, y)

    def designator(self, ref: str, at):
        self.page.insert_text(at, "CO" + sp.enc(ref), fontname="cour", fontsize=3, render_mode=3)
        self.page.insert_text(at, ref, fontname="helv", fontsize=4)

    def label(self, name: str, at, anchor: bool = True):
        """Net label: the bottom of its text box on the wire, text starting at `at`."""
        base = (at[0], at[1] + HELV.descender * FS)
        if anchor:
            self.page.insert_text(base, "NL" + sp.enc(name), fontname="cour", fontsize=FS, render_mode=3)
        self.page.insert_text(base, name, fontname="helv", fontsize=FS, color=self.h.label)

    def vlabel(self, name: str, at):
        """A net label reading upward: the bottom of its glyphs (their right side) on a vertical wire."""
        base = (at[0] + HELV.descender * FS, at[1])
        self.page.insert_text(base, "NL" + sp.enc(name), fontname="cour", fontsize=FS, render_mode=3, rotate=90)
        self.page.insert_text(base, name, fontname="helv", fontsize=FS, color=self.h.label, rotate=90)

    def ground(self, at, name: str | None = "GND"):
        x, y = at
        c, w = self.h.power, 0.72
        self.page.draw_line((x, y), (x, y + 5), color=c, width=w)
        for i, half in enumerate((6, 4, 2)):
            self.page.draw_line((x - half, y + 5 + 2 * i), (x + half, y + 5 + 2 * i), color=c, width=w)
        if name:
            self.page.insert_text((x - 8, y + 17), name, fontname="helv", fontsize=FS, color=c)

    def port(self, name: str, left_mid):
        x, y = left_mid
        pts = [(x, y - 4), (x + 34, y - 4), (x + 40, y), (x + 34, y + 4), (x, y + 4)]
        self.page.draw_polyline(pts, color=self.h.port_stroke, fill=self.h.port_fill, width=0.24, closePath=True)
        self.page.insert_text((x + 3, y + 2), name, fontname="helv", fontsize=FS, color=self.h.port_stroke)


def draw_main(S: Sheet, wires: bool = True, bridge: bool = False) -> None:
    pg = S.page
    pg.draw_rect((200, 100, 260, 180), color=(0, 0, 0), width=0.72)  # U1's body
    S.pin("U1", "1", (180, 120), (200, 120))
    S.pin("U1", "2", (180, 160), (200, 160))
    S.pin("U1", "3", (280, 120), (260, 120))
    S.pin("U1", "4", (280, 160), (260, 160))
    S.designator("U1", (205, 95))
    for num, at in (("1", (185, 117)), ("2", (185, 157)), ("3", (268, 117)), ("4", (268, 157))):
        pg.insert_text(at, num, fontname="helv", fontsize=4)  # pin numbers: black text, never labels
    S.resistor("R1", (60, 120))                      # R1-2 hot at (100, 120)
    S.resistor("R2", (340, 140), vertical=True)      # R2-1 (340, 140), R2-2 (340, 180)
    S.resistor("R3", (400, 120))                     # R3-1 (400, 120), R3-2 (440, 120)
    S.resistor("R4", (120, 220), vertical=True)      # R4-1 (120, 220), R4-2 (120, 260)
    S.resistor("R5", (20, 190))                      # R5-2 (60, 190)
    S.resistor("R6", (60, 300))                      # R6-1 (60, 300), R6-2 (100, 300)
    S.resistor("R7", (150, 320), vertical=True)      # R7-1 (150, 320)
    if not wires:
        return
    S.wire((180, 120), (100, 120))                   # SIG: U1-1 .. R1-2, named by a label
    S.label("SIG", (120, 120))
    S.label("LBLPIN", (60, 300))                     # a label right on R6-1's hot end, no wire
    S.wire((280, 120), (400, 120))                   # T-junction: U1-3, R3-1 and, by the T, R2-1
    S.wire((340, 120), (340, 140))
    S.dot((340, 120))                                # the dot a T gets drawn with (the T connects without it)
    S.wire((340, 180), (340, 200))                   # grounds: R2-2, U1-4, R4-2 (that one's name hidden)
    S.ground((340, 200))
    S.wire((280, 160), (300, 160), (300, 200))
    S.ground((300, 200))
    S.wire((120, 260), (120, 280))
    S.ground((120, 280), name=None)
    S.wire((440, 120), (480, 120))                   # R3-2 to the VOUT port, onto the middle of its edge
    S.port("VOUT", (480, 120))
    S.wire((180, 160), (120, 160), (120, 220))       # U1-2 .. R4-1, crossed below by ...
    S.wire((60, 190), (160, 190))                    # ... R5-2's wire, with no dot: must not connect
    S.label("XNET", (135, 190))
    S.wire((100, 300), (200, 300))                   # R6-2's wire crosses R7-1's, with a junction dot: connects
    S.wire((150, 280), (150, 320))
    S.dot((150, 300))
    S.label("DOTNET", (160, 300))
    if bridge:                                       # a wire the netlist does not have: SIG joined to XNET
        S.wire((100, 120), (100, 150), (60, 150), (60, 190))


def draw_aux(S: Sheet, wires: bool = True) -> None:
    S.resistor("C1", (100, 60), vertical=True)       # C1-1 (100, 60), C1-2 (100, 100)
    if wires:
        S.wire((100, 60), (100, 40), (160, 40))
        S.vlabel("VOUT", (100, 58))
        S.port("VOUT", (160, 40))
        S.wire((100, 100), (100, 120))
        S.ground((100, 120))


SHEETS = {
    "Main": {"page": 1, "components": {
        "U1": ["U1-1", "U1-2", "U1-3", "U1-4"], **{r: [f"{r}-1", f"{r}-2"] for r in
                                                  ("R1", "R2", "R3", "R4", "R5", "R6", "R7")}},
     "nets": {
        "SIG": {"pins": ["U1-1", "R1-2"], "labels": 1},
        "NetR1_1": {"pins": ["R1-1"]},
        "NetU1_2": {"pins": ["U1-2", "R4-1"]},
        "NetU1_3": {"pins": ["U1-3", "R2-1", "R3-1"]},
        "GND": {"pins": ["U1-4", "R2-2", "R4-2"]},
        "VOUT": {"pins": ["R3-2"], "ports": 1},
        "XNET": {"pins": ["R5-2"], "labels": 1},
        "NetR5_1": {"pins": ["R5-1"]},
        "LBLPIN": {"pins": ["R6-1"], "labels": 1},
        "DOTNET": {"pins": ["R6-2", "R7-1"], "labels": 1},
        "NetR7_2": {"pins": ["R7-2"]},
    }},
    "Aux": {"page": 2, "components": {"C1": ["C1-1", "C1-2"]}, "nets": {
        "VOUT": {"pins": ["C1-1"], "ports": 1, "labels": 1},
        "GND": {"pins": ["C1-2"]},
    }},
}


def toc(sheets: dict) -> list[list]:
    rows = [[1, "Schematic Prints", 1], [2, "Top.SchDoc(Top)", 1]]
    for name, S in sheets.items():
        pg = S["page"]
        rows.append([3, f"{name}.SchDoc({name})", pg])
        rows.append([4, "Components", pg])
        for ref, pins in S["components"].items():
            rows += [[5, ref, pg]] + [[6, p, pg] for p in pins]
        rows.append([4, "Nets", pg])
        for net, N in S["nets"].items():
            rows.append([5, net, pg])
            rows += [[6, "Pins", pg]] + [[7, p, pg] for p in N["pins"]]
            for kind, key in (("NetLabels", "labels"), ("Ports", "ports")):
                if N.get(key):
                    rows += [[6, kind, pg]] + [[7, net, pg] for _ in range(N[key])]
    return rows


def build(path: Path, house: House = ALT, *, sheets: dict | None = None, drop: set[str] = frozenset(),
          bare: set[str] = frozenset(), wires: bool = True, bridge: bool = False) -> Path:
    """`drop`: pin anchors not written; `bare`: pin lines not drawn (their anchors stay)."""
    doc = pymupdf.open()
    for _ in range(2):
        doc.new_page(width=600, height=400)
    draw_main(Sheet(doc[0], house, set(drop), set(bare)), wires, bridge)
    draw_aux(Sheet(doc[1], house, set(drop), set(bare)), wires)
    doc.set_toc(toc(sheets or SHEETS))
    doc.save(path)
    doc.close()
    return path


@pytest.fixture()
def pdf(tmp_path) -> Path:
    return build(tmp_path / "board.pdf")


def outline_of(path: Path) -> dict:
    with pymupdf.open(path) as doc:
        return sp.read_outline(doc.get_toc(simple=True))


def net_of(T: dict, pid: str) -> dict:
    return next(n for n in T["nets"] if pid in n["pins"])


# ---------------------------------------------------------------- the trace

def test_anchors_are_render_mode_3_and_never_visible_words(pdf):
    with pymupdf.open(pdf) as doc:
        page = doc[0]
        kinds = {sp_["type"] for sp_ in page.get_texttrace() if sp_["chars"] and chr(sp_["chars"][0][0]) == "P"}
        anchors, words = sp.page_text_layer(page)
    assert 3 in kinds
    assert "PIU101" in anchors and "COU1" in anchors and "NLSIG" in anchors
    assert not any(w[4].startswith(("PI", "CO", "NL")) for w in words)
    x0, y0, x1, y1 = anchors["PIU101"][0]
    assert (round(x0), round(x1), round((y0 + y1) / 2)) == (180, 200, 120)  # the anchor spans its pin line


def test_trace_wires_junctions_labels_ports_power(pdf):
    tr = geom.trace(pdf, outline_of(pdf))
    assert tr["sheets"] == {"Main": 1, "Aux": 2}
    T = tr["pages"][1]
    assert T["traced"] and not T["unanchored"]
    # T-junction: the stub's end on the middle of the other wire connects
    assert net_of(T, "U1-3")["pins"] == ["R2-1", "R3-1", "U1-3"]
    # a crossing with no endpoint and no dot never connects
    assert net_of(T, "U1-2")["pins"] == ["R4-1", "U1-2"]
    assert net_of(T, "R5-2")["pins"] == ["R5-2"] and net_of(T, "R5-2")["labels"] == ["XNET"]
    # a junction dot on a crossing does
    assert net_of(T, "R6-2")["pins"] == ["R6-2", "R7-1"] and net_of(T, "R6-2")["labels"] == ["DOTNET"]
    # labels on a wire and on a pin end
    assert net_of(T, "U1-1")["labels"] == ["SIG"] and net_of(T, "R6-1")["labels"] == ["LBLPIN"]
    # three ground symbols, one with its name hidden, are one net by name
    g = net_of(T, "U1-4")
    assert g["pins"] == ["R2-2", "R4-2", "U1-4"] and g["power"] == ["GND"]
    assert T["counts"]["power_ports"] == 3 and T["counts"]["power_ports_inferred_name"] == 1
    # the port joins what touches the middle of its edge, and names it
    assert net_of(T, "R3-2")["ports"] == ["VOUT"] and T["counts"]["ports_connected"] == 1
    # pin numbers (black text) never become labels
    assert not any(n["labels"] and n["labels"][0].isdigit() for n in T["nets"])
    # the second sheet: a two-sheet net carries the same port name on both pages
    A = tr["pages"][2]
    assert net_of(A, "C1-1")["ports"] == ["VOUT"] and net_of(A, "C1-2")["power"] == ["GND"]
    assert net_of(A, "C1-1")["labels"] == ["VOUT"]  # a label reading upward, on a vertical wire


def test_hot_ends_and_pin_confidence(pdf):
    T = geom.trace(pdf, outline_of(pdf))["pages"][1]
    P = T["pins"]
    assert P["R2-1"]["at"] == [340.0, 140.0] and P["R2-2"]["at"] == [340.0, 180.0]  # vertical pins
    assert P["U1-3"]["at"] == [280.0, 120.0]
    assert P["U1-3"]["conf"] == "high" and P["U1-3"]["basis"] == ["anchor", "pin-line", "hot-touch"]
    # an unconnected pin: its hot end is the end away from the body, with less confidence
    assert P["R1-1"]["at"] == [60.0, 120.0] and P["R1-1"]["conf"] == "medium" and "hot-body" in P["R1-1"]["basis"]


def test_unanchored_pin_is_reported(tmp_path):
    p = build(tmp_path / "b.pdf", drop={"PIR301"})
    T = geom.trace(p, outline_of(p))["pages"][1]
    assert T["unanchored"] == ["R3-1"] and "R3-1" not in T["pins"] and T["counts"]["pins_unanchored"] == 1
    assert net_of(T, "U1-3")["pins"] == ["R2-1", "U1-3"]


def test_a_pin_with_no_pin_line_takes_the_wire_end_inside_its_anchor(tmp_path):
    p = build(tmp_path / "b.pdf", bare={"PIR301"})
    T = geom.trace(p, outline_of(p))["pages"][1]
    assert T["pins"]["R3-1"]["at"] == [400.0, 120.0] and T["pins"]["R3-1"]["conf"] == "medium"
    assert T["pins"]["R3-1"]["basis"] == ["anchor", "anchor-box-wire-end"]
    assert net_of(T, "U1-3")["pins"] == ["R2-1", "R3-1", "U1-3"]


def test_without_an_outline_pins_are_named_from_the_tokens(pdf):
    tr = geom.trace(pdf)
    assert tr["sheets"] == {"page1": 1, "page2": 2}
    T = tr["pages"][1]
    assert net_of(T, "U1-3")["pins"] == ["R2-1", "R3-1", "U1-3"]
    assert T["pins"]["U1-3"]["basis"][0] == "anchor-decoded"


# ---------------------------------------------------------------- the house style

def test_detection_on_the_default_style(pdf):
    tr = geom.trace(pdf, outline_of(pdf))
    assert all(v.startswith("detected") for v in tr["style_basis"].values()), tr["style_basis"]
    assert tr["style"]["wire_rgb"] == [0.0, 0.0, 0.502] and tr["style"]["wire_width"] == 0.72


def test_another_house_style_is_detected_not_assumed(tmp_path):
    p = build(tmp_path / "other.pdf", OTHER)
    ol = outline_of(p)
    # with the defaults alone, nothing in this drawing is a wire
    fixed = geom.trace(p, ol, detect=False)
    assert fixed["pages"][1]["counts"]["wire_segments"] == 0 and not fixed["pages"][1]["traced"]
    tr = geom.trace(p, ol)
    st = tr["style"]
    assert st["wire_rgb"] == list(OTHER.wire) and st["wire_width"] == OTHER.wire_w
    assert st["power_rgb"] == list(OTHER.power) and st["port_fill_rgb"] == list(OTHER.port_fill)
    assert [list(c) for c in st["label_rgbs"]] == [list(OTHER.label)]
    xc = geom.crosscheck(ol, tr)
    assert xc["counts"]["sheet_nets_disagree"] == 0 and xc["counts"]["sheet_nets_agree"] == 13
    # an explicit style is the other door
    style = replace(geom.ALTIUM, wire_rgb=OTHER.wire, wire_width=OTHER.wire_w)
    assert geom.trace(p, ol, style=style, detect=False)["pages"][1]["counts"]["wire_segments"] > 0


# ---------------------------------------------------------------- the cross-check in `schematic`

def test_schematic_crosscheck_agrees(pdf):
    nl, _bom = sp.parse(pdf, "T", "board.pdf")
    assert nl["method"] == "smartpdf-outline+geometry"
    g = nl["counts"]["geometry"]
    assert g["sheet_nets"] == g["sheet_nets_agree"] == 13 and g["false_pairs"] == g["missed_pairs"] == 0
    assert g["nets_agree"] == 11 and g["pins_agree"] == 20 and g["pin_conf_high"] >= 14
    assert all(N["crosscheck"] == "agree" for N in nl["nets"].values())
    assert all(P["crosscheck"] == "agree" for C in nl["components"].values() for P in C["pins"].values())
    # the two-sheet net rolls up both sheets
    assert nl["nets"]["VOUT"]["pins"] == ["C1-1", "R3-2"] and nl["nets"]["VOUT"]["crosscheck"] == "agree"
    assert not any(i.startswith("geometry") for i in nl["issues"])


def test_disagreements_are_issues_and_never_change_the_netlist(tmp_path):
    # the drawing has a wire the netlist lacks: SIG and XNET joined (false pairs)
    p = build(tmp_path / "bridge.pdf", bridge=True)
    nl, _ = sp.parse(p, "T")
    assert nl["method"] == "smartpdf-outline"
    assert nl["nets"]["SIG"]["pins"] == ["R1-2", "U1-1"] and nl["nets"]["XNET"]["pins"] == ["R5-2"]
    assert nl["nets"]["SIG"]["crosscheck"] == nl["nets"]["XNET"]["crosscheck"] == "disagree"
    assert nl["components"]["R5"]["pins"]["2"]["crosscheck"] == "disagree"
    assert nl["nets"]["GND"]["crosscheck"] == "agree"
    g = nl["counts"]["geometry"]
    assert g["false_pairs"] == 2 and g["missed_pairs"] == 0 and g["sheet_nets_disagree"] == 2
    iss = [i for i in nl["issues"] if i.startswith("geometry disagrees on net XNET")]
    assert len(iss) == 1 and "page 1" in iss[0] and "R1-2(100.0, 120.0) of SIG" in iss[0]
    # the netlist says a pin is on a net the drawing never reaches (a missed pair)
    sheets = copy.deepcopy(SHEETS)
    sheets["Main"]["nets"]["NetU1_2"]["pins"].append("R5-2")
    del sheets["Main"]["nets"]["XNET"]
    nl, _ = sp.parse(build(tmp_path / "split.pdf", sheets=sheets), "T")
    assert nl["nets"]["NetU1_2"]["crosscheck"] == "disagree" and nl["counts"]["geometry"]["missed_pairs"] == 2
    assert any("NetU1_2" in i and "split into" in i and "R5-2(60.0, 190.0)" in i for i in nl["issues"])


def test_geometry_missing_is_not_a_disagreement(tmp_path):
    nl, _ = sp.parse(build(tmp_path / "noanchor.pdf", drop={"PIR301"}), "T")
    assert nl["nets"]["NetU1_3"]["crosscheck"] == "geometry-missing"
    assert nl["components"]["R3"]["pins"]["1"]["crosscheck"] == "geometry-missing"
    assert nl["components"]["U1"]["pins"]["3"]["crosscheck"] == "agree"
    assert nl["method"] == "smartpdf-outline+geometry"
    assert any("no anchor word" in i and "R3-1" in i for i in nl["issues"])
    # a print with no wires drawn is no evidence at all, never a disagreement
    nl, _ = sp.parse(build(tmp_path / "nowires.pdf", wires=False), "T")
    g = nl["counts"]["geometry"]
    assert g["pages_traced"] == 0 and g["sheet_nets_disagree"] == 0 and g["sheet_nets_geometry_missing"] == 13
    assert nl["method"] == "smartpdf-outline"


def test_cli_writes_geometry_json_deterministically(pdf, tmp_path):
    adir = tmp_path / "atlas"
    assert cli.main(["--atlas-dir", str(adir), "schematic", str(pdf), "--board", "T"]) == 0
    out = adir / "boards" / "T"
    first = {f: (out / f).read_bytes() for f in ("netlist.json", "geometry.json")}
    assert cli.main(["--atlas-dir", str(adir), "schematic", str(pdf), "--board", "T"]) == 0
    assert first == {f: (out / f).read_bytes() for f in first}
    geo = json.loads(first["geometry.json"])
    assert geo["board"] == "T" and geo["crosscheck"]["counts"]["sheet_nets_agree"] == 13
    assert geo["pages"]["1"]["pins"]["U1-1"]["conf"] == "high"
    assert cli.main(["--atlas-dir", str(adir), "schematic", str(pdf), "--board", "T", "--no-geometry"]) == 0
    assert not (out / "geometry.json").exists()
    nl = json.loads((out / "netlist.json").read_text())
    assert nl["method"] == "smartpdf-outline" and "geometry" not in nl["counts"]

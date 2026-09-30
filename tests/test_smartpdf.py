"""smartpdf: a synthetic Altium-style Smart PDF, built here with PyMuPDF.

The fixture has what the real exports carry: an outline with per-sheet
Components/Nets/Ports, invisible (render mode 3) anchor words, link annotations
calling ShowCompProps_<GUID>() and the document-level JavaScript defining them,
a multi-part symbol, a DNI parameter, a drawn not-fitted part, and a 1-pin test
point sitting inside an IC's body.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pymupdf
import pytest

from embarch_atlas import cli, smartpdf as sp

FS = 4.0  # anchor font size


def pdf_str(s: str) -> str:
    return "(" + s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ")"


def anchor(page, x: float, y: float, word: str, fs: float = FS) -> tuple[float, float]:
    """Invisible Courier anchor; returns its right end x."""
    page.insert_text((x, y), word, fontname="cour", fontsize=fs, render_mode=3)
    return x + pymupdf.get_text_length(word, fontname="cour", fontsize=fs)


def visible(page, x: float, y: float, word: str, fs: float = FS, color=(0, 0, 0)) -> None:
    page.insert_text((x, y), word, fontname="helv", fontsize=fs, color=color)


def add_popups(doc, page, entries: list[tuple[tuple, dict[str, str]]]) -> None:
    """Link annotations (rect in top-down page coords) whose JS calls ShowCompProps_<n>(), plus the name tree."""
    H = page.rect.height
    cat = doc.pdf_catalog()
    names = []
    annots = []
    for i, (rect, props) in enumerate(entries):
        fn = f"ShowCompProps_{page.number:02d}{i:04X}ABCDEF"
        args = ",".join('"' + f"{k}: {v}".replace('"', '\\"') + '"' for k, v in props.items())
        body = f'function {fn}(){{var sChoice = app.popUpMenu({args}); if (sChoice) {{app.alert(sChoice);}}}}'
        jx = doc.get_new_xref()
        doc.update_object(jx, f"<< /S /JavaScript /JS {pdf_str(body)} >>")
        names.append((fn, jx))
        ax = doc.get_new_xref()
        x0, y0, x1, y1 = rect
        doc.update_object(ax, f"<< /Type /Annot /Subtype /Link /Rect [{x0} {H - y1} {x1} {H - y0}] /Border [0 0 0] "
                              f"/A << /S /JavaScript /JS {pdf_str(fn + '();')} >> >>")
        annots.append(ax)
    t, v = doc.xref_get_key(page.xref, "Annots")
    old = v.strip("[] ") if t == "array" else ""
    doc.xref_set_key(page.xref, "Annots", "[" + old + " " + " ".join(f"{a} 0 R" for a in annots) + "]")
    t, v = doc.xref_get_key(cat, "Names")
    if t == "xref":
        tree = int(doc.xref_get_key(int(v.split()[0]), "JavaScript")[1].split()[0])
        arr = doc.xref_get_key(tree, "Names")[1].strip("[] ")
    else:
        tree, arr = doc.get_new_xref(), ""
        nx = doc.get_new_xref()
        doc.update_object(nx, f"<< /JavaScript {tree} 0 R >>")
        doc.xref_set_key(cat, "Names", f"{nx} 0 R")
    arr += " " + " ".join(f"{pdf_str(n)} {x} 0 R" for n, x in names)
    doc.update_object(tree, f"<< /Names [{arr}] >>")


def outline(sheets: dict[str, dict]) -> list[list]:
    """{sheet: {page, components {ref: [pin ids]}, nets {net: {pins, labels, ports}}, ports}} -> set_toc rows."""
    toc = [[1, 'Schematic Prints("All Documents",Physical)', 1], [2, "Top.SchDoc(Top)", 1]]
    for name, S in sheets.items():
        pg = S["page"]
        toc.append([3, f"{name}.SchDoc({name})", pg])
        toc.append([4, "Components", pg])
        for ref, pins in S["components"].items():
            toc.append([5, ref, pg])
            toc += [[6, p, pg] for p in pins]
        toc.append([4, "Nets", pg])
        for net, N in S["nets"].items():
            toc.append([5, net, pg])
            if N.get("pins"):
                toc.append([6, "Pins", pg])
                toc += [[7, p, pg] for p in N["pins"]]
            if N.get("labels"):
                toc.append([6, "NetLabels", pg])
                toc += [[7, net, pg] for _ in range(N["labels"])]
            if N.get("ports"):
                toc.append([6, "Ports", pg])
                toc += [[7, net, pg] for _ in range(N["ports"])]
        if S.get("ports"):
            toc.append([4, "Ports", pg])
            toc += [[5, p, pg] for p in S["ports"]]
    toc += [[3, "Nets", 1], [4, "VOUT", 1]]  # the top sheet's own section: sheet-entry nets, no pins
    return toc


SHEETS = {
    "Main": {"page": 1, "components": {
        "U1": ["U1-1", "U1-2", "U1-3", "U1-4"],
        "R1": ["R1-1", "R1-2"],
        "R2": ["R2-1", "R2-2"],
        "R3": ["R3-1", "R3-2"],
        "TP1": ["TP1-1"],
        "U2A": ["U2-1", "U2-2", "U2-5"],
        "U2B": ["U2-3", "U2-4"],
    }, "nets": {
        "SIG_IN": {"pins": ["U1-1", "R1-1"], "labels": 1},
        "NetR1_2": {"pins": ["R1-2", "U1-2"]},
        "VOUT": {"pins": ["U1-3", "U2-1"], "ports": 1},
        "GND": {"pins": ["U1-4", "U2-5", "R2-2", "R3-2"]},
        "LOCAL": {"pins": ["TP1-1", "R2-1"], "labels": 1},
        "NetU2_2": {"pins": ["U2-2"]},
        "GATE_OUT": {"pins": ["U2-3", "U2-4", "R3-1"], "labels": 1},
    }, "ports": ["VOUT"]},
    "Aux": {"page": 2, "components": {"C1": ["C1-1", "C1-2"]}, "nets": {
        "VOUT": {"pins": ["C1-1"], "ports": 1},
        "LOCAL": {"pins": ["C1-2"], "labels": 1},
    }, "ports": ["VOUT"]},
}


def draw_ic(page) -> list[tuple[tuple, dict]]:
    """U1 body (200,100)-(260,180), its link rectangle spanning the pins: 1,2 on the left, 3,4 on the right."""
    pops = []
    for pin, y, name in (("1", 120, "IN"), ("2", 140, "EN")):
        x1 = anchor(page, 180, y, f"PIU10{pin}")
        visible(page, x1 + 1.5, y, name)
        visible(page, 185, y - 5, pin)  # the pin number, above the pin line
    for pin, y, name in (("3", 120, "OUT"), ("4", 140, "GND")):
        x0 = 262
        anchor(page, x0, y, f"PIU10{pin}")
        w = pymupdf.get_text_length(name, fontname="helv", fontsize=FS)
        visible(page, x0 - 1.5 - w, y, name)
        visible(page, 268, y - 5, pin)
    anchor(page, 205, 95, "COU1")
    visible(page, 205, 95, "U1")
    visible(page, 205, 175, "IC4X-Q")  # the printed comment inside the body: never a pin name
    pops.append(((178, 100, 284, 180), {"Comment": "IC4X-Q", "Component Kind": "Standard",
                                       "Description": "Test IC, 4 pins", "Manufacturer Part Number 1": "IC4X-Q100",
                                       "Manufacturer 1": "Acme", "ComponentLink1Description": "Datasheet",
                                       "ComponentLink1URL": "https://example.com/ic4x.pdf"}))
    # TP1: a 1-pin test point whose pin lies inside U1's body rectangle, with no popup of its own
    anchor(page, 230, 160, "PITP101")
    anchor(page, 230, 155, "COTP1")
    return pops


def draw_two_pin(page, ref: str, x: float, y: float, props: dict | None, grey: bool = False, cross: bool = False):
    anchor(page, x, y, f"PI{ref}01")
    anchor(page, x + 30, y, f"PI{ref}02")
    anchor(page, x + 10, y - 6, f"CO{ref}")
    visible(page, x + 10, y - 6, ref, color=(0.749, 0.745, 0.737) if grey else (0, 0, 0))
    rect = (x - 2, y - 8, x + 42, y + 3)
    if cross:
        page.draw_line((rect[0] + 1, rect[1] + 1), (rect[2] - 1, rect[3] - 1), color=(1, 0, 0), width=2.16)
        page.draw_line((rect[0] + 1, rect[3] - 1), (rect[2] - 1, rect[1] + 1), color=(1, 0, 0), width=2.16)
    return [(rect, props)] if props is not None else []


def build_pdf(path: Path, with_nets: bool = True, with_popups: bool = True) -> Path:
    doc = pymupdf.open()
    doc.new_page(width=600, height=400)
    doc.new_page(width=600, height=400)
    p1, p2 = doc[0], doc[1]
    pops = draw_ic(p1)
    pops += draw_two_pin(p1, "R1", 60, 250, {"Comment": "RES-10K", "Component Kind": "Standard",
                                             "Manufacturer Part Number 1": "RES10K0402", "Value": "10k\\u03A9"})
    pops += draw_two_pin(p1, "R2", 60, 300, {"Component Kind": "Standard", "Manufacturer Part Number 1": "RES1K0402",
                                             "Value": "DNI", "Resistance": "1kR"})
    pops += draw_two_pin(p1, "R3", 160, 300, {"Component Kind": "Standard", "Manufacturer Part Number 1": "RES0R0402",
                                              "Value": "0R"}, grey=True, cross=True)
    # multi-part symbol: U2A (pins 1, 2, 5) and U2B (pins 3, 4), one body each
    for i, (sref, pins, x) in enumerate((("U2A", ("1", "2", "5"), 350), ("U2B", ("3", "4"), 450))):
        for j, pin in enumerate(pins):
            xe = anchor(p1, x, 120 + 12 * j, f"PIU20{pin}")
            visible(p1, xe + 1.5, 120 + 12 * j, f"P{pin}")
        anchor(p1, x + 5, 108, f"CO{sref}")
        pops.append(((x - 2, 110, x + 40, 160), {"Comment": "GATE01", "Manufacturer Part Number": "GATE01X",
                                                 "Component Kind": "Standard"}))
    for pin, y in (("1", 60), ("2", 80)):
        anchor(p2, 100, y, f"PIC10{pin}")
    if with_popups:
        add_popups(doc, p1, pops)
    if with_nets:
        doc.set_toc(outline(SHEETS))
    else:
        doc.set_toc([[1, "Schematic Prints", 1], [2, "Main.SchDoc(Main)", 1], [2, "Aux.SchDoc(Aux)", 2]])
    doc.set_metadata({"creator": "Altium Designer"})
    doc.save(path)
    doc.close()
    return path


@pytest.fixture()
def pdf(tmp_path) -> Path:
    return build_pdf(tmp_path / "board.pdf")


# ---------------------------------------------------------------- pure helpers

def test_encoding_and_splits():
    assert sp.enc("TP_X-1") == "TP0X01"
    assert sp.pin_token("U1", "12") == "PIU1012"
    assert sp.pin_token("U1", "01") == sp.pin_token("U10", "1")  # the ambiguity the outline resolves
    assert sp.physical_ref("U4A", "U4-3") == ("U4", "3")
    assert sp.physical_ref("R5", "R5-2") == ("R5", "2")
    assert sp.split_pin_id("TP_X-1", {"TP_X"}) == ("TP_X", "1")
    assert sp.sheet_name("Power.SchDoc(Power Stage)") == "Power Stage"
    assert sp.is_auto_net("NetR1_2", ["R1-2", "U1-2"])
    assert sp.is_auto_net("NetTP_A_1", ["TP_A-1"])
    assert not sp.is_auto_net("NetworkEnable", ["U1-2"])
    assert sp.natural_key("C2") < sp.natural_key("C10") < sp.natural_key("R1")


def test_js_parsing():
    js = 'function F(){var s = app.popUpMenu("Value: 10k\\u03A9","Comment: A (B)","Pin Info: ","Header"); if (s) {}}'
    props = sp.parse_props(js)
    assert props == {"Value": "10kΩ", "Comment": "A (B)", "Pin Info": ""}
    s = sp.summarize_props({"Value": "DNI", "Resistance": "4.7kR", "MPN": "N/A", "Manufacturer Part Number": "X12-3456",
                            "Component Kind": "Standard (No BOM)"})
    assert s["mpn"] == "X12-3456" and s["value"] == "4.7kR" and s["dni_evidence"] == ["parameter Value=DNI"]
    assert s["in_bom"] is False
    assert sp.datasheet_link({"ComponentLink1URL": "https://a/x", "ComponentLink2URL": "https://b/y",
                              "ComponentLink2Description": "Datasheet"}) == "https://b/y"


def test_resolve_ambiguous_tokens():
    parts = {"U1": [("U1", "01"), ("U1", "2")], "U10": [("U10", "1"), ("U10", "2")]}
    anchors = {"PIU1001": [(100, 10, 110, 12), (300, 10, 310, 12)],  # shared by U1-01 and U10-1
               "PIU102": [(100, 20, 110, 22)], "PIU1002": [(300, 20, 310, 22)]}
    boxes, issues = sp.resolve_pin_boxes(parts, anchors)
    assert boxes[("U1", "01")][0] == 100 and boxes[("U10", "1")][0] == 300
    assert not issues


def test_pin_count_rule_rejects_ic_popup_on_one_pin_part():
    # X's pins sit in two rectangles; X takes the one holding all four, and the second one (which also
    # holds the 1-pin pad P's only pin) must not fall to P.
    parts = {"X": [("X", "1"), ("X", "2"), ("X", "3"), ("X", "4")], "P": [("P", "1")]}
    pin_boxes = {("X", "1"): (10, 10, 12, 12), ("X", "2"): (10, 20, 12, 22), ("X", "3"): (50, 10, 52, 12),
                 ("X", "4"): (50, 20, 52, 22), ("P", "1"): (30, 15, 32, 17)}
    popups = [((0, 0, 60, 30), {"Manufacturer Part Number 1": "BIGIC"}),
              ((5, 5, 35, 25), {"Manufacturer Part Number 1": "BIGIC"})]
    got = sp.match_popups(parts, pin_boxes, {}, popups)
    assert got == {"X": 0}
    assert not sp.plausible(1, Counter({"X": 2, "P": 1}), "P")
    assert sp.plausible(2, Counter({"X": 2, "P": 1}), "P")
    # the designator fallback: no pin inside any rectangle, designator anchor inside a free one
    got = sp.match_popups({"Q": [("Q", "1"), ("Q", "2")]}, {}, {"Q": (70, 70, 74, 72)},
                          [((60, 60, 90, 90), {"MPN": "FET1"})])
    assert got == {"Q": 0}


# ---------------------------------------------------------------- the whole parse

def test_parse_synthetic_board(pdf):
    nl, bom = sp.parse(pdf, "T", "board.pdf")
    assert nl["board"] == "T" and nl["method"] == "smartpdf-outline"
    assert nl["source"] == {"file": "board.pdf", "sha256": nl["source"]["sha256"]} and len(nl["source"]["sha256"]) == 64
    assert nl["sheets"] == {"Top": {"page": 1, "parent": None}, "Main": {"page": 1, "parent": "Top"},
                            "Aux": {"page": 2, "parent": "Top"}}
    C = nl["components"]
    assert set(C) == {"U1", "R1", "R2", "R3", "TP1", "U2", "C1"}
    # multi-part fold
    assert C["U2"]["parts"] == ["U2A", "U2B"] and list(C["U2"]["pins"]) == ["1", "2", "3", "4", "5"]
    assert C["U2"]["mpn"] == "GATE01X"
    # every pin on its net
    assert C["U1"]["pins"]["1"]["net"] == "SIG_IN" and C["U1"]["pins"]["2"]["net"] == "NetR1_2"
    assert C["C1"]["pins"]["1"]["net"] == "VOUT"
    assert nl["counts"]["pins"] == nl["counts"]["pins_on_net"] == 18
    # pin names from the word past the anchor's body end; never the pin number or the printed comment
    assert {p: v["name"] for p, v in C["U1"]["pins"].items()} == {"1": "IN", "2": "EN", "3": "OUT", "4": "GND"}
    assert C["U2"]["pins"]["3"]["name"] == "P3"
    assert C["R1"]["pins"]["1"]["name"] is None  # 2-pin parts get no names
    # parameters
    assert C["U1"]["mpn"] == "IC4X-Q100" and C["R1"]["value"] == "10kΩ"
    assert C["TP1"]["mpn"] is None  # the pin-count rule: not U1's popup
    # DNI: a parameter, and a drawn cross + grey designator
    assert C["R2"]["dni"] and C["R2"]["dni_evidence"] == ["parameter Value=DNI"] and C["R2"]["value"] == "1kR"
    assert C["R3"]["dni"] and C["R3"]["dni_evidence"] == ["drawn-cross", "drawn-grey"]
    assert not C["U1"]["dni"] and "dni_evidence" not in C["U1"]
    # nets: merged across sheets by name, with counts; auto names detected
    N = nl["nets"]
    assert N["VOUT"]["pins"] == ["C1-1", "U1-3", "U2-1"] and N["VOUT"]["ports"] == 2
    assert N["VOUT"]["sheets"] == ["Aux", "Main", "Top"]
    assert N["NetR1_2"]["auto"] and not N["SIG_IN"]["auto"]
    assert any("LOCAL" in i and "net label" in i for i in nl["issues"])
    # bom.json: DESIGN.md's keys on every row
    keys = {"ref", "mpn", "value", "dni", "description", "datasheet_link"}
    assert all(keys <= set(e) for e in bom) and [e["ref"] for e in bom] == ["C1", "R1", "R2", "R3", "TP1", "U1", "U2"]
    u1 = next(e for e in bom if e["ref"] == "U1")
    assert u1["datasheet_link"] == "https://example.com/ic4x.pdf" and u1["description"] == "Test IC, 4 pins"
    assert nl["counts"]["popups_matched"] == 6 and nl["counts"]["parts"] == 7 and nl["counts"]["symbol_parts"] == 8


def test_cli_writes_board_files_deterministically(pdf, tmp_path):
    adir = tmp_path / "atlas"
    assert cli.main(["--atlas-dir", str(adir), "schematic", str(pdf), "--board", "T"]) == 0
    out = adir / "boards" / "T"
    first = {f: (out / f).read_bytes() for f in ("netlist.json", "bom.json", "nets.md", "parts.md")}
    assert cli.main(["--atlas-dir", str(adir), "schematic", str(pdf), "--board", "T"]) == 0
    assert first == {f: (out / f).read_bytes() for f in first}
    nets = (out / "nets.md").read_text()
    assert "T:SIG_IN T:R1-1 T:U1-1/IN" in nets and "T:~NetR1_2 T:R1-2 T:U1-2/EN" in nets
    parts = (out / "parts.md").read_text()
    assert "T:U1 IC4X-Q100 p1 | 1/IN=SIG_IN 2/EN=~NetR1_2 3/OUT=VOUT 4/GND=GND" in parts
    assert "T:R2 RES1K0402 1kR DNI p1" in parts
    json.loads((out / "netlist.json").read_text())


def test_no_net_bookmarks_is_refused(tmp_path, capsys):
    pdf = build_pdf(tmp_path / "plain.pdf", with_nets=False)
    with pytest.raises(sp.NoNetInfo):
        sp.parse(pdf, "T")
    rc = cli.main(["--atlas-dir", str(tmp_path / "atlas"), "schematic", str(pdf), "--board", "T"])
    err = capsys.readouterr().err
    assert rc != 0
    assert "not built yet" in err and "net information" in err and "component parameters" in err
    assert not (tmp_path / "atlas" / "boards" / "T" / "netlist.json").exists()


def test_without_popups_the_netlist_still_reads(tmp_path):
    nl, bom = sp.parse(build_pdf(tmp_path / "nopop.pdf", with_popups=False), "T")
    assert nl["counts"]["pins_on_net"] == 18 and nl["counts"]["parts_with_mpn"] == 0
    assert any("component parameters" in i for i in nl["issues"])

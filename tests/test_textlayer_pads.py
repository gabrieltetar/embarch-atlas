"""Pad names a module symbol prints as rotated text: recovered from the page's text layer, marked, or reported.

The synthetic module U9 has a bottom row of vertical pad anchors whose names are rotated words inside the
outline, further up than the word just past an anchor, plus one side pad whose name sits on its row inside the
outline. Pad 4 has no word in its column and pad 5 has two, so neither is named.
"""
from __future__ import annotations

from pathlib import Path

import pymupdf

from embarch_atlas import smartpdf as sp
from test_smartpdf import add_popups, anchor, outline, visible

BOTTOM = {"1": (110, "VDD"), "2": (125, "SDA"), "3": (140, "SCL"), "4": (155, None), "5": (170, "AAA")}


def rotated(page, x: float, y: float, word: str) -> None:
    page.insert_text((x, y), word, fontname="helv", fontsize=4.0, rotate=90)


def build_module_pdf(path: Path) -> Path:
    doc = pymupdf.open()
    page = doc.new_page(width=300, height=300)
    for pin, (x, name) in BOTTOM.items():
        page.insert_text((x, 205), f"PIU90{pin}", fontname="cour", fontsize=4.0, render_mode=3, rotate=90)
        rotated(page, x - 1, 183, pin)                    # the pad-number row, beside the name row
        if name:
            rotated(page, x - 1, 170, name)
    rotated(page, 171, 140, "BBB")                        # a second word in pad 5's column
    anchor(page, 85, 120, "PIU906")                       # side pad: number and name share a row inside the outline
    visible(page, 95, 115, "6")
    visible(page, 115, 115, "RST")
    anchor(page, 105, 105, "COU9")
    visible(page, 130, 105, "U9")
    visible(page, 130, 150, "MOD-X")                      # the printed comment: never a pad name
    add_popups(doc, page, [((80, 100, 185, 210), {"Comment": "MOD-X", "Manufacturer Part Number 1": "MOD-X100",
                                                   "Component Kind": "Standard"})])
    pins = [f"U9-{n}" for n in ("1", "2", "3", "4", "5", "6")]
    doc.set_toc(outline({"Mod": {"page": 1, "components": {"U9": pins},
                                 "nets": {f"N{i}": {"pins": [p]} for i, p in enumerate(pins, 1)}}}))
    doc.save(path)
    doc.close()
    return path


def test_rotated_pad_names_are_recovered_marked_and_the_rest_reported(tmp_path):
    nl, _ = sp.parse(build_module_pdf(tmp_path / "mod.pdf"), "T", "mod.pdf")
    pins = nl["components"]["U9"]["pins"]
    assert {p: v["name"] for p, v in pins.items()} == {"1": "VDD", "2": "SDA", "3": "SCL", "4": None, "5": None, "6": "RST"}
    assert [p for p, v in pins.items() if v.get("name_source") == "text-layer"] == ["1", "2", "3", "6"]
    assert nl["counts"]["pins_named_from_text_layer"] == 4 and nl["counts"]["pins_unpaired_text_layer"] == 2
    left = [i for i in nl["issues"] if "has no name" in i]
    assert len(left) == 2
    assert any("U9-4" in i and "no word in its column" in i for i in left)
    assert any("U9-5" in i and "2 candidate words" in i and "AAA, BBB" in i for i in left)
    assert sp.classify_issue(left[0], nl["sheets"])["kind"] == "pad-name-unpaired"


def test_recovery_without_an_outline_or_pads_names_nothing():
    got, why = sp.recover_pad_names([], [], None, set())
    assert got == {} and why == {}
    got, why = sp.recover_pad_names([("1", (0, 0, 1, 5))], [], None, set())
    assert "no symbol outline" in why["1"]


def test_pins_md_marks_a_recovered_name(tmp_path):
    from test_join import make_project, run_join
    from embarch_atlas.common import read_json, write_json

    ad, atlas = make_project(tmp_path)
    nlp = ad / "boards" / "M" / "netlist.json"
    nl = read_json(nlp)
    nl["components"]["U1"]["pins"]["12"].update({"name": "PF2-NRST", "name_source": "text-layer"})
    write_json(nlp, nl)
    run_join(ad, atlas)
    md = (atlas / "pins.md").read_text()
    assert "| PF2 (text-layer) |" in md
    assert "| PB7 |" in md

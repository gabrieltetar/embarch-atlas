"""schematic_text: the text of schematic PDF pages, each hit with its board, file, page and sheet."""
from __future__ import annotations

import shutil
from pathlib import Path

import pymupdf
import pytest

from embarch_atlas import query
from embarch_atlas.common import write_json

pytestmark = pytest.mark.skipif(shutil.which("pdftotext") is None, reason="needs pdftotext")


def make_pdf(path: Path) -> Path:
    doc = pymupdf.open()
    for lines in (["Power sheet", "VBUS_DET to U3 pin 4", "nothing else"], ["Sensor sheet", "SDA_1 connects U3-6", "vbus_det again"]):
        page = doc.new_page(width=300, height=300)
        for i, t in enumerate(lines):
            page.insert_text((20, 40 + 20 * i), t, fontname="helv", fontsize=10)
    doc.save(path)
    doc.close()
    return path


@pytest.fixture()
def atlas(tmp_path: Path) -> query.Atlas:
    fw = tmp_path / "fw"
    ad = fw / "embarch" / "atlas"
    (fw / "docs").mkdir(parents=True)
    make_pdf(fw / "docs" / "main.pdf")
    (ad).mkdir(parents=True)
    (ad / "atlas.toml").write_text(
        '[project]\nsources = "docs"\n\n[[boards]]\nlabel = "A"\npdf = "main.pdf"\n\n[[boards]]\nlabel = "B"\npdf = "gone.pdf"\n')
    write_json(ad / "boards" / "A" / "netlist.json", {"board": "A", "sheets": {"Power": {"page": 1, "parent": None},
               "Sensor": {"page": 2, "parent": None}}, "components": {}, "nets": {}})
    return query.Atlas(ad)


def test_hits_carry_board_file_page_and_sheet(atlas):
    out = query.schematic_text(atlas, "vbus_det", context=0)
    assert out.startswith("src: schematic text-layer, A=main.pdf (2 pages)") and "hits=2" in out
    assert "board A main.pdf p1 sheet Power\n> VBUS_DET to U3 pin 4" in out
    assert "board A main.pdf p2 sheet Sensor\n> vbus_det again" in out
    assert "gone.pdf not found" in out.splitlines()[0]          # an absent PDF is named, not silently skipped


def test_context_board_filter_and_misses(atlas):
    assert "\n  nothing else" in query.schematic_text(atlas, "VBUS_DET to", context=3)
    assert "No line matches" in query.schematic_text(atlas, "zzz")
    assert "src: none" in query.schematic_text(atlas, "x", board="Z")
    assert "src: none" in query.schematic_text(atlas, "x", board="B")      # its only PDF is missing
    assert "p1" not in query.schematic_text(atlas, "SDA_1", board="a")

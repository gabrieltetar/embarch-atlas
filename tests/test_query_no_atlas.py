"""part_card and mismatch_report before the first build: netlists, BOM, manifest and distillations only."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_join import make_project, run_join  # noqa: E402

from embarch_atlas import mismatch, query  # noqa: E402
from embarch_atlas.common import read_json, write_json  # noqa: E402


@pytest.fixture()
def pre(tmp_path: Path) -> Path:
    """The synthetic product's atlas dir with the netlists, a manifest and one distillation, and no atlas."""
    ad, _ = make_project(tmp_path)
    shutil.rmtree(ad / "atlases")
    nl = read_json(ad / "boards" / "M" / "netlist.json")
    nl["components"]["U2"]["mpn"] = "LOGIC-INV12"      # a part with two digits, so a manifest token can cover it
    write_json(ad / "boards" / "M" / "netlist.json", nl)
    write_json(ad / "inventory" / "manifest.json", {"documents": [
        {"file": "logic.pdf", "kind": "datasheet", "parts": ["LOGIC-INV12"], "sha256": "2" * 64}]})
    return ad


def test_part_card_by_board_mpn_without_an_atlas(pre):
    a = query.Atlas(pre)
    assert a.atlas is None
    out = query.part_card(a, "SNS701QR", board="all")
    assert out.startswith("src: sns701@a card pdf=")
    assert query.NO_ATLAS_NOTE in out.split("\n", 1)[0]
    assert "## On the boards" in out
    assert "- M:U3 SNS701QR: " in out and "B3=BUS0_SDA" in out
    assert "(no board ref" not in out
    assert "On the boards" not in query.part_card(a, "SNS701QR")


def test_part_card_covered_but_undistilled_names_the_file(pre):
    out = query.part_card(query.Atlas(pre), "LOGIC-INV12", board="")
    first = out.split("\n", 1)[0]
    assert first.startswith("src: boards netlist.json + coverage of LOGIC-INV12") and "no atlas built" in first
    assert "covered by logic.pdf" in out and "not translated yet" in out
    assert "M:U2 LOGIC-INV12" in out
    assert "src: none" in query.part_card(query.Atlas(pre), "NOPE123")


def test_mismatch_report_runs_the_netlist_checks_and_lists_the_rest(pre):
    out = query.mismatch_report(query.Atlas(pre))
    first = out.split("\n", 1)[0]
    assert first.startswith("src: boards/M,S netlist.json + BOM + coverage + distillations, no atlas")
    assert "Not run:" in out
    for name, why in mismatch.NEEDS_BUILD:
        assert f"- {name}: {why}" in out
    ran = out.split("Not run:", 1)[0]
    assert "net name agrees across a board connector" in ran
    assert "[cross-board-net-name]" in out
    for name, _ in mismatch.NEEDS_BUILD:
        assert f"- {name}: " not in ran       # a check that was not run is never listed as run
    assert not list(pre.glob("atlases/*"))     # nothing was written


def test_mismatch_report_with_an_atlas_is_the_stored_report(tmp_path):
    ad, atlas = make_project(tmp_path)
    run_join(ad, atlas)
    out = query.mismatch_report(query.Atlas(ad))
    assert out.startswith(f"src: {atlas.name} mismatches.json")
    assert "no atlas" not in out.split("\n", 1)[0]


def test_mismatch_report_without_netlists_still_says_build_first(tmp_path):
    ad = tmp_path / "atlas"
    ad.mkdir()
    out = query.mismatch_report(query.Atlas(ad))
    assert out.startswith("src: none") and "/atlas-build" in out

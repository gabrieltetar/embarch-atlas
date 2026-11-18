"""`board-diff`: a CAD project against a PDF-built netlist.json, on the synthetic project of tests/schdoc_synth.py.
The "PDF" netlist is the clean CAD read, copied and then mutated by hand."""
import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import schdoc_synth as syn  # noqa: E402
from embarch_atlas import boarddiff, cad  # noqa: E402
from embarch_atlas.cli import main  # noqa: E402


def rename(nl, old, new, auto=False):
    nl["nets"][new] = dict(nl["nets"].pop(old), auto=auto)
    for c in nl["components"].values():
        for P in c["pins"].values():
            if P["net"] == old:
                P["net"] = new


@pytest.fixture()
def setup(tmp_path):
    proj = syn.write_project(tmp_path / "hw")
    pdf = cad.read_project(str(proj))
    pdf["method"] = "smart-pdf"
    return proj, pdf, tmp_path / "atlas"


def put(atlas, pdf, label="Gen"):
    d = atlas / "boards" / label
    d.mkdir(parents=True, exist_ok=True)
    (d / "netlist.json").write_text(json.dumps(pdf))


def run(argv, atlas):
    return main(["--atlas-dir", str(atlas), "board-diff", *argv])


def test_clean_pair_exits_zero(setup, capsys):
    proj, pdf, atlas = setup
    put(atlas, pdf)
    assert run(["--cad", str(proj), "--board", "Gen"], atlas) == 0
    assert "no difference" in capsys.readouterr().out


def test_four_differences(setup, capsys):
    proj, pdf, atlas = setup
    bad = copy.deepcopy(pdf)
    bad["components"]["R3"]["value"] = "2k"                      # a value
    rename(bad, "NLBL", "NLBL_OLD")                              # a net renamed after release
    bad["components"]["R2"]["pins"]["1"]["net"] = "3V3"          # a pin on another net
    rename(bad, "LINK", "NetR1_2", auto=True)                    # a net the PDF left auto-named
    d = boarddiff.diff_netlists(bad, cad.read_project(str(proj)))
    assert d["differs"]
    assert d["parts_changed"] == [{"ref": "R3", "field": "value", "pdf": "2k", "cad": "1k"}]
    assert [(n["pdf"], n["cad"]) for n in d["nets_renamed"]] == [("NLBL_OLD", "NLBL")]
    assert d["nets_renamed"][0]["pins"] == ["R2-2", "R3-2"]      # a rename is not pins moving
    assert d["pin_nets"] == [{"pin": "R2-1", "pdf": "3V3", "cad": "SIG"}]
    assert [(n["pdf"], n["cad"]) for n in d["nets_named_by_cad"]] == [("NetR1_2", "LINK")]
    assert not d["parts_pdf_only"] and not d["parts_cad_only"] and not d["pins_pdf_only"] and not d["pins_cad_only"]
    put(atlas, bad)
    assert run(["--cad", str(proj), "--board", "Gen"], atlas) == 1
    out = capsys.readouterr().out
    assert "R3 value" in out and "NLBL_OLD -> CAD NLBL" in out and "R2-1: PDF 3V3, CAD SIG" in out and "not a difference" in out


def test_auto_named_alone_is_not_a_difference(setup):
    proj, pdf, atlas = setup
    bad = copy.deepcopy(pdf)
    rename(bad, "LINK", "NetR1_2", auto=True)
    assert not boarddiff.diff_netlists(bad, cad.read_project(str(proj)))["differs"]


def test_parts_and_pins_on_one_side(setup):
    proj, pdf, atlas = setup
    bad = copy.deepcopy(pdf)
    del bad["components"]["C2"]
    bad["components"]["X9"] = {"mpn": None, "value": None, "pins": {"1": {"net": "SIG"}}}
    del bad["components"]["R1"]["pins"]["2"]
    bad["components"]["R1"]["pins"]["7"] = {"net": "SIG"}
    d = boarddiff.diff_netlists(bad, cad.read_project(str(proj)))
    assert d["parts_cad_only"] == ["C2"] and d["parts_pdf_only"] == ["X9"]
    assert d["pins_cad_only"] == ["R1-2"] and d["pins_pdf_only"] == ["R1-7"]


def test_aliases_join_through_both_sides(setup):
    proj, pdf, atlas = setup
    bad = copy.deepcopy(pdf)
    rename(bad, "LINK", "LINK_OLD")
    bad["nets"]["LINK_OLD"]["aliases"] = ["LINK"]  # the PDF joined it under both names
    assert not boarddiff.diff_netlists(bad, cad.read_project(str(proj)))["differs"]


def test_variant_is_one_line_not_a_mismatch(setup, capsys):
    proj, pdf, atlas = setup
    put(atlas, pdf)
    assert run(["--cad", str(proj), "--board", "Gen", "--variant", "NoOpts"], atlas) == 0
    out = capsys.readouterr().out
    assert "does not fit 1 of them" in out and "R2" in out and "no difference" in out
    assert run(["--cad", str(proj), "--board", "Gen", "--variant", "NoOpts", "--json"], atlas) == 0
    assert json.loads(capsys.readouterr().out)["variant"]["not_fitted_vs_pdf_fitted"] == ["R2"]


def test_refusals_are_one_line(setup, capsys):
    proj, pdf, atlas = setup
    assert run(["--cad", str(proj), "--board", "Gen"], atlas) == 2
    assert capsys.readouterr().err.count("\n") == 1
    put(atlas, pdf)
    assert run(["--cad", str(proj), "--board", "Gen", "--variant", "Nope"], atlas) == 2
    assert capsys.readouterr().err.count("\n") == 1

"""pinnames: names for pins the PDF left unnamed, kept across a re-run, read by join, each with its source."""
from __future__ import annotations

import pytest

from embarch_atlas import cli, common, join, pinnames
from embarch_atlas.smartpdf import MIN_NAMED_PINS
from test_interfaces import comp, netlist
from test_smartpdf import pdf  # noqa: F401  (the synthetic Smart PDF fixture)


def board(tmp_path):
    adir = tmp_path / "atlas"
    u = comp({"1": "VIN", "2": "GND", "3": "EN"}, "IC3")
    u["pins"]["1"]["name"] = "IN"
    nl = netlist("B", {"U1": u, "R1": comp({"1": "VIN", "2": "EN"}, None)})
    nl["counts"] = {"nets": len(nl["nets"]), "pins": 6, "pins_on_net": 6}
    common.write_json(adir / "boards" / "B" / "netlist.json", nl)
    return adir


def test_apply_marks_every_name_with_its_source():
    nl = netlist("B", {"U1": comp({"1": "A", "2": "B", "3": "C"})})
    nl["components"]["U1"]["pins"]["1"]["name"] = "IN"
    pinnames.apply(nl, {"U1": {"2": {"name": "GND", "prov": "llm-unverified"}, "1": {"name": "X", "prov": "engineer-declared"}}})
    pins = nl["components"]["U1"]["pins"]
    assert (pins["1"]["name"], pins["1"]["name_source"]) == ("IN", "pdf-text")      # the PDF's text wins
    assert (pins["2"]["name"], pins["2"]["name_source"]) == ("GND", "overlay:llm-unverified")
    assert pins["3"]["name"] is None and "name_source" not in pins["3"]
    c = nl["counts"]
    assert (c["pins_named"], c["pins_named_from_overlay"], c["pins_unnamed_on_3plus_pin_parts"]) == (2, 1, 1)
    pinnames.apply(nl, {})                                                             # a dropped entry does not linger
    assert pins["2"]["name"] is None and nl["counts"]["pins_unnamed_on_3plus_pin_parts"] == 2
    assert MIN_NAMED_PINS == 3


def test_declare_writes_overlay_merges_netlist_and_replaces(tmp_path, capsys):
    adir = board(tmp_path)
    e = pinnames.declare(adir, "B", "U1", "3", "ENABLE", note="sheet 2 crop")
    assert e == {"board": "B", "ref": "U1", "pin": "3", "name": "ENABLE", "prov": "llm-unverified", "note": "sheet 2 crop"}
    ov = common.read_json(adir / "boards" / "B" / "pin_names.json")
    assert ov == {"board": "B", "pins": {"U1": {"3": {"name": "ENABLE", "prov": "llm-unverified", "note": "sheet 2 crop"}}}}
    p = common.read_json(adir / "boards" / "B" / "netlist.json")["components"]["U1"]["pins"]["3"]
    assert (p["name"], p["name_source"]) == ("ENABLE", "overlay:llm-unverified")
    pinnames.declare(adir, "B", "U1", "3", "EN_N", prov="engineer-confirmed")
    ov = common.read_json(adir / "boards" / "B" / "pin_names.json")["pins"]["U1"]
    assert ov == {"3": {"name": "EN_N", "prov": "engineer-confirmed"}}
    nl = common.read_json(adir / "boards" / "B" / "netlist.json")
    assert nl["components"]["U1"]["pins"]["3"]["name_source"] == "overlay:engineer-confirmed"
    assert nl["counts"]["pins_unnamed_on_3plus_pin_parts"] == 1   # U1-2 is still unnamed
    assert "U1-3/EN_N" in (adir / "boards" / "B" / "nets.md").read_text()
    rc = cli.main(["--atlas-dir", str(adir), "declare-pin-name", "B", "R1", "1", "P1"])
    assert rc == 0 and "llm-unverified" in capsys.readouterr().out


@pytest.mark.parametrize("kw, why", [
    ({"board": "Z"}, "no netlist"),
    ({"ref": "U9"}, "not a part"),
    ({"pin": "9"}, "no pin"),
    ({"pin": "1"}, "already named"),
    ({"name": " "}, "empty"),
    ({"prov": "deterministic"}, "prov"),
])
def test_declare_refuses(tmp_path, kw, why):
    adir = board(tmp_path)
    args = {"board": "B", "ref": "U1", "pin": "3", "name": "EN"} | kw
    with pytest.raises(ValueError, match=why):
        pinnames.declare(adir, **args)
    assert not (adir / "boards" / "B" / "pin_names.json").exists()


def test_schematic_rerun_keeps_overlay_and_join_reads_it(pdf, tmp_path, capsys):  # noqa: F811
    adir = tmp_path / "atlas"
    assert cli.main(["--atlas-dir", str(adir), "schematic", str(pdf), "--board", "T"]) == 0
    out = adir / "boards" / "T"
    nl = common.read_json(out / "netlist.json")
    assert nl["components"]["U1"]["pins"]["1"]["name_source"] == "pdf-text"
    assert nl["components"]["R1"]["pins"]["1"]["name"] is None
    pinnames.declare(adir, "T", "R1", "1", "SIDE_A", prov="engineer-declared")
    with pytest.raises(ValueError, match="already named"):
        pinnames.declare(adir, "T", "U1", "1", "OTHER")
    capsys.readouterr()
    assert cli.main(["--atlas-dir", str(adir), "schematic", str(pdf), "--board", "T"]) == 0
    line = capsys.readouterr().out
    assert "1 from pin_names.json" in line and "still unnamed" in line
    nl = common.read_json(out / "netlist.json")
    r1 = nl["components"]["R1"]["pins"]["1"]
    assert (r1["name"], r1["name_source"]) == ("SIDE_A", "overlay:engineer-declared")
    assert nl["counts"]["pins_named_from_overlay"] == 1
    B = join.Board("T", nl)
    assert B.pin_name["R1-1"] == "SIDE_A" and B.pin_name_source["R1-1"] == "overlay:engineer-declared"

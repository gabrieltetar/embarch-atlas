"""diff: netlist revision diff, and two atlases compared end to end."""
from __future__ import annotations

import copy
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_join import make_project, run_join  # noqa: E402

from embarch_atlas import diff  # noqa: E402
from embarch_atlas.common import read_json, write_json  # noqa: E402


def _nl(comps: dict, nets: dict) -> dict:
    return {"components": comps, "nets": {k: {"pins": v} for k, v in nets.items()}}


def test_diff_netlist_marks_stale_designators():
    old = _nl({"U1": {"mpn": "PART-A"}, "R1": {"value": "10kΩ"}, "TP1": {}, "C1": {"value": "1uF", "dni": False}},
              {"N1": ["U1-1", "R1-1"], "N2": ["R1-2", "TP1-1"], "GND": ["C1-2"]})
    new = copy.deepcopy(old)
    new["components"]["U1"]["mpn"] = "PART-A-REV"          # re-MPN'd
    del new["components"]["TP1"]                            # deleted test point
    new["components"]["C1"]["dni"] = True                   # now not fitted
    new["components"]["R1"]["value"] = "4.7kΩ"
    new["nets"]["N2"]["pins"] = ["R1-2"]
    new["nets"]["N3"] = {"pins": ["C1-1"]}
    d = diff.diff_netlist(old, new)
    assert d["components_removed"] == ["TP1"] and d["mpn_changed"] == [["U1", "PART-A", "PART-A-REV"]]
    assert d["value_changed"] == [["R1", "10kΩ", "4.7kΩ"]] and d["dni_changed"] == [["C1", False, True]]
    assert d["nets_added"] == ["N3"] and d["nets_changed"] == {"N2": {"+": [], "-": ["TP1-1"]}}
    assert d["stale_designators"] == ["C1", "R1", "TP1", "U1"]
    assert diff.diff_netlist(old, old)["stale_designators"] == []


def test_diff_two_atlases(tmp_path):
    ad, a = make_project(tmp_path)
    run_join(ad, a)
    # a later "commit": one DT flag flips, one pin gains a DT use, a board part is re-valued
    b = ad / "atlases" / "demo@bbbbbbbbbbbb"
    b.mkdir(parents=True)
    st = read_json(a / "atlas.json")
    st.update({"id": b.name, "commit": "b" * 40})
    write_json(b / "atlas.json", st)
    dtj = read_json(a / "dt.json")
    for u in dtj["uses"]:
        if u["prop"] == "gpios" and u["pin"] == "PB1":
            u["flags"] = "ACTIVE_LOW"
    dtj["uses"].append({"pin": "PA2", "node": "/aux", "kind": "gpio", "prop": "aux-gpios", "labels": ["aux"], "compat": None,
                        "compats": [], "flags": "ACTIVE_HIGH", "index": 0, "loc": "fw:board.dts:50"})
    dtj["nodes"]["/aux"] = {"labels": ["aux"], "compats": [], "okay": True, "ordinal": 50, "regs": [], "parent": "/", "props": {}}
    write_json(b / "dt.json", dtj)
    shutil.copy(a / "in_use.json", b / "in_use.json")
    nl = read_json(ad / "boards" / "M" / "netlist.json")
    nl["components"]["R5"]["value"] = "0.01 Ω"
    write_json(ad / "boards" / "M" / "netlist.json", nl)
    run_join(ad, b)
    d = diff.diff(a, b)
    assert d["stamp"]["commit"][1] == "b" * 40
    assert any(x.startswith("PA2 /aux aux-gpios") for x in d["dt"]["added"])
    assert any(x.startswith("PB1 /buttons/btn_0 gpios") and "ACTIVE_LOW|PULL_UP -> ACTIVE_LOW" in x for x in d["dt"]["changed"])
    pins = {r.get("port"): r for r in d["pins"]["pins"]}
    assert "aux.aux-gpios ACTIVE_HIGH" in pins["PA2"]["dt"]["+"]
    new = {(c["class"], c["key"]) for c in d["mismatches"]["new"]}
    gone = {(c["class"], c["key"]) for c in d["mismatches"]["resolved"]}
    assert ("connected-no-dt", "PA2") in gone
    assert ("dt-value-vs-bom", "/soc/i2c@40005400/mon-a@44") in new      # 5 mOhm in DT, 10 mOhm on the board now
    nd = d["inputs"]["netlists"]["M"]
    assert nd["value_changed"] == [["R5", "0.005 Ω", "0.01 Ω"]] and nd["stale_designators"] == ["R5"]
    md = diff.render(d)
    for h in ("## Stamp", "## Devicetree pin uses", "## Pin/net map", "## Mismatch report", "## Inputs"):
        assert h in md
    assert "stale designators: R5" in md


def test_diff_same_atlas_is_empty(tmp_path):
    ad, a = make_project(tmp_path)
    run_join(ad, a)
    d = diff.diff(a, a)
    assert d["dt"]["added"] == d["dt"]["removed"] == d["dt"]["changed"] == []
    assert d["pins"]["pins"] == [] and d["mismatches"]["new"] == [] and d["inputs"]["netlists"] == {}
    assert "- no change" in diff.render(d)

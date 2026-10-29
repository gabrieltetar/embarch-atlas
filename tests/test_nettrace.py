"""net_trace over a small synthetic two-board system (invented parts and nets)."""
from pathlib import Path

import pytest

from embarch_atlas import query
from embarch_atlas.common import write_json, write_jsonl


def _pins(**nets):
    return {num: {"name": name, "net": net} for num, (name, net) in nets.items()}


@pytest.fixture()
def atlas_dir(tmp_path: Path) -> Path:
    a = tmp_path / "atlas"
    d = a / "docs" / "xreg10@1"
    write_json(d / "doc.json", {"doc_id": "xreg10@1", "part": "XREG10", "kind": "datasheet"})
    base = {"kind": "pin", "doc": "xreg10@1", "p": 3, "src": "table", "prov": "llm-verified"}
    write_jsonl(d / "records" / "pin.jsonl", [
        dict(base, id="xreg10.pin.VIN", pin=1, name="VIN", dir="power", flow="sink"),
        dict(base, id="xreg10.pin.VOUT", pin=2, name="VOUT", dir="power", flow="source"),
        dict(base, id="xreg10.pin.EN", pin=3, name="EN", dir="in", type="logic"),
        dict(base, id="xreg10.pin.GND", pin=4, name="GND", dir="power", type="ground")])
    comps_a = {
        "U1": {"mpn": "XREG10-DBV", "pins": _pins(**{"1": ("VIN", "VIN_RAW"), "2": ("VOUT", "V3"),
                                                     "3": ("EN", "REG_EN"), "4": ("GND", "GND")})},
        "U9": {"mpn": "DEMO-MCU", "pins": _pins(**{"1": ("PA3", "REG_EN"), "2": ("VDD", "V3")})},
        "Q1": {"mpn": "NODOC1", "pins": _pins(**{"1": (None, "V3"), "2": (None, "GND")})},
        "C1": {"mpn": "CAP", "value": "10uF", "pins": _pins(**{"1": (None, "VIN_RAW"), "2": (None, "GND")})},
        "FB1": {"mpn": "FERRITE", "value": "600R", "pins": _pins(**{"1": (None, "V3"), "2": (None, "V3_FILT")})},
        "R2": {"mpn": "RES", "value": "10k", "pins": _pins(**{"1": (None, "V3_FILT"), "2": (None, "SENSE")})},
        "J1": {"mpn": "BARREL", "pins": _pins(**{"1": (None, "VIN_RAW"), "2": (None, "GND")})},
        "J3": {"mpn": "HDR3", "pins": _pins(**{"1": (None, "V3"), "2": (None, "SIG1"), "3": (None, "SIG2")})},
    }
    comps_b = {
        "J2": {"mpn": "HDR3", "pins": _pins(**{"1": (None, "V3"), "2": (None, "SIG1"), "3": (None, "SIG2")})},
        "U5": {"mpn": "NODOC2", "pins": _pins(**{"1": ("VCC", "V3")})},
    }
    for label, comps in (("A", comps_a), ("B", comps_b)):
        nets: dict = {}
        for ref, c in comps.items():
            for num, p in c["pins"].items():
                nets.setdefault(p["net"], {"pins": []})["pins"].append(f"{ref}-{num}")
        write_json(a / "boards" / label / "netlist.json", {"board": label, "components": comps, "nets": nets})
    at = a / "atlases" / "core@000000000002"
    write_json(at / "atlas.json", {"id": "core@000000000002", "commit": "0" * 40, "schematic_boards": ["A", "B"],
                                   "mcu": {"ref": "A:U9"}})
    write_json(at / "dt.json", {"uses": [{"pin": "PA3", "node": "/reg", "prop": "enable-gpios", "labels": ["reg"],
                                          "compat": "regulator-fixed"}],
                                "nodes": {"/reg": {"compats": ["regulator-fixed"], "labels": ["reg"]}}})
    write_json(a / "current.json", {"matches": [{"atlas": "core@000000000002"}]})
    return a


def test_net_trace_members_cross_the_connector_and_name_the_feed(atlas_dir):
    out = query.net_trace(query.Atlas(atlas_dir), "A:V3")
    assert out.startswith("src: boards/A,B netlist.json + connector mates")
    assert "A:U1-2 VOUT XREG10-DBV dir=power flow=source (xreg10@1 p3)" in out
    assert "B:V3 (" in out and "reached through A:J3-1 => B:J2-1" in out
    assert "A:U9-2 VDD (MCU supply or special pin)" in out
    up = out.split("## Upstream", 1)[1]
    assert "fed by A:U1-2 XREG10-DBV VOUT flow=source" in up
    assert "enable EN on A:REG_EN: MCU PA3 (DT reg.enable-gpios)" in up
    assert "input connector A:J1-1" in up
    assert "- NODOC1: A:Q1" in out and "- NODOC2: B:U5" in out


def test_net_trace_follows_low_ohm_series_parts_not_pulls(atlas_dir):
    a = query.Atlas(atlas_dir)
    out = query.net_trace(a, "A:V3_FILT")
    assert "through FB1 600R (series; direction not stated)" in out and "fed by A:U1-2" in out
    assert "through R2" not in out.split("## Upstream", 1)[1]


def test_net_trace_part_pin_by_name_part_view_and_near_names(atlas_dir):
    a = query.Atlas(atlas_dir)
    assert "A:U1-3 EN is on A:REG_EN" in query.net_trace(a, "A:U1-EN")
    part = query.net_trace(a, "A:Q1")
    assert "A:Q1 NODOC1 no datasheet" in part and "A:Q1-1 1 -> A:V3" in part
    assert "A:V3 (" in query.net_trace(a, "A:V_3")
    miss = query.net_trace(a, "A:V33")
    assert miss.startswith("src: none") and "Nearest names: A:V3" in miss


def test_pin_trace_miss_points_to_net_trace(atlas_dir):
    out = query.pin_trace(query.Atlas(atlas_dir), "V3_FILT")
    assert out.startswith("src: none") and "net_trace" in out


def test_net_trace_pages_to_the_uncut_answer(atlas_dir, monkeypatch):
    from paging_synth import assert_exact

    a = query.Atlas(atlas_dir)
    assert_exact(monkeypatch, "net_trace", lambda o: query.net_trace(a, "A:V3", offset=o))

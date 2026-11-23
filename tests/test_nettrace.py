"""net_trace over a small synthetic two-board system (invented parts and nets)."""
import asyncio
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


def test_net_find_substring_glob_board_filter_and_no_match(atlas_dir):
    a = query.Atlas(atlas_dir)
    sub = query.net_find(a, "sig")                                   # case-insensitive substring, both boards
    assert sub.startswith("src: boards/A,B netlist.json")
    assert "A:SIG1  J3-2\n" in sub and "B:SIG2  J2-3\n" in sub
    assert sub.index("A:SIG1") < sub.index("A:SIG2") < sub.index("B:SIG1")      # board, then net name
    glob = query.net_find(a, "V3*")                                  # glob matches the whole name
    assert "A:V3  " in glob and "A:V3_FILT  FB1-2 R2-1" in glob and "B:V3  J2-1 U5-1" in glob
    assert "SIG1" not in glob
    assert "A:V3  " in query.net_find(a, "V?")                       # `?` is one character
    only_b = query.net_find(a, "V3*", board="B")
    assert "B:V3  " in only_b and "A:V3" not in only_b
    miss = query.net_find(a, "NOSUCH*")
    assert "No net name matches 'NOSUCH*'" in miss and "A:" not in miss.split("matches", 1)[1].split("\n")[0].replace("A,B", "")
    assert "not found" in query.net_find(a, "V3", board="Z")


# ---------------------------------------------------------------- parts

def _parts_atlas(atlas_dir):
    write_json(atlas_dir / "boards" / "A" / "bom.json", [
        {"ref": "R1", "mpn": "RC0402FR-0786K6L", "value": "86.6K", "dni": False,
         "description": "RES SMD 86.6K OHM 1% 1/16W 0402", "tolerance": "1%", "voltage": None, "power": "1/16W"},
        {"ref": "R2", "mpn": None, "value": "10K", "dni": True, "description": "RES 10K OHM", "tolerance": None,
         "voltage": None, "power": None},
        {"ref": "C1", "mpn": "CAP1", "value": "0.1uF", "dni": False, "description": "CAP CER 0.1UF 16V X7R 0402"},
    ])
    return query.Atlas(atlas_dir)


def test_parse_description_reads_passives_and_never_guesses():
    from embarch_atlas import bomvals

    assert bomvals.parse_description("RES SMD 86.6K OHM 1% 1/16W 0402") == {"tolerance": "1%", "voltage": None, "power": "1/16W"}
    assert bomvals.parse_description("CAP CER 0.1UF 16V X7R 0402") == {"tolerance": None, "voltage": "16V", "power": None}
    assert bomvals.parse_description("CAP CER 10PF +/-0.25pF 50V C0G") == {"tolerance": "0.25pF", "voltage": "50V", "power": None}
    assert bomvals.parse_description("RES 0.1 OHM ±5% 250mW 0603")["power"] == "250mW"
    assert bomvals.parse_description("RES 10K OHM 0402") == {"tolerance": None, "voltage": None, "power": None}
    assert bomvals.parse_description(None) == {"tolerance": None, "voltage": None, "power": None}
    got = bomvals.from_props({"Tolerance": "0.1%"}, "RES 10K 1% 1/10W", None)
    assert got == {"tolerance": "0.1%", "voltage": None, "power": "1/10W"}        # a dedicated field wins


def test_parts_lookup_rows_unknowns_and_references_not_found(atlas_dir):
    a = _parts_atlas(atlas_dir)
    out = query.parts(a, "A", ["R1", "R2", "C1", "R99"])
    assert out.startswith("src: boards/A bom.json + netlist.json")
    assert "A:R1 mpn=RC0402FR-0786K6L value=86.6K tol=1% rating=1/16W dni=no" in out
    assert "A:R2 mpn=" in out and " value=10K tol=unknown rating=unknown dni=yes" in out
    assert "A:C1 mpn=CAP1 value=0.1uF tol=unknown rating=16V dni=no" in out     # an old bom.json: read from the description
    assert "3 of 4 references found" in out and "Not found on A: R99." in out
    assert "R1" in query.parts(a, "A", "R1,R2") and "not found" in query.parts(a, "Z", ["R1"])


def test_parts_is_a_read_only_mcp_tool_and_a_q_verb(atlas_dir):
    pytest.importorskip("mcp")
    from embarch_atlas.mcp_server import build_server

    tools = {t.name: t for t in asyncio.run(build_server(atlas_dir, live=False).list_tools())}
    assert "parts" in tools and tools["parts"].annotations.read_only_hint
    assert "parts" in query.TOOLS


def test_parts_md_rows_carry_a_known_tolerance():
    from embarch_atlas import smartpdf

    nl = {"board": "A", "source": {"file": "x.pdf"}, "counts": {"parts": 2, "symbol_parts": 2, "parts_with_mpn": 0, "parts_dni": 0},
          "components": {r: {"mpn": None, "value": "10K", "dni": False, "pins": {}, "pages": [1]} for r in ("R1", "R2")}, "nets": {}}
    bom = [{"ref": "R1", "tolerance": "1%"}, {"ref": "R2", "tolerance": None}]
    md = smartpdf.render_parts_md(nl, bom)
    assert "A:R1 - 10K tol=1% p1" in md and "A:R2 - 10K p1" in md


# ---------------------------------------------------------------- a part whose datasheet has two package pinouts

def _two_pkg_atlas(tmp_path: Path, u1_pins: dict, u1_extra: dict | None = None, bindings: dict | None = None) -> Path:
    """XLDO-18 distils a 5-pin SOT-23-5 pinout (1 GND, 2 VIN, 3 EN, 4 NC, 5 VOUT) and a 6-pin WSON6 pinout
    (1 VOUT, 2 NC, 3 GND, 4 EN, 5 NC, 6 VIN): pin 1 is ground in one and the output in the other."""
    a = tmp_path / "atlas"
    d = a / "docs" / "xldo18@1"
    write_json(d / "doc.json", {"doc_id": "xldo18@1", "part": "XLDO-18", "kind": "datasheet"})
    pk = {"SOT-23-5": [("GND", "ground"), ("VIN", "supply"), ("EN", None), ("NC", None), ("VOUT", "supply")],
          "WSON6": [("VOUT", "supply"), ("NC", None), ("GND", "ground"), ("EN", None), ("NC", None), ("VIN", "supply")]}
    recs = []
    for pkg, pins in pk.items():
        for i, (name, typ) in enumerate(pins, 1):
            r = {"id": f"xldo18.pin.{pkg}.{i}", "kind": "pin", "doc": "xldo18@1", "p": 2, "src": "table",
                 "prov": "llm-verified", "pin": i, "name": name, "package": pkg,
                 "dir": "power" if typ else "in"}
            if name == "GND":
                r["type"] = "ground"
            if name == "VOUT":
                r["flow"] = "source"
            if name == "VIN":
                r["flow"] = "sink"
            recs.append(r)
    write_jsonl(d / "records" / "pin.jsonl", recs)
    comps = {"U1": dict({"mpn": "XLDO-18", "pins": u1_pins}, **(u1_extra or {})),
             "C1": {"mpn": "CAP", "value": "1uF", "pins": _pins(**{"1": (None, "V1P8"), "2": (None, "GND")})},
             "J1": {"mpn": "BARREL", "pins": _pins(**{"1": (None, "VIN_RAW"), "2": (None, "GND")})}}
    nets: dict = {}
    for ref, c in comps.items():
        for num, p in c["pins"].items():
            nets.setdefault(p["net"], {"pins": []})["pins"].append(f"{ref}-{num}")
    write_json(a / "boards" / "A" / "netlist.json", {"board": "A", "components": comps, "nets": nets})
    if bindings:
        write_json(a / "boards" / "bindings.json", bindings)
    at = a / "atlases" / "core@000000000003"
    write_json(at / "atlas.json", {"id": "core@000000000003", "commit": "0" * 40, "schematic_boards": ["A"]})
    write_json(at / "dt.json", {"uses": [], "nodes": {}})
    write_json(a / "current.json", {"matches": [{"atlas": "core@000000000003"}]})
    return a


WSON_FITTED = _pins(**{"1": ("VOUT", "V1P8"), "2": ("NC", "NC2"), "3": ("GND", "GND"), "4": ("EN", "VIN_RAW"),
                       "5": ("NC", "NC5"), "6": ("VIN", "VIN_RAW")})


def test_net_trace_names_the_pins_of_the_fitted_package(tmp_path):
    out = query.net_trace(query.Atlas(_two_pkg_atlas(tmp_path, WSON_FITTED)), "A:V1P8")
    assert "A:U1-1 VOUT XLDO-18 dir=power flow=source (xldo18@1 p2)" in out
    assert "GND" not in out.split("## Upstream", 1)[0].split("A:U1-1", 1)[1].splitlines()[0]
    assert "fed by A:U1-1 XLDO-18 VOUT flow=source" in out.split("## Upstream", 1)[1]
    assert "package not resolved" not in out


def test_net_trace_unresolved_package_shows_the_schematic_name_and_says_so(tmp_path):
    blank = {num: dict(p, name=None) for num, p in WSON_FITTED.items() if num in "1236"}   # 4 pins, no symbol names
    out = query.net_trace(query.Atlas(_two_pkg_atlas(tmp_path, blank)), "A:V1P8")
    assert "A:U1-1 XLDO-18 (schematic pin name; package not resolved: " in out
    assert "package not resolved, so its pin roles are not read" in out
    assert "fed by" not in out and "GND" not in out.split("A:U1-1", 1)[1].splitlines()[0]


def test_net_trace_declared_package_wins_and_footprint_names_it(tmp_path):
    names = {n: dict(p, name=None) for n, p in WSON_FITTED.items()}
    out = query.net_trace(query.Atlas(_two_pkg_atlas(tmp_path / "a", names, bindings={"packages": {"U1": "wson6"}})), "A:V1P8")
    assert "A:U1-1 VOUT XLDO-18 dir=power flow=source" in out
    out = query.net_trace(query.Atlas(_two_pkg_atlas(tmp_path / "b", names, {"footprint": "WSON6_2x2mm"})), "A:V1P8")
    assert "A:U1-1 VOUT XLDO-18 dir=power flow=source" in out


def test_net_trace_resolves_the_package_by_pin_count(tmp_path):
    names = {n: dict(p, name=None) for n, p in WSON_FITTED.items()}
    out = query.net_trace(query.Atlas(_two_pkg_atlas(tmp_path, names)), "A:V1P8")
    assert "A:U1-1 VOUT XLDO-18 dir=power flow=source" in out

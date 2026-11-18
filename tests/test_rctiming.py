"""`q rc_timing` on an invented board (R1 10k 1%, C1 1uF 5%, a 3.3 V regulator, a gate): a hand-computed case,
the assumed temperature characteristic, discharging, and each named gap."""
from __future__ import annotations

import asyncio
import math
from pathlib import Path

import pytest

from embarch_atlas import query, rctiming as T
from embarch_atlas.common import write_json, write_jsonl


def _doc(a: Path, name: str, part: str, recs: list[dict]):
    d = a / "docs" / name
    write_json(d / "doc.json", {"doc_id": name, "part": part, "kind": "datasheet"})
    base = {"doc": name, "p": 7, "src": "table", "prov": "llm-verified"}
    for kind in {r["kind"] for r in recs}:
        write_jsonl(d / "records" / f"{kind}.jsonl", [dict(base, **r) for r in recs if r["kind"] == kind])


def _pins(**p):
    return {n: {"name": nm, "net": net} for n, (nm, net) in p.items()}


@pytest.fixture()
def ad(tmp_path: Path) -> Path:
    a = tmp_path / "atlas"
    comps = {
        "U1": {"mpn": "XREG33", "pins": _pins(**{"1": ("VOUT", "V33"), "2": ("GND", "GND")})},
        "U2": {"mpn": "XGATE", "pins": _pins(**{"1": ("IN", "SLOW"), "2": ("VDD", "V33")})},
        "R1": {"mpn": "RES", "value": "10k", "pins": _pins(**{"1": (None, "V33"), "2": (None, "SLOW")})},
        "C1": {"mpn": "CAP", "value": "1uF", "pins": _pins(**{"1": (None, "SLOW"), "2": (None, "GND")})},
        "R5": {"mpn": "RES", "value": "10k", "pins": _pins(**{"1": (None, "PD"), "2": (None, "GND")})},
        "C5": {"mpn": "CAP", "value": "1uF", "pins": _pins(**{"1": (None, "PD"), "2": (None, "GND")})},
        "U3": {"mpn": "XGATE", "pins": _pins(**{"1": ("IN", "PD"), "2": ("VDD", "V33")})},
        "R9": {"mpn": "RES", "value": "10k", "pins": _pins(**{"1": (None, "MULTI"), "2": (None, "V33")})},
        "R10": {"mpn": "RES", "value": "1k", "pins": _pins(**{"1": (None, "MULTI"), "2": (None, "V33")})},
        "C9": {"mpn": "CAP", "value": "1uF", "pins": _pins(**{"1": (None, "MULTI"), "2": (None, "GND")})},
    }
    nets: dict = {}
    for ref, c in comps.items():
        for num, p in c["pins"].items():
            nets.setdefault(p["net"], {"pins": []})["pins"].append(f"{ref}-{num}")
    write_json(a / "boards" / "A" / "netlist.json", {"board": "A", "components": comps, "nets": nets})
    write_json(a / "boards" / "A" / "bom.json", [
        {"ref": "R1", "value": "10k", "dni": False, "description": "RES 10K OHM 1% 1/16W", "tolerance": "1%"},
        {"ref": "C1", "value": "1uF", "dni": False, "description": "CAP CER 1UF 16V", "tolerance": "5%"},
        {"ref": "R5", "value": "10k", "dni": False, "description": "RES 10K OHM 1%", "tolerance": "1%"},
        {"ref": "C5", "value": "1uF", "dni": False, "description": "CAP CER 1UF 16V X7R", "tolerance": "5%"},
        {"ref": "R9", "value": "10k", "dni": False, "description": "RES 10K", "tolerance": None},
        {"ref": "R10", "value": "1k", "dni": False, "description": "RES 1K", "tolerance": None},
        {"ref": "C9", "value": "1uF", "dni": False, "description": "CAP", "tolerance": None}])
    _doc(a, "xreg33@1", "XREG33", [
        {"id": "xreg33.pin.VOUT", "kind": "pin", "pin": 1, "name": "VOUT", "dir": "power", "flow": "source"},
        {"id": "xreg33.limit.vout", "kind": "limit", "group": "recommended", "sym": "VOUT", "min": 3.234, "typ": 3.3,
         "max": 3.366, "unit": "V"}])
    _doc(a, "xgate@1", "XGATE", [
        {"id": "xgate.limit.vih.low", "kind": "limit", "group": "recommended", "sym": "VIH", "cond": "VDD = 1.8 V",
         "min": 1.2, "unit": "V"},
        {"id": "xgate.limit.vih", "kind": "limit", "group": "recommended", "sym": "VIH", "cond": "VDD = 3.0 V to 3.6 V",
         "min": 2.0, "unit": "V"},
        {"id": "xgate.limit.vil", "kind": "limit", "group": "recommended", "sym": "VIL", "cond": "VDD = 3.0 V to 3.6 V",
         "max": "0.3*VDD", "unit": "V"}])
    at = a / "atlases" / "core@000000000001"
    write_json(at / "atlas.json", {"id": "core@000000000001", "commit": "0" * 40})
    write_json(at / "pins.json", {"part_docs": {"A:U1": ["xreg33@1"], "A:U2": ["xgate@1"], "A:U3": ["xgate@1"]}})
    write_json(a / "current.json", {"matches": [{"atlas": "core@000000000001"}]})
    return a


def run(ad, node, **kw):
    return query.rc_timing(query.Atlas(ad), node, **kw)


def test_hand_computed_charging_case_to_three_significant_figures(ad):
    # tau_min = 9.9k * 0.95uF = 9.405 ms, tau_max = 10.1k * 1.05uF = 10.605 ms; VIH = 2.0 V, rail 3.234..3.366 V.
    # fastest at 3.366 V: ln(3.366/1.366) = 0.90184 -> 8.48 ms; slowest at 3.234 V: ln(3.234/1.234) = 0.96346 -> 10.2 ms.
    out = run(ad, "A:SLOW")
    assert out.startswith("src: boards/A netlist.json + bom.json; ") and "prov=derived" in out.split("\n")[0]
    assert "result (derived): t = 8.482 ms .. 10.22 ms" in out
    assert "8.48" in out and "10.2" in out
    assert "tol 1% (bom.json tolerance)" in out and "no temperature characteristic" in out
    assert "xgate@1 limit xgate.limit.vih p.7" in out and "xreg33@1 limit xreg33.limit.vout" in out
    assert "formula: t = tau * ln(V / (V - Vth))" in out and "margin" not in out.split("inputs:")[0]


def test_margin_is_printed_beside_never_folded_in(ad):
    out = run(ad, "A:SLOW", margin=10)
    assert "result (derived): t = 8.482 ms .. 10.22 ms" in out
    assert "margin 10% passed by the caller (not part of the result above): 7.634 ms .. 11.24 ms" in out


def test_eia_temperature_characteristic_is_named_assumed(ad):
    out = run(ad, "A:PD", rail="V33")
    assert "X7R (ASSUMED" in out and "- C5 temperature characteristic X7R: -15% / +15% from the EIA class table" in out
    # discharging: C = 1uF*(1 - 0.05 - 0.15) .. 1uF*(1 + 0.05 + 0.15); VIL = 0.3 x supply: t = tau * ln(1/0.3)
    k = math.log(1 / 0.3)
    lo, hi = 9900 * 0.8e-6 * k, 10100 * 1.2e-6 * k
    assert f"t = {T.fmt_time(lo)} .. {T.fmt_time(hi)}" in out
    assert "discharging from the rail" in out and "0.3 x supply" in out


def test_discharging_node_needs_a_rail(ad):
    out = run(ad, "A:PD")
    assert "Not handled" in out and "pass `rail=<net>`" in out


def test_a_node_that_is_not_one_r_and_one_c_names_the_parts(ad):
    out = run(ad, "A:MULTI")
    assert "Not handled" in out and "2 resistor(s) and 1 capacitor(s)" in out and "A:R9" in out and "A:R10" in out
    assert "Nothing was approximated" in out


def test_missing_tolerance_is_named_and_an_override_is_marked_assumed(ad):
    bom = ad / "boards" / "A" / "bom.json"
    import json
    rows = json.loads(bom.read_text())
    for r in rows:
        if r["ref"] in ("R1", "C1"):
            r["tolerance"] = None
    write_json(bom, rows)
    out = run(ad, "A:SLOW")
    assert "Not computed" in out and "R1 has no tolerance" in out and "C1 has no tolerance" in out
    out = run(ad, "A:SLOW", r_tol=1, c_tol=5)
    assert "result (derived): t = 8.482 ms .. 10.22 ms" in out
    assert "R1 tolerance 1% given by the caller" in out and "ASSUMED: given by the caller" in out


def test_missing_regulator_or_threshold_fact_is_a_named_gap(ad):
    (ad / "docs" / "xreg33@1" / "records" / "limit.jsonl").unlink()
    out = run(ad, "A:SLOW")
    assert "Not computed" in out and "no output-voltage `limit` record" in out
    assert "ms" not in out.split("Not computed")[1].split("Found:")[0]


def test_unknown_node_and_board(ad):
    assert "net 'NOPE' not found" in run(ad, "A:NOPE")
    assert "board 'Z' not found" in run(ad, "Z:SLOW")


def test_rc_timing_is_a_read_only_mcp_tool_and_a_q_verb(ad):
    pytest.importorskip("mcp")
    from embarch_atlas.mcp_server import build_server

    tools = {t.name: t for t in asyncio.run(build_server(ad, live=False).list_tools())}
    assert "rc_timing" in tools and tools["rc_timing"].annotations.read_only_hint
    assert "rc_timing" in query.TOOLS

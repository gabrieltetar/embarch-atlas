"""The measured plane, read: a `via: live` fact beside the claim it bears on, and a finding where they disagree."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_join import make_project, run_join  # noqa: E402

from embarch_atlas import measured, measuredview, query  # noqa: E402
from embarch_atlas.common import read_json, write_json, write_jsonl, write_text  # noqa: E402

IMG = "a" * 64


def meas(fact, value, unit=None, flag=None, n=1, build="pinned"):
    rec = {"id": f"measured.{fact}.s{n}", "kind": "measurement", "doc": "d", "p": None, "src": "live", "prov": "measured",
           "fact": fact, "value": value, "via": "live",
           "receipt": {"calls": [{"seq": n, "hash": f"h{n}", "method": "POST", "route": "/live/mem-read", "status": 200}],
                       "board": {"role": "dut", "probe_serial": "P1", "chip": "C"}, "time": "2026-11-23T10:00:00Z",
                       "image": {"sha256": IMG, "at_call": IMG, "source": "read back", "build": build, "check": ""},
                       "live_log": None}}
    if unit:
        rec["unit"] = unit
    if flag:
        rec["flag"] = flag
    return rec


def put(ad: Path, *recs):
    write_jsonl(measured.path(ad), list(recs))


def report(tmp_path, *recs):
    ad, atlas = make_project(tmp_path)
    put(ad, *recs)
    run_join(ad, atlas)
    return read_json(atlas / "mismatches.json")


def found(rep):
    return [c for c in rep["candidates"] if c["class"] == "measured-vs-datasheet"]


def check(rep):
    return next(c for c in rep["checks"] if c["check"] == measuredview.CHECK)


def test_a_value_outside_the_datasheets_range_is_a_finding(tmp_path):
    rep = report(tmp_path, meas("sns701.ec.VS", 6.1, "V"))
    (f,) = found(rep)
    assert f["key"] == "sns701.ec.VS" and f["severity"] == "check"
    assert "measured 6.1 V via live (development evidence)" in f["summary"] and "2.7..5.5 V" in f["summary"]
    assert any("sns701@a p5" in e for e in f["evidence"]) and any("2026-11-23T10:00:00Z" in e for e in f["evidence"])
    assert (check(rep)["ran"], check(rep)["passed"]) == (1, 0)


def test_a_value_that_agrees_raises_no_finding_and_counts_as_passed(tmp_path):
    rep = report(tmp_path, meas("sns701.ec.VS", 3.3, "V"))
    assert found(rep) == [] and (check(rep)["ran"], check(rep)["passed"]) == (1, 1)


def test_a_finding_on_another_build_says_so(tmp_path):
    rep = report(tmp_path, meas("sns701.ec.VS", 9, "V", flag=measured.DIFFERENT_BUILD, build="different"))
    (f,) = found(rep)
    assert "measured on a different build" in f["summary"]


def test_what_cannot_be_compared_is_folded_not_a_finding(tmp_path):
    rep = report(tmp_path, meas("sns701.ec.VS", 3.3, "mV"), meas("sns701.quirk.alert_default", 1, n=2),
                 meas("sns701.nope", 1, n=3))
    assert found(rep) == []
    folded = rep["folded"]
    assert any("claim is in V, measured in mV" in x for x in folded["measured value shown beside its claim, not compared"])
    assert folded["measured fact whose record no document holds now"] == ["sns701.nope"]


def test_no_measurements_runs_nothing(tmp_path):
    rep = report(tmp_path)
    assert found(rep) == [] and measuredview.CHECK not in {c["check"] for c in rep["checks"]}


@pytest.mark.parametrize("claim,val,state", [
    ({"kind": "register", "reset": "0b00100000"}, "0x20", "agrees"),
    ({"kind": "register", "reset": "0x00000001"}, 2, "DISAGREES"),
    ({"kind": "field", "reset": "0b0"}, "0", "agrees"),
    ({"kind": "limit", "typ": 1.2}, 1.2, "not compared"),
    ({"kind": "limit", "max": 80}, 81, "DISAGREES"),
    ({"kind": "limit", "min": 1}, 1, "agrees"),
    ({"kind": "register", "reset": "0x1"}, "n/a", "not compared"),
])
def test_compare(claim, val, state):
    assert measuredview.compare(claim, {"value": val})[0] == state


@pytest.fixture()
def atlas_dir(tmp_path: Path) -> Path:
    a = tmp_path / "atlas"
    d = a / "docs" / "demo-bb@A"
    write_json(d / "doc.json", {"doc_id": "demo-bb@A", "part": "DEMO-BB", "doc_no": "DEMO001", "kind": "datasheet"})
    write_text(d / "card.md", "# DEMO-BB card\n\nBuck-boost.\n")
    reg = {"id": "demo-bb.reg.MODE", "kind": "register", "doc": "demo-bb@A", "name": "MODE", "addr": "0x06",
           "width": 8, "reset": "0b00100000", "access": "rw", "p": 9, "src": "table", "prov": "llm-verified"}
    lim = {"id": "demo-bb.ec.VOVP", "kind": "limit", "doc": "demo-bb@A", "p": 6, "src": "table", "prov": "llm-verified",
           "group": "ec", "sym": "VOVP", "param": "output OVP threshold", "min": 22.5, "typ": 23.5, "max": 24.5, "unit": "V"}
    write_jsonl(d / "records" / "register.jsonl", [reg])
    write_jsonl(d / "records" / "limit.jsonl", [lim])
    write_text(d / "facts.txt", "\n".join([
        "demo-bb.ec.VOVP kind=limit; group=ec; sym=VOVP; param=output OVP threshold; min=22.5; typ=23.5; max=24.5; unit=V; cite=demo-bb@A p6; prov=llm-verified",
        "demo-bb.reg.MODE kind=register; name=MODE; reset=0b00100000; cite=demo-bb@A p9; prov=llm-verified"]))
    return a


def test_register_shows_the_measured_value_beside_the_datasheets(atlas_dir):
    put(atlas_dir, meas("demo-bb.reg.MODE", 33, flag=measured.DIFFERENT_BUILD, build="different"))
    out = query.register_lookup(query.Atlas(atlas_dir), "demo-bb.MODE")
    assert "reset=0b00100000" in out and "## Measured (development evidence)" in out
    assert "measured 33 via live (development evidence, not citable)" in out
    assert "receipt 2026-11-23T10:00:00Z, image read back" in out and "FLAG: measured on a different build" in out
    assert "reset 0b00100000): DISAGREES" in out


def test_find_fact_shows_a_value_that_agrees_beside_the_hit(atlas_dir):
    put(atlas_dir, meas("demo-bb.ec.VOVP", 23.4, "V"))
    out = query.find_fact(query.Atlas(atlas_dir), "OVP threshold")
    hit, beside = out.splitlines()[-2:]
    assert hit.startswith("demo-bb.ec.VOVP kind=limit") and beside.startswith("  - measured 23.4 V via live (development evidence")
    assert "(22.5..24.5 V): agrees" in beside and "FLAG" not in beside


def test_part_card_lists_the_measurements_of_its_records(atlas_dir):
    put(atlas_dir, meas("demo-bb.ec.VOVP", 30, "V"), meas("elsewhere.x", 1, n=2))
    out = query.part_card(query.Atlas(atlas_dir), "DEMO-BB")
    assert "## Measured (development evidence)" in out and "measured 30 V via live" in out and "DISAGREES" in out
    assert "elsewhere.x" not in out


def test_without_measurements_the_lookups_are_unchanged(atlas_dir):
    a = query.Atlas(atlas_dir)
    assert "Measured" not in query.register_lookup(a, "demo-bb.MODE")
    assert "Measured" not in query.part_card(a, "DEMO-BB") and "measured" not in query.find_fact(a, "OVP threshold")

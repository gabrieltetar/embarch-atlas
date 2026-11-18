"""`q decode`: a command-then-read part's reply frame, on an invented sensor (`demo-psense`) and a synthetic
devicetree: status bits by name, the count, the transfer function with the node's range, never clamped."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from embarch_atlas import cli, decode as D, query, records as R
from embarch_atlas.common import write_json, write_jsonl

NODE = "/soc/i2c@40005800/psense@28"
EQ = "Output = (count - Output_min) x (Pmax - Pmin) / (Output_max - Output_min) + Pmin"


def frame(**kw) -> dict:
    r = {"id": "demo-psense.frame.read-data", "kind": "frame", "doc": "demo-psense@A", "p": 8, "src": "table",
         "q": "Byte 0 status Bytes 1-3 count", "name": "Read data", "size": 4, "prov": "llm-verified",
         "fields": [{"name": "POWERED", "role": "status", "byte": 0, "bits": [6, 6], "enum": {"1": "on", "0": "off"}},
                    {"name": "BUSY", "role": "status", "byte": 0, "bits": [5, 5]},
                    {"name": "MEM_ERR", "role": "status", "byte": 0, "bits": [2, 2]},
                    {"name": "OUTPUT", "role": "count", "byte": [1, 3]}]}
    r.update(kw)
    return r


def transfer(variant="A", **kw) -> dict:
    r = {"id": f"demo-psense.transfer.read-data.{variant.lower()}", "kind": "transfer", "doc": "demo-psense@A", "p": 9,
         "src": "equation", "q": EQ, "frame": "Read data", "form": "linear", "variant": variant, "equation": EQ,
         "when": {"prop": "transfer-function", "value": variant}, "prov": "llm-verified",
         "params": {"out_min": 1677722, "out_max": 15099494, "phys_min": "dt:pressure-min",
                    "phys_max": "dt:pressure-max", "unit": "dt:pressure-unit"}}
    r.update(kw)
    return r


def make(tmp: Path, recs: list[dict], props: dict | None = None) -> Path:
    ad = tmp / "atlas"
    atlas = ad / "atlases" / "core@000000000000"
    write_json(atlas / "atlas.json", {"created": "2026-11-18T00:00:00Z"})
    write_json(atlas / "pins.json", {"part_docs": {"U5": ["demo-psense@A"]}, "i2c": {"buses": [{"bus": "/soc/i2c@40005800", "families": [
        {"compat": "acme,psense", "dt": [{"node": NODE, "reg": 0x28}], "parts": [{"part": "U5", "addrs": [0x28]}]}]}]}})
    props = {"transfer-function": {"val": "A"}, "pressure-min": {"val": 0}, "pressure-max": {"val": 100},
             "pressure-unit": {"val": "kPa"}} if props is None else props
    write_json(atlas / "dt.json", {"nodes": {NODE: {"labels": ["psense"], "props": props}}})
    d = ad / "docs" / "demo-psense@A"
    write_json(d / "doc.json", {"doc_id": "demo-psense@A", "kind": "datasheet", "part": "PSENSE"})
    write_jsonl(d / "records" / "frame.jsonl", [r for r in recs if r["kind"] == "frame"])
    write_jsonl(d / "records" / "transfer.jsonl", [r for r in recs if r["kind"] == "transfer"])
    return ad


def dec(ad: Path, data: str, node: str = "psense") -> str:
    return query.decode(query.Atlas(ad), node, data)


def test_parse_bytes_forms():
    assert D.parse_bytes("0x48 0x1A 2b 3C") == [0x48, 0x1A, 0x2B, 0x3C]
    assert D.parse_bytes("481a2b3c") == [0x48, 0x1A, 0x2B, 0x3C]
    assert D.parse_bytes("48,1a") == [0x48, 0x1A]
    with pytest.raises(ValueError):
        D.parse_bytes("48 zz")
    with pytest.raises(ValueError):
        D.parse_bytes("481")


def test_records_validate():
    assert R.validate(frame()) == [] and R.validate(transfer()) == []
    assert R.validate(frame(size=0))      # a frame has at least one byte


def test_decodes_status_bits_count_and_value_with_citations(tmp_path):
    ad = make(tmp_path, [frame(), transfer()])
    out = dec(ad, "0x44 0x80 0x00 0x00")        # 0x44: POWERED=1, BUSY=0, MEM_ERR=1; count 0x800000 = 8388608
    assert out.startswith("src: demo-psense@A frame p.8; transfer p.9")
    assert "- POWERED = 1 (byte 0 bits 6:6): on" in out
    assert "- BUSY = 0" in out and "- MEM_ERR = 1" in out
    assert "count = 8388608 (0x800000)" in out
    val = (8388608 - 1677722) * 100 / (15099494 - 1677722)
    assert f"value = {val:.6g} kPa" in out
    assert "(8388608 - 1677722) * (100 - 0) / (15099494 - 1677722) + 0" in out
    assert "below" not in out and "above" not in out


def test_count_below_minimum_is_negative_not_clamped(tmp_path):
    ad = make(tmp_path, [frame(), transfer()])
    out = dec(ad, "40 00 00 00")                # count 0: what a part that is not measuring returns
    assert "count = 0 (0x0)" in out
    assert "value = -12.5" in out
    assert "below the minimum" in out and "not clamped" in out


def test_count_above_maximum_is_reported_too(tmp_path):
    ad = make(tmp_path, [frame(), transfer()])
    out = dec(ad, "40 FF FF FF")
    assert "above the maximum" in out and "value = 1" in out and "value = 1e" not in out


def test_variant_is_chosen_by_the_node(tmp_path):
    b = transfer("B", params={"out_min": 0, "out_max": 16777215, "phys_min": 0, "phys_max": 2, "unit": "bar"})
    ad = make(tmp_path, [frame(), transfer(), b], props={"transfer-function": {"val": "B"}})
    out = dec(ad, "40 80 00 00")
    assert "value = 1 bar" in out and "[variant B]" in out


def test_node_without_the_selecting_property_names_it(tmp_path):
    ad = make(tmp_path, [frame(), transfer(), transfer("B")], props={})
    out = dec(ad, "40 80 00 00")
    assert "count = 8388608" in out                      # the frame is still decoded
    assert "value: not computed" in out and "transfer-function" in out and "A (when" in out and "B (when" in out


def test_missing_dt_range_property_is_named_not_guessed(tmp_path):
    ad = make(tmp_path, [frame(), transfer()], props={"transfer-function": {"val": "A"}, "pressure-max": {"val": 100}})
    out = dec(ad, "40 80 00 00")
    assert "value: not computed" in out and "`pressure-min` is not set" in out and "`pressure-unit` is not set" in out
    assert "value =" not in out


def test_part_with_no_frame_layout_says_what_is_missing(tmp_path):
    ad = make(tmp_path, [])
    out = dec(ad, "40 80 00 00")
    assert out.startswith("src: none") and "no `frame` record" in out and "demo-psense@A" in out


def test_frame_without_a_transfer_function_still_decodes_and_says_so(tmp_path):
    ad = make(tmp_path, [frame()])
    out = dec(ad, "40 80 00 00")
    assert "count = 8388608" in out and "no `transfer` record" in out and "value =" not in out


def test_wrong_length_bad_hex_and_unknown_node_are_refused(tmp_path):
    ad = make(tmp_path, [frame(), transfer()])
    assert "is 4 bytes; 3 given" in dec(ad, "40 80 00")
    assert "Cannot read the bytes" in dec(ad, "40 zz")
    assert "no MCU peripheral, DT label or part" in dec(ad, "40 80 00 00", node="nosuch")


def test_unverified_records_say_so_in_the_header(tmp_path):
    ad = make(tmp_path, [frame(prov="llm-unverified"), transfer(prov="llm-unverified")])
    assert "prov=llm-unverified" in dec(ad, "40 80 00 00").splitlines()[0]


def test_decode_is_a_read_only_mcp_tool_and_a_q_verb(tmp_path):
    pytest.importorskip("mcp")
    from embarch_atlas.mcp_server import build_server

    ad = make(tmp_path, [frame(), transfer()])
    tools = {t.name: t for t in asyncio.run(build_server(ad, live=False).list_tools())}
    assert tools["decode"].annotations.read_only_hint
    assert set(tools["decode"].input_schema["properties"]) == {"node", "data", "offset"}


def test_q_decode_cli_prints_the_answer(tmp_path, capsys):
    ad = make(tmp_path, [frame(), transfer()])
    assert cli.main(["--atlas-dir", str(ad), "q", "decode", "psense", "0x40", "0x80", "0x00", "0x00"]) == 0
    assert "count = 8388608" in capsys.readouterr().out

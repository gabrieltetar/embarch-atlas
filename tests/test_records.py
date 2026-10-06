import json

import pytest

import datasheet_synth as ds
from embarch_atlas import records as R

GOOD = [
    {"id": "demo-switch.ec.VOVP.vbus_rising", "kind": "limit", "doc": "demo-switch@A", "p": 8, "src": "table",
     "q": "VBUS rising 22 23 25 V", "group": "ec", "sym": "VOVP", "cond": "VBUS rising", "min": 22, "typ": 23,
     "max": 25, "unit": "V", "fn": [24], "guar": "design"},
    {"id": "demo-switch.timing.tDEB", "kind": "timing", "doc": "demo-switch@A", "p": 7, "src": "table", "q": "10 16 25",
     "group": "ec", "sym": "tDEB", "min": 10, "typ": 16, "max": 25, "unit": "ms", "edge": "EN high -> OUT rising"},
    {"id": "demo-switch.pin.FLT", "kind": "pin", "doc": "demo-switch@A", "p": 3, "src": "table", "q": "D2 FLT",
     "pin": ["D2"], "name": "FLT", "dir": "out", "type": "od", "active": "low"},
    {"id": "demo-switch.rule.otp", "kind": "rule", "doc": "demo-switch@A", "p": [21], "src": "text", "q": "x",
     "cond": "TJ > 150 C", "text": "switch off", "recover": "TJ < 130 C"},
    {"id": "demo-conv.reg.MODE", "kind": "register", "doc": "demo-conv@B", "p": 32, "src": "table", "q": "x",
     "name": "MODE", "addr": "0x06", "width": 8, "reset": "0b00100000", "access": "rw", "prov": "deterministic"},
    {"id": "demo-conv.reg.MODE.OE", "kind": "field", "doc": "demo-conv@B", "p": 32, "src": "table", "q": "7 OE R/W 0b",
     "reg": "demo-conv.reg.MODE", "name": "OE", "bits": [7, 7], "reset": "0b0", "access": "rw",
     "enum": {"0": "off", "1": "on"}},
    {"id": "demo-switch.quirk.ovp_below_range", "kind": "quirk", "doc": "demo-switch@A", "p": [5, 8], "src": "text",
     "q": ["a", "b"], "class": "table-vs-table", "text": "OVP min is below the range max", "refs": ["x.y.z"]},
    {"id": "demo-switch.fig.figure-1", "kind": "figure", "doc": "demo-switch@A", "p": 10, "src": "figure", "q": None,
     "fig": "Figure 1", "caption": "UVLO timing"},
    {"id": "demo-conv.strap.MODE.r_6k19", "kind": "strap", "doc": "demo-conv@B", "p": 16, "src": "table", "q": "x",
     "pin": "MODE", "condition": "RMODE = 6.19 kΩ", "value": "address 0x74"},
    {"id": "demo-conv.check.fs", "kind": "check", "doc": "demo-conv@B", "p": [5], "src": "text",
     "expr": "R['a.b.c'].max > 1", "expect": "pass"},
    {"id": "demo-prd.req.charge-temp", "kind": "requirement", "doc": "demo-prd@3", "p": 4, "src": "text", "q": "x",
     "rid": "prd.4.2.charge-temp", "text": "charge 0 to 45 C", "fw_relevant": True},
    {"id": "demo-conv.conflict.MODE.x", "kind": "conflict", "doc": "demo-conv@B", "p": 32, "src": "table", "q": "x",
     "refs": ["demo-conv.reg.MODE"], "sources": ["figure", "table"], "text": "access differs",
     "prov": "deterministic"},
    {"id": "demo-sw.pt.S1", "kind": "passthrough", "doc": "demo-sw@1", "p": 2, "src": "text", "q": "x",
     "from_pin": "A1", "to_pin": "B1", "when": "EN high"},
    {"id": "demo-gauge.bus.i2c", "kind": "bus", "doc": "demo-gauge@A", "p": [12], "src": "text",
     "q": "multibyte values are ordered least significant byte first", "iface": "i2c", "byte_order": "lsb-first",
     "reg_addr_bytes": 1, "windows": [{"from": "0x100", "to": "0x1FF", "addr": "0x0B", "base": "0x100"}]}
]


def test_schema_accepts_every_kind():
    for rec in GOOD:
        assert R.validate(rec) == [], rec["id"]
    assert {r["kind"] for r in GOOD} == set(R.KINDS)


@pytest.mark.parametrize("patch, needle", [
    ({"tpy": 23}, "unknown field 'tpy'"),                           # a typo is caught
    ({"guar": True}, "guar"),                                        # the YAML yes -> True trap
    ({"p": 0}, ".p"),
    ({"doc": "Demo Switch"}, "does not match"),
    ({"kind": "limits"}, "not one of"),
    ({"src": "image"}, "not one of"),
    ({"min": [1]}, "not of type"),
])
def test_schema_rejects(patch, needle):
    errs = R.validate(dict(GOOD[0], **patch))
    assert any(needle in e for e in errs), errs


def test_schema_required_per_kind():
    rec = {k: v for k, v in GOOD[5].items() if k != "bits"}
    assert any("bits" in e for e in R.validate(rec))
    rec = {k: v for k, v in GOOD[2].items() if k != "name"}
    assert any("name" in e for e in R.validate(rec))


def test_facts_line_format():
    line = R.facts_line(GOOD[0])
    assert line.startswith("demo-switch.ec.VOVP.vbus_rising kind=limit; group=ec; sym=VOVP; ")
    assert "cond=VBUS rising; min=22; typ=23; max=25; unit=V; guar=design; " in line
    assert line.endswith("cite=demo-switch@A p8 fn24; prov=llm-unverified")
    f = R.facts_line(dict(GOOD[5], prov="deterministic+crosschecked"))
    assert "bits=7:7" in f and f.endswith("cite=demo-conv@B p32; prov=deterministic+crosschecked")
    q = R.facts_line(dict(GOOD[3], text='off; latched "hard"'))
    assert 'text="off; latched \\"hard\\""' in q                     # a ';' in a value is quoted
    assert R.facts_line(GOOD[6]).count("cite=demo-switch@A p5,8;") == 1


def test_load_save_roundtrip(tmp_path):
    docdir = tmp_path / "doc"
    R.save_file(docdir, "limit.jsonl", [GOOD[0]])
    R.save_file(docdir, "regparse.jsonl", [GOOD[4], GOOD[5]])
    pairs = R.load_records(docdir)
    assert [f for f, _ in pairs] == ["limit.jsonl", "regparse.jsonl", "regparse.jsonl"]
    assert R.save_records(docdir, pairs) == []          # unchanged: nothing rewritten
    pairs[0][1]["prov"] = "llm-verified"
    assert R.save_records(docdir, pairs) == ["limit.jsonl"]
    line = (docdir / "records" / "limit.jsonl").read_text().splitlines()[0]
    assert json.loads(line)["prov"] == "llm-verified"


def test_helpers():
    assert R.to_int("0x1F") == 31 and R.to_int("0b101") == 5 and R.to_int(None) is None
    assert R.short_id("demo.ec.VOVP.vbus") == "VOVP.vbus"
    assert R.slug("Logic Pin (EN1, EN2) Specifications") == "logic-pin-en1-en2-specifications"
    assert all(R.validate(r) == [] for r in ds.records())


def test_schema_batch_1_conventions():
    quirk = dict(GOOD[6], **{"class": "text-vs-text"})
    assert R.validate(quirk) == []
    req = dict(GOOD[10], modality="nice-to-have", priority="P2", order=3)
    assert R.validate(req) == []
    assert any("modality" in e for e in R.validate(dict(req, modality="must")))
    assert any("order" in e for e in R.validate(dict(req, order=0)))
    assert R.validate(dict(GOOD[2], pin=[3, "B2"])) == []                     # [number, ball]
    fixed = {"id": "demo-conv.strap.fixed", "kind": "strap", "doc": "demo-conv@B", "p": 16, "src": "text",
             "q": "x", "pin": "", "condition": "fixed", "value": "0x6B (1101011b)"}
    assert R.validate(fixed) == []
    reg = dict(GOOD[4], reset=None, note="0x20 on 1S parts, 0x21 on 2S-5S")
    assert R.validate(reg) == []
    strap = R.schema()["$defs"]["strap"]["description"]
    assert 'pin ""' in strap.replace('\\"', '"') and '"fixed"' in strap and "7-bit" in strap
    line = R.facts_line(req)
    assert "rid=prd.4.2.charge-temp; modality=nice-to-have; priority=P2; order=3; text=" in line

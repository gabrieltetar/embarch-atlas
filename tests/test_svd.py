"""mcu svd on a tiny synthetic CMSIS-SVD: inheritance, derivedFrom, clusters, dim arrays, records, slices."""
from __future__ import annotations

from pathlib import Path

import pytest

from embarch_atlas import cli, svd
from embarch_atlas.common import read_json, read_jsonl

SVD = """<?xml version="1.0" encoding="utf-8"?>
<device schemaVersion="1.3">
  <name>DEMO_MCU</name>
  <version>0.9</version>
  <cpu><name>CM0</name><endian>little</endian></cpu>
  <width>32</width>
  <size>32</size>
  <access>read-write</access>
  <resetValue>0x00000000</resetValue>
  <peripherals>
    <peripheral>
      <name>SER0</name>
      <description>Serial   port</description>
      <groupName>SER</groupName>
      <baseAddress>0x40010000</baseAddress>
      <addressBlock><offset>0</offset><size>0x400</size><usage>registers</usage></addressBlock>
      <interrupt><name>SER0</name><description>Serial 0</description><value>7</value></interrupt>
      <registers>
        <register>
          <name>CTRL</name><addressOffset>0x0</addressOffset><resetValue>0x00000105</resetValue>
          <fields>
            <field><name>EN</name><bitOffset>0</bitOffset><bitWidth>1</bitWidth>
              <enumeratedValues>
                <enumeratedValue><name>OFF</name><value>0</value></enumeratedValue>
                <enumeratedValue><name>ON</name><value>1</value></enumeratedValue>
              </enumeratedValues></field>
            <field><name>MODE</name><bitRange>[3:1]</bitRange></field>
            <field><name>DIV</name><lsb>8</lsb><msb>11</msb><access>read-only</access></field>
            <field><name>IE%s</name><dim>4</dim><dimIncrement>1</dimIncrement><bitOffset>16</bitOffset><bitWidth>1</bitWidth></field>
          </fields>
        </register>
        <register>
          <name>STAT</name><addressOffset>0x4</addressOffset><access>read-only</access><size>16</size>
          <fields>
            <field><name>BUSY</name><bitOffset>0</bitOffset><bitWidth>1</bitWidth></field>
            <field><name>ERR</name><bitOffset>1</bitOffset><bitWidth>1</bitWidth>
              <access>read-write</access><modifiedWriteValues>oneToClear</modifiedWriteValues></field>
            <field><name>DATA</name><bitOffset>8</bitOffset><bitWidth>8</bitWidth><readAction>clear</readAction></field>
          </fields>
        </register>
        <register derivedFrom="CTRL">
          <name>CTRL2</name><addressOffset>0x8</addressOffset>
        </register>
        <register>
          <name>BUF[%s]</name><dim>2</dim><dimIncrement>0x4</dimIncrement><addressOffset>0x10</addressOffset>
          <fields><field><name>B</name><bitOffset>0</bitOffset><bitWidth>32</bitWidth></field></fields>
        </register>
        <cluster>
          <name>CH%s</name><dim>2</dim><dimIncrement>0x10</dimIncrement><dimIndex>A,B</dimIndex>
          <addressOffset>0x20</addressOffset>
          <register><name>CFG</name><addressOffset>0x0</addressOffset><resetValue>0x3</resetValue>
            <fields><field><name>LEN</name><bitOffset>0</bitOffset><bitWidth>2</bitWidth></field></fields></register>
          <register><name>CNT</name><addressOffset>0x4</addressOffset><access>write-only</access></register>
        </cluster>
      </registers>
    </peripheral>
    <peripheral derivedFrom="SER0">
      <name>SER1</name>
      <baseAddress>0x40010400</baseAddress>
      <interrupt><name>SER1</name><value>8</value></interrupt>
    </peripheral>
    <peripheral>
      <name>CLK</name>
      <groupName>CLK</groupName>
      <baseAddress>0x40020000</baseAddress>
      <registers>
        <register><name>CFGR</name><addressOffset>0x0</addressOffset>
          <fields><field><name>SW</name><bitOffset>0</bitOffset><bitWidth>2</bitWidth></field></fields></register>
      </registers>
    </peripheral>
  </peripherals>
</device>
"""


@pytest.fixture
def svd_file(tmp_path: Path) -> Path:
    f = tmp_path / "demo.svd"
    f.write_text(SVD)
    return f


def test_parse_inheritance_derived_clusters_and_dims(svd_file):
    dev = svd.parse_svd(svd_file)
    assert dev["device"] == "DEMO_MCU" and dev["version"] == "0.9"
    p = {x["name"]: x for x in dev["peripherals"]}
    assert list(p) == ["SER0", "SER1", "CLK"]
    ser0 = {r["name"]: r for r in p["SER0"]["registers"]}
    assert set(ser0) == {"CTRL", "STAT", "CTRL2", "BUF0", "BUF1", "CHA_CFG", "CHA_CNT", "CHB_CFG", "CHB_CNT"}
    assert [ser0[n]["off"] for n in ("BUF0", "BUF1", "CHA_CFG", "CHA_CNT", "CHB_CFG", "CHB_CNT")] == [
        0x10, 0x14, 0x20, 0x24, 0x30, 0x34]
    assert ser0["CHB_CFG"]["cluster"] == "CHB" and ser0["CHB_CFG"]["reset"] == 3
    # device defaults: size 32, read-write; register overrides: STAT 16-bit read-only
    assert ser0["CTRL"]["size"] == 32 and ser0["CTRL"]["access"] == "rw"
    assert ser0["STAT"]["size"] == 16 and ser0["STAT"]["access"] == "ro" and ser0["CHA_CNT"]["access"] == "wo"
    f = {x["name"]: x for x in ser0["CTRL"]["fields"]}
    assert (f["MODE"]["lsb"], f["MODE"]["width"]) == (1, 3)
    assert (f["DIV"]["lsb"], f["DIV"]["width"], f["DIV"]["access"]) == (8, 4, "ro")
    assert [f[f"IE{k}"]["lsb"] for k in range(4)] == [16, 17, 18, 19]
    assert [e["name"] for e in f["EN"]["enums"]] == ["OFF", "ON"]
    st = {x["name"]: x for x in ser0["STAT"]["fields"]}
    assert st["ERR"]["access"] == "woclr" and st["DATA"]["access"] == "rclr" and st["BUSY"]["access"] == "ro"
    # register derivedFrom copies the fields, keeps its own offset
    assert ser0["CTRL2"]["off"] == 8 and [x["name"] for x in ser0["CTRL2"]["fields"]] == [x["name"] for x in ser0["CTRL"]["fields"]]
    # a derived peripheral inherits registers and group, not interrupts
    assert p["SER1"]["group"] == "SER" and len(p["SER1"]["registers"]) == len(p["SER0"]["registers"])
    assert [i["value"] for i in p["SER1"]["irqs"]] == [8] and p["SER1"]["size"] == 0x400
    c = svd.counts(dev)
    assert (c["peripherals"], c["base_definitions"], c["derived"]) == (3, 2, 1)
    assert c["registers_defined"] == 10 and c["register_instances"] == 19
    assert c["interrupt_entries"] == 2 and c["enum_values_defined"] == 4


def test_svd_integers():
    assert svd.svd_int("0x1F") == 31 and svd.svd_int("#101") == 5 and svd.svd_int("#1x1") == 5
    assert svd.svd_int("32") == 32 and svd.svd_int("0b11") == 3 and svd.svd_int("4k") == 4096
    with pytest.raises(ValueError):
        svd.svd_int("nope")


def test_cli_writes_records_sidecar_slices_and_card(svd_file, tmp_path, capsys):
    root = tmp_path / "atlas"
    assert cli.main(["--atlas-dir", str(root), "mcu", "svd", str(svd_file), "--doc-id", "demo-svd"]) == 0
    d = root / "docs" / "demo-svd"
    regs = {r["id"]: r for r in read_jsonl(d / "records" / "register.jsonl")}
    fields = {r["id"]: r for r in read_jsonl(d / "records" / "field.jsonl")}
    ctrl = regs["demo_mcu.reg.SER0.CTRL".replace("demo_mcu", "demomcu")]
    assert ctrl["addr"] == "0x40010000" and ctrl["reset"] == "0x00000105" and ctrl["prov"] == "deterministic"
    assert ctrl["src"] == "svd" and ctrl["p"] is None and ctrl["doc"] == "demo-svd"
    ser1 = regs["demomcu.reg.SER1.CTRL"]
    assert ser1["addr"] == "0x40010400"
    mode = fields["demomcu.reg.SER0.CTRL.MODE"]
    assert mode["bits"] == [3, 1] and mode["reset"] == "0x2" and mode["reg"] == "demomcu.reg.SER0.CTRL"
    assert fields["demomcu.reg.SER0.CTRL.EN"]["enum"] == {"0": "OFF", "1": "ON"}
    assert fields["demomcu.reg.SER0.STAT.ERR"]["access"] == "woclr"
    # the shared schema: only the SVD-specific p/src/doc differ (no page, src svd, doc id without @)
    from embarch_atlas.records import validate
    for rec in list(regs.values()) + list(fields.values()):
        errs = validate(rec)
        assert all(any(k in e for k in (".doc:", ".p:", ".src:")) for e in errs), errs
    side = read_json(d / "peripherals.json")
    assert side["counts"]["peripherals"] == 3 and side["peripherals"]["SER1"]["derived_from"] == "SER0"
    assert side["interrupts"] == [{"value": 7, "names": ["SER0"], "peripherals": ["SER0"], "desc": ["Serial 0"]},
                                  {"value": 8, "names": ["SER1"], "peripherals": ["SER1"], "desc": []}]
    assert read_json(d / "doc.json")["kind"] == "svd"
    sl = (d / "s" / "SER0.md").read_text()
    assert "0x010 BUF0 rw rst=0x00000000 | B[31:0]" in sl
    assert "DIV[11:8]/ro" in sl and "base 0x40010000" in sl and "IRQ 7 SER0" in sl
    assert side["peripherals"]["SER0"]["slice_tokens"] < 1000
    card = (d / "card.md").read_text()
    assert "| SER0 | 0x40010000 | SER |" in card and "SER1 0x40010400" in card
    capsys.readouterr()
    assert cli.main(["mcu", "svd", str(svd_file), "--slice", "CLK"]) == 0
    assert "0x000 CFGR rw" in capsys.readouterr().out


def test_svddoc_lookup(svd_file, tmp_path):
    root = tmp_path / "atlas"
    svd.write_svd_doc(svd_file, "demo-svd", root)
    s = svd.SvdDoc.load(root, "demo-svd")
    assert s.part == "demomcu" and s.base_of("SER1") == "SER0" and s.instances_of("SER0") == ["SER0", "SER1"]
    assert sorted(s.definitions()) == ["CLK", "SER0"]
    assert s.has("SER0.CTRL.EN") and s.has("SER0.CTRL") and s.has("CTRL.MODE", ["SER1"])
    assert not s.has("SER0.CTRL.NOPE") and not s.has("CFGR.NOPE") and s.has("A.B.C.D") is None
    assert svd.find_svd_doc(root) == "demo-svd"

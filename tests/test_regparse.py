import pytest

import datasheet_synth as ds
from embarch_atlas import cli, records as R, regparse


@pytest.fixture(scope="module")
def parsed(tmp_path_factory):
    atlas, docdir = ds.make_doc(tmp_path_factory.mktemp("reg"))
    st = regparse.run(docdir, "ti")
    recs = {r["id"]: r for f, r in R.load_records(docdir) if f == regparse.OUT_FILE}
    return atlas, docdir, st, recs


def test_registers_and_fields(parsed):
    _, _, st, recs = parsed
    assert st["registers"] == 3 and st["fields"] == 6
    ctrl = recs["syn1234.reg.CTRL"]
    assert ctrl["addr"] == "0x00" and ctrl["width"] == 8 and ctrl["prov"] == "deterministic"
    assert ctrl["reset"] == "0b10000001"                      # "10000001h" is binary with an h suffix
    assert "binary printed with an h suffix" in ctrl["note"]
    assert recs["syn1234.reg.CTRL.EN"]["bits"] == [7, 7] and recs["syn1234.reg.CTRL.EN"]["access"] == "rw"
    assert recs["syn1234.reg.CTRL.STAT"]["access"] == "rclr"  # "does not reset until it is read"
    assert recs["syn1234.reg.CTRL.RESERVED_6_1"]["reset"] == "0b000000"
    assert "1b = enabled (default)" in recs["syn1234.reg.CTRL.EN"]["desc"]  # continuation line joined
    for r in recs.values():
        assert R.validate(r) == [], r["id"]


def test_multibyte_register_split_into_bus_bytes(parsed):
    _, _, _, recs = parsed
    lsb, msb = recs["syn1234.reg.DATA_LSB"], recs["syn1234.reg.DATA_MSB"]
    assert (lsb["addr"], lsb["reset"]) == ("0x01", "0b00000011")
    assert (msb["addr"], msb["reset"]) == ("0x02", "0b00000000")
    assert recs["syn1234.reg.DATA_LSB.VALUE_7_0"]["bits"] == [7, 0]
    assert recs["syn1234.reg.DATA_MSB.VALUE_9_8"]["bits"] == [1, 0]
    assert recs["syn1234.reg.DATA_MSB.RESERVED_7_2"]["bits"] == [7, 2]


def test_document_inconsistencies_become_conflicts(parsed):
    _, _, st, recs = parsed
    conflicts = {c["id"]: c for c in st["conflicts"]}
    acc = conflicts["syn1234.conflict.CTRL.RESERVED_6_1.access"]
    assert "access rw in the bit diagram, ro in the field table" in acc["text"]
    assert acc["refs"] == ["syn1234.reg.CTRL.RESERVED_6_1"] and acc["prov"] == "deterministic"
    assert "syn1234.conflict.summary.GHOST" in conflicts            # a summary row with no register section
    assert st["summary_covered"] == 3 and st["summary_rows"] == 4
    assert recs["syn1234.reg.DATA_MSB"]["summary"]["name"] == "DATA_MSB"
    assert all(ref in recs for c in conflicts.values() for ref in c["refs"])


def test_rerun_replaces_only_its_own_file(parsed):
    _, docdir, _, _ = parsed
    before = {f for f, _ in R.load_records(docdir)}
    regparse.run(docdir, "ti")
    assert {f for f, _ in R.load_records(docdir)} == before
    assert (docdir / "records" / "limit.jsonl").exists()


def test_reset_token_forms():
    assert regparse._bits_value("0101b") == (5, 4)
    assert regparse._bits_value("E4h") == (0xE4, None)
    assert regparse._bits_value("x") == (None, None)
    assert regparse._heading_reset("11100100h", 1) == (0xE4, "binary printed with an h suffix")
    assert regparse._heading_reset("E4h", 1) == (0xE4, None)
    assert regparse._heading_reset("00h", 1) == (0, None)


def test_template_registry_and_cli(parsed, capsys):
    atlas, _, _, _ = parsed
    assert "ti" in regparse.TEMPLATES and issubclass(regparse.TEMPLATES["ti"], regparse.Template)
    assert cli.main(["--atlas-dir", str(atlas), "doc", "regparse", ds.DOC_ID, "--template", "ti"]) == 0
    assert "3 registers, 6 fields" in capsys.readouterr().out
    with pytest.raises(ValueError):
        regparse.run(atlas / "docs" / ds.DOC_ID, "adi")


# ---------------------------------------------------------------- TI variants, Maxim, diagnostics

import regparse_synth as rsyn  # noqa: E402


def _recs(docdir):
    return {r["id"]: r for f, r in R.load_records(docdir) if f == regparse.OUT_FILE}


def _schema_valid(rec):
    """Schema errors, ignoring the register keys the schema may not list yet (reg_type, nv_backup, mirror_of)."""
    allowed = set(R.schema()["$defs"]["register"]["propertyNames"]["enum"])
    r = {k: v for k, v in rec.items() if rec.get("kind") != "register" or k in allowed
         or k not in ("reg_type", "nv_backup", "mirror_of")}
    return R.validate(r)


@pytest.fixture(scope="module")
def synth(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("regsynth")
    out = {}
    for did, tpl in (("syn700@A", "ti"), ("syn257@A", "ti"), ("mxsyn@1", "maxim")):
        atlas, docdir = rsyn.make(tmp, did)
        out[did] = (atlas, docdir, regparse.run(docdir, tpl), _recs(docdir))
    return out


def test_ti_acronym_headings_and_sized_summary(synth):
    _, _, st, recs = synth["syn700@A"]
    assert (st["registers"], st["fields"]) == (4, 11)
    cfg = recs["syn700.reg.CFG"]
    assert (cfg["addr"], cfg["width"], cfg["reset"], cfg["name"]) == ("0x00", 16, "0x0000", "CFG")
    assert recs["syn700.reg.CFG.INIT_DLY"]["bits"] == [14, 6]
    assert recs["syn700.reg.CFG.RESERVED_4"]["reset"] == "0b1"
    assert (recs["syn700.reg.ACC"]["width"], recs["syn700.reg.ACC.ACC"]["bits"]) == (40, [39, 0])
    assert recs["syn700.reg.MFR_ID"]["reset"] == "0x4E53"
    assert "every register" in recs["syn700.reg.CFG.RST"]["desc"]          # description wrapped onto two lines
    conflict = recs["syn700.conflict.CFG.reset"]
    assert "compose to 0000000000010000" in conflict["text"] and conflict["refs"] == ["syn700.reg.CFG"]
    assert st["summary_covered"] == st["summary_rows"] == 4
    rdy = recs["syn700.reg.ALERT.CNVRDY"]                                  # clear-on-read only when LATCH = 1
    assert rdy["access"] == "ro" and "clears on read only when LATCH =1" in rdy["note"]
    assert recs["syn700.reg.ALERT"]["reset"] == "0x0001"


def test_clear_on_read_wording():
    assert regparse._clear_on_read("ro", "Status. Does not reset until it is read.") == ("rclr", None)
    assert regparse._clear_on_read("ro", "The flag is latched if triggered until a read from host.") == ("rclr", None)
    assert regparse._clear_on_read("ro", "If enabled, this bit is cleared on read.")[0] == "ro"
    assert regparse._clear_on_read("rw", "cleared on read") == ("rw", None)


def test_ti_slash_pair_byte_order(synth):
    _, _, st, recs = synth["syn257@A"]
    msb, lsb = recs["syn257.reg.ChgOpt0_MSB"], recs["syn257.reg.ChgOpt0_LSB"]
    assert (msb["addr"], msb["reset"]) == ("0x01", "0b10000010")         # in 01/00h, 00h is the LSB
    assert (lsb["addr"], lsb["reset"]) == ("0x00", "0b00000011")
    assert recs["syn257.reg.ChgOpt0_MSB.EN_LOWPWR"]["bits"] == [7, 7]
    assert recs["syn257.reg.ChgOpt0_LSB.WDOG_ADJ"]["bits"] == [1, 0]
    assert not [c for c in st["conflicts"] if "ChgOpt0" in c["id"]]        # field resets compose to the heading
    assert any("MSB/LSB" in n for n in st["notes"])
    hi, lo = recs["syn257.reg.BoostV_MSB"], recs["syn257.reg.BoostV_LSB"]  # the 07h table numbers bits 15-8
    assert (hi["addr"], recs["syn257.reg.BoostV_MSB.BOOST_V_HI"]["bits"]) == ("0x07", [5, 0])
    assert (lo["addr"], lo["reset"]) == ("0x06", "0b01010101")


def test_ti_field_names(synth):
    _, _, _, recs = synth["syn257@A"]
    f = recs["syn257.reg.ChgOpt0_MSB.Fault_OVP_IN"]
    assert (f["name"], f["access"]) == ("Fault OVP_IN", "rclr")            # name verbatim, id slugged
    assert recs["syn257.reg.ChgOpt0_MSB.AUTO_LIM_DISABLE"]["bits"] == [1, 1]   # name wrapped over two lines
    hi, lo = recs["syn257.reg.LoadCur_MSB.LOAD_CURRENT_12_8"], recs["syn257.reg.LoadCur_LSB.LOAD_CURRENT_7_6"]
    assert (hi["bits"], lo["bits"]) == ([4, 0], [7, 6]) and "per-bit rows" in hi["note"]
    adc = recs["syn257.reg.ADCVINVOUT_MSB"]
    assert adc["name"] == "ADCVIN/VOUT_MSB" and "reset" not in adc          # no [reset = ...] in the heading
    assert "named after Table 5-6" in recs["syn257.reg.ADCVINVOUT_MSB.ADCVIN"]["note"]
    assert recs["syn257.reg.ADCVINVOUT_LSB.ADCVOUT"]["bits"] == [7, 0]
    chip = recs["syn257.reg.ChipID"]                                       # the summary names it by its long name
    assert (chip["name"], chip["reset"]) == ("Chip ID", "0b01111010") and "ChipAddress" in chip["note"]


def test_ti_variant_resets(synth):
    _, _, st, recs = synth["syn257@A"]
    lsb, msb = recs["syn257.reg.FloorGuard_LSB"], recs["syn257.reg.FloorGuard_MSB"]
    assert "reset" not in lsb and msb["reset"] == "0b00000000"             # both variants agree on the MSB
    assert "reset varies" in lsb["note"]
    thr = recs["syn257.reg.FloorGuard_LSB.FLOOR_THR"]
    assert "reset" not in thr and "reset varies" in thr["note"]
    cap = recs["syn257.conflict.FloorGuard.caption_reset"]
    assert "Figure 5-6" in cap["text"] and set(cap["refs"]) == {lsb["id"], msb["id"]}


def _pair_pages(order_header: str | None) -> dict[int, str]:
    lines = []
    if order_header:
        lines += ["      I2C ADDR", f"     ({order_header})", " 0B/0Ah            PairReg()              R/W         a pair   Go"]
    lines += ["9.1.1 PairReg Register (I2C address = 0B/0Ah) [reset = 1234h]",
              "            Table 9-1. PairReg Register (I2C address = 0Bh) Field Descriptions", rsyn.HDR,
              rsyn.row("15-8", "HIGH", "R/W", "00010010b", "High byte."),
              "            Table 9-2. PairReg Register (I2C address = 0Ah) Field Descriptions", rsyn.HDR,
              rsyn.row("7-0", "LOW", "R/W", "00110100b", "Low byte.")]
    return {1: "\n".join(lines)}


def test_ti_byte_order_from_bit_numbering_and_its_contradiction():
    recs = {r["id"]: r for r in regparse.to_records(regparse.TI().parse(_pair_pages(None)), "pq1@A", "pq1", "ti")}
    assert (recs["pq1.reg.PairReg_MSB"]["addr"], recs["pq1.reg.PairReg_MSB"]["reset"]) == ("0x0B", "0b00010010")
    assert recs["pq1.reg.PairReg_LSB.LOW"]["bits"] == [7, 0]
    bad = regparse.to_records(regparse.TI().parse(_pair_pages("LSB/MSB")), "pq1@A", "pq1", "ti")
    assert any(r["id"] == "pq1.conflict.PairReg.byte_order" for r in bad)


def test_maxim_registers_from_headings_maps_and_tables(synth):
    _, _, st, recs = synth["mxsyn@1"]
    assert (st["registers"], st["fields"]) == (8, 12)
    ev = recs["mxsyn.reg.EvtFlags"]
    assert (ev["addr"], ev["width"], ev["reset"], ev["reg_type"]) == ("0x000", 16, "0x0802", "Special")
    assert recs["mxsyn.reg.CurveT00"]["addr"] == "0x012"                  # memory map only
    assert recs["mxsyn.reg.sLowCapAlarm"]["addr"] == "0x023"              # a hyphen-wrapped map cell
    assert (recs["mxsyn.reg.nModeCfg"]["addr"], recs["mxsyn.reg.nModeCfg"]["reset"]) == ("0x1B0", "0x0000")
    at = recs["mxsyn.reg.AtLoad"]                                          # heading 0C4h, memory map 0D4h
    assert at["addr"] == "0x0D4" and at["reg_type"] == "Current"
    assert recs["mxsyn.conflict.AtLoad.addr"]["refs"] == ["mxsyn.reg.AtLoad"]
    assert any("without a drawn row boundary" in n for n in st["notes"])   # the diagram with no grid is not read


def test_maxim_bit_diagram_fields(synth):
    _, _, _, recs = synth["mxsyn@1"]
    assert (recs["mxsyn.reg.EvtFlags.THR"]["bits"], recs["mxsyn.reg.EvtFlags.THR"]["reset"]) == ([11, 8], "0b1000")
    assert recs["mxsyn.reg.EvtFlags.RST"]["reset"] == "0b1"                # bit 1 of the 0x0802 initial value
    x = recs["mxsyn.reg.EvtFlags.RESERVED_14_12"]
    assert x["access"] == "rsvd" and "reset" not in x
    mode = recs["mxsyn.reg.ThrCfg.MODE"]
    assert (mode["bits"], mode["reset"]) == ([12, 9], "0b0010") and "reset derived from register reset" in mode["note"]
    slow = recs["mxsyn.reg.ThrCfg.Slow_Filter"]                            # a cell name on two lines
    assert (slow["name"], slow["bits"]) == ("Slow Filter", [8, 8])
    for r in recs.values():
        if r["kind"] == "field" and r.get("reset") is not None:
            assert "reset derived from register reset" in r["note"], r["id"]
    fixed = recs["mxsyn.conflict.ThrCfg.fixed_3_0"]
    assert "fixes 0b0110" in fixed["text"] and fixed["refs"] == ["mxsyn.reg.ThrCfg.RESERVED_3_0"]


def test_maxim_nonvolatile_backup_relation(synth):
    _, _, _, recs = synth["mxsyn@1"]
    ram, nv = recs["mxsyn.reg.ThrCfg"], recs["mxsyn.reg.nThrCfg"]
    assert ram["nv_backup"] == "mxsyn.reg.nThrCfg" and nv["mirror_of"] == "mxsyn.reg.ThrCfg"
    assert not [r for r in recs.values() if r.get("reg") == nv["id"]]       # the layout is not duplicated
    assert ram["reset"] == "0xA4C3" and "nThrCfg section" in ram["note"]   # the n-section's alternate initial value
    assert nv["reset"] == "0xA4C2" and "factory default" in nv["note"]


def test_every_record_is_valid(synth):
    for _, _, _, recs in synth.values():
        for r in recs.values():
            assert _schema_valid(r) == [], r["id"]
            assert r["prov"] == "deterministic"


def test_zero_registers_is_an_error_with_a_diagnostic(synth, capsys):
    atlas = synth["mxsyn@1"][0]
    assert cli.main(["--atlas-dir", str(atlas), "doc", "regparse", "mxsyn@1", "--template", "ti"]) == 1
    cap = capsys.readouterr()
    assert "0 registers" in cap.out and "Patterns tried" in cap.err and "register heading" in cap.err
    assert "EvtFlags Register (000h)" in cap.err                           # a line that almost matched
    assert cli.main(["--atlas-dir", str(atlas), "doc", "regparse", "mxsyn@1", "--template", "maxim"]) == 0
    assert "8 registers, 12 fields, 2 conflicts, 3 pages (1-3)" in capsys.readouterr().out


def test_maxim_needs_the_pdf(synth, tmp_path):
    import shutil
    _, docdir, _, _ = synth["mxsyn@1"]
    copy = tmp_path / "atlas" / "docs" / docdir.name
    shutil.copytree(docdir, copy)
    with pytest.raises(ValueError, match="source PDF"):
        regparse.run(copy, "maxim")

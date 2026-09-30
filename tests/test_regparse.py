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

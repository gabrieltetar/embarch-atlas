"""`cad`: an Altium project's SchDoc sources, on a synthetic project (tests/schdoc_synth.py)."""
import json
import subprocess
import sys

import pytest

import olefile  # in `dev`: a missing olefile is a red here, not a skip

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
import schdoc_synth as syn  # noqa: E402
from embarch_atlas import cad  # noqa: E402
from embarch_atlas.cli import main  # noqa: E402


@pytest.fixture()
def proj(tmp_path):
    return syn.write_project(tmp_path / "hw")


def test_writer_output_opens_in_olefile(proj):
    ole = olefile.OleFileIO(str(proj / "Main.SchDoc"))
    assert ole.exists("FileHeader") and ole.get_size("FileHeader") >= syn.CUTOFF
    ole.close()


def test_components_and_pin_nets(proj):
    d = cad.read_project(str(proj))
    assert d["method"] == "altium-schdoc"
    assert list(d["sheets"]) == ["Main", "Second"]  # from the .PrjPcb, not a glob: Stray.SchDoc is not read
    assert "U99" not in d["components"]
    C = d["components"]
    assert set(C) == {"U1", "R1", "R2", "R3", "C1", "C2"}
    # two-part symbol folds into one part; the pin of a part that is not current is dropped
    assert set(C["U1"]["pins"]) == {"1", "2", "3"}
    assert C["U1"]["unique_ids"] == ["AAAAAAA1", "AAAAAAA2"] and C["U1"]["part_ids"] == [1, 2]
    assert C["U1"]["mpn"] == "PART-U1" and C["U1"]["value"] == "U1 value" and C["R1"]["mpn"] == "PART-R"
    assert C["U1"]["pins"]["1"]["name"] == "IN"
    net = {ref: {p: v["net"] for p, v in c["pins"].items()} for ref, c in C.items()}
    # wire vertices, a label on a segment, a T-junction, and a pin attached at either end
    assert net["U1"]["1"] == net["R1"]["1"] == net["R2"]["1"] == "SIG"
    # power port, plus a pin end lying on the wire's segment, joined by name across sheets
    assert net["U1"]["2"] == net["C1"]["1"] == net["C2"]["1"] == "3V3"
    # a port of the same name joins two sheets, and with no label it names the net
    assert net["R1"]["2"] == net["R3"]["1"] == "LINK"
    assert d["nets"]["LINK"]["ports"] == 2 and d["nets"]["LINK"]["sheets"] == ["Main", "Second"]
    # a same-named net label on two sheets joins by name
    assert net["R2"]["2"] == net["R3"]["2"] == "NLBL"
    assert net["C1"]["2"] == "NetC1_2" and net["U1"]["3"] == "NetU1_3"
    assert d["nets"]["3V3"]["pins"] == ["C1-1", "C2-1", "U1-2"] and d["nets"]["3V3"]["sheets"] == ["Main", "Second"]
    assert d["source"]["rev"] is None and len(d["source"]["sha256"]) == 64


def test_variant_matches_by_unique_id(proj):
    d = cad.read_project(str(proj), variant="NoOpts")
    v = d["variant"]
    assert v["not_fitted"] == [{"designator": "R2", "unique_id": "\\BBBBBBB2"}]
    assert v["unknown_unique_ids"] == [{"designator": "R9", "unique_id": "DDDDDDD9"}]
    assert d["components"]["R2"]["dni"] and d["components"]["R2"]["dni_evidence"] == ["variant:NoOpts"]
    assert not d["components"]["R1"]["dni"] and not d["components"]["C1"]["dni"]  # Kind=0 is not "not fitted"
    assert any("Kind=0" in i for i in d["issues"])
    assert "variant" not in cad.read_project(str(proj))
    assert cad.read_project(str(proj), variant="Alt")["variant"]["not_fitted"] == []


def test_variant_survives_a_renamed_designator(proj):
    s = syn.main_sheet()
    for r in s.recs:
        if r.get("TEXT") == "R2":
            r["TEXT"] = "R20"
    (proj / "Main.SchDoc").write_bytes(s.file())
    d = cad.read_project(str(proj), variant="NoOpts")
    assert d["variant"]["not_fitted"][0]["designator"] == "R20" and "R2" not in d["components"]
    assert any("names R2 but unique ID" in i for i in d["issues"])


def test_project_file_path_and_cli(proj, capsys):
    assert main(["cad", str(proj / "Gen.PrjPcb"), "--json", "--variant", "NoOpts"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert d["board"] == "Gen" and d["variant"]["name"] == "NoOpts"
    assert main(["cad", str(proj), "--variant", "NoOpts"]) == 0
    out = capsys.readouterr().out
    assert "R2  mpn=PART-R" in out and "DNI (variant:NoOpts)" in out and "no longer in the schematic" in out


def test_unknown_variant_is_refused_in_one_line(proj, capsys):
    assert main(["cad", str(proj), "--variant", "Nope"]) == 2
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "Nope" in err and "Alt, NoOpts" in err


def test_missing_olefile_is_refused_in_one_line(proj, capsys, monkeypatch):
    monkeypatch.setitem(sys.modules, "olefile", None)
    assert main(["cad", str(proj)]) == 2
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "embarch-atlas[cad]" in err


def test_bad_inputs_are_one_line(proj, tmp_path, capsys):
    (tmp_path / "empty").mkdir()
    for args in (["cad", str(tmp_path / "empty")], ["cad", str(tmp_path / "nope")]):
        assert main(args) == 2
        assert capsys.readouterr().err.count("\n") == 1
    (proj / "Main.SchDoc").write_bytes(b"not a compound file")
    assert main(["cad", str(proj)]) == 2
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and "Main.SchDoc" in err


def git(cwd, *a):
    subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@example.com", *a], check=True, capture_output=True)


def test_rev_reads_the_commit_and_touches_nothing(proj, tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "one")
    s = syn.main_sheet()
    s.comp("Z1", "ZZZZZZZ1")
    (proj / "Main.SchDoc").write_bytes(s.file())  # the working tree moves on, uncommitted
    before = subprocess.run(["git", "-C", str(tmp_path), "status", "--porcelain"], capture_output=True, text=True).stdout
    assert "Z1" in cad.read_project(str(proj))["components"]
    old = cad.read_project(str(proj), rev="HEAD", variant="NoOpts")
    assert "Z1" not in old["components"] and old["source"]["rev"] == "HEAD"
    assert old["variant"]["not_fitted"][0]["designator"] == "R2"
    assert subprocess.run(["git", "-C", str(tmp_path), "status", "--porcelain"], capture_output=True, text=True).stdout == before
    with pytest.raises(cad.CadError, match="at nosuchrev"):
        cad.read_project(str(proj), rev="nosuchrev")
    with pytest.raises(cad.CadError, match="not a commit"):
        cad.read_project(str(proj), rev="--output=x")


def test_coordinates_join_on_fractions():
    assert cad.coord({"LOCATION.X": "5", "LOCATION.X_FRAC": "50000"}, "LOCATION.X") == 550000
    assert cad.pin_ends({"LOCATION.X": "1", "LOCATION.Y": "2", "PINLENGTH": "3", "PINCONGLOMERATE": "6"}) == [
        (100000, 200000), (-200000, 200000)]

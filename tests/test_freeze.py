"""doc freeze / doc thaw-check on the synthetic datasheet (tests/datasheet_synth.py: invented part SYN1234)."""
import json
import shutil

import pytest

import datasheet_synth as ds
from embarch_atlas import cli, common, freeze, records as R, regparse, render, rm

SECTION = ("# SYN1234 7.1: Overview (p.3)\n\nThermal shutdown at 165 C, restart below 142 C (p.3).\n\n"
           "<!-- atlas:table kind=rule -->\n<!-- atlas:end -->\n")
CARD = "# SYN1234 card (p.1-5)\n\n- UVLO 2.6 / 2.8 / 3.05 V (p.2)\n- TSD 165 C (p.3)\n"


@pytest.fixture()
def doc(tmp_path):
    atlas, docdir = ds.make_doc(tmp_path)
    regparse.run(docdir, "ti")                                   # deterministic records: never frozen
    (docdir / "s").mkdir()
    (docdir / "s" / "7.1.md").write_text(SECTION)
    (docdir / "card.md").write_text(CARD)
    render.render_doc(docdir)
    return atlas, docdir


def _change_page(docdir, p, extra="A sentence added in the new revision.\n"):
    f = docdir / "pages" / f"p{p:03d}.txt"
    f.write_text(f.read_text() + extra)


def test_unit_key_is_the_rm_scheme():
    class Pages:
        def sha(self, p):
            return f"sha{p}"
    u = {"id": "7.1", "pages": [3, 5]}
    assert freeze.unit_key("7.1", ["sha3", "sha4", "sha5"], rm.PROCEDURE) == rm.unit_key(Pages(), u)


def test_freeze_records_llm_output_only(doc):
    atlas, docdir = doc
    path, e = freeze.freeze(atlas, ds.DOC_ID, model="test-model")
    assert path.parent == atlas / "cache" / "llm" and path.name == common.sha256_text(json.dumps(
        {"procedure": "datasheet-procedure/1", "model": "test-model", "doc": ds.DOC_ID,
         "units": sorted(u["key"] for u in e["units"].values())}, sort_keys=True)) + ".json"
    assert common.read_json(path) == e
    types = {}
    for uid, u in e["units"].items():
        types[u["type"]] = types.get(u["type"], 0) + 1
    assert types == {"record": len(ds.records()), "prose": 1, "card": 1}
    assert not any(u["file"] == "records/regparse.jsonl" for u in e["units"].values())
    iq = e["units"]["rec:syn1234.ec.IQ.vin"]
    assert iq["pages"] == [2] and iq["content"]["q"] == "VIN = 7 V 355 505 uA" and "prov" not in iq["content"]
    assert iq["key"] == freeze.unit_key("rec:syn1234.ec.IQ.vin",
                                        [rm.page_sha((docdir / "pages" / "p002.txt").read_text())],
                                        "datasheet-procedure/1")
    prose = e["units"]["prose:s/7.1.md"]
    assert prose["pages"] == [3] and "switch turns off" not in prose["content"]   # the rendered rows are not prose
    assert "<!-- atlas:table kind=rule -->\n<!-- atlas:end -->" in prose["content"]
    assert e["units"]["card"]["pages"] == [1, 2, 3, 4, 5]


def test_thaw_check_keeps_unchanged_pages_and_sends_changed_ones_back(doc, capsys):
    atlas, docdir = doc
    freeze.freeze(atlas, ds.DOC_ID, model="m")
    rep = freeze.thaw_check(atlas, ds.DOC_ID)
    assert rep["redo"] == [] and rep["pages_changed"] == [] and rep["pdf_unchanged"]
    assert len(rep["kept"]) == len(ds.records()) + 2
    _change_page(docdir, 3)                                      # a new doc prep: p3's text changed
    rep = freeze.thaw_check(atlas, ds.DOC_ID)
    assert rep["pages_changed"] == [3]
    redo = {r["unit"]: r["changed"] for r in rep["redo"]}
    assert redo == {"rec:syn1234.rule.tsd": [3], "rec:syn1234.rule.out_max": [3], "prose:s/7.1.md": [3],
                    "card": [3]}
    assert "rec:syn1234.ec.IQ.vin" in {k["unit"] for k in rep["kept"]}
    # a kept record edited after the freeze is reported, a new one is not frozen yet
    pairs = R.load_records(docdir)
    for _, r in pairs:
        if r["id"] == "syn1234.ec.IQ.vin":
            r["note"] = "edited"
    pairs.append(("limit.jsonl", dict(pairs[0][1], id="syn1234.ec.NEW")))
    R.save_records(docdir, pairs)
    rep = freeze.thaw_check(atlas, ds.DOC_ID)
    assert rep["edited"] == ["rec:syn1234.ec.IQ.vin"] and rep["unfrozen"] == ["rec:syn1234.ec.NEW"]
    assert cli.main(["--atlas-dir", str(atlas), "doc", "thaw-check", ds.DOC_ID]) == 1
    out = capsys.readouterr().out
    assert "pages changed since the freeze: p3" in out and "redo 4 units" in out
    assert "rec:syn1234.rule.tsd (records/rule.jsonl) changed p3" in out


def test_thaw_check_from_previous_revision_and_restore(doc):
    atlas, docdir = doc
    freeze.freeze(atlas, ds.DOC_ID, model="m")
    new = atlas / "docs" / "syn1234@2c"                           # the next revision: p2 changed, the rest identical
    new.mkdir()
    shutil.copytree(docdir / "pages", new / "pages")
    common.write_json(new / "doc.json", dict(common.read_json(docdir / "doc.json"), doc_id="syn1234@2c",
                                             sha256="0" * 64, rev="2c"))
    _change_page(new, 2)
    rep = freeze.thaw_check(atlas, "syn1234@2c", from_doc=ds.DOC_ID)
    assert rep["from"] == ds.DOC_ID and not rep["pdf_unchanged"] and rep["pages_changed"] == [2]
    assert {r["unit"] for r in rep["redo"]} >= {"rec:syn1234.ec.IQ.vin", "card"}
    assert cli.main(["--atlas-dir", str(atlas), "doc", "thaw-check", "syn1234@2c", "--from", ds.DOC_ID,
                     "--restore"]) == 1
    got = {r["id"]: r for _, r in R.load_records(new)}
    assert "syn1234.rule.tsd" in got and "syn1234.ec.IQ.vin" not in got          # kept restored, redo not
    assert got["syn1234.rule.tsd"]["doc"] == "syn1234@2c" and "prov" not in got["syn1234.rule.tsd"]
    assert (new / "s" / "7.1.md").read_text() == freeze.prose((docdir / "s" / "7.1.md").read_text())
    assert not (new / "card.md").exists()                                         # it cites p2


def test_cli_freeze_and_errors(doc, capsys):
    atlas, docdir = doc
    assert cli.main(["--atlas-dir", str(atlas), "doc", "thaw-check", ds.DOC_ID]) == 2       # nothing frozen
    assert cli.main(["--atlas-dir", str(atlas), "doc", "freeze", ds.DOC_ID, "--model", "m"]) == 0
    out = capsys.readouterr().out
    assert f"frozen {ds.DOC_ID}: {len(ds.records())} records, 1 s/ files, 1 card" in out
    assert cli.main(["--atlas-dir", str(atlas), "doc", "thaw-check", ds.DOC_ID]) == 0
    assert "kept 15 units" in capsys.readouterr().out
    assert cli.main(["--atlas-dir", str(atlas), "doc", "freeze", "nope@1"]) == 2
    meta = common.read_json(docdir / "doc.json")
    common.write_json(docdir / "doc.json", dict(meta, kind="reference-manual"))
    with pytest.raises(SystemExit, match="rm-freeze"):
        freeze.freeze(atlas, ds.DOC_ID)

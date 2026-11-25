"""`embarch-atlas refs`: sections, symbols and nodes in one read, on synthetic fixtures."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_join import git, make_project  # noqa: E402

from embarch_atlas import cli, query, refs  # noqa: E402
from embarch_atlas.common import write_json  # noqa: E402


def _docs(ad: Path) -> None:
    ds = ad / "docs" / "sns701@a"
    write_json(ds / "toc.json", {"sections": [
        {"id": "1", "title": "Features", "level": 1, "pages": [1, 2], "tokens": 100},
        {"id": "1.1", "title": "Pin functions", "level": 2, "pages": [3, 3]},
        {"title": "no id", "level": 1, "pages": [4, 4]}]})
    rm = ad / "docs" / "mcu-rm"
    write_json(rm / "doc.json", {"doc_id": "mcu-rm", "kind": "reference-manual", "part": "MCU0"})
    write_json(rm / "outline.json", {"entries": [{"id": "I2C", "title": "I2C interface", "level": 1, "start": 700, "end": 760}]})


def _build(atlas: Path) -> None:
    write_json(atlas / "kconfig.json", {"symbols": {
        "I2C": {"t": "bool", "v": "y", "p": "I2C support"},
        "HIDDEN": {"t": "int", "v": "3"}}})
    dt = json.loads((atlas / "dt.json").read_text())
    dt["aliases"] = {"led0": "/leds/led_0", "status": "/leds/led_0"}
    dt["nodes"]["/leds/led_0"]["status"] = "okay"
    dt["nodes"]["/soc/adc@40012400"]["status"] = "disabled"
    write_json(atlas / "dt.json", dt)


def _run(capsys, *argv) -> dict:
    assert cli.main(list(argv)) == 0
    return json.loads(capsys.readouterr().out)


def test_sections_symbols_and_nodes(tmp_path, capsys):
    ad, atlas = make_project(tmp_path)
    _docs(ad)
    _build(atlas)
    out = _run(capsys, "--atlas-dir", str(ad), "refs", "--json")
    assert out["atlas_refs"] == 1 and out["atlas"] == atlas.name and out["stale"] == []
    assert out["commit"] == json.loads((atlas / "atlas.json").read_text())["commit"]
    sec = {(s["doc"], s["id"]): s for s in out["sections"]}
    assert sec[("sns701@a", "1.1")] == {"doc": "sns701@a", "kind": "datasheet", "part": "SNS701", "id": "1.1",
                                        "title": "Pin functions", "level": 2, "page": 3}
    assert sec[("mcu-rm", "I2C")]["kind"] == "reference-manual" and sec[("mcu-rm", "I2C")]["page"] == 700
    assert len(out["sections"]) == 3                      # the row with no id is not a reference
    assert out["symbols"] == [{"name": "HIDDEN", "prompt": None, "type": "int"},
                              {"name": "I2C", "prompt": "I2C support", "type": "bool"}]
    nodes = {n["path"]: n for n in out["nodes"]}
    assert nodes["/leds/led_0"] == {"path": "/leds/led_0", "labels": ["status_led"], "compats": [],
                                    "status": "okay", "aliases": ["led0", "status"]}
    assert nodes["/soc/adc@40012400"]["status"] == "disabled" and nodes["/soc/adc@40012400"]["aliases"] == []


def test_counts_without_json_and_nothing_written(tmp_path, capsys):
    ad, atlas = make_project(tmp_path)
    _docs(ad)
    _build(atlas)
    before = sorted(p for p in ad.rglob("*") if p.is_file())
    assert cli.main(["--atlas-dir", str(ad), "refs"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[1:3] == ["3 sections", "2 symbols"] and lines[3].endswith(" nodes") and int(lines[3].split()[0]) > 0
    assert sorted(p for p in ad.rglob("*") if p.is_file()) == before    # no usage.jsonl row, no file


def test_no_atlas_folder(tmp_path, capsys):
    out = _run(capsys, "refs", "--json", "--atlas-dir", str(tmp_path / "absent"))
    assert out == {"atlas_refs": 1, "atlas_dir": None, "atlas": None, "commit": None, "stale": [],
                   "sections": [], "symbols": [], "nodes": []}


def test_documents_without_a_built_atlas(tmp_path, capsys):
    ad, _ = make_project(tmp_path)
    _docs(ad)
    shutil.rmtree(ad / "atlases")
    out = _run(capsys, "--atlas-dir", str(ad), "refs", "--json")
    assert out["atlas_dir"] == str(ad.resolve()) and out["atlas"] is None
    assert len(out["sections"]) == 3 and out["symbols"] == [] and out["nodes"] == []


def test_stale_atlas_still_answers(tmp_path, capsys):
    ad, atlas = make_project(tmp_path)
    _build(atlas)
    fw = ad.parent.parent
    (fw / "more.txt").write_text("x")
    git(fw, "add", "more.txt")
    git(fw, "commit", "-q", "-m", "move HEAD")
    out = _run(capsys, "--atlas-dir", str(ad), "refs", "--json")
    assert out["atlas"] == atlas.name and out["stale"] and "HEAD" in " ".join(out["stale"])
    assert [s["name"] for s in out["symbols"]] == ["HIDDEN", "I2C"]


def test_linked_worktree_reads_the_main_clones_atlas(tmp_path, capsys, monkeypatch):
    ad, atlas = make_project(tmp_path)
    _build(atlas)
    fw = ad.parent.parent
    wt = tmp_path / "wt"
    git(fw, "worktree", "add", "-q", str(wt), "-b", "side")
    assert not (wt / "embarch").exists()
    monkeypatch.chdir(wt)
    out = _run(capsys, "refs", "--json")
    assert out["atlas_dir"] == str(ad.resolve()) and out["atlas"] == atlas.name

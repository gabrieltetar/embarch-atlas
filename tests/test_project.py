"""project: `init` writes atlas.toml once; `status` finds current and stale atlases."""
from __future__ import annotations

import os
import subprocess
import tomllib
from pathlib import Path

import pymupdf
import pytest

from embarch_atlas import cli, common, project

GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env=GIT_ENV).stdout.strip()


def make_fw(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / ".west").mkdir(parents=True)
    fw = ws / "fw"
    (fw / "apps" / "demo").mkdir(parents=True)
    (fw / "apps" / "demo" / "prj.conf").write_text("CONFIG_GPIO=y\n")
    (fw / "boards" / "acme" / "demo_board").mkdir(parents=True)
    (fw / "boards" / "acme" / "demo_board" / "board.yml").write_text("board:\n  name: demo_board\n")
    hw = fw / "embarch" / "hardware"
    hw.mkdir(parents=True)
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Sheet 1 of 1")
    doc.set_metadata({"creator": "Altium Designer"})
    doc.save(hw / "main-board.pdf")
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "A datasheet")
    doc.set_metadata({"creator": "Word"})
    doc.save(hw / "part.pdf")
    git(fw, "init", "-q")
    git(fw, "add", ".")
    git(fw, "commit", "-q", "-m", "one")
    return fw


def test_load_config_missing(tmp_path):
    with pytest.raises(FileNotFoundError, match="init"):
        project.load_config(tmp_path)


def test_find_west_topdir(tmp_path):
    (tmp_path / "a" / ".west").mkdir(parents=True)
    (tmp_path / "a" / "b" / "c").mkdir(parents=True)
    assert project.find_west_topdir(tmp_path / "a" / "b" / "c") == (tmp_path / "a").resolve()
    assert project.find_west_topdir(tmp_path / "a" / "b" / "c" / ".." / "..") == (tmp_path / "a").resolve()


def test_init_writes_once(tmp_path, capsys):
    fw = make_fw(tmp_path)
    assert cli.main(["init", str(fw)]) == 0
    path = fw / "embarch" / "atlas" / "atlas.toml"
    text = path.read_text()
    cfg = tomllib.loads(text)
    assert cfg["project"]["fw"] == str(fw.resolve())
    assert cfg["project"]["west_topdir"] == str((tmp_path / "ws").resolve())
    assert cfg["project"]["sources"] == "embarch/hardware"
    assert "boards" not in cfg and "targets" not in cfg  # left as commented examples
    assert "# [[boards]]" in text and "# [[targets]]" in text
    assert "Schematic PDFs found in sources: main-board.pdf" in text  # only the EDA-made PDF
    assert "apps/demo" in text and "demo_board" in text
    assert project.load_config(path.parent) == cfg
    path.write_text(text + "\n# engineer edit\n")
    assert cli.main(["init", str(fw)]) == 0
    assert path.read_text().endswith("# engineer edit\n")  # never overwritten
    assert "left untouched" in capsys.readouterr().out


def test_init_without_west(tmp_path):
    fw = tmp_path / "lonely"
    fw.mkdir()
    path, written = project.init(fw)
    assert written
    cfg = project.load_config(path.parent)
    assert "west_topdir" not in cfg["project"]
    assert "does not exist yet" in path.read_text()


def stamp(adir: Path, name: str, **kw) -> None:
    common.write_json(adir / "atlases" / name / "atlas.json", {"board": "demo_board", "dirty": False, **kw})


def test_status_current_and_stale(tmp_path, capsys):
    fw = make_fw(tmp_path)
    project.init(fw)
    adir = fw / "embarch" / "atlas"
    cfg_text = (adir / "atlas.toml").read_text() + '\n[[boards]]\nlabel = "M"\npdf = "main-board.pdf"\n'
    (adir / "atlas.toml").write_text(cfg_text)
    head = git(fw, "rev-parse", "HEAD")
    man = adir / "inventory" / "manifest.json"
    common.write_json(man, {"documents": []})
    msha = common.sha256_file(man)
    pdf_sha = common.sha256_file(fw / "embarch" / "hardware" / "main-board.pdf")
    stamp(adir, f"core@{head[:12]}", commit=head, sources=msha, schematics={"M": pdf_sha})
    stamp(adir, "core@000000000000", commit="0" * 40, sources=msha, schematics={"M": pdf_sha})
    st = project.status(adir)
    rows = {r["atlas"]: r for r in st["atlases"]}
    assert rows[f"core@{head[:12]}"]["matches_head"] and rows[f"core@{head[:12]}"]["stale"] == []
    assert rows["core@000000000000"]["stale"] == ["commit differs from HEAD"]
    cur = common.read_json(adir / "current.json")
    assert cur == {"commit": head, "matches": [{"atlas": f"core@{head[:12]}", "board": "demo_board", "stale": []}]}
    # a changed manifest and a changed board PDF make the HEAD atlas stale too
    common.write_json(man, {"documents": [{"file": "x"}]})
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Sheet 1 of 1, rev B")
    doc.save(fw / "embarch" / "hardware" / "main-board.pdf")
    st = project.status(adir)
    rows = {r["atlas"]: r for r in st["atlases"]}
    assert rows[f"core@{head[:12]}"]["stale"] == ["inventory/manifest.json changed", "board M PDF changed"]
    assert cli.main(["--atlas-dir", str(adir), "status"]) == 0
    out = capsys.readouterr().out
    assert "current" in out and "stale: commit differs from HEAD" in out and "working tree dirty" in out


def test_status_empty(tmp_path, capsys):
    fw = make_fw(tmp_path)
    project.init(fw)
    adir = fw / "embarch" / "atlas"
    assert cli.main(["--atlas-dir", str(adir), "status"]) == 0
    assert "no atlases yet" in capsys.readouterr().out
    assert common.read_json(adir / "current.json")["matches"] == []


def test_wire_session_map_is_idempotent_and_git_excluded(tmp_path):
    import subprocess
    from embarch_atlas import project
    fw = tmp_path / "fw"
    fw.mkdir()
    subprocess.run(["git", "init", "-q", str(fw)], check=True)
    first = project.wire_session_map(fw)
    assert len(first) == 2
    text = (fw / "CLAUDE.local.md").read_text()
    assert project.IMPORT_LINE in text
    assert "CLAUDE.local.md" in (fw / ".git" / "info" / "exclude").read_text()
    assert project.wire_session_map(fw) == []
    out = subprocess.run(["git", "-C", str(fw), "status", "--porcelain"], capture_output=True, text=True).stdout
    assert out == ""

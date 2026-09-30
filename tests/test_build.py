"""build: worktree, module override, the from-the-worktree proof, the stamp.

A fake `west` (a Python script) stands in for the real one, so the whole verb
runs without Zephyr. A test against a real west workspace runs only when
EMBARCH_ATLAS_REAL_WEST_TOPDIR, _FW, _BOARD and _APP are set (it builds, which
takes minutes).
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from embarch_atlas import build
from embarch_atlas.common import read_json, write_json

FAKE_WEST = r'''#!{python}
import json, os, sys
from pathlib import Path
a = sys.argv[1:]
top = Path.cwd()
projects = json.loads(os.environ["FAKE_WEST_PROJECTS"])
if a[0] == "list":
    for p in projects:
        print("\t".join([p["name"], p["path"], "main", "0" * 40, str(top / p["path"])]))
elif a[0] == "config":
    print("zephyr")
elif a[0] == "build":
    d = Path(a[a.index("-d") + 1]); s = Path(a[a.index("-s") + 1])
    mods = next(x for x in a if x.startswith("-DZEPHYR_MODULES=")).split("=", 1)[1].split(";")
    fwmod = next(m for m in mods if (Path(m) / "boards").is_dir())
    (d / "zephyr").mkdir(parents=True, exist_ok=True)
    (d / "build_info.yml").write_text(
        "cmake:\n  application:\n    source-dir: '%s'\n  board:\n    name: 'demo_board'\n    path:\n     - '%s/boards/demo_board'\n"
        "  devicetree:\n    files:\n     - '%s/boards/demo_board/demo_board.dts'\n" % (s, fwmod, fwmod))
    (d / "zephyr_modules.txt").write_text("".join('"%s":"%s":"%s"\n' % (Path(m).name, m, m) for m in mods))
    tus = [{"directory": str(d), "file": str(s / "src" / "main.c"), "command": "cc -I%s/include -c main.c" % fwmod}]
    leak = os.environ.get("FAKE_WEST_LEAK")
    if leak:
        tus.append({"directory": str(d), "file": leak, "command": "cc -c x.c"})
    (d / "compile_commands.json").write_text(json.dumps(tus))
    for f in ("edt.pickle", ".config", "zephyr.elf", "zephyr.dts"):
        (d / "zephyr" / f).write_text(f)
    (d / "zephyr" / "zephyr.dts.d").write_text("x: %s/boards/demo_board/demo_board.dts\n" % fwmod)
    (d / "CMakeCache.txt").write_text("CMAKE_NM:FILEPATH=/opt/tc/bin/nm\n")
else:
    sys.exit(2)
'''


def git(repo: Path, *argv: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *argv], capture_output=True, text=True, check=True).stdout.strip()


def workspace(tmp: Path) -> tuple[Path, Path, str, str]:
    """A west-like topdir with a fw repo (itself a Zephyr module with a board) and one other module."""
    top = tmp / "ws"
    fw = top / "fw-repo"
    (fw / "zephyr").mkdir(parents=True)
    (fw / "zephyr" / "module.yml").write_text("build:\n  settings:\n    board_root: .\n    dts_root: .\n")
    (fw / "boards" / "demo_board").mkdir(parents=True)
    (fw / "boards" / "demo_board" / "board.yml").write_text("board:\n  name: demo_board\n  vendor: acme\n")
    (fw / "app" / "src").mkdir(parents=True)
    (fw / "app" / "src" / "main.c").write_text("int main(void) { return 0; }\n")
    hal = top / "modules" / "hal"
    (hal / "zephyr").mkdir(parents=True)
    (hal / "zephyr" / "module.yml").write_text("name: hal_demo\nbuild:\n  cmake: .\n")
    (top / "zephyr").mkdir()
    (top / "manifest").mkdir()
    git(fw, "init", "-q")
    git(fw, "config", "user.email", "t@example.com")
    git(fw, "config", "user.name", "t")
    git(fw, "add", ".")
    git(fw, "commit", "-q", "-m", "one")
    c1 = git(fw, "rev-parse", "HEAD")
    (fw / "app" / "src" / "main.c").write_text("int main(void) { return 1; }\n")
    git(fw, "commit", "-q", "-am", "two")
    c2 = git(fw, "rev-parse", "HEAD")
    west = tmp / "bin" / "west"
    west.parent.mkdir()
    west.write_text(FAKE_WEST.replace("{python}", sys.executable))
    west.chmod(west.stat().st_mode | stat.S_IEXEC)
    os.environ["FAKE_WEST_PROJECTS"] = json.dumps([
        {"name": "manifest", "path": "manifest"}, {"name": "zephyr", "path": "zephyr"},
        {"name": "fw-repo", "path": "fw-repo"}, {"name": "hal_demo", "path": "modules/hal"}])
    return top, fw, c1, c2


def cfg_for(top: Path, fw: Path, west: Path) -> dict:
    return {"target": "demo", "fw": str(fw), "west_topdir": str(top), "west": str(west), "sources": "hw",
            "board": "demo_board", "app": "app", "schematic_boards": ["M"], "extra_args": [],
            "mcu": {"part": "MCU0", "svd": None, "ref": None}, "boards": {"M": "main.pdf"}, "from_toml": False}


def test_build_end_to_end_with_fake_west(tmp_path, monkeypatch):
    top, fw, c1, _c2 = workspace(tmp_path)
    (fw / "hw").mkdir()
    (fw / "hw" / "main.pdf").write_bytes(b"%PDF-1.4 demo")
    ad = fw / "embarch" / "atlas"
    monkeypatch.delenv("FAKE_WEST_LEAK", raising=False)
    out = build.run_build(ad, cfg_for(top, fw, tmp_path / "bin" / "west"), c1, log=lambda *a: None)
    assert out == ad / "atlases" / f"demo@{c1[:12]}"
    st = read_json(out / "atlas.json")
    assert st["commit"] == c1 and st["dirty"] is False and st["id"] == f"demo@{c1[:12]}"
    assert st["schematics"]["M"] and st["sources"] is None
    fwrow = next(w for w in st["west"] if w["name"] == "fw-repo")
    assert fwrow["head"] == c1 and fwrow["atlas_commit"]           # the pinned commit, not the checkout's HEAD
    ev = st["build"]["from_worktree"]
    wt = ad / ".work" / c1[:12] / "fw-repo"
    assert Path(ev["worktree"]) == wt.resolve()
    assert ev["tus_from_main_checkout"] == 0 and ev["tus_in_worktree"] == 1
    assert ev["fw_module_is_worktree"] is True
    assert ev["main_checkout_board_definitions_excluded"] == [str(fw / "boards" / "demo_board")]
    assert ev["board_definitions"] == [str(wt / "boards" / "demo_board")]
    mods = st["build"]["zephyr_modules"]
    assert mods == [str((top / "modules" / "hal").resolve()), str(wt)]
    for f in build.REQUIRED:
        assert (out / "build" / f).is_file(), f
    assert st["build"]["tools"]["CMAKE_NM"] == "/opt/tc/bin/nm"
    # the worktree is at the pinned commit (older than the checkout), detached
    assert (wt / "app" / "src" / "main.c").read_text().endswith("return 0; }\n")
    assert git(fw, "rev-parse", "HEAD") != c1
    # the engineer's checkout is untouched
    assert git(fw, "status", "--porcelain", "--untracked-files=no") == ""
    # a second build of the same commit is a no-op
    assert build.run_build(ad, cfg_for(top, fw, tmp_path / "bin" / "west"), c1, log=lambda *a: None) == out


def test_build_fails_when_a_tu_comes_from_the_main_checkout(tmp_path, monkeypatch):
    top, fw, c1, _ = workspace(tmp_path)
    ad = fw / "embarch" / "atlas"
    monkeypatch.setenv("FAKE_WEST_LEAK", str(fw / "app" / "src" / "main.c"))
    with pytest.raises(build.BuildError, match="did not come"):
        build.run_build(ad, cfg_for(top, fw, tmp_path / "bin" / "west"), c1, log=lambda *a: None)
    assert not (ad / "atlases" / f"demo@{c1[:12]}" / "atlas.json").exists()


def test_worktree_reuse_and_dirty(tmp_path):
    _top, fw, c1, c2 = workspace(tmp_path)
    ad = fw / "embarch" / "atlas"
    wt = build.ensure_worktree(fw, ad, c1)
    assert wt == ad / ".work" / c1[:12] / fw.name
    assert build.ensure_worktree(fw, ad, c1) == wt              # reused
    (wt / "app" / "src" / "main.c").write_text("dirty\n")
    with pytest.raises(build.BuildError, match="not clean"):
        build.ensure_worktree(fw, ad, c1)
    assert build.ensure_worktree(fw, ad, c2).name == fw.name     # another commit, another worktree


def test_module_list_and_duplicate_boards(tmp_path):
    top, fw, _, _ = workspace(tmp_path)
    wt = tmp_path / "wt" / "fw-repo"
    (wt / "zephyr").mkdir(parents=True)
    (wt / "zephyr" / "module.yml").write_text("build:\n  settings:\n    board_root: .\n")
    (wt / "boards" / "demo_board").mkdir(parents=True)
    (wt / "boards" / "demo_board" / "board.yml").write_text("board:\n  name: demo_board\n")
    projects = [{"abspath": str(top / p)} for p in ("manifest", "zephyr", "fw-repo", "modules/hal")]
    mods = build.module_list(projects, fw, wt, top / "zephyr")
    assert mods == [str((top / "modules" / "hal").resolve()), str(wt)]
    one = build.board_definitions("demo_board", build.board_roots(mods, top / "zephyr", wt / "app"))
    assert one == [str(wt / "boards" / "demo_board")]
    both = build.board_definitions("demo_board", build.board_roots(mods + [str(fw)], top / "zephyr", wt / "app"))
    assert len(both) == 2                                        # what the override prevents


def test_west_command_merges_cmake_args():
    cmd = build.west_command("west", Path("/b"), "brd", Path("/s"), ["--sysbuild", "--", "-DX=1", "-DZEPHYR_MODULES=/old"],
                             ["/m1", "/wt"])
    assert cmd[:10] == ["west", "build", "-d", "/b", "-b", "brd", "-s", "/s", "-p", "auto"]
    assert cmd[10] == "--sysbuild" and cmd[11] == "--"
    assert cmd[12:] == ["-DX=1", "-DZEPHYR_MODULES=/m1;/wt"]


def test_verify_detects_leaks(tmp_path):
    fw, ad = tmp_path / "fw", tmp_path / "fw" / "embarch" / "atlas"
    wt, bdir = ad / ".work" / "abc" / "fw", ad / ".work" / "build"
    bdir.mkdir(parents=True)
    wt.mkdir(parents=True)
    (bdir / "build_info.yml").write_text(textwrap.dedent(f"""\
        cmake:
          application:
            source-dir: '{wt}/app'
          board:
            path:
             - '{wt}/boards/b'
        """))
    (bdir / "compile_commands.json").write_text(json.dumps([{"file": f"{wt}/app/main.c", "command": f"cc -I{bdir}/gen -c"}]))
    ev = build.verify_from_worktree(bdir, fw, wt, ad)
    assert ev["board_dir"] == f"{wt}/boards/b" and ev["tus_in_worktree"] == 1
    (bdir / "compile_commands.json").write_text(json.dumps([{"file": f"{wt}/app/main.c", "command": f"cc -I{fw}/include -c"}]))
    with pytest.raises(build.BuildError, match="include"):
        build.verify_from_worktree(bdir, fw, wt, ad)


def test_target_config_from_toml_and_flags(tmp_path):
    ad = tmp_path / "fw" / "embarch" / "atlas"
    ad.mkdir(parents=True)
    (ad / "atlas.toml").write_text(textwrap.dedent("""\
        [project]
        fw = "/x/fw"
        west_topdir = "/x"
        [mcu]
        part = "MCU0"
        [[boards]]
        label = "M"
        pdf = "main.pdf"
        [[targets]]
        name = "demo"
        board = "demo_board"
        app = "app"
        schematic_boards = ["M"]
        extra_args = ["--", "-DFOO=1"]
        """))

    class A:
        fw = None
        west_topdir = None
        board = "other_board"
        schematic_boards = None
        extra_arg = None
        board_pdf = ["S=side.pdf"]

    cfg = build.target_config(ad, "demo", A())
    assert cfg["fw"] == "/x/fw" and cfg["board"] == "other_board" and cfg["app"] == "app"
    assert cfg["extra_args"] == ["--", "-DFOO=1"] and cfg["mcu"]["part"] == "MCU0"
    assert cfg["boards"] == {"M": "main.pdf", "S": "side.pdf"} and cfg["from_toml"]


def test_resolve_atlas(tmp_path):
    ad = tmp_path
    for aid in ("demo@aaaaaaaaaaaa", "demo@bbbbbbbbbbbb"):
        write_json(ad / "atlases" / aid / "atlas.json", {"id": aid})
    assert build.resolve_atlas(ad, "demo@aaaaaaaaaaaa") == ad / "atlases" / "demo@aaaaaaaaaaaa"
    assert build.resolve_atlas(ad, str(ad / "atlases" / "demo@bbbbbbbbbbbb")).name == "demo@bbbbbbbbbbbb"
    assert build.resolve_atlas(ad, "demo@bbb").name == "demo@bbbbbbbbbbbb"
    write_json(ad / "current.json", {"commit": "b" * 40, "matches": [{"atlas": "demo@bbbbbbbbbbbb"}]})
    assert build.resolve_atlas(ad, "demo").name == "demo@bbbbbbbbbbbb"
    with pytest.raises(SystemExit):
        build.resolve_atlas(ad, "nothing")


REAL = {k: os.environ.get(f"EMBARCH_ATLAS_REAL_{k}") for k in ("WEST_TOPDIR", "FW", "BOARD", "APP")}


@pytest.mark.skipif(not all(REAL.values()), reason="needs a real west workspace (EMBARCH_ATLAS_REAL_* env vars)")
def test_real_worktree_build(tmp_path):
    cfg = {"target": "real", "fw": REAL["FW"], "west_topdir": REAL["WEST_TOPDIR"], "west": os.environ.get("EMBARCH_ATLAS_REAL_WEST"),
           "sources": "embarch/hardware", "board": REAL["BOARD"], "app": REAL["APP"], "schematic_boards": [],
           "extra_args": [], "mcu": {}, "boards": {}, "from_toml": False}
    ad = Path(REAL["FW"]) / "embarch" / "atlas"
    out = build.run_build(ad, cfg, "HEAD", log=lambda *a: None)
    ev = read_json(out / "atlas.json")["build"]["from_worktree"]
    assert ev["tus_from_main_checkout"] == 0 and ev["includes_from_main_checkout"] == 0

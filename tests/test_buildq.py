"""config and dt_node over synthetic builds (invented symbols, nodes and files)."""
import json
from pathlib import Path

import pytest

from embarch_atlas import kconfig as KC, query
from embarch_atlas.common import read_json, write_json, write_text

BUILD_INFO = """cmake:
  board:
    name: 'demo_board'
  kconfig:
    files:
     - '{d}/demo_defconfig'
     - '{d}/prj.conf'
    user-files:
     - '{d}/prj.conf'
  zephyr:
    version: '9.9.0'
version: '0.1.0'
"""


@pytest.fixture()
def atlas_dir(tmp_path: Path) -> Path:
    a = tmp_path / "fw" / "embarch" / "atlas"
    src = tmp_path / "fw" / "app"
    write_text(src / "demo_defconfig", "CONFIG_GPIO=y\n")
    write_text(src / "prj.conf", "CONFIG_SHELL=y\nCONFIG_FANCY=y\n# CONFIG_LOG is not set\n")
    at = a / "atlases" / "core@000000000003"
    write_json(at / "atlas.json", {"id": "core@000000000003", "commit": "0" * 40,
                                   "build": {"worktree": str(tmp_path / "fw"), "zephyr_base": str(tmp_path / "zb")}})
    write_text(at / "build" / ".config", "CONFIG_GPIO=y\nCONFIG_SHELL=y\n# CONFIG_FANCY is not set\n"
                                         "# CONFIG_LOG is not set\nCONFIG_STACK=1024\n")
    write_text(at / "build" / "build_info.yml", BUILD_INFO.format(d=src))
    write_json(at / "dt.json", {
        "nodes": {
            "/soc/i2c@100": {"labels": ["i2c1"], "compats": ["acme,i2c"], "okay": True, "binding": True,
                             "ordinal": 3, "parent": "/soc", "props": {}},
            "/soc/i2c@100/conv@74": {"labels": ["conv0"], "compats": ["acme,conv42"], "okay": True,
                                     "binding": True, "ordinal": 7, "parent": "/soc/i2c@100", "on_bus": ["i2c"],
                                     "props": {"reg": {"loc": "fw:board.dts:12", "val": [116]},
                                               "slew": {"loc": None, "val": 5}}}},
        "uses": [{"node": "/soc/i2c@100/conv@74", "pin": "PB3", "prop": "en-gpios", "index": 0,
                  "flags": "ACTIVE_LOW", "loc": "fw:board.dts:14"}],
        "aliases": {"buck": "/soc/i2c@100/conv@74"}, "chosen": {},
        "drivers": {"acme_conv42": ["fw:drivers/conv42.c"]}, "consumers": {}, "instantiated_ordinals": [7]})
    write_json(a / "current.json", {"matches": [{"atlas": "core@000000000003"}]})
    return a


def _kconfig_json(at: Path) -> None:
    write_json(at / "kconfig.json", {"zephyr": "v9.9.0", "app": "abc", "fragments": [], "symbols": {
        "SHELL": {"t": "bool", "v": "y", "at": ["zephyr:subsys/shell/Kconfig:8"], "p": "Shell",
                  "asg": [["fw:app/prj.conf", 1, "y"]]},
        "FANCY": {"t": "bool", "v": "n", "at": ["zephyr:lib/Kconfig:3"], "p": "Fancy", "dep": "SHELL && NET",
                  "dv": "n", "asg": [["fw:app/prj.conf", 2, "y"]], "ign": True},
        "STACK": {"t": "int", "v": "1024", "at": ["zephyr:soc/demo/Kconfig.defconfig:4", "zephyr:kernel/Kconfig:9"],
                  "at_inactive": 12, "def": [["1024", "y"], ["2048", "SHELL"]]},
        "GPIO": {"t": "bool", "v": "y", "at": ["zephyr:drivers/gpio/Kconfig:1"], "sel": [["SHELL", "y"]],
                 "asg": [["fw:app/demo_defconfig", 1, "y"]]}}})


def test_config_without_kconfig_json_gives_value_and_fragment(atlas_dir):
    out = query.config(query.Atlas(atlas_dir), "CONFIG_FANCY")
    assert out.startswith("src: atlas core@000000000003 kconfig")
    assert "CONFIG_FANCY=n" in out and "assigned y in" in out and "prj.conf:2" in out
    assert "not applied" in out and "no kconfig.json" in out


def test_config_with_kconfig_json_explains_the_value(atlas_dir):
    _kconfig_json(atlas_dir / "atlases" / "core@000000000003")
    a = query.Atlas(atlas_dir)
    fancy = query.config(a, "FANCY")
    assert "depends on SHELL && NET  [NOT met]" in fancy
    assert "why n: the fragment asks for it, but its dependencies are not met" in fancy
    stack = query.config(a, "stack")
    assert "default 1024  <- applies" in stack and "default 2048 if SHELL" in stack
    assert "defined at zephyr:soc/demo/Kconfig.defconfig:4" in stack and "+12 definitions" in stack
    assert "selected by CONFIG_SHELL" in query.config(a, "GPIO")
    assert "Nearest: CONFIG_SHELL" in query.config(a, "SHEL")
    listed = query.config(a, "S*")
    assert "CONFIG_SHELL=y" in listed and "CONFIG_STACK=1024" in listed


def test_dt_node_by_label_alias_compatible_and_name(atlas_dir):
    a = query.Atlas(atlas_dir)
    out = query.dt_node(a, "conv0")
    assert out.startswith("src: atlas core@000000000003 devicetree")
    assert "/soc/i2c@100/conv@74" in out and "a device in the ELF" in out
    assert "parent /soc/i2c@100 (i2c1), on bus i2c" in out
    assert "driver fw:drivers/conv42.c (compiled)" in out
    assert "reg = [0x74]  (fw:board.dts:12)" in out and "slew = 5  (binding default)" in out
    assert "PB3 en-gpios[0] ACTIVE_LOW" in out
    for q in ("buck", "acme,conv42", "conv", "conv42", "&conv0"):
        assert "/soc/i2c@100/conv@74" in query.dt_node(a, q), q
    assert "Nearest: conv0" in query.dt_node(a, "conv1")


def test_kconfig_file_readers(tmp_path):
    d = tmp_path / "b"
    write_text(d / "build_info.yml", BUILD_INFO.format(d="/x"))
    assert KC.fragment_files(d) == ["/x/demo_defconfig", "/x/prj.conf"]
    write_text(tmp_path / "p.conf", 'CONFIG_A=y\nCONFIG_S="hi"\n# CONFIG_B is not set\nCONFIG_A=n\n')
    asg = KC.assignments([str(tmp_path / "p.conf")])
    assert [v for _, _, v in asg["A"]] == ["y", "n"] and asg["S"][0][2] == "hi" and asg["B"][0][2] == "n"
    write_text(d / "zephyr" / "include" / "generated" / "zephyr" / "version.h", "#define BUILD_VERSION v9.9.0-1-gabc\n")
    assert KC.version_h(d, "BUILD_VERSION") == "v9.9.0-1-gabc"


def test_ninja_menuconfig_env_and_workspace_guard(tmp_path):
    b = tmp_path / "b"
    zb = tmp_path / "zb"
    zb.mkdir()
    line = (f"  COMMAND = cd {b}/zephyr/kconfig && /usr/bin/cmake -E env ZEPHYR_BASE={zb} srctree={zb} "
            f"KCONFIG_CONFIG={b}/zephyr/.config /venv/bin/python3 {zb}/scripts/kconfig/menuconfig.py {zb}/Kconfig\n")
    write_text(b / "build.ninja", "build menuconfig: CUSTOM_COMMAND\n" + line)
    env, py, root, cwd = KC.ninja_menuconfig(b)
    assert env["ZEPHYR_BASE"] == str(zb) and py == "/venv/bin/python3" and root == f"{zb}/Kconfig"
    assert cwd == f"{b}/zephyr/kconfig"
    assert "no BUILD_VERSION" in KC.workspace_mismatch(b)
    write_text(b / "zephyr" / "include" / "generated" / "zephyr" / "version.h", "#define BUILD_VERSION v9.9.0\n")
    assert "the build used Zephyr v9.9.0" in KC.workspace_mismatch(b)


def test_named_build_off_workspace_falls_back_to_value(atlas_dir, tmp_path):
    b = tmp_path / "fw" / "build"
    zb = tmp_path / "zb"
    zb.mkdir()
    write_text(b / "build.ninja", f"  COMMAND = /usr/bin/cmake -E env ZEPHYR_BASE={zb} /py "
                                  f"{zb}/scripts/kconfig/menuconfig.py {zb}/Kconfig\n")
    write_text(b / "zephyr" / ".config", "CONFIG_SHELL=y\n")
    write_text(b / "zephyr" / "include" / "generated" / "zephyr" / "version.h", "#define BUILD_VERSION v9.9.0\n")
    out = query.config(query.Atlas(atlas_dir), "SHELL", build="build")
    assert out.startswith(f"src: build {b} (app ?, zephyr v9.9.0;")
    assert "CONFIG_SHELL=y" in out and "the build used Zephyr v9.9.0" in out
    assert f"(no {b}/zephyr/edt.pickle)" in query.dt_node(query.Atlas(atlas_dir), "conv0", build="build")


def test_config_and_dt_node_page_to_the_uncut_answer(atlas_dir, monkeypatch):
    from paging_synth import assert_exact

    _kconfig_json(atlas_dir / "atlases" / "core@000000000003")
    a = query.Atlas(atlas_dir)
    assert_exact(monkeypatch, "config", lambda o: query.config(a, "S*", offset=o))
    assert_exact(monkeypatch, "dt_node", lambda o: query.dt_node(a, "conv0", offset=o))


def test_config_quotes_why_the_extraction_failed_and_names_a_recovery(atlas_dir):
    at = atlas_dir / "atlases" / "core@000000000003"
    st = read_json(at / "atlas.json")
    st["build"]["kconfig"] = "not extracted: kconfiglib missing in the build's Python"
    write_json(at / "atlas.json", st)
    out = query.config(query.Atlas(atlas_dir), "CONFIG_FANCY")
    assert "kconfiglib missing in the build's Python" in out
    assert "embarch-atlas kconfig-extract core@000000000003" in out and "--force" in out
    assert "the next /atlas-update writes one" not in out


def test_kconfig_extract_writes_for_a_built_atlas_and_keeps_the_zephyr_guard(atlas_dir, monkeypatch):
    from embarch_atlas import build
    at = atlas_dir / "atlases" / "core@000000000003"
    bdir = atlas_dir / ".work" / "build-core-000000000003"
    write_text(bdir / "zephyr" / ".config", "CONFIG_GPIO=y\n")
    st = read_json(at / "atlas.json")
    st["build"]["dir"] = str(Path(".work") / "build-core-000000000003")
    write_json(at / "atlas.json", st)
    monkeypatch.setattr(KC, "workspace_mismatch", lambda z: "the build used Zephyr v1; zb is at v2 now")
    monkeypatch.setattr(KC, "extract", lambda z, b: {"symbols": {"GPIO": {"v": "y"}}})
    with pytest.raises(build.BuildError, match="v2 now.*--force"):
        build.extract_kconfig(atlas_dir, at, log=lambda *a: None)
    assert not (at / "kconfig.json").exists()
    monkeypatch.setattr(KC, "workspace_mismatch", lambda z: None)
    build.extract_kconfig(atlas_dir, at, log=lambda *a: None)
    assert read_json(at / "kconfig.json")["symbols"]["GPIO"]["v"] == "y"
    # build dir gone: no false route
    (bdir / "zephyr" / ".config").unlink()
    (at / "kconfig.json").unlink()
    with pytest.raises(build.BuildError, match="is gone.*--force"):
        build.extract_kconfig(atlas_dir, at, log=lambda *a: None)


def test_config_takes_several_names_one_line_each(atlas_dir):
    _kconfig_json(atlas_dir / "atlases" / "core@000000000003")
    a = query.Atlas(atlas_dir)
    out = query.config(a, "SHELL,CONFIG_FANCY, stack,GPIO")
    assert out.startswith("src: atlas core@000000000003 kconfig query='SHELL,CONFIG_FANCY,stack,GPIO'")
    body = out.split("\n\n", 1)[1].splitlines()
    assert body == ["CONFIG_SHELL=y  fw:app/prj.conf:1",
                    "CONFIG_FANCY=n  fw:app/prj.conf:2 asked y, not applied; depends on SHELL && NET NOT met",
                    "CONFIG_STACK=1024  default",
                    "CONFIG_GPIO=y  fw:app/demo_defconfig:1"]
    assert query.config(a, ["SHELL", "FANCY", "STACK", "GPIO"]).split("\n\n", 1)[1] == "\n".join(body) + "\n"
    listed = query.config(a, ["G*", "S*", "SHELL", "SHEL"]).split("\n\n", 1)[1].splitlines()
    assert listed[:3] == ["CONFIG_GPIO=y  fw:app/demo_defconfig:1", "CONFIG_SHELL=y  fw:app/prj.conf:1",
                          "CONFIG_STACK=1024  default"]                     # a name already listed is not repeated
    assert listed[3] == "CONFIG_SHEL: not a symbol of this build (nearest CONFIG_SHELL)"
    assert "src: none" in query.config(a, [])


def test_config_list_without_kconfig_json_says_per_line_what_it_could_not_give(atlas_dir):
    out = query.config(query.Atlas(atlas_dir), "SHELL,FANCY,STACK")
    body = out.split("\n\n", 1)[1].splitlines()
    assert body[0].startswith("(core@000000000003 has no kconfig.json")
    assert body[1] == "CONFIG_SHELL=y  fw:app/prj.conf:1"
    assert body[2] == ("CONFIG_FANCY=n  fw:app/prj.conf:2 asked y, not applied [not given: depends on]")
    assert body[3] == "CONFIG_STACK=1024  no fragment [not given: select/default]"


def test_config_list_pages_to_the_uncut_answer(atlas_dir, monkeypatch):
    from paging_synth import assert_exact

    _kconfig_json(atlas_dir / "atlases" / "core@000000000003")
    a = query.Atlas(atlas_dir)
    assert_exact(monkeypatch, "config", lambda o: query.config(a, ["SHELL", "FANCY", "STACK", "GPIO"], offset=o))


# ---- two builds that differ in exactly three symbols and one devicetree property

def _second_build(atlas_dir: Path, tmp_path: Path, monkeypatch, zephyr_matches: bool = True) -> Path:
    """build/bringup beside the atlas: the pinned build's .config with STACK 2048 and LOG on (bringup.conf),
    which selects NEWSYM, and FANCY dropped from .config (still n); its devicetree differs only in conv0's
    `slew`, and its node ordinals moved (not a difference)."""
    from embarch_atlas import buildq

    b = tmp_path / "fw" / "build" / "bringup"
    zb = tmp_path / "zb"
    zb.mkdir(exist_ok=True)
    app = tmp_path / "fw" / "app"
    write_text(app / "bringup.conf", "CONFIG_LOG=y\nCONFIG_STACK=2048\n")
    write_text(b / "build.ninja", f"  COMMAND = /usr/bin/cmake -E env ZEPHYR_BASE={zb} /py "
                                  f"{zb}/scripts/kconfig/menuconfig.py {zb}/Kconfig\n")
    write_text(b / "zephyr" / ".config", "CONFIG_GPIO=y\nCONFIG_SHELL=y\nCONFIG_LOG=y\nCONFIG_NEWSYM=y\nCONFIG_STACK=2048\n")
    write_text(b / "build_info.yml", BUILD_INFO.format(d=app).replace(f"     - '{app}/prj.conf'\n",
                                                                       f"     - '{app}/prj.conf'\n     - '{app}/bringup.conf'\n", 1))
    write_text(b / "zephyr" / "include" / "generated" / "zephyr" / "version.h", "#define BUILD_VERSION v9.9.0\n")
    (b / "zephyr" / "edt.pickle").write_bytes(b"not really a pickle")
    pinned_dt = read_json(atlas_dir / "atlases" / "core@000000000003" / "dt.json")
    dt = json.loads(json.dumps(pinned_dt))
    dt["nodes"]["/soc/i2c@100/conv@74"]["props"]["slew"] = {"loc": "fw:app/bringup.overlay:3", "val": 7}
    dt["nodes"]["/soc/i2c@100/conv@74"]["ordinal"] = 9
    dt["instantiated_ordinals"] = [9]
    kc = {"zephyr": "v9.9.0", "symbols": {
        "SHELL": {"t": "bool", "v": "y", "asg": [["fw:app/prj.conf", 1, "y"]]},
        "GPIO": {"t": "bool", "v": "y", "asg": [["fw:app/demo_defconfig", 1, "y"]]},
        "FANCY": {"t": "bool", "v": "n", "dep": "SHELL && NET", "dv": "n", "asg": [["fw:app/prj.conf", 2, "y"]]},
        "LOG": {"t": "bool", "v": "y", "asg": [["fw:app/prj.conf", 3, "n"], ["fw:app/bringup.conf", 1, "y"]]},
        "NEWSYM": {"t": "bool", "v": "y", "sel": [["LOG", "y"]]},
        "STACK": {"t": "int", "v": "2048", "asg": [["fw:app/bringup.conf", 2, "2048"]],
                  "def": [["1024", "y"]]}}}
    if zephyr_matches:
        monkeypatch.setattr(KC, "workspace_mismatch", lambda z: None)
    else:
        monkeypatch.setattr(KC, "workspace_mismatch", lambda z: "the build used Zephyr v9.9.0; zb is at v9.9.1 now")
    monkeypatch.setattr(KC, "extract", lambda z, bases: kc)
    monkeypatch.setattr(buildq, "_dt_of_build", lambda *a: dt)
    return b


def test_config_and_dt_node_vs_list_exactly_the_differences(atlas_dir, tmp_path, monkeypatch):
    _kconfig_json(atlas_dir / "atlases" / "core@000000000003")
    b = _second_build(atlas_dir, tmp_path, monkeypatch)
    a = query.Atlas(atlas_dir)
    out = query.config(a, build="build/bringup", vs="pinned")
    head, body = out.split("\n\n", 1)
    assert head.startswith(f"src: build {b} (app ?, zephyr v9.9.0;") and " kconfig vs atlas core@000000000003 kconfig" in head
    assert head.endswith("query='*'")
    lines = body.splitlines()
    assert lines[0] == "3 of 6 symbols differ (this build | vs):"
    assert lines[1:] == [
        "CONFIG_LOG: y (fw:app/bringup.conf:1) | vs n (fw:app/prj.conf:3)",
        "CONFIG_NEWSYM: y (selected by CONFIG_LOG) | vs absent (not a symbol of that build)",
        "CONFIG_STACK: 2048 (fw:app/bringup.conf:2) | vs 1024 (default)"]
    # named symbols and patterns narrow it; an unchanged one is not listed
    narrowed = query.config(a, "SHELL,ST*", build="build/bringup", vs="pinned").split("\n\n", 1)[1].splitlines()
    assert narrowed == ["1 of 2 symbols differ (this build | vs):",
                        "CONFIG_STACK: 2048 (fw:app/bringup.conf:2) | vs 1024 (default)"]
    assert "no difference in the 1 symbols compared" in query.config(a, "GPIO", build="build/bringup", vs="pinned")
    assert "no difference in the 5 symbols compared" in query.config(a, vs="pinned")

    dt = query.dt_node(a, build="build/bringup", vs="pinned")
    head, body = dt.split("\n\n", 1)
    assert " devicetree vs atlas core@000000000003 devicetree" in head
    assert body.splitlines() == ["1 of 2 nodes differ (this build | vs):", "/soc/i2c@100/conv@74",
                                 "  slew = 7 (fw:app/bringup.overlay:3) | vs 5 (binding default)"]
    assert query.dt_node(a, "conv0", build="build/bringup", vs="pinned").split("\n\n", 1)[1] == body.replace(
        "1 of 2 nodes", "1 of 1 nodes")
    assert "no difference in the 1 nodes compared" in query.dt_node(a, "i2c1", build="build/bringup", vs="pinned")
    assert "in either build" in query.dt_node(a, "nope", build="build/bringup", vs="pinned")
    # the named build's extraction was cached in the atlas dir, never in the build
    assert list((atlas_dir / "cache" / "kconfig").glob("*.json")) and list((atlas_dir / "cache" / "dt").glob("*.json"))


def test_vs_keeps_the_zephyr_guard_and_says_per_line_what_it_could_not_give(atlas_dir, tmp_path, monkeypatch):
    _kconfig_json(atlas_dir / "atlases" / "core@000000000003")
    _second_build(atlas_dir, tmp_path, monkeypatch, zephyr_matches=False)
    a = query.Atlas(atlas_dir)
    lines = query.config(a, build="build/bringup", vs="pinned").split("\n\n", 1)[1].splitlines()
    assert lines[0] == "3 of 6 symbols differ (this build | vs):"
    assert lines[1].startswith("(this build: the build used Zephyr v9.9.0; zb is at v9.9.1 now")
    assert lines[2:] == [
        "CONFIG_LOG: y (fw:app/bringup.conf:1) | vs n (fw:app/prj.conf:3)",
        "CONFIG_NEWSYM: y (no fragment [not given: select/default]) | vs absent (not a symbol of that build)",
        "CONFIG_STACK: 2048 (fw:app/bringup.conf:2) | vs 1024 (default)"]
    dt = query.dt_node(a, build="build/bringup", vs="pinned")
    assert "no devicetree comparison: this build: the build used Zephyr v9.9.0" in dt and "vs:" not in dt.split("\n\n", 1)[1]
    assert not (atlas_dir / "cache" / "kconfig").exists()


def test_config_and_dt_node_vs_page_to_the_uncut_answer(atlas_dir, tmp_path, monkeypatch):
    from paging_synth import assert_exact

    _kconfig_json(atlas_dir / "atlases" / "core@000000000003")
    _second_build(atlas_dir, tmp_path, monkeypatch)
    a = query.Atlas(atlas_dir)
    assert_exact(monkeypatch, "config", lambda o: query.config(a, build="build/bringup", vs="pinned", offset=o))
    assert_exact(monkeypatch, "dt_node", lambda o: query.dt_node(a, build="build/bringup", vs="pinned", offset=o))


def test_mcp_and_q_verb_take_the_list_and_vs(atlas_dir, tmp_path, monkeypatch, capsys):
    import asyncio

    from embarch_atlas import cli

    _kconfig_json(atlas_dir / "atlases" / "core@000000000003")
    _second_build(atlas_dir, tmp_path, monkeypatch)
    want = query.config(query.Atlas(atlas_dir), ["SHELL", "FANCY"])
    assert cli.main(["--atlas-dir", str(atlas_dir), "q", "config", "SHELL,FANCY"]) == 0
    assert capsys.readouterr().out == want
    assert cli.main(["--atlas-dir", str(atlas_dir), "q", "config", "SHELL", "FANCY"]) == 0
    assert capsys.readouterr().out == want
    assert cli.main(["--atlas-dir", str(atlas_dir), "q", "dt-node", "--build", "build/bringup", "--vs", "pinned"]) == 0
    assert "slew = 7" in capsys.readouterr().out
    pytest.importorskip("mcp")
    from embarch_atlas.mcp_server import build_server

    server = build_server(atlas_dir, live=False)

    def call(tool, args):
        res = asyncio.run(server.call_tool(tool, args))
        return json.dumps(res.model_dump() if hasattr(res, "model_dump") else res, default=str)

    assert "CONFIG_FANCY=n  fw:app/prj.conf:2 asked y" in call("config", {"symbols": ["SHELL", "FANCY"]})
    assert "CONFIG_FANCY=n  fw:app/prj.conf:2 asked y" in call("config", {"symbol": "SHELL", "symbols": ["FANCY"]})
    assert "why n: the fragment asks for it" in call("config", {"symbol": "FANCY"})
    assert "CONFIG_STACK: 2048" in call("config", {"build": "build/bringup", "vs": "pinned"})
    assert "slew = 7" in call("dt_node", {"node": "conv0", "build": "build/bringup", "vs": "pinned"})

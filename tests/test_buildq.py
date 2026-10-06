"""config and dt_node over synthetic builds (invented symbols, nodes and files)."""
from pathlib import Path

import pytest

from embarch_atlas import kconfig as KC, query
from embarch_atlas.common import write_json, write_text

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

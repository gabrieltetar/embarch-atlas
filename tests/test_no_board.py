"""A target that states it has no Zephyr board yet: build skips the west build, join and the lookups say so."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_join import make_project, run_join  # noqa: E402

from embarch_atlas import build, common, project, query, stale  # noqa: E402
from embarch_atlas.common import read_json  # noqa: E402

TOML = """\
[project]
fw = "{fw}"
{west}

[mcu]
part = "MCU0"

[[targets]]
name = "demo"
schematic_boards = ["M", "S"]
{target}
"""


WEST = 'west_topdir = "/nonexistent-west"'


def write_toml(ad: Path, target_lines: str, west: str = "") -> None:
    (ad / "atlas.toml").write_text(TOML.format(fw=ad.parents[1], west=west, target=target_lines), encoding="utf-8")


@pytest.fixture()
def nb(tmp_path: Path):
    """The synthetic product's atlas dir: netlists and a distillation, no atlas, a target with no board."""
    ad, _ = make_project(tmp_path)
    shutil.rmtree(ad / "atlases")
    write_toml(ad, "no_board = true")
    return ad


def build_nb(ad: Path, **flags) -> Path:
    cfg = build.target_config(ad, "demo", SimpleNamespace(**flags) if flags else None)
    return build.run_build(ad, cfg, log=lambda *_: None)


def test_build_with_no_board_writes_a_stamp_that_says_so(nb):
    out = build_nb(nb)
    st = read_json(out / "atlas.json")
    assert st["board"] is None and st["id"].startswith("demo@") and len(st["commit"]) == 40
    assert st["build"]["built"] is False and "no Zephyr board" in st["build"]["reason"]
    assert common.no_build(st)
    assert not (out / "build").exists() and not (nb / ".work").exists()     # no worktree, no west build


def test_a_missing_board_still_fails_as_before(nb):
    write_toml(nb, "", west=WEST)
    with pytest.raises(build.BuildError, match=r"board unknown: set it in atlas.toml or pass --board"):
        build.run_build(nb, build.target_config(nb, "demo"), log=lambda *_: None)
    write_toml(nb, 'no_board = false\napp = "app"', west=WEST)
    with pytest.raises(build.BuildError, match="board unknown"):
        build.run_build(nb, build.target_config(nb, "demo"), log=lambda *_: None)


def test_no_board_and_a_board_is_a_conflict(nb):
    write_toml(nb, 'no_board = true\nboard = "demo_board"')
    with pytest.raises(build.BuildError, match="no_board and names board demo_board"):
        build.run_build(nb, build.target_config(nb, "demo"), log=lambda *_: None)


def test_the_flag_states_it_too(nb):
    write_toml(nb, "")
    assert build.target_config(nb, "demo", SimpleNamespace(no_board=True))["no_board"] is True
    assert build_nb(nb, no_board=True).name.startswith("demo@")


def test_join_runs_what_needs_no_build_and_writes_no_dt(nb):
    out = build_nb(nb)
    run_join(nb, out)
    for f in ("pins.json", "pins.md", "pin_slice.txt", "checks.md", "mismatches.json", "MAP.md", "graph.json"):
        assert (out / f).is_file(), f
    for f in ("dt.json", "in_use.json", "kconfig.json", "codecheck.json", "limits.json"):
        assert not (out / f).exists(), f
    pins = read_json(out / "pins.json")
    assert pins["part_docs"] and pins["counts"]["package_pins"] > 0 and pins["counts"]["dt_pins"] == 0
    rep = read_json(out / "mismatches.json")
    not_run = {n["check"]: n["why"] for n in rep["not_run"]}
    from embarch_atlas import mismatch
    for name, _ in mismatch.NEEDS_BUILD:
        if not name.startswith("second look"):
            assert "no Zephyr board yet" in not_run[name]
    assert "net name agrees across a board connector" in {c["check"] for c in rep["checks"]}
    assert read_json(out / "graph.json")["code_note"].startswith("no build")
    assert "no Zephyr board yet" in (out / "checks.md").read_text()


def test_map_and_status_name_each_absent_output(nb, capsys):
    out = build_nb(nb)
    run_join(nb, out)
    text = (out / "MAP.md").read_text()
    head = text.split("\n", 3)[2]
    assert "no Zephyr board yet" in head and "Zephyr board `" not in head
    assert "## No build: this target has no Zephyr board yet" in text
    for name, _ in common.NO_BUILD_ABSENT + common.NO_BUILD_DEGRADED:
        assert name in text, name
    assert (nb / "MAP.md").read_text() == text                       # it is the current atlas: the session's map
    args = SimpleNamespace(atlas_dir=str(nb))
    assert project._cmd_status(args) == 0
    shown = capsys.readouterr().out
    assert "board=none (no build)" in shown
    for name, _ in common.NO_BUILD_ABSENT + common.NO_BUILD_DEGRADED:
        assert name in shown, name


def test_build_dependent_queries_say_the_target_has_no_board(nb):
    out = build_nb(nb)
    run_join(nb, out)
    a = query.Atlas(nb)
    assert a.atlas == out
    for text in (query.config(a, "CONFIG_X"), query.dt_node(a, "i2c0"), query.footprint(a), query.stacks(a),
                 query.footprint(a, vs="pinned"), query.code(a, "main"), query.states(a, "app")):
        assert text.startswith("src: none") and "no Zephyr board yet" in text
        assert "Traceback" not in text and "No such file" not in text
    ok = query.pin_trace(a, "PA0")                                    # what needs no build still answers
    assert "no Zephyr board yet" not in ok
    assert "no Zephyr board yet" in query.mismatch_report(a)


def test_register_says_firmware_references_are_absent(nb):
    out = build_nb(nb)
    run_join(nb, out)
    tail = query._reg_tail(query.Atlas(nb), [], ["x"])
    assert "## Firmware references" in "\n".join(tail) and "no Zephyr board yet" in "\n".join(tail)


def test_standalone_build_verbs_name_the_reason(nb):
    from embarch_atlas import codecheck, graph, limits, rm
    out = build_nb(nb)
    run_join(nb, out)
    for fn in (codecheck.run, limits.run):
        with pytest.raises(SystemExit, match="no Zephyr board yet"):
            fn(nb, out)
    with pytest.raises(SystemExit, match="no Zephyr board yet"):
        rm.load_in_use(out)
    assert graph.cmd_graph(SimpleNamespace(atlas_dir=str(nb), atlas=str(out))) == 0


def test_a_board_supersedes_the_no_board_atlas_and_staleness_sees_it(nb):
    out = build_nb(nb)
    run_join(nb, out)
    assert stale.reasons(nb, out)[0] == []
    write_toml(nb, 'board = "demo_board"\napp = "app"', west=WEST)
    why = stale.reasons(nb, out)[0]
    assert any("has no build" in w and "demo_board" in w for w in why)
    cfg = build.target_config(nb, "demo")
    with pytest.raises((build.BuildError, OSError)):                   # no west here: the normal path was taken
        build.run_build(nb, cfg, log=lambda *_: None)
    assert not out.exists()                                            # the no-board atlas was superseded


def test_an_existing_board_atlas_is_not_replaced_by_no_board(tmp_path):
    ad, atlas = make_project(tmp_path)
    write_toml(ad, "no_board = true")
    logs: list[str] = []
    got = build.run_build(ad, build.target_config(ad, "demo"), log=logs.append)
    assert got == atlas and "built with board demo_board" in logs[0]
    assert read_json(atlas / "atlas.json")["board"] == "demo_board"


def test_update_runs_for_a_target_with_no_board(nb):
    from embarch_atlas import update
    lines: list[str] = []
    assert update.run(nb, "demo", log=lines.append) == 0
    atlas = next((nb / "atlases").glob("demo@*"))
    assert common.no_build(read_json(atlas / "atlas.json")) and (atlas / "MAP.md").is_file()
    assert not any("reference manual" in x for x in lines)

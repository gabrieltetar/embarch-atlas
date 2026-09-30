"""mapgen: MAP.md is small, byte-stable, cites its sources; current.json and the root MAP follow HEAD."""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_join import git, make_project, run_join  # noqa: E402

from embarch_atlas import mapgen  # noqa: E402
from embarch_atlas.common import read_json, write_json  # noqa: E402


def test_map_contents_and_budget(tmp_path):
    ad, atlas = make_project(tmp_path)
    run_join(ad, atlas)
    text = (atlas / "MAP.md").read_text()
    aid = atlas.name
    assert text.startswith(f"# {aid}: hardware atlas for target `demo`")
    assert "Protocol: MAP -> CARD -> section -> page" in text
    assert f"atlases/{aid}/pins.md" in text and f"atlases/{aid}/checks.md" in text
    assert "| SNS701QR | M:U3, S:U1 | `sns701@a` | (no card yet) |" in text
    assert "M:J1 <-> S:J1: mirror" in text
    assert "In use (instantiated or touched by code): GPIOA, I2C1, SYSCFG" in text
    assert "DT okay, no driver in this build: ADC" in text
    assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:", text)       # no timestamps in the body
    assert mapgen.est_tokens(text) <= mapgen.BUDGET


def test_map_is_byte_stable(tmp_path):
    ad, atlas = make_project(tmp_path)
    run_join(ad, atlas)
    one = (atlas / "MAP.md").read_bytes()
    # a new stamp time must not change the MAP
    st = read_json(atlas / "atlas.json")
    st["created"] = "2030-01-01T00:00:00Z"
    write_json(atlas / "atlas.json", st)
    run_join(ad, atlas)
    assert (atlas / "MAP.md").read_bytes() == one


def test_card_path_when_present(tmp_path):
    ad, atlas = make_project(tmp_path)
    (ad / "docs" / "sns701@a" / "card.md").write_text("# card\n")
    run_join(ad, atlas)
    assert "`sns701@a` | docs/sns701@a/card.md |" in (atlas / "MAP.md").read_text()


def test_current_follows_head(tmp_path):
    ad, atlas = make_project(tmp_path)
    run_join(ad, atlas)
    cur = read_json(ad / "current.json")
    assert any(m["atlas"] == atlas.name for m in cur["matches"])
    assert (ad / "MAP.md").read_bytes() == (atlas / "MAP.md").read_bytes()
    # HEAD moves on: the atlas is no longer current, and the root MAP is left for status to mark stale
    fw = Path(read_json(atlas / "atlas.json")["build"]["fw"])
    (fw / "x.txt").write_text("x")
    git(fw, "add", "x.txt")
    git(fw, "commit", "-q", "-m", "next")
    assert mapgen.update_current(ad, atlas, read_json(atlas / "atlas.json")) is False


def test_compress_runs():
    assert mapgen._compress(["GPIOA", "GPIOB", "GPIOC", "I2C1", "I2C2", "RCC"]) == "GPIOA-C, I2C1, I2C2, RCC"
    assert mapgen._compress(["GPIOA", "GPIOC"]) == "GPIOA, GPIOC"

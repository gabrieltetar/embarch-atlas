"""Staleness on every answer, current.json that follows HEAD, and the MAP's reference-manual line."""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import test_rm  # noqa: E402
from test_join import git, make_project, run_join  # noqa: E402

from embarch_atlas import query, stale, update  # noqa: E402
from embarch_atlas.common import read_json, write_json  # noqa: E402


def _commit(fw: Path, name: str) -> str:
    (fw / name).write_text(name)
    git(fw, "add", name)
    git(fw, "commit", "-q", "-m", name)
    return git(fw, "rev-parse", "HEAD").strip()


def _fw(atlas: Path) -> Path:
    return Path(read_json(atlas / "atlas.json")["build"]["fw"])


def _first(text: str) -> str:
    return text.split("\n", 1)[0]


def test_every_lookup_says_when_head_moved_and_stops_when_rebuilt(tmp_path):
    ad, atlas = make_project(tmp_path)
    run_join(ad, atlas)
    a = query.Atlas(ad)
    assert "stale=" not in _first(query.mismatch_report(a))
    fw = _fw(atlas)
    new = _commit(fw, "next.txt")
    a = query.Atlas(ad)
    lookups = [query.mismatch_report(a), query.pin_trace(a, "PB9"), query.find_fact(a, "anything"),
               query.part_card(a, "nothing-like-this")]
    for out in lookups:
        assert out.startswith("src:")
        assert f"stale=commit differs from HEAD (HEAD {new[:12]})" in _first(out), out
    # a rebuild: a newer atlas at the new commit
    aid = f"demo@{new[:12]}"
    nat = ad / "atlases" / aid
    shutil.copytree(atlas, nat)
    write_json(nat / "atlas.json", {**read_json(atlas / "atlas.json"), "id": aid, "commit": new})
    a = query.Atlas(ad)
    assert a.atlas.name == aid
    assert "stale=" not in _first(query.mismatch_report(a))


def test_current_json_for_another_commit_is_no_match(tmp_path):
    ad, atlas = make_project(tmp_path)
    run_join(ad, atlas)
    assert query.current_atlas(ad) == atlas
    old = ad / "atlases" / "demo@000000000001"
    write_json(old / "atlas.json", {"id": old.name, "commit": "0" * 40})
    # current.json (written by the join) names `atlas`; HEAD moves, so it must not be trusted
    _commit(_fw(atlas), "next.txt")
    cur = read_json(ad / "current.json")
    assert any(m["atlas"] == atlas.name for m in cur["matches"])         # still says it matches: stale file
    import os
    os.utime(old / "atlas.json", (time.time() + 100, time.time() + 100))
    assert query.current_atlas(ad) == old                                 # fell through to the newest


def test_cost_with_an_unchanged_head_is_a_few_stats(tmp_path):
    ad, atlas = make_project(tmp_path)
    run_join(ad, atlas)
    _commit(_fw(atlas), "next.txt")
    a = query.Atlas(ad)
    assert stale.mark(ad, a.atlas)
    calls = []
    real = stale.subprocess.run
    stale.subprocess.run = lambda *x, **k: (calls.append(x), real(*x, **k))[1]
    try:
        n = 200
        t = time.perf_counter()
        for _ in range(n):
            stale.mark(ad, a.atlas)
            query.current_atlas(ad)
        per = (time.perf_counter() - t) / n * 1000
    finally:
        stale.subprocess.run = real
    assert not calls                     # no git call while HEAD stands still
    assert per < 5, f"{per:.3f} ms per call"
    # HEAD moves: one rev-parse, then the new sha
    new = _commit(_fw(atlas), "again.txt")
    assert f"(HEAD {new[:12]})" in stale.mark(ad, a.atlas)


def _with_rm(tmp_path):
    ad, atlas = make_project(tmp_path)
    (tmp_path / "rmsrc").mkdir()
    rm_root = test_rm.demo_atlas(tmp_path / "rmsrc")
    shutil.copytree(rm_root / "docs" / "demo-rm@1", ad / "docs" / "demo-rm@1")
    write_json(ad / "docs" / "demo-rm@1" / "doc.json", {"doc_id": "demo-rm@1", "kind": "reference-manual", "pages": 9})
    shutil.copytree(rm_root / "docs" / "demo-svd", ad / "docs" / "demo-svd")
    iu = read_json(atlas / "in_use.json")          # the join's own peripherals, plus one the RM covers
    iu["peripherals"]["UART1"] = {"dt_okay": True, "instantiated": True, "touched_by_code": True,
                                  "compatible": "demo,uart", "node": "/soc/uart@1"}
    write_json(atlas / "in_use.json", iu)
    return ad, atlas


def _rm_line(atlas: Path) -> str:
    return next(ln for ln in (atlas / "MAP.md").read_text().splitlines() if ln.startswith("- RM chapters in use"))


def test_map_lists_rm_chapters_after_a_hand_run_join(tmp_path):
    ad, atlas = _with_rm(tmp_path)
    # a worklist left for another atlas, as a previous build + update leaves it
    write_json(ad / "docs" / "demo-rm@1" / "worklist.json", {"atlas": str(ad / "atlases" / "other@1"), "chapters": []})
    run_join(ad, atlas)                          # no rm-sections, no update
    assert "not selected yet" not in _rm_line(atlas), _rm_line(atlas)
    assert "Universal widget" in _rm_line(atlas)
    assert read_json(ad / "docs" / "demo-rm@1" / "worklist.json")["atlas"] == str(atlas.resolve())


def test_map_lists_rm_chapters_after_update(tmp_path, monkeypatch):
    from embarch_atlas import build as B
    ad, atlas = _with_rm(tmp_path)
    monkeypatch.setattr(B, "target_config", lambda ad_, target: {})
    monkeypatch.setattr(B, "run_build", lambda ad_, cfg, commit, log=print: atlas)
    monkeypatch.setattr(update, "second_look_diff", lambda ad_, a_, prev: {
        "frozen": 0, "carried": 0, "queued": 0, "diff": 0, "backlog": 0, "batches": 0, "prev_queue": False})
    assert update.run(ad, "demo", "HEAD", log=lambda *_: None) == 0
    assert "Universal widget" in _rm_line(atlas) and "not selected yet" not in _rm_line(atlas)

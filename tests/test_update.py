"""`update`: the diff between two atlases' second-look queues, and which atlas came before."""
from pathlib import Path

from embarch_atlas import secondlook as S, update
from embarch_atlas.common import read_json, write_json, write_jsonl


def _atlas(ad: Path, name: str, created: str) -> Path:
    a = ad / "atlases" / name
    write_json(a / "atlas.json", {"id": name, "created": created})
    return a


def test_previous_atlas_is_the_newest_other_one(tmp_path):
    old = _atlas(tmp_path, "core@000000000001", "2026-10-01T00:00:00Z")
    mid = _atlas(tmp_path, "core@000000000002", "2026-10-03T00:00:00Z")
    new = _atlas(tmp_path, "core@000000000003", "2026-10-05T00:00:00Z")
    _atlas(tmp_path, "other@000000000009", "2026-10-09T00:00:00Z")
    assert update.previous_atlas(tmp_path, "core") == new
    assert update.previous_atlas(tmp_path, "core", exclude=new) == mid
    assert old.is_dir()


def test_only_what_the_previous_atlas_did_not_owe_is_batched(tmp_path, monkeypatch):
    prev = _atlas(tmp_path, "core@000000000001", "2026-10-01T00:00:00Z")
    new = _atlas(tmp_path, "core@000000000002", "2026-10-02T00:00:00Z")
    write_jsonl(prev / "second_look_queue.jsonl", [{"id": "a"}, {"id": "b"}])
    item = lambda i: {"id": i, "doc": "demo@1", "p": [1], "why": ["limit"], "ask": "x", "key": i, "priority": 1,
                      "kind": "limit", "pdf": None, "src": "table"}
    queue = [item("a"), item("c"), item("d")]
    monkeypatch.setattr(S, "build_queue", lambda ad, atlas: (queue, {"frozen": 5, "frozen_keys": []}))
    monkeypatch.setattr(S, "carry_frozen", lambda ad, atlas, keys: 0)
    out = update.second_look_diff(tmp_path, new, prev)
    assert (out["diff"], out["backlog"], out["frozen"]) == (2, 1, 5)
    batched = {it["id"] for b in read_json(new / "second_look_batches.json") for it in b["items"]}
    assert batched == {"c", "d"}


def test_carried_verdicts_are_joined_again_so_the_report_counts_them(tmp_path, monkeypatch):
    from embarch_atlas import build as B, join as J, update as U
    ad = tmp_path / "atlas"
    new = ad / "atlases" / "core@b"
    new.mkdir(parents=True)
    calls = []
    monkeypatch.setattr(B, "target_config", lambda ad_, target: {})
    monkeypatch.setattr(B, "run_build", lambda ad_, cfg, commit, log=print: new)
    monkeypatch.setattr(U, "previous_atlas", lambda *a, **k: None)
    monkeypatch.setattr(J, "run", lambda ad_, atlas, log=print: calls.append("join"))
    monkeypatch.setattr(U, "rm_todo", lambda ad_, atlas: None)
    monkeypatch.setattr(U, "second_look_diff", lambda ad_, atlas, prev: calls.append("carry") or
                        {"frozen": 3, "carried": 3, "queued": 0, "diff": 0, "backlog": 0, "batches": 0, "prev_queue": False})
    assert U.run(ad, "core", "HEAD", log=lambda *_: None) == 0
    assert calls == ["join", "carry", "join"]

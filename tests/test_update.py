"""`update`: when it stops for a translation, the diff between two atlases' second-look queues, and which
atlas came before."""
from pathlib import Path

import datasheet_synth as ds
from embarch_atlas import freeze, secondlook as S, update
from embarch_atlas.common import read_json, sha256_file, write_json, write_jsonl


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


def _manifest(ad: Path, *docs: dict) -> None:
    write_json(ad / "inventory" / "manifest.json", {"documents": list(docs)})


def test_update_stops_only_while_a_translation_is_behind_its_file(tmp_path):
    ad, docdir = ds.make_doc(tmp_path)
    meta = read_json(docdir / "doc.json")
    entry = {"file": "SYN1234-2b.pdf", "sha256": meta["sha256"], "doc_id": ds.DOC_ID, "kind": "datasheet"}
    _manifest(ad, entry)
    freeze.freeze(ad, ds.DOC_ID, model="m")
    assert update.stale_translations(ad) == []
    # a new copy of the file: inventory saw it, doc prep has not run
    _manifest(ad, dict(entry, sha256="1" * 64))
    assert update.stale_translations(ad) == [
        f"docs/{ds.DOC_ID} was prepped from an older SYN1234-2b.pdf: embarch-atlas doc prep, then doc thaw-check"]
    # doc prep ran and p3's text changed: the frozen units on p3 go back to the LLM
    write_json(docdir / "doc.json", dict(meta, sha256="1" * 64))
    page = docdir / "pages" / "p003.txt"
    page.write_text(page.read_text() + "A sentence added in the new revision.\n")
    (why,) = update.stale_translations(ad)
    assert why.startswith(f"docs/{ds.DOC_ID}: ") and "rest on changed pages (p3)" in why
    # re-distilled and frozen again: nothing is behind, whatever the previous atlas was built from
    freeze.freeze(ad, ds.DOC_ID, model="m")
    assert update.stale_translations(ad) == []


def test_a_document_is_found_under_another_doc_id_and_an_undistilled_one_never_stops(tmp_path):
    ad, docdir = ds.make_doc(tmp_path)
    sha = read_json(docdir / "doc.json")["sha256"]
    spec = {"file": "spec-r1.pdf", "sha256": sha, "doc_id": "spec-r1@1", "kind": "other"}
    _manifest(ad, spec, {"file": "newpart.pdf", "sha256": "2" * 64, "doc_id": "newpart@a", "kind": "datasheet"},
              {"file": "notes.md", "sha256": "3" * 64, "doc_id": "notes@norev", "kind": "notes"})
    assert update.distillation_of(spec, update._distillations(ad)) == ds.DOC_ID     # prepped with --doc-id
    assert update.stale_translations(ad) == []


def test_a_board_netlist_from_another_pdf_stops_update(tmp_path):
    ad = tmp_path / "atlas"
    hw = tmp_path / "fw" / "hw"
    hw.mkdir(parents=True)
    (hw / "main.pdf").write_bytes(b"%PDF rev A")
    ad.mkdir()
    (ad / "atlas.toml").write_text(f'[project]\nfw = "{tmp_path / "fw"}"\nsources = "hw"\n\n'
                                   '[[boards]]\nlabel = "M"\npdf = "main.pdf"\n')
    write_json(ad / "boards" / "M" / "netlist.json", {"source": {"file": "main.pdf",
                                                                 "sha256": sha256_file(hw / "main.pdf")}})
    assert update.stale_translations(ad) == []
    (hw / "main.pdf").write_bytes(b"%PDF rev B")
    assert update.stale_translations(ad) == [
        "board M: netlist.json is from another PDF than main.pdf: embarch-atlas schematic main.pdf --board M"]


def test_changes_since_reads_the_previous_join_file_list(tmp_path):
    ad, docdir = ds.make_doc(tmp_path)
    sha = read_json(docdir / "doc.json")["sha256"]
    prev = _atlas(ad, "core@000000000001", "2026-10-01T00:00:00Z")
    write_json(prev / "inputs.json", {"source_files": {"SYN1234-2b.pdf": "0" * 64, "notes.md": "3" * 64,
                                                       "gone.pdf": "4" * 64}})
    _manifest(ad, {"file": "SYN1234-2b.pdf", "sha256": sha, "doc_id": ds.DOC_ID, "kind": "datasheet"},
              {"file": "notes.md", "sha256": "3" * 64, "doc_id": "notes@norev", "kind": "notes"},
              {"file": "spec.pdf", "sha256": "5" * 64, "doc_id": "spec@1", "kind": "other"})
    rows, basis = update.changes_since(ad, prev)
    assert basis == "core@000000000001's file list"
    assert [update._change_line(r) for r in rows] == [
        "added spec.pdf (other, not distilled)", f"changed SYN1234-2b.pdf (docs/{ds.DOC_ID})", "removed gone.pdf"]
    write_json(prev / "inputs.json", {"source_files": {"SYN1234-2b.pdf": sha, "notes.md": "0" * 64}})
    rows, _ = update.changes_since(ad, prev)
    assert [update._change_line(r) for r in rows] == ["added spec.pdf (other, not distilled)",
                                                      "changed notes.md (notes, read as is)"]
    assert [r["file"] for r in rows if update._undistilled(r)] == ["spec.pdf"]


def test_update_runs_once_the_changed_document_is_translated(tmp_path, monkeypatch):
    """The dead end: the previous atlas's stamp names the old manifest, and that never changes."""
    from embarch_atlas import build as B, join as J
    ad, docdir = ds.make_doc(tmp_path)
    prev = _atlas(ad, "core@000000000001", "2026-10-01T00:00:00Z")
    write_json(prev / "atlas.json", {"id": prev.name, "created": "2026-10-01T00:00:00Z", "sources": "old"})
    new = ad / "atlases" / "core@000000000002"
    _manifest(ad, {"file": "SYN1234-2b.pdf", "sha256": read_json(docdir / "doc.json")["sha256"],
                   "doc_id": ds.DOC_ID, "kind": "datasheet"})
    freeze.freeze(ad, ds.DOC_ID, model="m")
    monkeypatch.setattr(B, "target_config", lambda ad_, target: {})
    monkeypatch.setattr(B, "run_build", lambda ad_, cfg, commit, log=print: _atlas(ad, new.name, "2026-10-02T00:00:00Z"))
    monkeypatch.setattr(J, "run", lambda ad_, atlas, log=print: None)
    monkeypatch.setattr(update, "rm_todo", lambda ad_, atlas: None)
    monkeypatch.setattr(update, "second_look_diff", lambda ad_, atlas, prev: {
        "frozen": 0, "carried": 0, "queued": 0, "diff": 0, "backlog": 0, "batches": 0, "prev_queue": False})
    monkeypatch.setattr("embarch_atlas.diff.diff", lambda a, b: {})
    monkeypatch.setattr("embarch_atlas.diff.render", lambda d: "diff\n")
    out = []
    assert update.run(ad, "core", "HEAD", log=out.append) == 0
    assert (new / f"diff-from-{prev.name}.md").is_file()


def test_second_look_queue_diff_from_batches_what_update_would(tmp_path, monkeypatch, capsys):
    from embarch_atlas import cli
    prev = _atlas(tmp_path, "core@000000000001", "2026-10-01T00:00:00Z")
    new = _atlas(tmp_path, "core@000000000002", "2026-10-02T00:00:00Z")
    item = lambda i: {"id": i, "doc": "demo@1", "p": [1], "why": ["limit"], "ask": "x", "key": i, "priority": 1,
                      "kind": "limit", "pdf": None, "src": "table"}
    monkeypatch.setattr(S, "build_queue", lambda ad, atlas: ([item("a"), item("c"), item("d")], {
        "frozen": 0, "frozen_keys": [], "atlas": atlas.name, "used_records": 3, "needs_look": 3, "queued": 3,
        "by_reason": {}}))
    monkeypatch.setattr(S, "carry_frozen", lambda ad, atlas, keys: 0)
    args = ["--atlas-dir", str(tmp_path), "second-look", "queue", new.name, "--diff-from", prev.name]
    assert cli.main(args) == 2                                   # no queue in prev: nothing to tell the diff by
    write_jsonl(prev / "second_look_queue.jsonl", [{"id": "a"}, {"id": "b"}])
    assert cli.main(args) == 0
    batched = {it["id"] for b in read_json(new / "second_look_batches.json") for it in b["items"]}
    assert batched == {"c", "d"}
    assert "2 items new or changed since core@000000000001 (1 queued there already)" in capsys.readouterr().out


def test_a_declared_board_with_no_netlist_does_not_stop_update(tmp_path):
    """Stopping would block every firmware bump until a schematic that `update` cannot make is run;
    the atlas says it does not cover the board (a stale reason) instead. DESIGN.md "When `update` stops"."""
    ad = tmp_path / "atlas"
    hw = tmp_path / "fw" / "hw"
    hw.mkdir(parents=True)
    (hw / "b.pdf").write_bytes(b"%PDF b")
    ad.mkdir()
    (ad / "atlas.toml").write_text(f'[project]\nfw = "{tmp_path / "fw"}"\nsources = "hw"\n\n'
                                   '[[boards]]\nlabel = "B"\npdf = "b.pdf"\n')
    assert update.stale_translations(ad) == []


def test_a_changed_fact_that_keeps_its_id_is_batched_not_backlog(tmp_path, monkeypatch):
    prev = _atlas(tmp_path, "core@000000000001", "2026-10-01T00:00:00Z")
    new = _atlas(tmp_path, "core@000000000002", "2026-10-02T00:00:00Z")
    write_jsonl(prev / "second_look_queue.jsonl", [{"id": "same", "key": "k1"}, {"id": "moved", "key": "k1"}])
    item = lambda i, k: {"id": i, "doc": "demo@1", "p": [1], "why": ["limit"], "ask": "x", "key": k, "priority": 1,
                         "kind": "limit", "pdf": None, "src": "table"}
    queue = [item("same", "k1"), item("moved", "k2"), item("fresh", "k9")]
    monkeypatch.setattr(S, "build_queue", lambda ad, atlas: (queue, {"frozen": 0, "frozen_keys": []}))
    monkeypatch.setattr(S, "carry_frozen", lambda ad, atlas, keys: 0)
    out = update.second_look_diff(tmp_path, new, prev)
    assert (out["diff"], out["changed"], out["backlog"]) == (2, 1, 1)
    batched = {it["id"] for b in read_json(new / "second_look_batches.json") for it in b["items"]}
    assert batched == {"moved", "fresh"}
    # the same split through `second-look queue --diff-from`
    diff, changed = S.split_diff(queue, S.owed_by(prev))
    assert ({it["id"] for it in diff}, changed) == ({"moved", "fresh"}, 1)

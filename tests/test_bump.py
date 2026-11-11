"""Bumping on HEAD moves: the hooks, one bump per burst, the bump lock, the previous atlas answering until the
new one joined, a stopped bump's reason on the stale line and `src:`, a west workspace off its manifest, and the
second look batched from what answers returned. DESIGN.md "Bumping when HEAD moves"."""
from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_join import git, make_project  # noqa: E402
from test_no_board import write_toml  # noqa: E402
from test_query import atlas_dir  # noqa: E402,F401  (fixture)

from embarch_atlas import build, bump, project, query, secondlook as S, update  # noqa: E402
from embarch_atlas.common import read_json, write_json  # noqa: E402


@pytest.fixture()
def nb(tmp_path: Path) -> Path:
    """The synthetic product's atlas dir, no atlas yet, target `demo` with no Zephyr board (no west needed)."""
    ad, _ = make_project(tmp_path)
    shutil.rmtree(ad / "atlases")
    write_toml(ad, "no_board = true")
    return ad


def _wait_done(ad: Path, head: str, timeout_s: float = 120.0) -> dict:
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        s = bump.read_state(ad).get("demo") or {}
        if s.get("head") == head and s.get("result") != "running" and not (ad / "bump" / bump.WORKER_PID).exists():
            return s
        time.sleep(0.2)
    log = (ad / "bump" / bump.LOG)
    raise AssertionError("no bump finished; log:\n" + (log.read_text() if log.is_file() else "(none)"))


# ---------------------------------------------------------------- hooks

def test_install_writes_four_hooks_keeps_a_foreign_one_and_uninstall_restores_it(nb):
    fw = nb.parents[1]
    hooks = fw / ".git" / "hooks"
    foreign = "#!/bin/sh\necho lfs-like post-checkout \"$@\"\n"
    (hooks / "post-checkout").write_text(foreign)
    done = bump.install(nb, ["demo"], debounce_s=7)
    assert len(done) == 4
    for h in bump.HOOKS:
        text = (hooks / h).read_text()
        assert text.count(bump.BEGIN) == 1 and "hook run --debounce 7 demo" in text
        assert os.access(hooks / h, os.X_OK)
    pc = (hooks / "post-checkout").read_text()
    assert pc.startswith(foreign) and 'if [ "$3" != 0 ]' in pc       # a file checkout does not move HEAD
    assert bump.install(nb, ["demo"], debounce_s=7) == []             # idempotent: one block, never two
    assert bump.installed(nb) == list(bump.HOOKS)
    bump.uninstall(nb)
    assert (hooks / "post-checkout").read_text() == foreign
    assert not (hooks / "post-commit").exists() and bump.installed(nb) == []


def test_a_burst_of_head_moves_is_one_bump_of_the_last_head(nb):
    """Real git hooks: five commits in a row start one detached worker, which waits for HEAD to be quiet and
    bumps once, to the last commit."""
    fw = nb.parents[1]
    bump.install(nb, ["demo"], debounce_s=1.5)
    for i in range(5):
        git(fw, "commit", "-q", "--allow-empty", "-m", f"move {i}")
    head = git(fw, "rev-parse", "HEAD")
    burst_end = time.monotonic()
    st = _wait_done(nb, head)
    # hooks that fired before the first worker wrote its pid started more workers; let them all find `handled`
    time.sleep(max(0.0, burst_end + bump.HANDOFF_S + 1.0 - time.monotonic()))
    free = bump.wait_lock(nb, bump.WORKER_LOCK, 30)
    bump.release(free)
    log = (nb / "bump" / bump.LOG).read_text()
    assert st["result"] == "ok" and st["via"] == "hook", log
    assert log.count("bump start: demo") == 1, log
    assert sorted(p.name for p in (nb / "atlases").iterdir()) == [f"demo@{head[:12]}"]
    assert read_json(nb / "current.json")["matches"][0]["atlas"] == f"demo@{head[:12]}"
    assert (nb / "MAP.md").read_text().startswith(f"# demo@{head[:12]}")
    assert not list((nb / "atlases").glob("*/second_look_reading*"))   # the hook queues the second look, never runs it


def test_the_bump_lock_refuses_a_second_concurrent_bump(nb):
    held = bump.try_lock(nb, bump.UPDATE_LOCK)
    try:
        bump.write_state(nb, "demo", head="f" * 40, result="running", pid=os.getpid(), started="2026-11-10T00:00:00Z")
        lines: list[str] = []
        assert update.run(nb, "demo", log=lines.append) == 3
        assert "refused, one bump at a time" in lines[0] and f"pid {os.getpid()}" in lines[0]
        assert not (nb / "atlases").exists()
        worker = bump.try_lock(nb, bump.WORKER_LOCK)
        try:
            assert bump.run_worker(nb, ["demo"], debounce_s=0, handoff_s=0.1) == 3
        finally:
            bump.release(worker)
    finally:
        bump.release(held)
    assert update.run(nb, "demo", log=lambda *_: None) == 0            # free again


def test_the_previous_atlas_answers_until_the_new_one_has_joined(nb, monkeypatch):
    from embarch_atlas import join as J
    fw = nb.parents[1]
    assert update.run(nb, "demo", log=lambda *_: None) == 0
    old = next((nb / "atlases").glob("demo@*"))
    git(fw, "commit", "-q", "--allow-empty", "-m", "move")
    head = git(fw, "rev-parse", "HEAD")
    real_join, seen = J.run, {}

    def join_and_look(ad, atlas, log=print):
        if atlas.name.endswith(head[:12]) and not seen:            # built, not joined yet
            seen["current"] = query.current_atlas(ad)
            st = project.status(ad)
            seen["matches"] = st["current"]["matches"]
            seen["joining"] = [r["atlas"] for r in st["atlases"] if r.get("joining")]
            seen["map"] = (ad / "MAP.md").read_text().splitlines()[0]
        return real_join(ad, atlas, log=log)

    monkeypatch.setattr(J, "run", join_and_look)
    assert update.run(nb, "demo", log=lambda *_: None) == 0
    assert seen["current"] == old and seen["matches"] == [] and seen["joining"] == [f"demo@{head[:12]}"]
    assert seen["map"].startswith("> **Stale:**") and "a bump to HEAD is running" in seen["map"]
    assert query.current_atlas(nb).name == f"demo@{head[:12]}"
    assert (nb / "MAP.md").read_text().startswith(f"# demo@{head[:12]}")


def test_a_stopped_or_failed_bump_says_why_on_the_stale_line_and_src(nb, monkeypatch):
    fw = nb.parents[1]
    assert update.run(nb, "demo", log=lambda *_: None) == 0
    git(fw, "commit", "-q", "--allow-empty", "-m", "move")
    monkeypatch.setattr(update, "stale_translations",
                        lambda ad: ["docs/syn@1 was prepped from an older syn.pdf: embarch-atlas doc prep"])
    assert update.run(nb, "demo", log=lambda *_: None) == 2
    project.status(nb)
    first = (nb / "MAP.md").read_text().splitlines()[0]
    assert first.startswith("> **Stale:**") and "the bump to HEAD stopped" in first and "prepped from an older" in first
    src = query.part_card(query.Atlas(nb), "sns701").splitlines()[0]
    assert "stale=" in src and "the bump to HEAD stopped" in src

    monkeypatch.setattr(update, "stale_translations", lambda ad: [])

    def broken(ad, cfg, commit="HEAD", force=False, log=print):
        raise build.BuildError("west build failed (1); last lines of x.log:\nboom")
    monkeypatch.setattr(build, "run_build", broken)
    assert update.run(nb, "demo", log=lambda *_: None) == 1
    st = bump.read_state(nb)["demo"]
    assert st["result"] == "failed" and st["reason"].startswith("build failed: west build failed (1)")
    assert "the bump to HEAD failed" in query.part_card(query.Atlas(nb), "sns701").splitlines()[0]


# ---------------------------------------------------------------- the west workspace

FAKE_WEST = r'''#!{python}
import json, os, sys
from pathlib import Path
a = sys.argv[1:]
projects = json.loads(os.environ["FAKE_WEST_DRIFT"])
if a[0] != "list":
    sys.exit(2)
fmt = a[a.index("-f") + 1]
names = [x for i, x in enumerate(a[1:], 1) if x != "-f" and a[i - 1] != "-f"]
for p in projects:
    if names and p["name"] not in names:
        continue
    print(fmt.format(name=p["name"], path=p["path"], revision=p["revision"], sha=p.get("sha", "N/A"),
                     abspath=str(Path.cwd() / p["path"]), cloned="cloned" if p.get("cloned", True) else "not-cloned"))
'''


def _module(top: Path, name: str, commits: int) -> list[str]:
    repo = top / name
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    shas = []
    for i in range(commits):
        git(repo, "commit", "-q", "--allow-empty", "-m", f"c{i}")
        shas.append(git(repo, "rev-parse", "HEAD"))
    return shas


def test_a_west_workspace_off_the_manifest_stops_the_bump_before_building(tmp_path, monkeypatch):
    ad, _ = make_project(tmp_path)
    fw = ad.parents[1]
    top = tmp_path / "ws"
    hal = _module(top, "hal", 2)                          # checked out at its second commit; the manifest says the first
    lib = _module(top, "lib", 1)
    git(top / "lib", "tag", "v1.0")                       # a tag revision, checked out where the tag is
    projects = [{"name": "hal", "path": "hal", "revision": hal[0]},
                {"name": "lib", "path": "lib", "revision": "v1.0", "sha": lib[0]},
                {"name": "tools", "path": "tools", "revision": "main", "cloned": False}]
    west = tmp_path / "bin" / "west"
    west.parent.mkdir()
    west.write_text(FAKE_WEST.replace("{python}", sys.executable))
    west.chmod(west.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("FAKE_WEST_DRIFT", json.dumps(projects))
    (ad / "atlas.toml").write_text(f'[project]\nfw = "{fw}"\nwest_topdir = "{top}"\nwest = "{west}"\n\n'
                                   '[[targets]]\nname = "demo"\nboard = "demo_board"\napp = "app"\n', encoding="utf-8")
    monkeypatch.setattr(build, "run_build", lambda *a, **k: pytest.fail("built against a workspace off its manifest"))
    lines: list[str] = []
    assert update.run(ad, "demo", log=lines.append) == 2
    said = "\n".join(lines)
    assert f"west project hal is at {hal[1][:12]}, the manifest says {hal[0]}" in said and "west update" in said
    assert "lib" not in said.split("stopped")[1] and "not cloned" in said and "tools" in said
    st = bump.read_state(ad)["demo"]
    assert st["result"] == "stopped" and "not at the manifest's revisions" in st["reason"] and "hal" in st["reason"]

    git(top / "hal", "checkout", "-q", hal[0])            # `west update` would do this
    drift, uncloned = update.workspace_drift(build.target_config(ad, "demo"))
    assert drift == [] and uncloned == ["tools"]


# ---------------------------------------------------------------- the second look from use

def test_lookups_log_the_facts_their_answers_returned(atlas_dir):  # noqa: F811
    a = query.Atlas(atlas_dir)
    query.find_fact(a, "VOVP")
    query.register_lookup(a, "DEMO-BB.MODE")
    query.part_card(a, "DEMO-BB-QFN", board="")
    rows = [json.loads(x) for x in (atlas_dir / "usage.jsonl").read_text().splitlines()]
    assert {"doc": "demo-bb@A", "id": "demo-bb.ec.VOVP", "tool": "find_fact"} in rows
    reg = {r["id"] for r in rows if r["tool"] == "register"}
    assert {"demo-bb.reg.MODE", "demo-bb.reg.MODE.OE", "demo-bb.rule.ocp"} <= reg
    assert [r["page"] for r in rows if r["tool"] == "part_card"] == [1, 9]


def test_second_look_queue_used_batches_what_answers_returned(tmp_path, monkeypatch, capsys):
    from embarch_atlas import cli
    atlas = tmp_path / "atlases" / "core@000000000002"
    write_json(atlas / "atlas.json", {"id": atlas.name})
    item = lambda i, p, pr: {"id": i, "doc": "demo@1", "p": [p], "why": ["limit"], "ask": "x", "key": i, "priority": pr,
                             "kind": "limit", "pdf": None, "src": "table"}
    queue = [item("a", 2, 50), item("b", 3, 40), item("c", 4, 10), item("d", 7, 10)]
    monkeypatch.setattr(S, "build_queue", lambda ad, at: (queue, {
        "frozen": 0, "frozen_keys": [], "atlas": at.name, "used_records": 4, "needs_look": 4, "queued": 4, "by_reason": {}}))
    monkeypatch.setattr(S, "carry_frozen", lambda ad, at, keys: 0)
    log = tmp_path / "usage.jsonl"
    rows = [{"doc": "demo@1", "id": "c", "tool": "find_fact"}] * 3 + [{"doc": "demo@1", "id": "a", "tool": "register"}] \
        + [{"doc": "demo@1", "page": 7, "tool": "part_card"}] * 2 + [{"doc": "demo@1", "section": "1", "page": 3, "tool": "find_text"}]
    log.write_text("".join(json.dumps(r) + "\n" for r in rows) + "not json\n")
    args = ["--atlas-dir", str(tmp_path), "second-look", "queue", atlas.name, "--used", str(log), "--top", "2"]
    assert cli.main(args) == 0
    bs = read_json(atlas / "second_look_batches.json")
    assert [it["id"] for b in bs for it in b["items"]] == ["c", "d"]          # most used, not highest priority
    assert [b["uses"] for b in bs] == [3, 2]
    out = capsys.readouterr().out
    assert "3 of 4 queued items were returned by answers" in out and "(6 lookups read)" in out
    assert cli.main(["--atlas-dir", str(tmp_path), "second-look", "queue", atlas.name, "--used", str(tmp_path / "x")]) == 2

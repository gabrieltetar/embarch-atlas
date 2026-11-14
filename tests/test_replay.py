"""scripts/replay.py over synthetic transcripts and a synthetic atlas dir (invented symbols and paths)."""
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from embarch_atlas.common import write_json, write_text

SPEC = importlib.util.spec_from_file_location("replay", Path(__file__).resolve().parents[1] / "scripts" / "replay.py")
R = importlib.util.module_from_spec(SPEC)
sys.modules["replay"] = R
SPEC.loader.exec_module(R)

DOTCONFIG = "CONFIG_GPIO=y\nCONFIG_SHELL=y\n# CONFIG_FANCY is not set\nCONFIG_STACK=1024\n"


def _use(i, cmd):
    return json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": i, "name": "Bash", "input": {"command": cmd}}]}})


def _res(i, text, as_list=False):
    c = [{"type": "text", "text": text}] if as_list else text
    return json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": i, "content": c}]}})


@pytest.fixture()
def transcripts(tmp_path):
    d = tmp_path / "proj"
    (d / "sess" / "subagents").mkdir(parents=True)
    write_text(d / "a.jsonl", "\n".join([
        _use("1", "cd /x && grep -E 'CONFIG_GPIO|CONFIG_SHELL' /x/build/zephyr/.config"),
        _res("1", "CONFIG_GPIO=y\nCONFIG_SHELL=y\n"),
        _use("2", "grep -n CONFIG_STACK build/zephyr/.config | head -3"),     # a pipe: not a pattern
        _res("2", "x"),
        _use("3", "rm -rf /x/build && grep CONFIG_GPIO /x/.config"),          # not the grep segment's business
        _res("3", "CONFIG_GPIO=y\n", as_list=True),
        _use("4", "diff <(grep -E 'CONFIG_GPIO|CONFIG_STACK' a/zephyr/.config) <(grep -E 'CONFIG_GPIO|CONFIG_STACK' b/zephyr/.config)"),
        _res("4", "2c2\n< CONFIG_STACK=1024\n---\n> CONFIG_STACK=2048\n"),
        _use("5", "grep CONFIG_GPIO /etc/passwd"),                              # not a .config
        _res("5", "x"),
        "not json",
    ]) + "\n")
    write_text(d / "sess" / "subagents" / "b.jsonl", "\n".join([
        _use("1", "grep -c CONFIG_SHELL x/zephyr/.config"), _res("1", "1\n")]) + "\n")
    return d


def test_scan_lists_matching_calls_with_recorded_sizes(transcripts):
    calls = R.scan(R.transcript_files([str(transcripts)]))
    assert [(c.pattern, c.symbols, c.recorded_chars) for c in calls] == [
        ("config-grep", ["CONFIG_GPIO", "CONFIG_SHELL"], 29),
        ("config-grep", ["CONFIG_GPIO"], 14),
        ("config-diff", ["CONFIG_GPIO", "CONFIG_STACK"], 48),
        ("config-grep", ["CONFIG_SHELL"], 2)]
    assert [c.pattern for c in R.scan(R.transcript_files([str(transcripts)]), ["config-diff"])] == ["config-diff"]


def test_only_named_read_only_patterns_parse():
    for cmd in ("grep CONFIG_A x/.config > out", "grep --include=y CONFIG_A x/.config", "grep -r CONFIG_A x/.config",
                "grep CONFIG_A $(pwd)/.config", "grep FOO x/.config", "sed -n 1p x/.config", "rm x/.config",
                "diff <(grep CONFIG_A a/.config) <(rm b/.config)", "diff -r <(grep CONFIG_A a/.config) <(grep CONFIG_A b/.config)"):
        assert R.match(cmd) is None, cmd
    c = R.match("grep -e CONFIG_A -e 'CONFIG_B.*' x/.config")
    assert c.symbols == ["CONFIG_A", "CONFIG_B*"]


def _tree(tmp_path):
    atlas = tmp_path / "fw" / "embarch" / "atlas"
    write_text(atlas / "current.json", "{}")
    write_text(atlas / "atlases" / "t@1" / "atlas.json", "{}")
    b1, b2 = tmp_path / "b1", tmp_path / "b2"
    write_text(b1 / "zephyr" / ".config", DOTCONFIG)
    write_text(b2 / "zephyr" / ".config", DOTCONFIG.replace("1024", "2048"))
    return atlas, b1, b2


def _snapshot(root: Path):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def test_replay_counts_before_and_after_and_follows_pages(tmp_path, transcripts):
    atlas, b1, b2 = _tree(tmp_path)
    seen = []

    def lookup(copy, args):
        seen.append((copy, args))
        off = int(args[args.index("--offset") + 1]) if "--offset" in args else 0
        return "src: x\n\npage\n… truncated (next=10)\n" if off == 0 and args[2] != "CONFIG_SHELL" else "last page\n"

    rows = R.replay(R.scan(R.transcript_files([str(transcripts)])), atlas, b1, b2, lookup=lookup)
    assert [(r.before.calls, r.before.chars, r.after.calls) for r in rows] == [
        (1, len("CONFIG_GPIO=y\nCONFIG_SHELL=y\n"), 2), (1, len("CONFIG_GPIO=y\n"), 2),
        (1, len("2c2\n< CONFIG_STACK=1024\n---\n> CONFIG_STACK=2048\n"), 2), (1, 2, 1)]
    assert rows[0].after.chars == len("src: x\n\npage\n… truncated (next=10)\n") + len("last page\n")
    assert all(r.ok for r in rows)
    diff_args = [a for _, a in seen if "--vs" in a][0]
    assert diff_args[:2] == ["q", "config"] and diff_args[diff_args.index("--vs") + 1] == str(b2)
    assert all(a[a.index("--build") + 1] == str(b1) for _, a in seen)


def test_replay_writes_only_under_the_copy(tmp_path, transcripts):
    atlas, b1, b2 = _tree(tmp_path)
    before = {**_snapshot(atlas), **{"b1/" + k: v for k, v in _snapshot(b1).items()}}
    copies = []

    def lookup(copy, args):
        copies.append(copy)
        write_text(copy / "cache" / "scribble", "x")        # what a lookup's cache write would do
        return "ok\n"

    R.replay(R.scan(R.transcript_files([str(transcripts)])), atlas, b1, b2, lookup=lookup)
    assert copies and all(c != atlas and not str(c).startswith(str(atlas)) and str(c).startswith("/") for c in copies)
    assert not any(c.exists() for c in copies)               # the copy is gone, with its writes
    assert not (atlas / "cache").exists()
    assert {**_snapshot(atlas), **{"b1/" + k: v for k, v in _snapshot(b1).items()}} == before


def test_report_is_numbers_only(tmp_path, transcripts):
    atlas, b1, b2 = _tree(tmp_path)
    rows = R.replay(R.scan(R.transcript_files([str(transcripts)])), atlas, b1, b2, lookup=lambda c, a: "ok\n")
    rep = R.report(rows)
    lines = rep.splitlines()
    assert lines[2].startswith("| config-diff | 1 | 0 | 48 | 1 | 48 | 1 | 3 |")
    assert lines[-1].startswith("| all | 4 | 0 | 93 |")
    text = rep + R.detail(rows) + R.listing(R.scan(R.transcript_files([str(transcripts)])))
    for word in ("GPIO", "SHELL", "STACK", "zephyr", str(tmp_path)):
        assert word not in text


def test_failed_replay_is_counted_not_hidden(tmp_path, transcripts):
    atlas, b1, _ = _tree(tmp_path)
    rows = R.replay(R.scan(R.transcript_files([str(transcripts)])), atlas, b1, None, lookup=lambda c, a: "ok\n")
    assert [r.ok for r in rows] == [True, True, False, True]
    assert R.totals(rows)["config-diff"][:2] == [0, 1]


def test_end_to_end_through_the_real_lookup_on_a_synthetic_atlas(tmp_path, transcripts):
    atlas = tmp_path / "fw" / "embarch" / "atlas"
    at = atlas / "atlases" / "core@000000000003"
    write_json(at / "atlas.json", {"id": "core@000000000003", "commit": "0" * 40, "build": {}})
    write_text(at / "build" / ".config", DOTCONFIG)
    write_json(atlas / "current.json", {"matches": [{"atlas": "core@000000000003"}]})
    b1 = tmp_path / "b1"
    write_text(b1 / "zephyr" / ".config", DOTCONFIG)
    calls = R.scan(R.transcript_files([str(transcripts)]), ["config-grep"])
    rows = R.replay(calls, atlas, b1, pinned=True)
    assert all(r.ok and r.after.calls == 1 and r.after.chars > 0 for r in rows), rows
    assert not (atlas / "cache").exists()

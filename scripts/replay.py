#!/usr/bin/env python3
"""Replay recorded shell calls through the atlas lookup that replaces them, numbers only.

A developer-side measuring tool, not a CLI verb or an MCP tool: it reads Claude Code
transcripts, which are private, so it lives in `scripts/` and ships in no wheel. Output is
counts and character totals; no symbol, path or value from the project is printed.

    scripts/replay.py --project NAME                       # scan: list matching calls, sizes
    scripts/replay.py --project NAME --atlas-dir A --build-dir B [--vs-dir C] [--report]

Only a recorded command that parses as one of the PATTERNS below is ever re-run, and it is
re-run as an argument vector rebuilt from its parsed parts (never the recorded string, never
a shell): `grep` over a `.config`, or `diff` of two such greps. Every write goes under a
temporary copy of the atlas dir; the firmware tree and the real atlas dir are never written.
DESIGN.md, "The replay harness", has the rule.
"""
from __future__ import annotations

import argparse
import contextlib
import glob
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

GREP_FLAGS = set("EFiwnchoxs")        # read-only, output-shaping flags a replay will pass on
DIFF_FLAGS = set("u")
SYMBOL = re.compile(r"CONFIG_[A-Za-z0-9_]*(?:\.\*|\*)?")
NEXT = re.compile(r"\(next=(\d+)\)\n$")
MAX_PAGES = 200
TIMEOUT_S = 60


@dataclass
class Call:
    pattern: str                      # a name from PATTERNS
    symbols: list[str]                # the lookup's `symbol` arguments
    greps: list["Grep"]               # one for config-grep, two for config-diff
    diff_flags: str = ""
    recorded_chars: int = 0


@dataclass
class Grep:
    flags: str
    patterns: list[str]
    file: str


def _split_segments(cmd: str) -> list[str]:
    """The command's `&&`, `;` and newline separated segments, leaving `<( … )` intact."""
    out, cur, depth, i = [], "", 0, 0
    while i < len(cmd):
        c = cmd[i]
        if cmd.startswith("<(", i):
            depth, cur, i = depth + 1, cur + "<(", i + 2
            continue
        if c == ")" and depth:
            depth -= 1
        if not depth and (cmd.startswith("&&", i) or c in ";\n"):
            out.append(cur.strip())
            cur, i = "", i + (2 if c == "&" else 1)
            continue
        cur, i = cur + c, i + 1
    out.append(cur.strip())
    return [s for s in out if s]


def parse_grep(segment: str) -> Grep | None:
    """`grep [flags] (-e PAT)... | PAT  <file>.config`, or None for anything else."""
    try:
        toks = shlex.split(segment)
    except ValueError:
        return None
    if not toks or toks[0] != "grep" or any(t in ("|", ">", ">>", "<") or t.startswith(("$", "`")) for t in toks):
        return None
    flags, pats, rest, i = "", [], [], 1
    while i < len(toks):
        t = toks[i]
        if t == "-e" and i + 1 < len(toks):
            pats.append(toks[i + 1])
            i += 2
            continue
        if t.startswith("-") and len(t) > 1 and not t.startswith("--"):
            if not set(t[1:]) <= GREP_FLAGS:
                return None
            flags += t[1:]
        elif t.startswith("--"):
            return None
        else:
            rest.append(t)
        i += 1
    if not pats and rest:
        pats.append(rest.pop(0))
    if len(rest) != 1 or not rest[0].endswith(".config") or not pats:
        return None
    return Grep("".join(sorted(set(flags))), pats, rest[0])


def _symbols(greps: list[Grep]) -> list[str]:
    out: list[str] = []
    for g in greps:
        for p in g.patterns:
            for s in SYMBOL.findall(p):
                s = s.replace(".*", "*")
                if s != "CONFIG_" and s not in out:
                    out.append(s)
    return out


DIFF = re.compile(r"^diff((?:\s+-[a-z]+)*)\s+<\((.+?)\)\s+<\((.+)\)$")


def parse_config_grep(cmd: str) -> Call | None:
    for seg in _split_segments(cmd):
        g = parse_grep(seg)
        if g and _symbols([g]):
            return Call("config-grep", _symbols([g]), [g])
    return None


def parse_config_diff(cmd: str) -> Call | None:
    for seg in _split_segments(cmd):
        m = DIFF.match(seg)
        if not m:
            continue
        flags = "".join(re.findall(r"-([a-z]+)", m.group(1)))
        a, b = parse_grep(m.group(2)), parse_grep(m.group(3))
        if a and b and set(flags) <= DIFF_FLAGS and _symbols([a, b]):
            return Call("config-diff", _symbols([a, b]), [a, b], flags)
    return None


# name -> (parser, what the lookup is called with). The table is the whole of what a replay may run.
PATTERNS: dict[str, Callable[[str], Call | None]] = {
    "config-diff": parse_config_diff,     # diff <(grep … a/.config) <(grep … b/.config)  ->  q config SYMS --vs B
    "config-grep": parse_config_grep,     # grep -E 'CONFIG_A|CONFIG_B' …/.config         ->  q config SYMS
}


def match(cmd: str, names: list[str] | None = None) -> Call | None:
    for name in names or list(PATTERNS):
        c = PATTERNS[name](cmd)
        if c:
            return c
    return None


# ---------------------------------------------------------------- transcripts

def transcript_files(paths: list[str]) -> list[Path]:
    """Every `*.jsonl` under the given files and directories, subagent transcripts included."""
    out: list[Path] = []
    for p in map(Path, paths):
        out += sorted(p.rglob("*.jsonl")) if p.is_dir() else [p]
    return out


def project_dirs(project: str) -> list[str]:
    return sorted(glob.glob(os.path.expanduser(f"~/.claude*/projects/{project}*")))


def _blocks(line: str) -> Iterator[dict]:
    try:
        msg = (json.loads(line).get("message") or {})
    except (ValueError, AttributeError):
        return
    content = msg.get("content") if isinstance(msg, dict) else None
    for b in content if isinstance(content, list) else []:
        if isinstance(b, dict):
            yield b


def _result_chars(content) -> int:
    if isinstance(content, str):
        return len(content)
    return sum(len(x.get("text", "")) for x in content or [] if isinstance(x, dict))


def scan(files: list[Path], names: list[str] | None = None) -> list[Call]:
    """The `Bash` calls matching a named pattern, each paired with its `tool_result`'s size."""
    calls: list[Call] = []
    for f in files:
        pending: dict[str, Call] = {}
        try:
            lines = f.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            for b in _blocks(line):
                if b.get("type") == "tool_use" and b.get("name") == "Bash":
                    c = match(str((b.get("input") or {}).get("command", "")), names)
                    if c:
                        pending[b.get("id", "")] = c
                elif b.get("type") == "tool_result" and b.get("tool_use_id") in pending:
                    c = pending.pop(b["tool_use_id"])
                    c.recorded_chars = _result_chars(b.get("content"))
                    calls.append(c)
    return calls


# ---------------------------------------------------------------- replay

@dataclass
class Counts:
    calls: int = 0
    chars: int = 0


@dataclass
class Row:
    pattern: str
    recorded: int
    before: Counts = field(default_factory=Counts)
    after: Counts = field(default_factory=Counts)
    ok: bool = True


def _run_grep(g: Grep, build_dir: Path) -> str:
    argv = ["grep", *([f"-{g.flags}"] if g.flags else [])]
    for p in g.patterns:
        argv += ["-e", p]
    argv.append(str(build_dir / "zephyr" / ".config"))
    r = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT_S,
                       env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    if r.returncode > 1:
        raise RuntimeError("grep failed")
    return r.stdout


def run_before(call: Call, build_dir: Path, vs_dir: Path | None, scratch: Path) -> str:
    if call.pattern == "config-grep":
        return _run_grep(call.greps[0], build_dir)
    if vs_dir is None:
        raise RuntimeError("config-diff needs --vs-dir")
    paths = []
    for n, (g, d) in enumerate(zip(call.greps, (build_dir, vs_dir))):
        p = scratch / f"side{n}"
        p.write_text(_run_grep(g, d))
        paths.append(str(p))
    argv = ["diff", *([f"-{call.diff_flags}"] if call.diff_flags else []), *paths]
    r = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT_S)
    if r.returncode > 1:
        raise RuntimeError("diff failed")
    return r.stdout


def lookup_args(call: Call, build_dir: Path, vs_dir: Path | None, pinned: bool) -> list[str]:
    argv = ["q", "config", *call.symbols]
    if not pinned:
        argv += ["--build", str(build_dir)]
    if call.pattern == "config-diff":
        argv += ["--vs", str(vs_dir)]
    return argv


def run_lookup(atlas_copy: Path, args: list[str]) -> str:
    """One `embarch-atlas` invocation against the copy, in process, stdout returned."""
    from embarch_atlas import cli
    buf = io.StringIO()
    old = os.environ.get("GIT_OPTIONAL_LOCKS")
    os.environ["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        with contextlib.redirect_stdout(buf):
            cli.main(["--atlas-dir", str(atlas_copy), *args])
    finally:
        if old is None:
            os.environ.pop("GIT_OPTIONAL_LOCKS", None)
        else:
            os.environ["GIT_OPTIONAL_LOCKS"] = old
    return buf.getvalue()


def after(atlas_copy: Path, args: list[str], lookup: Callable[[Path, list[str]], str]) -> Counts:
    """The lookup with its `next=` pages followed to the end."""
    n, offset = Counts(), 0
    while n.calls < MAX_PAGES:
        out = lookup(atlas_copy, args + (["--offset", str(offset)] if offset else []))
        n.calls += 1
        n.chars += len(out)
        m = NEXT.search(out)
        if not m:
            break
        offset = int(m.group(1))
    return n


def replay(calls: list[Call], atlas_dir: Path, build_dir: Path, vs_dir: Path | None = None, pinned: bool = False,
           lookup: Callable[[Path, list[str]], str] = run_lookup) -> list[Row]:
    """Every call's before and after. Writes only under a temporary copy of `atlas_dir`."""
    rows: list[Row] = []
    with tempfile.TemporaryDirectory(prefix="atlas-replay-") as tmp:
        copy = Path(tmp) / "atlas"
        shutil.copytree(atlas_dir, copy, symlinks=True)
        scratch = Path(tmp) / "scratch"
        scratch.mkdir()
        for c in calls:
            row = Row(c.pattern, c.recorded_chars)
            try:
                row.before = Counts(1, len(run_before(c, build_dir, vs_dir, scratch)))
                row.after = after(copy, lookup_args(c, build_dir, vs_dir, pinned), lookup)
            except (RuntimeError, OSError, subprocess.SubprocessError):
                row.ok = False
            rows.append(row)
    return rows


# ---------------------------------------------------------------- output

def totals(rows: list[Row]) -> dict[str, list[int]]:
    """pattern (and `all`) -> [replayed, skipped, recorded chars, before calls, before chars, after calls, after chars]."""
    t: dict[str, list[int]] = {}
    for r in rows:
        for key in (r.pattern, "all"):
            v = t.setdefault(key, [0] * 7)
            if r.ok:
                v[0] += 1
                v[2:] = [v[2] + r.recorded, v[3] + r.before.calls, v[4] + r.before.chars,
                         v[5] + r.after.calls, v[6] + r.after.chars]
            else:
                v[1] += 1
    return t


def report(rows: list[Row]) -> str:
    t = totals(rows)
    out = ["| pattern | replayed | skipped | recorded chars | before calls | before chars | after calls | after chars |",
           "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for k in [*sorted(k for k in t if k != "all"), "all"] if t else []:
        out.append(f"| {k} | " + " | ".join(map(str, t[k])) + " |")
    return "\n".join(out) + "\n"


def listing(calls: list[Call]) -> str:
    out = ["n  pattern      symbols  recorded_chars"]
    out += [f"{i:<3}{c.pattern:<13}{len(c.symbols):<9}{c.recorded_chars}" for i, c in enumerate(calls, 1)]
    out.append(f"{len(calls)} calls, {sum(c.recorded_chars for c in calls)} recorded chars")
    return "\n".join(out) + "\n"


def detail(rows: list[Row]) -> str:
    out = ["n  pattern      recorded  before(calls,chars)  after(calls,chars)"]
    for i, r in enumerate(rows, 1):
        tail = f"{r.before.calls},{r.before.chars:<8}  {r.after.calls},{r.after.chars}" if r.ok else "skipped"
        out.append(f"{i:<3}{r.pattern:<13}{r.recorded:<10}{tail}")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--project", help="project name prefix under ~/.claude*/projects/")
    p.add_argument("--transcripts", nargs="*", default=[], help="transcript files or directories instead of --project")
    p.add_argument("--pattern", action="append", choices=sorted(PATTERNS), help="only these (default: all)")
    p.add_argument("--atlas-dir", help="the project's atlas dir; copied to /tmp, never written")
    p.add_argument("--build-dir", help="the build dir the shell calls re-run against (and the lookup reads)")
    p.add_argument("--vs-dir", help="the second build dir, for config-diff")
    p.add_argument("--pinned", action="store_true", help="the lookup answers from the atlas's pinned build, not --build-dir")
    p.add_argument("--report", action="store_true", help="print the numbers-only table a task's `## Result` takes")
    a = p.parse_args(argv)
    paths = a.transcripts + (project_dirs(a.project) if a.project else [])
    if not paths:
        p.error("no transcripts: give --project or --transcripts")
    calls = scan(transcript_files(paths), a.pattern)
    if not (a.atlas_dir and a.build_dir):
        print(listing(calls), end="")
        return 0
    rows = replay(calls, Path(a.atlas_dir), Path(a.build_dir), Path(a.vs_dir) if a.vs_dir else None, a.pinned)
    print(report(rows) if a.report else detail(rows) + report(rows), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())

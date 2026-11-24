"""The live log: one decoded line per live call, appended to the file `embarch-api` chose for the session.

`embarch-api` appends a raw line per lease-gated call to `<fw>/embarch/live/<date>-<session>.jsonl`
(embarch-doc `embarch-api/interfaces/live-log.md`, schema `embarch-live/1`). The atlas writes the line
the engineer reads: the decoded answer, with every Core call-log entry the call made, which the CLI's
`--json` output carries as `core_calls`. Same file, one `write` of the whole line in append mode, so the
decoded line follows the raw lines it decodes and a reader never sees half of one.

A call that reached no lease-gated route writes nothing, as in `embarch-api` (a refusal by the
read-only list, an unknown register name, a guard on a clearing read). The lease token never reaches a
line: the whole serialised line goes through `live._scrub` before it is written.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

SCHEMA = "embarch-live/1"
WRITER = "embarch-atlas"
ANSWER_CAP_BYTES = 4096           # embarch-api's cap (chosen, not measured); the atlas matches it
# `embarch-api` verbs that reach no lease-gated route: a call that made only these writes no line.
UNGATED = frozenset({"status", "list-signals", "lease-show", "list-projects", "call-log"})


class Trace:
    """What one live call did through `embarch-api`: the Core entries it cited and what Core said of the board."""

    def __init__(self) -> None:
        self.gated = False
        self.core: list[dict[str, Any]] = []
        self.board: dict[str, Any] = {"role": None, "probe_serial": None, "chip": None}


_TRACE: contextvars.ContextVar[Trace | None] = contextvars.ContextVar("atlas_live_trace", default=None)
_TOOL: contextvars.ContextVar[str | None] = contextvars.ContextVar("atlas_live_tool", default=None)
_VIA: contextvars.ContextVar[str] = contextvars.ContextVar("atlas_live_via", default="cli")


def set_context(tool: str | None, via: str) -> tuple:
    """Name the tool (MCP) or subcommand (CLI) that is calling, for the line's `tool` and `via`."""
    return _TOOL.set(tool), _VIA.set(via)


def reset_context(tokens: tuple) -> None:
    _TOOL.reset(tokens[0])
    _VIA.reset(tokens[1])


def note(args: list[str], out: dict[str, Any] | None) -> None:
    """Called by `call_api` with the arguments and the parsed `--json` answer (None when it was not JSON):
    marks the call gated and keeps the Core entries and the board Core named."""
    tr = _TRACE.get()
    if tr is None or not args:
        return
    if args[0] not in UNGATED:
        tr.gated = True
    if not isinstance(out, dict):
        return
    tr.core += [c for c in out.get("core_calls") or [] if isinstance(c, dict)]
    for key in ("read", "state"):
        d = out.get(key)
        if isinstance(d, dict):
            for f in tr.board:
                if tr.board[f] is None and d.get(f) is not None:
                    tr.board[f] = d[f]


# --------------------------------------------------------------------------- where

def _git_top(start: Path) -> Path | None:
    for d in [start, *start.parents]:
        if (d / ".git").exists():
            return d
    return None


def log_repo(atlas_dir: Path, cwd: Path | None = None) -> Path:
    """`embarch-api`'s rule: the nearest directory from the working directory up to its git root that holds an
    `embarch/` folder; failing that the firmware checkout `atlas.toml` names."""
    from .project import fw_dir, load_config
    here = (cwd or Path.cwd()).resolve()
    top = _git_top(here)
    for d in [here, *here.parents]:
        if (d / "embarch").is_dir():
            return d
        if d == top:
            break
    try:
        cfg = load_config(atlas_dir)
    except FileNotFoundError:
        cfg = {}
    return fw_dir(atlas_dir, cfg)


def _holder_part(holder: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", holder)


def session_of(s: dict[str, Any]) -> dict[str, str] | None:
    """`{holder, started_at}` of the live lease from `embarch-api lease-show`, None with no lease (or no answer)."""
    from . import live
    try:
        out = live.call_api(s, ["lease-show"], live.STATUS_TIMEOUT_S)
    except live.LiveError:
        return None
    lease = out.get("lease")
    if isinstance(lease, dict) and lease.get("holder") and lease.get("started_at"):
        return {"holder": str(lease["holder"]), "started_at": str(lease["started_at"])}
    return None


def file_name(session: dict[str, str] | None, now: datetime) -> str:
    """`<date>-<session>.jsonl`: the lease's start date, its holder (every character that is not an ASCII letter
    or digit turned into `-`) and its start time as `HHMMSS`; `<today>-no-lease` with none. The date and time are
    read off the RFC 3339 `started_at` text, taken as UTC (live-log.md does not say which zone: an assumption)."""
    if session:
        m = re.match(r"(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})", session["started_at"])
        if m:
            return f"{m[1]}-{_holder_part(session['holder'])}-{m[2]}{m[3]}{m[4]}.jsonl"
    return f"{now.strftime('%Y-%m-%d')}-no-lease.jsonl"


# --------------------------------------------------------------------------- the line

def answer_block(text: str) -> dict[str, Any]:
    raw = text.encode("utf-8")
    cut = raw[:ANSWER_CAP_BYTES].decode("utf-8", errors="ignore") if len(raw) > ANSWER_CAP_BYTES else text
    return {"text": cut, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
            "truncated": len(raw) > ANSWER_CAP_BYTES}


def build_line(tr: Trace, tool: str, via: str, sent: Any, role: str | None, text: str, ok: bool, outcome: str,
               session: dict[str, str] | None, now: datetime) -> dict[str, Any]:
    board = dict(tr.board)
    if board["role"] is None:
        board["role"] = role
    return {"schema": SCHEMA, "t": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "writer": WRITER, "via": via, "tool": tool,
            "sent": sent, "board": board, "image_sha256": "unknown", "ok": ok, "outcome": outcome,
            "answer": answer_block(text), "core": tr.core, "session": session}


def append(path: Path, line: dict[str, Any]) -> None:
    """One `write` of the whole line in append mode (`O_APPEND`), the directory made on the first write."""
    from . import live
    data = (live._scrub(json.dumps(line, ensure_ascii=False)) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def sent_cli(tool: str, args: dict[str, Any], positional: tuple[str, ...] = ()) -> str:
    """The subcommand as the CLI would have been given it: positionals, then `--flag value` for each argument set."""
    parts = [tool] + [str(args[k]) for k in positional if args.get(k) is not None]
    for k, v in args.items():
        if k in positional or v is None or v is False:
            continue
        parts.append(f"--{k.replace('_', '-')}" + ("" if v is True else f" {v}"))
    return " ".join(parts)


Described = tuple[str, bool, str]       # (the answer text, ok, outcome)


def logged(atlas_dir: Path, default_tool: str, args: dict[str, Any], fn: Callable[[], Any],
           describe: Callable[[Any], Described], role: str | None = None, positional: tuple[str, ...] = ()) -> Any:
    """Run `fn` (a live call) and, when it reached a gated route, append the decoded line. Returns what `fn`
    returned, with a note about a line that could not be written added the way `embarch-api` adds its own:
    on stderr for the CLI, at the end of the text for an MCP tool. Nested calls (`shell_read` -> `shell`)
    write once, at the outermost."""
    if _TRACE.get() is not None:
        return fn()
    tr = Trace()
    token = _TRACE.set(tr)
    try:
        result = fn()
    finally:
        _TRACE.reset(token)
    if not tr.gated:
        return result
    from . import live
    text, ok, outcome = describe(result)
    via, tool = _VIA.get(), _TOOL.get() or default_tool
    problem = None
    try:
        s = live.settings(atlas_dir)
        session = session_of(s)
        now = datetime.now(timezone.utc)
        sent: Any = dict(args) if via == "mcp" else sent_cli(tool, args, positional)
        line = build_line(tr, tool, via, sent, role or s["role"], text, ok, outcome, session, now)
        append(log_repo(atlas_dir) / "embarch" / "live" / file_name(session, now), line)
    except OSError as e:
        problem = f"live log: no line written ({e})"
    if problem is None:
        return result
    if via == "cli":
        print(problem, file=sys.stderr)
        return result
    return _with_note(result, problem)


def _with_note(result: Any, problem: str) -> Any:
    if isinstance(result, tuple):
        return (result[0] + f"\n{problem}\n", *result[1:])
    return result + f"\n{problem}\n"

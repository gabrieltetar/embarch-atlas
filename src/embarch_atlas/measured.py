"""The measured plane: a live finding written back as a `measured` fact, with Core's receipt.

`embarch-atlas live write-back` (MCP `write_back`) takes the Core call-log entries a live call made (`--seq`),
the fact or entity the finding bears on, and the value read. **The receipt is read from Core**, through
`embarch-api call-log --seq N --json`, never from the caller: the calls (seq, hash, route, status, time), the board
and the time are Core's entry's. An entry Core does not hold, one Core's chain does not verify, a fold, or a call
Core refused, is refused: it settled nothing. The live log's line that cites the entry is attached beside it, and
where the line and Core's entry disagree on the board, Core's entry wins and the disagreement is named.

The image the finding was measured on is named the way `notes/hil/05-live.md` says: "read back" when `image_check`
matched the pinned build, else "as last flashed by Core" (`last_images`, through `livelog.image_sha256`), else
"unknown". A finding on another build is written, never refused, and flagged "measured on a different build".

`via: live` only. Records are appended, one `write` in append mode, to `<atlas dir>/measured.jsonl`: the file
sits above the per-commit atlases so a rebuild never touches it, and nothing rewrites a line (append-only, as the
measured plane is). Schema: `$defs/measurement` in `schemas/record.schema.json`.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import common, live, livelog, records

FILE = "measured.jsonl"
VIA = "live"
DIFFERENT_BUILD = "measured on a different build"
READ_BACK = "read back"
LAST_FLASHED = "as last flashed by Core"
UNKNOWN = "unknown"


class Refused(Exception):
    """The write-back was declined; nothing was written."""


def path(atlas_dir: Path) -> Path:
    return Path(atlas_dir) / FILE


def load(atlas_dir: Path) -> list[dict]:
    p = path(atlas_dir)
    return list(common.read_jsonl(p)) if p.exists() else []


# --------------------------------------------------------------------------- Core's entries

def _rfc3339(ms: Any) -> str | None:
    if isinstance(ms, bool) or not isinstance(ms, (int, float)):
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def core_entry(s: dict[str, Any], seq: int) -> dict[str, Any]:
    """Core's call-log entry `seq`, from `embarch-api call-log --seq`; Refused when Core does not hold it."""
    try:
        out = live.call_api(s, ["call-log", "--seq", str(seq)], live.STATUS_TIMEOUT_S)
    except live.LiveError as e:
        raise Refused(f"Core's call log could not be read for entry {seq}: {e}")
    log = out.get("call_log") if isinstance(out.get("call_log"), dict) else {}
    if log.get("verified") is False:
        raise Refused(f"Core's call-log chain does not verify ({log.get('error') or 'no reason given'}): "
                      f"entry {seq} is not cited")
    hits = []
    for item in log.get("entries") or []:
        body = item.get("entry") if isinstance(item, dict) and isinstance(item.get("entry"), dict) else item
        if isinstance(body, dict) and body.get("seq") == seq:
            hits.append((body, item.get("hash") if isinstance(item, dict) else None))
    if not hits:
        raise Refused(f"Core holds no call-log entry {seq}")
    body, h = hits[0]
    if body.get("type") != "call":
        raise Refused(f"entry {seq} is a {body.get('type')!r} entry, not a call: it measured nothing")
    status = body.get("status")
    if isinstance(status, int) and status >= 400:
        raise Refused(f"entry {seq} is a call Core answered {status}: a refused call settled nothing")
    return {"seq": seq, "hash": h or body.get("hash"), "method": body.get("method"),
            "route": body.get("route"), "status": status, "at_utc_ms": body.get("at_utc_ms"),
            "at": _rfc3339(body.get("at_utc_ms")), "board": body.get("board") or {}}


def board_of(entries: list[dict]) -> dict[str, Any]:
    """The first role, probe serial, chip and image hash any entry names (Core's own, not the caller's)."""
    keys = ("role", "probe_serial", "chip", "image_sha256")
    return {k: next((e["board"][k] for e in entries
                     if isinstance(e["board"], dict) and e["board"].get(k) not in (None, "")), None) for k in keys}


# --------------------------------------------------------------------------- the live-log line beside it

def log_line(atlas_dir: Path, entries: list[dict], board: dict[str, Any]) -> dict[str, Any] | None:
    """The live-log line that cites one of these entries (by seq and hash): its file, line number, writer, tool,
    time and the answer's SHA-256, and `disagrees`, the board fields on which it differs from Core's entry
    (Core's entry wins). None when no line cites them."""
    want = {(e["seq"], e["hash"]) for e in entries}
    folder = livelog.log_repo(atlas_dir) / "embarch" / "live"
    if not folder.is_dir():
        return None
    for f in sorted(folder.glob("*.jsonl")):
        for n, row in common.read_jsonl_numbered(f):
            core = row.get("core") if isinstance(row, dict) else None
            if not any(isinstance(c, dict) and (c.get("seq"), c.get("hash")) in want for c in core or []):
                continue
            theirs = row.get("board") or {}
            return {"file": f.name, "line": n, "writer": row.get("writer"), "tool": row.get("tool"),
                    "t": row.get("t"), "answer_sha256": (row.get("answer") or {}).get("sha256"),
                    "disagrees": sorted(k for k in ("role", "probe_serial", "chip")
                                        if theirs.get(k) and board.get(k) and theirs[k] != board[k])}
    return None


# --------------------------------------------------------------------------- the image

def image_of(s: dict[str, Any], atlas_dir: Path, board: dict[str, Any], build: str | None) -> dict[str, Any]:
    """Which image the finding was measured on, and whether it is the pinned build.
    `source`: "read back" when `image_check` found the flash holding the pinned build's ELF, else "as last flashed
    by Core" when `last_images` names the board, else "unknown". `build`: `pinned` (read back and matched), `different`
    (the read-back did not match, or the image Core stamped on the call is not the one it holds now), `unknown`
    (the read-back could not be made: no ELF, no lease, Core down)."""
    from . import buildq
    from .query import Atlas
    last = livelog.image_sha256(livelog._status(s), board.get("chip"), board.get("probe_serial"))
    at_call = board.get("image_sha256") or UNKNOWN
    same: bool | None = None
    why = ""
    try:
        src = buildq.source(Atlas(Path(atlas_dir)), build)
        same, why = live.image_check(s, board.get("role") or s["role"], src.elf, atlas_dir)
    except (LookupError, FileNotFoundError, OSError, ValueError, RuntimeError, live.LiveError,
            subprocess.CalledProcessError) as e:
        why = f"no read-back: {e}"
    held = LAST_FLASHED if last != UNKNOWN else UNKNOWN
    source = READ_BACK if same else held
    reflashed = UNKNOWN not in (last, at_call) and last != at_call
    state = "different" if same is False or reflashed else "pinned" if same else UNKNOWN
    return {"sha256": last, "at_call": at_call, "source": source, "build": state, "check": why}


# --------------------------------------------------------------------------- the record

def _slug_target(target: dict[str, str]) -> str:
    return records.slug(target.get("fact") or target["entity"])


def _value(text: str) -> Any:
    for conv in (int, float):
        try:
            return conv(text)
        except ValueError:
            pass
    return text


def _fact_doc(atlas_dir: Path, fact_id: str) -> str | None:
    """The doc a fact record of this id sits in, None when no document holds it."""
    for rdir in sorted((Path(atlas_dir) / "docs").glob("*/records")):
        for f in sorted(rdir.glob("*.jsonl")):
            for rec in common.read_jsonl(f):
                if isinstance(rec, dict) and rec.get("id") == fact_id:
                    return str(rec.get("doc") or rdir.parent.name)
    return None


def _new_id(existing: list[dict], target: dict[str, str], seqs: list[int]) -> str:
    stem = f"measured.{_slug_target(target)}.s{'-'.join(str(n) for n in seqs)}"
    taken = sum(1 for r in existing if str(r.get("id", "")).startswith(stem))
    return stem if not taken else f"{stem}.{taken + 1}"


def append(atlas_dir: Path, rec: dict[str, Any]) -> None:
    """One `write` of the whole line in append mode; nothing already in the file is touched."""
    data = (live._scrub(json.dumps(rec, sort_keys=True, ensure_ascii=False)) + "\n").encode("utf-8")
    p = path(atlas_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def build_record(atlas_dir: Path, seqs: list[int], fact: str | None, entity: str | None, value: str,
                 unit: str | None, note: str | None, build: str | None) -> dict[str, Any]:
    """The `measurement` record for this finding. Refused: no entry named, both or neither of fact and entity, a
    fact no document holds, no value, an entry Core does not hold."""
    if not seqs:
        raise Refused("name at least one Core call-log entry (--seq)")
    if bool(fact) == bool(entity):
        raise Refused("name the fact (--fact) or the entity (--entity) the finding bears on, one of the two")
    if value is None or not str(value).strip():
        raise Refused("name the value read (--value)")
    doc = "measured"
    if fact:
        doc = _fact_doc(atlas_dir, fact) or ""
        if not doc:
            raise Refused(f"no fact {fact!r} in this atlas's documents (an entity is named with --entity)")
    s = live.settings(atlas_dir)
    entries = [core_entry(s, n) for n in dict.fromkeys(seqs)]
    board = board_of(entries)
    target = {"fact": fact} if fact else {"entity": entity}
    img = image_of(s, atlas_dir, board, build)
    ll = log_line(atlas_dir, entries, board)
    calls = [{k: e[k] for k in ("seq", "hash", "method", "route", "status", "at_utc_ms", "at")} for e in entries]
    first = next((e["at"] for e in entries if e["at"]), None)
    rec: dict[str, Any] = {
        "id": _new_id(load(atlas_dir), target, [e["seq"] for e in entries]),
        "kind": "measurement", "doc": doc, "p": None, "src": "live", "prov": "measured",
        **target, "value": _value(str(value).strip()), "via": VIA,
        "receipt": {"calls": calls, "board": {k: board[k] for k in ("role", "probe_serial", "chip")},
                    "time": first, "image": img, "live_log": ll},
    }
    if unit:
        rec["unit"] = unit
    if img["build"] == "different":
        rec["flag"] = DIFFERENT_BUILD
    if note:
        rec["note"] = note
    errs = records.validate(rec)
    if errs:
        raise Refused("the record is not schema-valid: " + "; ".join(errs))
    return rec


def write_back(atlas_dir: Path, seqs: list[int], fact: str | None = None, entity: str | None = None,
               value: str = "", unit: str | None = None, note: str | None = None,
               build: str | None = None) -> str:
    """Write one `measured` record, `via: live`, and say what it holds; `Refused: ...` and nothing written otherwise."""
    try:
        rec = build_record(atlas_dir, seqs, fact, entity, value, unit, note, build)
    except Refused as e:
        return f"Refused: {e}. Nothing was written.\n"
    append(atlas_dir, rec)
    r, img = rec["receipt"], rec["receipt"]["image"]
    b = r["board"]
    lines = [f"Wrote {rec['id']} to {FILE} (prov measured, via {VIA}).",
             f"bears on: {rec.get('fact') or rec.get('entity')}; value: {rec['value']}"
             + (f" {rec['unit']}" if rec.get("unit") else ""),
             "Core's entries: " + ", ".join(f"#{c['seq']} {c['method']} {c['route']} ({c['status']})"
                                            for c in r["calls"]),
             f"board: role {b['role']}, probe {b['probe_serial']}, chip {b['chip']}; time {r['time']}",
             f"image: {img['source']} ({img['sha256']}); the pinned build: {img['build']}"]
    if r["live_log"]:
        ll = r["live_log"]
        lines.append(f"live log: {ll['file']} line {ll['line']}"
                     + (f"; disagrees with Core on {', '.join(ll['disagrees'])}: Core's entry is kept"
                        if ll["disagrees"] else ""))
    else:
        lines.append("live log: no line cites these entries")
    if rec.get("flag"):
        lines.append(f"flag: {rec['flag']}")
    lines.append("A `via: live` fact is development evidence: it cannot be cited from the atlas.")
    return "\n".join(lines) + "\n"

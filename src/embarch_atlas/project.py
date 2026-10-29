"""Project config and atlas bookkeeping: `init <fw>` and `status`.

`init` writes `<fw>/embarch/atlas/atlas.toml` (DESIGN.md "atlas.toml") and never
overwrites one that exists. What it cannot know (schematic board labels, the
Zephyr board and app of each target, the MCU part) is left as commented
examples, with the candidates it found listed beside them: the labels and the
target list are the engineer's call.

`status` lists `atlases/*/atlas.json`, says which one matches the firmware
repo's HEAD (recorded in `current.json`) and which are stale: an atlas is stale
when HEAD differs from its commit, or `inventory/manifest.json` or a board PDF
no longer hashes to what its stamp recorded.

`load_config(atlas_dir)` is the one reader of `atlas.toml` for every module.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

from . import common

CONFIG_NAME = "atlas.toml"
DEFAULT_SOURCES = "embarch/hardware"
SCHEMATIC_CREATORS = ("altium", "orcad", "capture", "eeschema", "kicad", "allegro", "pads", "xpedition")


# --------------------------------------------------------------------------- config

def load_config(atlas_dir: Path) -> dict[str, Any]:
    """Parse `<atlas_dir>/atlas.toml`. Raises FileNotFoundError with the fix in the message."""
    path = Path(atlas_dir) / CONFIG_NAME
    if not path.is_file():
        raise FileNotFoundError(f"{path} not found; run `embarch-atlas init <fw>` first")
    with open(path, "rb") as f:
        return tomllib.load(f)


def fw_dir(atlas_dir: Path, cfg: dict[str, Any]) -> Path:
    """The firmware repo: `[project] fw`, else the atlas dir's grandparent (`<fw>/embarch/atlas`)."""
    fw = (cfg.get("project") or {}).get("fw")
    return Path(fw) if fw else Path(atlas_dir).resolve().parents[1]


def sources_dir(atlas_dir: Path, cfg: dict[str, Any]) -> Path:
    """Where the engineer drops documents: `[project] sources`, relative to fw unless absolute."""
    src = Path((cfg.get("project") or {}).get("sources") or DEFAULT_SOURCES)
    return src if src.is_absolute() else fw_dir(atlas_dir, cfg) / src


def board_entries(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    return [b for b in cfg.get("boards") or [] if b.get("label")]


# --------------------------------------------------------------------------- init

def find_west_topdir(start: Path) -> Path | None:
    """Nearest directory at or above `start` holding `.west/`."""
    start = start.resolve()
    for d in (start, *start.parents):
        if (d / ".west").is_dir():
            return d
    return None


def _pdf_creator(path: Path) -> str:
    try:
        out = subprocess.run(["pdfinfo", str(path)], capture_output=True, text=True, errors="replace", timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""
    for line in out.splitlines():
        if line.startswith(("Creator:", "Producer:")):
            return line.split(":", 1)[1].strip()
    return ""


def _schematic_candidates(sources: Path) -> list[str]:
    """PDFs in the sources dir whose creator is an EDA tool (a hint for [[boards]], not a decision)."""
    if not sources.is_dir():
        return []
    found = []
    for p in sorted(sources.rglob("*.pdf")):
        if any(part.startswith(".") for part in p.relative_to(sources).parts):
            continue
        if any(c in _pdf_creator(p).lower() for c in SCHEMATIC_CREATORS):
            found.append(p.relative_to(sources).as_posix())
    return found


def _app_candidates(fw: Path) -> list[str]:
    """Directories (up to three levels down) holding a `prj.conf`: likely Zephyr apps."""
    out = []
    for depth in ("*/prj.conf", "*/*/prj.conf", "*/*/*/prj.conf"):
        for p in fw.glob(depth):
            rel = p.parent.relative_to(fw)
            if any(part.startswith(".") or part in ("build", "embarch") for part in rel.parts):
                continue
            out.append(rel.as_posix())
    return sorted(set(out))


def _board_candidates(fw: Path) -> list[str]:
    """Zephyr board names defined in this repo (`boards/**/board.yml`)."""
    names = []
    for p in sorted((fw / "boards").glob("**/board.yml")) if (fw / "boards").is_dir() else []:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if s.startswith("name:"):
                names.append(s.split(":", 1)[1].strip().strip("'\""))
                break
    return sorted(set(names))


def _q(s: str) -> str:
    """A TOML basic string (JSON's escapes are a subset of TOML's)."""
    return json.dumps(s, ensure_ascii=False)


def render_config(fw: Path, west_topdir: Path | None, sources_rel: str, sources_exists: bool,
                  schematics: list[str], apps: list[str], zboards: list[str]) -> str:
    L = [
        "# embarch-atlas project config (DESIGN.md \"atlas.toml\"). Written by `embarch-atlas init`; edit freely.",
        "# Paths are local to this machine. Nothing here is ever fetched.",
        "",
        "[project]",
        f"fw = {_q(str(fw))}  # the firmware git repo",
    ]
    if west_topdir:
        L.append(f"west_topdir = {_q(str(west_topdir))}  # the west workspace holding .west/")
    else:
        L.append("# west_topdir = \"/abs/path/to/ws\"  # no .west/ found above fw: set the west workspace here")
    note = "" if sources_exists else " (does not exist yet: create it and drop the documents there)"
    L += [
        f"sources = {_q(sources_rel)}  # where the engineer drops documents, relative to fw{note}",
        "",
        "[mcu]",
        "# part = \"<MPN prefix>\"            # family: picks SVD/RM and finds the MCU on the schematic by MPN prefix",
        "# svd = \"/abs/path/to/part.svd\"    # local file, never fetched",
        "# cubemx_db = \"/abs/path/to/db/mcu\" # optional, the engineer's local install",
        "",
        "# One [[boards]] per schematic PDF; the label is the engineer's name for the board (\"A\", \"B\", \"main\").",
    ]
    L.append("# Schematic PDFs found in sources: " + (", ".join(schematics) if schematics else "none"))
    L += [
        "# [[boards]]",
        "# label = \"A\"",
        "# pdf = \"<file in sources>\"",
        "",
        "# One [[targets]] per firmware build an atlas can be pinned to.",
    ]
    L.append("# Apps found (dirs with prj.conf): " + (", ".join(apps) if apps else "none"))
    L.append("# Zephyr boards defined in this repo: " + (", ".join(zboards) if zboards else "none"))
    L += [
        "# [[targets]]",
        "# name = \"core\"",
        "# board = \"<zephyr board>\"",
        "# app = \"apps/<app>\"",
        "# schematic_boards = [\"A\", \"B\"]    # which schematic boards make up this product",
        "# extra_args = []                    # extra `west build` arguments",
        "",
    ]
    return "\n".join(L)


def init(fw: Path, atlas_dir: Path | None = None, sources_rel: str = DEFAULT_SOURCES) -> tuple[Path, bool]:
    """Write atlas.toml unless it exists. Returns (path, written)."""
    fw = fw.resolve()
    adir = Path(atlas_dir).resolve() if atlas_dir else fw / "embarch" / "atlas"
    path = adir / CONFIG_NAME
    if path.exists():
        return path, False
    sources = fw / sources_rel
    text = render_config(fw, find_west_topdir(fw), sources_rel, sources.is_dir(),
                         _schematic_candidates(sources), _app_candidates(fw), _board_candidates(fw))
    tomllib.loads(text)  # never write a file we cannot read back
    common.write_text(path, text)
    return path, True


def _git_ignored(fw: Path, path: Path) -> bool | None:
    try:
        r = subprocess.run(["git", "-C", str(fw), "check-ignore", "-q", str(path)], capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return {0: True, 1: False}.get(r.returncode)


IMPORT_LINE = "@embarch/atlas/MAP.md"
LOCAL_MEMORY = "CLAUDE.local.md"


def wire_session_map(fw: Path) -> list[str]:
    """Make every Claude Code session in `fw` load the current atlas map, touching no tracked file.

    `<fw>/CLAUDE.local.md` gets the import line (created, or appended to), and the file
    is added to `.git/info/exclude` unless git already ignores it. Returns what changed.
    """
    fw = fw.resolve()
    changed: list[str] = []
    mem = fw / LOCAL_MEMORY
    text = mem.read_text(encoding="utf-8") if mem.is_file() else ""
    if IMPORT_LINE not in text.split():
        head = "" if text else "# Local only (git-excluded). Loads the hardware atlas map every session; written by embarch-atlas.\n"
        sep = "" if not text or text.endswith("\n") else "\n"
        mem.write_text(text + sep + head + IMPORT_LINE + "\n", encoding="utf-8")
        changed.append(f"{mem}: import {IMPORT_LINE}")
    if _git_ignored(fw, mem) is False:
        exclude = fw / ".git" / "info" / "exclude"
        if exclude.parent.is_dir():
            with open(exclude, "a", encoding="utf-8") as f:
                f.write("# added by embarch-atlas: loads embarch/atlas/MAP.md every session\n" + LOCAL_MEMORY + "\n")
            changed.append(f"{exclude}: {LOCAL_MEMORY}")
    return changed


MCP_FILE = ".mcp.json"
MCP_NAME = "embarch-atlas"


def wire_mcp(fw: Path, command: str = "embarch-atlas") -> list[str]:
    """Register the atlas MCP server for sessions in `fw` through a git-excluded `.mcp.json`.

    Merges into an existing `.mcp.json` (other servers are kept), sets `alwaysLoad` so the
    atlas tools skip deferred tool search, and excludes the file unless git ignores it or
    already tracks it (a tracked one is edited in place and reported, never excluded).
    Claude Code asks the engineer to approve a project server on first use.
    """
    fw = fw.resolve()
    changed: list[str] = []
    path = fw / MCP_FILE
    data = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8")) or {}
        except ValueError:
            return [f"{path}: not valid JSON, left untouched"]
    servers = data.setdefault("mcpServers", {})
    want = {"type": "stdio", "command": command,
            "args": ["--atlas-dir", str(fw / "embarch" / "atlas"), "mcp"], "alwaysLoad": True}
    if servers.get(MCP_NAME) != want:
        servers[MCP_NAME] = want
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        changed.append(f"{path}: server {MCP_NAME} (alwaysLoad)")
    tracked = subprocess.run(["git", "-C", str(fw), "ls-files", "--error-unmatch", MCP_FILE],
                             capture_output=True).returncode == 0
    if tracked:
        changed.append(f"{path} is tracked by git: the server entry is a local edit to review")
    elif _git_ignored(fw, path) is False:
        exclude = fw / ".git" / "info" / "exclude"
        if exclude.parent.is_dir():
            with open(exclude, "a", encoding="utf-8") as f:
                f.write("# added by embarch-atlas: the atlas MCP server for local sessions\n" + MCP_FILE + "\n")
            changed.append(f"{exclude}: {MCP_FILE}")
    return changed


# --------------------------------------------------------------------------- status

def git_head(fw: Path) -> tuple[str | None, bool]:
    """(HEAD sha, working tree dirty) of the fw repo; (None, False) when it is not a git repo."""
    try:
        head = subprocess.run(["git", "-C", str(fw), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30)
        if head.returncode != 0:
            return None, False
        dirty = subprocess.run(["git", "-C", str(fw), "status", "--porcelain", "--untracked-files=no"],
                               capture_output=True, text=True, timeout=60)
        return head.stdout.strip(), bool(dirty.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return None, False


def _current_schematic_shas(atlas_dir: Path, cfg: dict[str, Any]) -> dict[str, str | None]:
    """label -> sha256 of the board PDF today (None when the file is gone)."""
    out: dict[str, str | None] = {}
    src = sources_dir(atlas_dir, cfg)
    for b in board_entries(cfg):
        p = Path(b.get("pdf") or "")
        p = p if p.is_absolute() else src / p
        out[b["label"]] = common.sha256_file(p) if p.is_file() else None
    return out


def joined_stamp(atlas: Path, stamp: dict[str, Any]) -> dict[str, Any]:
    """The stamp, with the documents and schematics the atlas's last `join` read (`inputs.json`) in
    place of the build's: a join after a document or schematic change makes the atlas fresh again,
    and `atlas.json` itself is never edited."""
    try:
        inp = common.read_json(atlas / "inputs.json")
    except (OSError, ValueError):
        return stamp
    out = dict(stamp)
    if "sources" in inp:
        out["sources"] = inp["sources"]
    if inp.get("schematics"):
        out["schematics"] = {**(stamp.get("schematics") or {}), **inp["schematics"]}
    if inp.get("missing_boards"):
        out["missing_boards"] = list(inp["missing_boards"])
    return out


def atlas_state(atlas_dir: Path, stamp: dict[str, Any], head: str | None,
                manifest_sha: str | None, schematic_shas: dict[str, str | None]) -> list[str]:
    """Reasons an atlas is stale; empty when it is fresh."""
    why = []
    if head and stamp.get("commit") != head:
        why.append("commit differs from HEAD")
    if "sources" in stamp and stamp.get("sources") != manifest_sha:
        if not manifest_sha:
            why.append("inventory/manifest.json missing")
        elif stamp.get("sources") is None:
            why.append("built before inventory/manifest.json existed")
        else:
            why.append("inventory/manifest.json changed")
    for label, sha in sorted((stamp.get("schematics") or {}).items()):
        if label in schematic_shas and schematic_shas[label] != sha:
            why.append(f"board {label} PDF changed")
    for label in stamp.get("missing_boards") or []:
        why.append(f"board {label} is declared but has no netlist, so the atlas does not cover it "
                   f"(embarch-atlas schematic <pdf> --board {label}, then join)")
    return why


def status(atlas_dir: Path) -> dict[str, Any]:
    """Collect the atlas list and update current.json. Returns what it found."""
    atlas_dir = Path(atlas_dir)
    try:
        cfg = load_config(atlas_dir)
    except FileNotFoundError:
        cfg = {}
    fw = fw_dir(atlas_dir, cfg)
    head, dirty = git_head(fw)
    man = atlas_dir / "inventory" / "manifest.json"
    manifest_sha = common.sources_fingerprint(man)
    sch = _current_schematic_shas(atlas_dir, cfg) if cfg else {}
    rows = []
    for stamp_path in sorted((atlas_dir / "atlases").glob("*/atlas.json")):
        try:
            stamp = joined_stamp(stamp_path.parent, common.read_json(stamp_path))
        except (OSError, ValueError) as e:
            rows.append({"atlas": stamp_path.parent.name, "error": str(e)})
            continue
        rows.append({
            "atlas": stamp_path.parent.name,
            "board": stamp.get("board"),
            "commit": stamp.get("commit"),
            "matches_head": bool(head) and stamp.get("commit") == head,
            "stale": atlas_state(atlas_dir, stamp, head, manifest_sha, sch),
        })
    current = {"commit": head, "matches": [{"atlas": r["atlas"], "board": r["board"], "stale": r["stale"]}
                                           for r in rows if r.get("matches_head")]}
    cur_path = atlas_dir / "current.json"
    old = common.read_json(cur_path) if cur_path.is_file() else None
    if head and old != current:
        common.write_json(cur_path, current)
    if head:
        mark_map(atlas_dir, head, rows)
    return {"fw": str(fw), "head": head, "dirty": dirty, "manifest_sha256": manifest_sha, "atlases": rows,
            "current": current}


STALE_MARK = "> **Stale:**"
MAP_HEAD = re.compile(r"^# (\S+@[0-9a-f]{12}): hardware atlas")


def mark_map(atlas_dir: Path, head: str, rows: list[dict[str, Any]]) -> bool:
    """Keep the session's `<atlas_dir>/MAP.md` honest: while the atlas it describes is stale (HEAD moved
    past its commit, or a source changed), its first line says so and names /atlas-update; the line
    goes again once that atlas is fresh. Only that line changes, so the rest stays byte-stable.
    Returns whether the file was rewritten."""
    path = Path(atlas_dir) / "MAP.md"
    if not path.is_file():
        return False
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    body = lines[1:] if lines and lines[0].startswith(STALE_MARK) else lines
    m = MAP_HEAD.match(body[0]) if body else None
    if not m:
        return False
    row = next((r for r in rows if r.get("atlas") == m.group(1)), None)
    why = (row or {}).get("stale") or ([] if row else ["no stamp for this atlas in atlases/"])
    new = body
    if why:
        new = [f"{STALE_MARK} {'; '.join(why)} (HEAD `{head[:12]}`); this map describes `{m.group(1)}`. "
               f"Say so when you use it, and run `/atlas-update` for the current commit.\n"] + body
    if new == lines:
        return False
    common.write_text(path, "".join(new))
    return True


# --------------------------------------------------------------------------- CLI

def _cmd_init(args) -> int:
    fw = Path(args.fw)
    if not fw.is_dir():
        print(f"init: {fw} is not a directory", file=sys.stderr)
        return 2
    path, written = init(fw, Path(args.atlas_dir) if args.atlas_dir else None, args.sources)
    for line in wire_session_map(fw) + wire_mcp(fw):
        print(f"wired: {line}")
    if not written:
        print(f"{path} exists; left untouched")
        return 0
    print(f"wrote {path}")
    cfg = load_config(path.parent)
    wt = (cfg.get("project") or {}).get("west_topdir")
    print(f"  west_topdir: {wt or 'not found (set it by hand)'}")
    print("  next: set [mcu] part, add one [[boards]] per schematic PDF and one [[targets]] per build")
    if _git_ignored(fw.resolve(), path.parent) is False:
        print(f"  warning: {path.parent} is not git-ignored in {fw}; add it to .git/info/exclude", file=sys.stderr)
    return 0


def _cmd_status(args) -> int:
    adir = common.atlas_dir(args.atlas_dir)
    st = status(adir)
    head = st["head"]
    print(f"atlas dir: {adir}")
    print(f"fw: {st['fw']}  HEAD {head[:12] if head else '(not a git repo)'}{'  (working tree dirty: not in any atlas)' if st['dirty'] else ''}")
    print(f"inventory/manifest.json: {st['manifest_sha256'][:12] if st['manifest_sha256'] else 'missing'}")
    if not st["atlases"]:
        print("no atlases yet (atlases/*/atlas.json)")
    for r in st["atlases"]:
        if "error" in r:
            print(f"  {r['atlas']}: unreadable stamp: {r['error']}")
            continue
        tag = "current" if r["matches_head"] else "       "
        state = "stale: " + "; ".join(r["stale"]) if r["stale"] else "fresh"
        print(f"  {tag} {r['atlas']}  board={r['board']}  {state}")
    if head:
        m = st["current"]["matches"]
        print("current.json: " + (", ".join(x["atlas"] for x in m) if m else "no atlas matches HEAD"))
    return 0


def register(sub, groups) -> None:
    p = sub.add_parser("init", help="write <fw>/embarch/atlas/atlas.toml with defaults (never overwrites)")
    p.add_argument("fw", help="the firmware git repo")
    p.add_argument("--sources", default=DEFAULT_SOURCES, help=f"documents dir, relative to fw (default {DEFAULT_SOURCES})")
    p.set_defaults(func=_cmd_init)
    p = sub.add_parser("status", help="atlases, which one matches HEAD (current.json), which are stale")
    p.set_defaults(func=_cmd_status)

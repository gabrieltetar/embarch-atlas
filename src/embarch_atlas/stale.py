"""Staleness on every answer: is the atlas a lookup reads behind the firmware repo's HEAD?

`project.status` computes it, but only when something runs `status`. A session that never does
answered from a weeks-old atlas with nothing saying so. This module computes the same reasons
(`project.atlas_state`) per call and keeps them keyed on what they depend on: the mtime of
`.git/HEAD` and of the ref it names (plus `packed-refs`), and the stat of the atlas stamp, the
manifest, the board PDFs and `atlas.toml`. While none of those move a call costs a handful of
`stat`s; when HEAD moves it costs one `git rev-parse`.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from . import common

_HEAD: dict[str, tuple[Any, str | None]] = {}
_STALE: dict[tuple[str, str], tuple[Any, str]] = {}


def _stat(p: Path) -> tuple[int, int] | None:
    try:
        s = p.stat()
    except OSError:
        return None
    return (s.st_mtime_ns, s.st_size)


def _gitdirs(fw: Path) -> tuple[Path, Path] | None:
    """(git dir, common dir): they differ in a linked worktree, where refs live in the common one."""
    g = fw / ".git"
    try:
        if g.is_dir():
            return g, g
        if g.is_file():
            t = g.read_text().strip()
            if t.startswith("gitdir:"):
                gd = Path(t[7:].strip())
                gd = gd if gd.is_absolute() else fw / gd
                cd = gd
                try:
                    c = (gd / "commondir").read_text().strip()
                    cd = gd / c if not Path(c).is_absolute() else Path(c)
                except OSError:
                    pass
                return gd, cd
    except OSError:
        pass
    return None


def _git_sig(fw: Path) -> Any:
    dirs = _gitdirs(fw)
    if not dirs:
        return None
    gd, cd = dirs
    sig: list[Any] = [_stat(gd / "HEAD"), _stat(cd / "packed-refs")]
    try:
        txt = (gd / "HEAD").read_text().strip()
    except OSError:
        return tuple(sig)
    if txt.startswith("ref:"):
        ref = txt[4:].strip()
        sig.append(_stat(gd / ref) or _stat(cd / ref))
    return tuple(sig)


def _config(atlas_dir: Path) -> dict[str, Any]:
    from . import project
    try:
        return project.load_config(atlas_dir)
    except (FileNotFoundError, OSError, ValueError):
        return {}


def _fw(atlas_dir: Path, cfg: dict[str, Any]) -> Path:
    from . import project
    return project.fw_dir(atlas_dir, cfg)


def head(atlas_dir: Path) -> str | None:
    """The firmware repo's HEAD sha, or None when it is not a git repo. One `git rev-parse` per move."""
    atlas_dir = Path(atlas_dir)
    fw = _fw(atlas_dir, _config(atlas_dir))
    sig = _git_sig(fw)
    key = str(atlas_dir.resolve())
    hit = _HEAD.get(key)
    if hit and sig is not None and hit[0] == (str(fw), sig):
        return hit[1]
    sha = None
    if sig is not None:
        try:
            r = subprocess.run(["git", "-C", str(fw), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30)
            sha = r.stdout.strip() if r.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            sha = None
    _HEAD[key] = ((str(fw), sig), sha)
    return sha


def reasons(atlas_dir: Path, atlas: Path) -> tuple[list[str], str | None]:
    """(why the atlas is stale, HEAD); the list is empty when it is fresh or HEAD is unknown."""
    from . import project
    atlas_dir = Path(atlas_dir)
    cfg = _config(atlas_dir)
    h = head(atlas_dir)
    man = atlas_dir / "inventory" / "manifest.json"
    boards = project.board_entries(cfg) if cfg else []
    src = project.sources_dir(atlas_dir, cfg) if cfg else atlas_dir
    pdfs = []
    for b in boards:
        p = Path(b.get("pdf") or "")
        pdfs.append(_stat(p if p.is_absolute() else src / p))
    sig = (h, _stat(atlas_dir / "atlas.toml"), _stat(atlas / "atlas.json"), _stat(atlas / "inputs.json"),
           _stat(man), tuple(pdfs))
    key = (str(atlas_dir.resolve()), atlas.name)
    hit = _STALE.get(key)
    if hit and hit[0] == sig:
        return (list(hit[1][0]), h)
    try:
        stamp = project.joined_stamp(atlas, common.read_json(atlas / "atlas.json"))
    except (OSError, ValueError):
        _STALE[key] = (sig, ([],))
        return [], h
    why = project.atlas_state(atlas_dir, stamp, h, common.sources_fingerprint(man),
                              project._current_schematic_shas(atlas_dir, cfg) if cfg else {})
    _STALE[key] = (sig, (why,))
    return list(why), h


def mark(atlas_dir: Path, atlas: Path | None) -> str:
    """`stale=<reasons> (HEAD <sha12>)`, or "" when the atlas is fresh."""
    if atlas is None:
        return ""
    why, h = reasons(atlas_dir, atlas)
    return f"stale={'; '.join(why)} (HEAD {h[:12]})" if why and h else ""

"""The shared datasheet library, and the copy of every file an atlas read (DESIGN.md "Where documents come from").

`[project] library` names one local folder, outside any firmware repo, that the engineer fills by hand. This module
only ever **reads** it: no download, no network call, no write. Three jobs:

- `library_dir`: the setting, or None (unset: nothing here runs and every file is written as before).
- `scan` and `tied`: `inventory` reads the library beside the sources folder, but a library file enters the
  manifest only when a part on the boards or the MCU role is tied to it (a part token, a rung, a confirmation),
  since the others serve other projects. A path, size and mtime index keeps a large library from being parsed again.
- `keep_copy` and `adopt_prepped`: a file an atlas reads past the inventory is copied to
  `<atlas dir>/files/<sha256>/<file name>`, and every later step reads the copy. The tool never deletes one.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from . import answers as answersmod
from . import common, coverage, inventory, project

INDEX = "library-index.json"
INDEX_VERSION = 1
FILES = "files"
COPIED_KINDS = ("schematic", "datasheet", "reference-manual", "errata", "appnote", "other")


def library_dir(atlas_dir: Path, cfg: dict[str, Any]) -> Path | None:
    """`[project] library`, `~` expanded, relative to fw like `sources`; None when unset."""
    raw = ((cfg.get("project") or {}).get("library") or "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    return p if p.is_absolute() else project.fw_dir(atlas_dir, cfg) / p


# ---------------------------------------------------------------- the copy

def copy_rel(sha256: str, name: str) -> str:
    return f"{FILES}/{sha256}/{name}"


def keep_copy(atlas_dir: Path, path: Path, sha256: str | None = None) -> Path:
    """Copy `path` to `<atlas dir>/files/<sha256>/<name>` (once: a copy that is there is kept) and return the copy.
    A file that already is a copy there is returned as it is."""
    atlas_dir, path = Path(atlas_dir).resolve(), Path(path).resolve()
    root = atlas_dir / FILES
    if root in path.parents:
        return path
    sha256 = sha256 or common.sha256_file(path)
    dest = root / sha256 / path.name
    if dest.is_file() and dest.stat().st_size == path.stat().st_size and common.sha256_file(dest) == sha256:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    shutil.copyfile(path, tmp)
    os.replace(tmp, dest)
    return dest


def adopt_prepped(atlas_dir: Path, manifest: dict[str, Any]) -> list[str]:
    """A document prepped before the copy existed: copy its file now, when the file in the manifest still has the
    sha256 `doc.json` records, and point `doc.json` `path` at the copy. Returns the doc ids it moved."""
    atlas_dir = Path(atlas_dir)
    by_sha: dict[str, dict[str, Any]] = {}
    for d in manifest.get("documents") or []:
        if d.get("sha256"):
            by_sha.setdefault(d["sha256"], d)
    moved = []
    for dj in sorted((atlas_dir / "docs").glob("*/doc.json")):
        try:
            meta = common.read_json(dj)
        except (OSError, ValueError):
            continue
        sha = meta.get("sha256")
        have = Path(str(meta.get("path") or ""))
        if not sha or have.parts[:2] == (FILES, sha) and (atlas_dir / have).is_file() or sha not in by_sha:
            continue
        src = inventory.doc_path(atlas_dir, by_sha[sha], manifest)
        if not src.is_file() or common.sha256_file(src) != sha:
            continue
        copy = keep_copy(atlas_dir, src, sha)
        meta["path"] = copy_rel(sha, copy.name)
        common.write_json(dj, meta)
        moved.append(str(meta.get("doc_id") or dj.parent.name))
    return moved


# ---------------------------------------------------------------- the scan

def _load_index(atlas_dir: Path) -> dict[str, Any]:
    try:
        idx = common.read_json(Path(atlas_dir) / "inventory" / INDEX)
        if idx.get("version") == INDEX_VERSION and isinstance(idx.get("files"), dict):
            return idx["files"]
    except (OSError, ValueError, AttributeError):
        pass
    return {}


def scan(atlas_dir: Path, lib: Path, overrides: dict[str, dict[str, Any]], exclude: list[Path] = ()) -> list[dict[str, Any]]:
    """The inventory record of every file in the library, `file` relative to the library. A file whose path, size
    and mtime match the index is not hashed or read again."""
    old = _load_index(atlas_dir)
    new: dict[str, Any] = {}
    out = []
    for p, rel in inventory.list_sources(lib, exclude):
        st = p.stat()
        e = old.get(rel)
        fresh = e and e.get("size") == st.st_size and e.get("mtime_ns") == st.st_mtime_ns and "rec" in e
        sha = e["sha256"] if fresh else common.sha256_file(p)
        override = ((overrides or {}).get(sha[:12]) or {}).get("kind")
        if fresh and e.get("override") == override and inventory.text_cache_path(atlas_dir, sha).is_file():
            rec = e["rec"]
        else:
            rec = inventory.inventory_file(p, rel, overrides, atlas_dir)
        new[rel] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": rec["sha256"], "override": override,
                    "rec": rec}
        out.append(rec)
    if new != old:
        common.write_json(Path(atlas_dir) / "inventory" / INDEX, {"version": INDEX_VERSION, "files": new})
    return [dict(r) for r in out]


def index_shas(atlas_dir: Path) -> set[str]:
    return {e["sha256"] for e in _load_index(atlas_dir).values() if e.get("sha256")}


# ---------------------------------------------------------------- tied to a part or the MCU role

class Ties:
    """What a library file can be tied to: the BOM's parts, the engineer's confirmations, the MCU, the boards' PDFs."""

    def __init__(self, atlas_dir: Path, cfg: dict[str, Any]):
        self.atlas_dir = Path(atlas_dir)
        self.parts = coverage.bom_parts(self.atlas_dir)
        try:
            given = answersmod.load(self.atlas_dir)
        except ValueError:
            given = {}
        docs = [a for a in given.values() if a.get("answer") == "document"]
        self.confirmed_sha = {a["sha256"] for a in docs if a.get("sha256")}
        self.confirmed_file = {a["file"] for a in docs if a.get("file")}
        self.mcu = coverage.norm((cfg.get("mcu") or {}).get("part"))
        self.boards = {Path(b.get("pdf") or "").as_posix() for b in project.board_entries(cfg) if b.get("pdf")}

    def _pages(self, rec: dict[str, Any], path: Path) -> list[str]:
        pages = inventory.read_text_cache(inventory.text_cache_path(self.atlas_dir, rec["sha256"]))
        if pages is None and path.suffix.lower() == ".pdf":
            pages = inventory.pdf_pages_text(path, int(rec.get("pages") or 0))
        return pages or []

    def tied(self, rec: dict[str, Any], path: Path) -> bool:
        kind, rel = rec.get("kind"), rec["file"]
        if kind == "schematic":
            return rel in self.boards
        if kind not in (*coverage.MATCH_KINDS, "appnote", "errata") or rec.get("needs_export"):
            return False
        if rec["sha256"] in self.confirmed_sha or rel in self.confirmed_file:
            return True
        if kind in coverage.DOC_KINDS and any(p.match(coverage.norm(m)) for p in coverage.doc_patterns(rec)
                                              for m in self.parts):
            return True
        pages = None
        if self.parts and kind != "errata":
            pages = self._pages(rec, path)
            t = coverage._Text(rec, pages)
            if any(coverage._match(m, links, t) for m, links in self.parts.items()):
                return True
        if self.mcu and kind in ("datasheet", "reference-manual", "errata", "other"):
            rx = coverage.token_rx(self.mcu)
            pages = self._pages(rec, path) if pages is None else pages
            if (rx.search(Path(rel).stem) or rx.search((rec.get("meta") or {}).get("Title") or "")
                    or any(rx.search(p) for p in pages[:coverage.STEM_PAGES])):
                return True
        return False


def library_documents(atlas_dir: Path, cfg: dict[str, Any], lib: Path, sources_docs: list[dict[str, Any]],
                      overrides: dict[str, dict[str, Any]], exclude: list[Path] = ()) -> list[dict[str, Any]]:
    """The library's records that belong in the manifest: a file identical to one in the sources folder (it then
    becomes its `duplicate_of`, the sources folder winning), and one tied to a part or the MCU role. Each carries
    `library: true`; a name the sources folder also has becomes `library/<name>` and keeps `library_path`."""
    ties = Ties(atlas_dir, cfg)
    have_sha = {d["sha256"] for d in sources_docs}
    taken = {d["file"] for d in sources_docs}
    out = []
    for rec in scan(atlas_dir, lib, overrides, exclude):
        if rec["sha256"] not in have_sha and not ties.tied(rec, lib / rec["file"]):
            continue
        rec["library"] = True
        if rec["file"] in taken:
            rec["library_path"], rec["file"] = rec["file"], f"library/{rec['file']}"
        out.append(rec)
    return out

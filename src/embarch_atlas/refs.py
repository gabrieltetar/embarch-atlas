"""`embarch-atlas refs [--json]`: every section, Kconfig symbol and devicetree node the atlas holds, in one read.

For another tool (the chat linker) that wants all of them: the lookups answer one thing at a time in
markdown. Read-only: no model, no network, no `usage.jsonl` row, nothing written. Sections come from
every translated document, which exist without a built atlas; symbols and nodes from the current atlas.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

from . import common, query, stale

MARKER = 1


def _sections(a: query.Atlas) -> list[dict]:
    out = []
    for d in a.docs:
        meta = a.doc_meta(d)
        for r in query._toc_rows(d):
            if r["id"] is None:
                continue
            out.append({"doc": d.name, "kind": meta.get("kind"), "part": meta.get("part"), "id": r["id"],
                        "title": r["title"], "level": r["level"], "page": r["start"]})
    return out


def _symbols(atlas: Path) -> list[dict]:
    kc = query._json(atlas / "kconfig.json") or {}
    return [{"name": n, "prompt": e.get("p") or None, "type": e.get("t")}
            for n, e in sorted((kc.get("symbols") or {}).items())]


def _nodes(atlas: Path) -> list[dict]:
    dt = query._json(atlas / "dt.json") or {}
    by_path: dict[str, list[str]] = {}
    for alias, path in sorted((dt.get("aliases") or {}).items()):
        by_path.setdefault(path, []).append(alias)
    return [{"path": p, "labels": n.get("labels") or [], "compats": n.get("compats") or [],
             "status": n.get("status", "okay"), "aliases": by_path.get(p, [])}
            for p, n in sorted((dt.get("nodes") or {}).items())]


def collect(atlas_dir: Path | None, atlas_id: str | None = None) -> dict[str, Any]:
    """The `atlas_refs` document for `atlas_dir` (None or a missing folder: no atlas, empty lists)."""
    out: dict[str, Any] = {"atlas_refs": MARKER, "atlas_dir": None, "atlas": None, "commit": None, "stale": [],
                           "sections": [], "symbols": [], "nodes": []}
    if atlas_dir is None or not Path(atlas_dir).is_dir():
        return out
    a = query.Atlas(Path(atlas_dir), atlas_id)
    out["atlas_dir"] = str(a.dir)
    out["sections"] = _sections(a)
    if a.atlas is None:
        return out
    out["atlas"] = a.atlas.name
    out["commit"] = (query._json(a.atlas / "atlas.json") or {}).get("commit")
    try:
        out["stale"] = stale.reasons(a.dir, a.atlas)[0]
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    out["symbols"] = _symbols(a.atlas)
    out["nodes"] = _nodes(a.atlas)
    return out


def cmd_refs(ns: argparse.Namespace) -> int:
    try:
        ad: Path | None = common.atlas_dir(getattr(ns, "atlas_dir", None))
    except (subprocess.CalledProcessError, OSError):
        ad = None                                  # not in a git repo and no --atlas-dir: no atlas
    doc = collect(ad, ns.atlas)
    if ns.json:
        print(json.dumps(doc, ensure_ascii=False))
        return 0
    print(f"atlas: {doc['atlas'] or 'none'}" + (f" ({doc['atlas_dir']})" if doc["atlas_dir"] else "")
          + (f" stale: {'; '.join(doc['stale'])}" if doc["stale"] else ""))
    for k in ("sections", "symbols", "nodes"):
        print(f"{len(doc[k])} {k}")
    return 0


def register(sub, groups) -> None:
    p = sub.add_parser("refs", help="every section, Kconfig symbol and devicetree node the atlas holds")
    p.add_argument("--json", action="store_true", help="the `atlas_refs` document (one JSON object) instead of count lines")
    p.add_argument("--atlas-dir", dest="atlas_dir", default=argparse.SUPPRESS, help="the project's atlas dir")
    p.add_argument("--atlas", help="atlas id (default: current)")
    p.set_defaults(func=cmd_refs)

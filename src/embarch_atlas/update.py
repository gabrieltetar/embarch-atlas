"""`update <target> [commit]`: bump an atlas to another commit, doing only what the diff needs.

The deterministic half of the atlas-update skill as one command. Against the target's previous
atlas it:

1. stops when a translation is behind its source, naming it: a `docs/<id>` prepped from an older
   file, a distillation whose frozen units rest on changed pages (`doc thaw-check`), a board's
   netlist made from an older PDF. Fixing that needs `/atlas-translate-docs` or `schematic`, steps
   this command does not take. A new or changed document with no distillation does not stop it:
   it is listed, with its kind, as added to or changed since the previous atlas;
2. builds the new commit (an existing atlas for it is reused, never edited);
3. joins it;
4. writes the reference manual's worklist and counts the units still to distil;
5. queues the second look, and batches **only the diff**: records that now need a look and were
   not already in the previous atlas's queue — a fact whose checked fields or pages changed, or a
   new one. The previous atlas's backlog is counted, not batched (it was already owed);
6. writes the diff against the previous atlas to `diff-from-<previous>.md` in the new atlas.

It ends by saying what an LLM still has to do: RM units to distil, second-look batches to read.
Every LLM output already frozen is reused by content (translations by page hash, verdicts by the
fields the second look checks), so a bump costs what changed and nothing else.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from . import common
from .common import read_json, read_jsonl, write_json, write_jsonl, write_text


def previous_atlas(ad: Path, target: str, exclude: Path | None = None) -> Path | None:
    """The newest other atlas of `target`, by its stamp's creation time."""
    best = None
    for st in (ad / "atlases").glob(f"{target}@*/atlas.json"):
        if exclude is not None and st.parent.resolve() == exclude.resolve():
            continue
        try:
            created = read_json(st).get("created") or ""
        except (OSError, ValueError):
            continue
        if best is None or created > best[0]:
            best = (created, st.parent)
    return best[1] if best else None


def _distillations(ad: Path) -> dict[str, dict]:
    """doc_id -> doc.json of every `docs/<id>/` that `doc prep` wrote."""
    out = {}
    for f in sorted((ad / "docs").glob("*/doc.json")):
        try:
            out[f.parent.name] = read_json(f)
        except (OSError, ValueError):
            continue
    return out


def distillation_of(entry: dict, dists: dict[str, dict]) -> str | None:
    """The `docs/<id>` made from a manifest file: the same doc_id, else the same content (a doc
    prepped under another `--doc-id`), else the same file name (an older copy of the file)."""
    if entry.get("doc_id") in dists:
        return entry["doc_id"]
    sha, name = entry.get("sha256"), Path(entry.get("file") or "").name
    for d, meta in dists.items():
        if sha and meta.get("sha256") == sha:
            return d
    for d, meta in dists.items():
        if name and Path(meta.get("file") or "").name == name:
            return d
    return None


def _manifest_entries(ad: Path) -> list[dict]:
    man = ad / "inventory" / "manifest.json"
    try:
        docs = read_json(man).get("documents") if man.is_file() else None
    except (OSError, ValueError, AttributeError):
        docs = None
    return [e for e in docs or [] if isinstance(e, dict) and "duplicate_of" not in e]


def stale_translations(ad: Path) -> list[str]:
    """Translations that exist but are behind their source: what stops an update.

    - a `docs/<id>` whose doc.json hash is not the manifest file's (prepped from an older file);
    - a distillation whose newest freeze has units resting on pages that changed (`doc thaw-check`);
      the reference manual's units are the worklist's business (`rm_todo`), not this check's;
    - a board whose `netlist.json` was made from another PDF than the one `atlas.toml` names.
    A document with no distillation is not behind anything: `changes_since` lists it."""
    from . import freeze as F
    from .project import _current_schematic_shas, board_entries, load_config
    why = []
    dists = _distillations(ad)
    for e in _manifest_entries(ad):
        if e.get("kind") == "schematic":
            continue
        d = distillation_of(e, dists)
        if d is None:
            continue
        meta = dists[d]
        if meta.get("sha256") != e.get("sha256"):
            why.append(f"docs/{d} was prepped from an older {e.get('file')}: embarch-atlas doc prep, then doc thaw-check")
            continue
        if meta.get("kind") == "reference-manual" or not meta.get("pages"):
            continue
        rep = F.thaw_check(ad, d)
        if rep.get("entry") and rep["redo"]:
            pages = sorted({p for r in rep["redo"] for p in r["changed"]})
            why.append(f"docs/{d}: {len(rep['redo'])} distilled units rest on changed pages ({F._runs(pages)}): "
                       "re-distil them, then doc check and doc freeze")
    try:
        cfg = load_config(ad)
    except FileNotFoundError:
        cfg = {}
    shas = _current_schematic_shas(ad, cfg) if cfg else {}
    for b in board_entries(cfg):
        nl, now = ad / "boards" / b["label"] / "netlist.json", shas.get(b["label"])
        if now is None or not nl.is_file():
            continue
        try:
            made_from = (read_json(nl).get("source") or {}).get("sha256")
        except (OSError, ValueError):
            made_from = None
        if made_from != now:
            why.append(f"board {b['label']}: netlist.json is from another PDF than {b.get('pdf')}: "
                       f"embarch-atlas schematic {b.get('pdf')} --board {b['label']}")
    return why


def changes_since(ad: Path, prev: Path) -> tuple[list[dict], str]:
    """The documents added, changed or removed since `prev` was joined: [{change, file, kind, doc}],
    `doc` the `docs/<id>` made from the file (None: not distilled); and where the list came from.

    `prev`'s own file list (`inputs.json` `source_files`, written by join) when it has one; else the
    inventory's diff against the manifest before it, when the fingerprint moved since `prev`."""
    from .project import joined_stamp
    man = ad / "inventory" / "manifest.json"
    now = common.source_files(man) or {}
    try:
        inp = read_json(prev / "inputs.json")
    except (OSError, ValueError):
        inp = {}
    if isinstance(inp.get("source_files"), dict):
        was = inp["source_files"]
        moved = {"added": set(now) - set(was), "changed": {f for f in set(now) & set(was) if now[f] != was[f]},
                 "removed": set(was) - set(now)}
        basis = f"{prev.name}'s file list"
    else:
        if joined_stamp(prev, read_json(prev / "atlas.json")).get("sources") == common.sources_fingerprint(man):
            return [], ""
        try:
            d = read_json(man).get("diff") or {}
        except (OSError, ValueError, AttributeError):
            d = {}
        moved = {k: set(d.get(k) or []) for k in ("added", "changed", "removed")}
        basis = f"the last inventory's diff ({prev.name} predates per-file lists)"
    entries = {e.get("file"): e for e in _manifest_entries(ad)}
    dists = _distillations(ad)
    rows = []
    for change in ("added", "changed", "removed"):
        for f in sorted(moved[change]):
            e = entries.get(f) or {}
            rows.append({"change": change, "file": f, "kind": e.get("kind"),
                         "doc": distillation_of(e, dists) if e and change != "removed" else None})
    return rows, basis


def _change_line(r: dict) -> str:
    if r["change"] == "removed":
        return f"removed {r['file']}"
    what = f"docs/{r['doc']}" if r["doc"] else ("netlist: schematic" if r["kind"] == "schematic" else
                                                f"{r['kind'] or 'unknown kind'}, not distilled")
    return f"{r['change']} {r['file']} ({what})"


def rm_todo(ad: Path, atlas: Path) -> dict[str, Any] | None:
    """Write the reference manual's worklist for `atlas`; its totals, or None without an RM."""
    from . import rm as R
    try:
        doc_id = R._rm_doc_arg(ad, None)
    except SystemExit:
        return None
    rmdoc = R.RmDoc.load(ad, doc_id)
    in_use, src = R.load_in_use(atlas)
    wl = R.build_worklist(rmdoc, in_use, R._svd(ad, None), R.default_core(ad), R.frozen_index(ad, rmdoc.doc_id))
    wl["atlas"], wl["in_use"] = str(atlas.resolve()), str(src.resolve())
    write_json(rmdoc.dir / "worklist.json", wl)
    return {"doc": rmdoc.doc_id, **wl["totals"]}


def second_look_diff(ad: Path, atlas: Path, prev: Path | None) -> dict[str, Any]:
    """Queue the second look for `atlas`; batch only the items `prev` did not already owe."""
    from . import secondlook as S
    queue, st = S.build_queue(ad, atlas)
    write_jsonl(atlas / "second_look_queue.jsonl", queue)
    carried = S.carry_frozen(ad, atlas, st["frozen_keys"])
    owed = set()
    pq = prev / "second_look_queue.jsonl" if prev else None
    if pq and pq.is_file():
        owed = {q.get("id") for q in read_jsonl(pq)}
    diff = [q for q in queue if q.get("id") not in owed]
    bs = S.batches(diff, len(diff)) if diff else []
    write_json(atlas / "second_look_batches.json", bs)
    return {"frozen": st["frozen"], "carried": carried, "queued": len(queue), "diff": len(diff),
            "backlog": len(queue) - len(diff), "batches": len(bs), "prev_queue": bool(owed)}


def run(ad: Path, target: str, commit: str = "HEAD", log=print) -> int:
    from .build import BuildError, run_build, target_config
    from . import diff as D, join as J
    cfg = target_config(ad, target)
    prev = previous_atlas(ad, target)
    behind = stale_translations(ad)
    if behind:
        log("stopped: " + "; ".join(behind) + ". Finish the translation (/atlas-translate-docs) or re-run the "
            "schematic, then update again.")
        return 2
    try:
        atlas = run_build(ad, cfg, commit, log=log)
    except BuildError as e:
        log(f"update: build failed: {e}")
        return 1
    if prev and prev.resolve() == atlas.resolve():
        prev = previous_atlas(ad, target, exclude=atlas)
    moved, basis = changes_since(ad, prev) if prev else ([], "")
    if moved:
        log(f"documents since {prev.name}, from {basis}: " + "; ".join(_change_line(r) for r in moved))
    J.run(ad, atlas, log=log)
    rm = rm_todo(ad, atlas)
    sl = second_look_diff(ad, atlas, prev)
    if sl["carried"]:
        # The queue needs the join (which facts the atlas uses); the verdicts it carried over need
        # a join to count in the mismatch report and the MAP.
        J.run(ad, atlas, log=lambda *_: None)
    if prev:
        text = D.render(D.diff(prev, atlas))
        write_text(atlas / f"diff-from-{prev.name}.md", text)
    log("")
    log(f"{atlas.name}: built and joined" + (f"; diff from {prev.name} in diff-from-{prev.name}.md" if prev else ""))
    undistilled = [r["file"] for r in moved if r["change"] != "removed" and not r["doc"] and r["kind"] != "schematic"]
    if undistilled:
        log(f"  new or changed, not distilled: {'; '.join(undistilled)}. /atlas-translate-docs <file> for any the "
            "firmware needs, then update again")
    if rm:
        log(f"  reference manual {rm['doc']}: {rm['frozen']}/{rm['units']} units frozen, {rm['todo_units']} to distil"
            + (" -> /atlas-translate-docs rm-worklist " + atlas.name if rm["todo_units"] else ""))
    log(f"  second look: {sl['frozen']} verdicts reused, {sl['diff']} new or changed facts in {sl['batches']} batches"
        + (f" (second_look_batches.json), {sl['backlog']} already owed before" if sl["prev_queue"] else
           ", no previous queue to tell the diff from the backlog"))
    if sl["diff"]:
        log(f"  next: one blind reader per batch: embarch-atlas second-look prompt {atlas.name} <batch>, then "
            f"compare, record, and join {atlas.name} again")
    return 0


def cmd_update(args) -> int:
    from .build import resolve_atlas_dir
    return run(resolve_atlas_dir(args), args.target, args.commit)


def register(sub, groups) -> None:
    p = sub.add_parser("update", help="bump a target's atlas to a commit: build, join, RM worklist, a "
                                      "second look of the diff only, and the diff")
    p.add_argument("target")
    p.add_argument("commit", nargs="?", default="HEAD")
    p.set_defaults(func=cmd_update)


if __name__ == "__main__":  # pragma: no cover
    sys.exit("run as `embarch-atlas update`")

"""`update <target> [commit]`: bump an atlas to another commit, doing only what the diff needs.

The deterministic half of the atlas-update skill as one command. Against the target's previous
atlas it:

1. stops when a document or a board schematic changed since then, naming them: that needs
   `/atlas-translate-docs` first, an LLM step this command does not take;
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


def source_changes(ad: Path, prev: Path) -> list[str]:
    """What changed in the documents or the schematics since `prev` was built."""
    from .project import _current_schematic_shas, atlas_state, load_config
    try:
        cfg = load_config(ad)
    except FileNotFoundError:
        cfg = {}
    manifest = common.sources_fingerprint(ad / "inventory" / "manifest.json")
    sch = _current_schematic_shas(ad, cfg) if cfg else {}
    return [w for w in atlas_state(ad, read_json(prev / "atlas.json"), None, manifest, sch)
            if not w.startswith("commit")]


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
    if prev:
        changed = source_changes(ad, prev)
        if changed:
            log(f"stopped: since {prev.name} " + "; ".join(changed) + ". Translate the changed documents "
                "(/atlas-translate-docs) or re-run the schematic, then update again.")
            return 2
    try:
        atlas = run_build(ad, cfg, commit, log=log)
    except BuildError as e:
        log(f"update: build failed: {e}")
        return 1
    if prev and prev.resolve() == atlas.resolve():
        prev = previous_atlas(ad, target, exclude=atlas)
    J.run(ad, atlas, log=log)
    rm = rm_todo(ad, atlas)
    sl = second_look_diff(ad, atlas, prev)
    if prev:
        text = D.render(D.diff(prev, atlas))
        write_text(atlas / f"diff-from-{prev.name}.md", text)
    log("")
    log(f"{atlas.name}: built and joined" + (f"; diff from {prev.name} in diff-from-{prev.name}.md" if prev else ""))
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

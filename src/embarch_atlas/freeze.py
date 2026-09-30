"""`doc freeze <doc_id>` and `doc thaw-check <doc_id>`: frozen LLM output of a datasheet or a PRD.

DESIGN.md principle 7: LLM outputs are frozen by content hash; a re-run re-verifies and does not
re-ask the model, and only changed pages go back to it. The reference manual has its own verb for
this (`mcu rm-freeze`); this is the same scheme for every other document.

A unit is one piece of LLM-written output and the pages it rests on:

  rec:<id>           one LLM record of records/*.jsonl (deterministic producers' records are not LLM
                     output: regparse.jsonl and every record with a deterministic prov are left out);
                     its pages are its `p`
  prose:s/<file>.md  a tier-2 file's prose: the file with the bodies of its rendered atlas:table
                     blocks removed (the renderer rewrites those); its pages are the pages it cites.
                     An s/invariants.md the renderer wrote and nobody edited is not LLM output
  card               card.md; its pages are the pages it cites

A unit with no page (no `p`, no cite) rests on every page.

The key of a unit is the RM worklist's (`rm.unit_key`): sha256 over {"sec": <unit id>, "intro":
false, "procedure": <procedure version>, "pages": [rm.page_sha of each page's text]}, so both kinds
of frozen output use one scheme. `doc freeze` writes one entry, `cache/llm/<key>.json`, whose key
covers the procedure, the model, the document and every unit key; it holds each unit's key, pages,
content and content hash, and the last `doc check` result.

`doc thaw-check` recomputes every frozen unit's key from the document's current pages/ (after a new
`doc prep`): a unit whose pages did not change is kept, one with a changed or missing page goes back
to the LLM. `--from <doc_id>` checks another document's freeze (the previous revision) against this
one's pages, and `--restore` copies its kept units into this document where they are missing.
"""
from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path
from typing import Any

from . import common, records as R
from .check import cited_pages
from .pdftext import page_file
from .render import END, INVARIANTS, START, invariants_stub
from .rm import page_sha

PROCEDURES = {"datasheet": "datasheet-procedure/1", "prd": "prd-procedure/1"}
PRODUCER_FILES = {"regparse.jsonl"}
ENTRY_KIND = "doc-distillation"


def procedure(kind: str | None) -> str:
    return PROCEDURES.get(kind or "datasheet", PROCEDURES["datasheet"])


def unit_key(sec: str, page_shas: list[str], proc: str, intro: bool = False) -> str:
    """rm.unit_key's scheme: the unit id, the hashes of its pages' text and the procedure version."""
    payload = {"sec": sec, "intro": intro, "procedure": proc, "pages": page_shas}
    return common.sha256_text(json.dumps(payload, sort_keys=True))


# ---------------------------------------------------------------- the document's pages and units

def page_shas(docdir: Path) -> dict[int, str]:
    meta = common.read_json(Path(docdir) / "doc.json")
    out = {}
    for p in range(1, int(meta["pages"]) + 1):
        f = page_file(docdir, p)
        if f.exists():
            out[p] = page_sha(f.read_text(encoding="utf-8"))
    return out


def prose(text: str) -> str:
    """A tier-2 file without the bodies of its rendered blocks: what the LLM wrote."""
    out, inside = [], False
    for ln in text.split("\n"):
        if START.match(ln):
            inside = True
            out.append(ln)
        elif END.match(ln):
            inside = False
            out.append(ln)
        elif not inside:
            out.append(ln)
    return "\n".join(out)


def _record_content(rec: dict) -> dict:
    return {k: v for k, v in rec.items() if k != "prov"}


def _hash(content: Any) -> str:
    if isinstance(content, str):
        return common.sha256_text(content)
    return common.sha256_text(json.dumps(content, sort_keys=True, ensure_ascii=False))


def units(docdir: Path, n_pages: int) -> dict[str, dict]:
    """unit id -> {type, file, pages, content, sha256} for every piece of LLM output of a document."""
    docdir = Path(docdir)
    every = list(range(1, n_pages + 1))
    out: dict[str, dict] = {}
    for fname, rec in R.load_records(docdir):
        if fname in PRODUCER_FILES or rec.get("prov") not in R.LLM or not rec.get("id"):
            continue
        uid, k = f"rec:{rec['id']}", 2
        while uid in out:
            uid, k = f"rec:{rec['id']}#{k}", k + 1
        content = _record_content(rec)
        out[uid] = {"type": "record", "file": f"records/{fname}", "pages": sorted(set(R.pages(rec))) or every,
                    "content": content, "sha256": _hash(content)}
    for f in sorted((docdir / "s").glob("*.md")) if (docdir / "s").is_dir() else []:
        text = prose(f.read_text(encoding="utf-8"))
        if f.name == INVARIANTS and text == prose(invariants_stub(docdir)):
            continue                     # the renderer wrote it, not the LLM
        rel = f"s/{f.name}"
        out[f"prose:{rel}"] = {"type": "prose", "file": rel, "pages": sorted(cited_pages(text)) or every,
                               "content": text, "sha256": _hash(text)}
    if (docdir / "card.md").exists():
        text = (docdir / "card.md").read_text(encoding="utf-8")
        out["card"] = {"type": "card", "file": "card.md", "pages": sorted(cited_pages(text)) or every,
                       "content": text, "sha256": _hash(text)}
    return out


def _key_for(uid: str, pages: list[int], shas: dict[int, str], proc: str) -> str:
    return unit_key(uid, [shas.get(p, "missing") for p in pages], proc)


# ---------------------------------------------------------------- freeze

def _check_state(docdir: Path) -> dict:
    cj = docdir / "checks.json"
    if not cj.exists():
        return {"ran": False}
    rep = common.read_json(cj)
    newest = max((f.stat().st_mtime for f in [*(docdir / "records").glob("*.jsonl"), *(docdir / "s").glob("*.md"),
                                               docdir / "card.md"] if f.exists()), default=0)
    s = rep.get("summary", {})
    return {"ran": True, "exit": s.get("exit"), "failed": s.get("failed"), "verified": s.get("verified"),
            "fresh": cj.stat().st_mtime >= newest,
            "status": {x["id"]: x["status"] for x in rep.get("records", []) if x.get("prov") in R.LLM}}


def freeze(atlas: Path, doc_id: str, model: str | None = None, note: str | None = None) -> tuple[Path, dict]:
    docdir = Path(atlas) / "docs" / doc_id
    meta = common.read_json(docdir / "doc.json")
    if meta.get("kind") == "reference-manual":
        raise SystemExit(f"{doc_id} is a reference manual: freeze its work items with `mcu rm-freeze`")
    proc = procedure(meta.get("kind"))
    shas = page_shas(docdir)
    us = units(docdir, int(meta["pages"]))
    if not us:
        raise SystemExit(f"{doc_id}: no LLM-written records, s/ prose or card.md to freeze")
    state = _check_state(docdir)
    status = state.pop("status", {})
    for uid, u in us.items():
        u["key"] = _key_for(uid, u["pages"], shas, proc)
        if u["type"] == "record" and u["content"]["id"] in status:
            u["check"] = status[u["content"]["id"]]
    cited = sorted({p for u in us.values() for p in u["pages"]})
    entry = {
        "kind": ENTRY_KIND, "doc": doc_id, "doc_kind": meta.get("kind") or "datasheet", "procedure": proc,
        "model": model, "pdf_sha256": meta.get("sha256"), "pages": int(meta["pages"]),
        "page_sha": {str(p): shas.get(p, "missing") for p in cited},
        "units": us, "check": state,
        "created": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        **({"note": note} if note else {}),
    }
    key = common.sha256_text(json.dumps({"procedure": proc, "model": model, "doc": doc_id,
                                         "units": sorted(u["key"] for u in us.values())}, sort_keys=True))
    path = Path(atlas) / "cache" / "llm" / f"{key}.json"
    common.write_json(path, entry)
    return path, entry


# ---------------------------------------------------------------- thaw-check

def frozen_entries(atlas: Path, doc_id: str) -> list[tuple[Path, dict]]:
    """This document's frozen entries, newest first."""
    cache = Path(atlas) / "cache" / "llm"
    out = []
    for f in sorted(cache.glob("*.json")) if cache.is_dir() else []:
        try:
            e = common.read_json(f)
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(e, dict) and e.get("kind") == ENTRY_KIND and e.get("doc") == doc_id:
            out.append((f, e))
    out.sort(key=lambda fe: (fe[1].get("created", ""), fe[0].stat().st_mtime), reverse=True)
    return out


def _runs(ps: list[int]) -> str:
    ps = sorted(set(ps))
    out, i = [], 0
    while i < len(ps):
        j = i
        while j + 1 < len(ps) and ps[j + 1] == ps[j] + 1:
            j += 1
        out.append(f"p{ps[i]}" if i == j else f"p{ps[i]}-{ps[j]}")
        i = j + 1
    return ", ".join(out)


def thaw_check(atlas: Path, doc_id: str, from_doc: str | None = None) -> dict:
    """Which frozen units of `from_doc` (default: this document) are still valid against this
    document's current pages, and which must go back to the LLM."""
    docdir = Path(atlas) / "docs" / doc_id
    meta = common.read_json(docdir / "doc.json")
    src = from_doc or doc_id
    entries = frozen_entries(atlas, src)
    if not entries:
        return {"doc": doc_id, "from": src, "entry": None}
    path, e = entries[0]
    proc = e.get("procedure") or procedure(meta.get("kind"))
    shas = page_shas(docdir)
    frozen_sha = {int(p): h for p, h in e.get("page_sha", {}).items()}
    changed = sorted(p for p, h in frozen_sha.items() if shas.get(p, "missing") != h)
    current = units(docdir, int(meta["pages"])) if from_doc in (None, doc_id) else {}
    kept, redo, edited, gone = [], [], [], []
    for uid, u in sorted(e["units"].items()):
        if _key_for(uid, u["pages"], shas, proc) != u["key"]:
            redo.append({"unit": uid, "file": u["file"], "pages": u["pages"],
                         "changed": [p for p in u["pages"] if shas.get(p, "missing") != frozen_sha.get(p)]})
            continue
        kept.append({"unit": uid, "file": u["file"], "type": u["type"]})
        if from_doc in (None, doc_id):
            cur = current.get(uid)
            if cur is None:
                gone.append(uid)
            elif cur["sha256"] != u["sha256"]:
                edited.append(uid)
    unfrozen = sorted(set(current) - set(e["units"]))
    by_type: dict[str, int] = {}
    for k in kept:
        by_type[k["type"]] = by_type.get(k["type"], 0) + 1
    return {"doc": doc_id, "from": src, "entry": path.name, "created": e.get("created"), "model": e.get("model"),
            "procedure": proc, "superseded": len(entries) - 1, "pdf_unchanged": e.get("pdf_sha256") == meta.get("sha256"),
            "pages_changed": changed, "kept": kept, "kept_by_type": by_type, "redo": redo,
            "edited": edited, "gone": gone, "unfrozen": unfrozen}


def restore(atlas: Path, doc_id: str, from_doc: str, report: dict) -> list[str]:
    """Copy the kept units of `from_doc`'s freeze into `doc_id` where they are missing: records get
    this document's `doc` and lose `prov` (doc check sets it); prose and card files are written only
    when the file does not exist (doc render fills their blocks). Returns what was written."""
    docdir = Path(atlas) / "docs" / doc_id
    path = Path(atlas) / "cache" / "llm" / report["entry"]
    e = common.read_json(path)
    have = {r.get("id") for _, r in R.load_records(docdir)}
    add: dict[str, list[dict]] = {}
    written = []
    for k in report["kept"]:
        u = e["units"][k["unit"]]
        if u["type"] == "record":
            rec = dict(u["content"], doc=doc_id)
            if rec["id"] in have:
                continue
            add.setdefault(Path(u["file"]).name, []).append(rec)
        else:
            f = docdir / u["file"]
            if not f.exists():
                common.write_text(f, u["content"])
                written.append(u["file"])
    for name, recs in sorted(add.items()):
        f = R.records_dir(docdir) / name
        old = list(common.read_jsonl(f)) if f.exists() else []
        common.write_jsonl(f, old + recs)
        written.append(f"records/{name} (+{len(recs)})")
    return written


# ---------------------------------------------------------------- CLI

def cmd_freeze(args) -> int:
    atlas = common.atlas_dir(args.atlas_dir)
    if not (atlas / "docs" / args.doc_id / "doc.json").exists():
        print(f"doc freeze: no docs/{args.doc_id}/doc.json; run doc prep first", file=sys.stderr)
        return 2
    path, e = freeze(atlas, args.doc_id, args.model, args.note)
    n: dict[str, int] = {}
    for u in e["units"].values():
        n[u["type"]] = n.get(u["type"], 0) + 1
    st = e["check"]
    chk = ("no doc check yet" if not st.get("ran") else
           f"last doc check exit {st['exit']}, {st['failed']} failed" + ("" if st["fresh"] else
                                                                       " (older than the files: re-run doc check)"))
    print(f"frozen {args.doc_id}: {n.get('record', 0)} records, {n.get('prose', 0)} s/ files, "
          f"{n.get('card', 0)} card over {len(e['page_sha'])} pages; {chk} -> cache/llm/{path.name}")
    return 0


def cmd_thaw_check(args) -> int:
    atlas = common.atlas_dir(args.atlas_dir)
    if not (atlas / "docs" / args.doc_id / "doc.json").exists():
        print(f"doc thaw-check: no docs/{args.doc_id}/doc.json; run doc prep first", file=sys.stderr)
        return 2
    rep = thaw_check(atlas, args.doc_id, args.from_doc)
    if rep["entry"] is None:
        print(f"doc thaw-check: no frozen output for {rep['from']} in cache/llm/ (run doc freeze)", file=sys.stderr)
        return 2
    if args.restore:
        if rep["from"] == args.doc_id:
            print("doc thaw-check: --restore needs --from <another doc_id>", file=sys.stderr)
            return 2
        rep["restored"] = restore(atlas, args.doc_id, rep["from"], rep)
    if args.json:
        print(json.dumps(rep, indent=2, sort_keys=True))
        return 1 if rep["redo"] else 0
    kb = rep["kept_by_type"]
    print(f"== {rep['doc']}: frozen {rep['entry']} ({rep['created']}, model {rep['model']}, {rep['procedure']})"
          + (f" from {rep['from']}" if rep["from"] != rep["doc"] else ""))
    print(f"pages changed since the freeze: {_runs(rep['pages_changed']) or 'none'}"
          + ("; the PDF is unchanged" if rep["pdf_unchanged"] else ""))
    print(f"kept {len(rep['kept'])} units ({kb.get('record', 0)} records, {kb.get('prose', 0)} s/ prose, "
          f"{kb.get('card', 0)} card): their pages are unchanged")
    print(f"redo {len(rep['redo'])} units: a page they rest on changed")
    for r in rep["redo"][:args.limit]:
        print(f"   {r['unit']} ({r['file']}) changed {_runs(r['changed'])}")
    if len(rep["redo"]) > args.limit:
        print(f"   ... {len(rep['redo']) - args.limit} more (--json lists all)")
    if rep["edited"]:
        print(f"edited since the freeze: {len(rep['edited'])} kept units ({', '.join(rep['edited'][:5])}"
              f"{' ...' if len(rep['edited']) > 5 else ''}): doc check, then doc freeze again")
    if rep["gone"]:
        print(f"frozen but no longer in the document: {len(rep['gone'])} ({', '.join(rep['gone'][:5])})")
    if rep["unfrozen"]:
        print(f"not frozen yet: {len(rep['unfrozen'])} units")
    for w in rep.get("restored", []):
        print(f"restored {w}")
    return 1 if rep["redo"] else 0


def register(sub, groups) -> None:
    g = common.verb_group(sub, groups, "doc", "per-document steps: prep, check, render, regparse")
    p = g.add_parser("freeze", help="record the LLM-written records, s/ prose and card in cache/llm/, keyed by page content")
    p.add_argument("doc_id")
    p.add_argument("--model", help="the model that wrote them")
    p.add_argument("--note", help="free text kept in the cache entry")
    p.set_defaults(func=cmd_freeze)
    p = g.add_parser("thaw-check", help="after a new doc prep: which frozen units are still valid, which go back to the LLM")
    p.add_argument("doc_id")
    p.add_argument("--from", dest="from_doc", help="check another document's freeze (the previous revision) against this one")
    p.add_argument("--restore", action="store_true", help="with --from: copy the kept units into this document where missing")
    p.add_argument("--json", action="store_true", help="print the full report as JSON")
    p.add_argument("--limit", type=int, default=20, help="redo units to list (default 20)")
    p.set_defaults(func=cmd_thaw_check)


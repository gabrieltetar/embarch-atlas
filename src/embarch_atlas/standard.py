"""`doc sections <doc_id>`: a standard's worklist, the sections someone asked to distil.

A standard (DESIGN.md "Standards and specifications") is hundreds of pages, and a firmware project implements a
few of its sections, so it is distilled lazily, as the reference manual is: `doc prep` keeps every page's text,
and only the sections in `docs/<doc>/worklist.json` are distilled.

- `doc sections <doc> --add <sec>[,<sec>...] --why <text>` adds sections (a toc id or title), with why;
  adding one again only adds the reason.
- `doc sections <doc>` lists them with pages, tokens and status: `todo` (no frozen `s/<sec>.md`), `frozen`
  (its newest freeze holds the section's file and none of its pages changed since), `redo` (frozen, and a page
  it rests on changed). A section's key is the RM's unit key over the section id, its pages' text hashes and
  the procedure version, so a re-run on unchanged pages redoes nothing.
- `doc sections <doc> --suggest [--used <usage log>]` ranks the sections not in the worklist by how often
  sessions' lookups landed in them: `find_text` hits and `doc_section tier=text` pages of that document in the
  usage log (`<atlas dir>/usage.jsonl`), each credited to the deepest toc section holding its page. The
  engineer adds sections from measured use instead of a guess.

Every listing also rewrites the card's generated `sections` block (between `<!-- atlas:table sections -->` and
`<!-- atlas:end -->`): the distilled sections, and `doc_toc` / `find_text` for every other one. Freezing reads
the card without that block's body, so the block never thaws a frozen card.
"""
from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path
from typing import Any

from . import common

PROCEDURE = "standard-procedure/1"
WORKLIST = "worklist.json"
CARD_BLOCK = "<!-- atlas:table sections -->"
CARD_END = "<!-- atlas:end -->"


def toc_rows(docdir: Path) -> list[dict]:
    """The toc as rows: id, title, level, start, end, tokens (sections with no pages are left out)."""
    try:
        secs = common.read_json(Path(docdir) / "toc.json").get("sections") or []
    except (OSError, ValueError, AttributeError):
        return []
    out = []
    for s in secs:
        p = s.get("pages") or [None, None]
        if isinstance(p[0], int):
            out.append({"id": str(s.get("id")), "title": s.get("title") or "", "level": int(s.get("level") or 1),
                        "start": p[0], "end": p[-1] if isinstance(p[-1], int) else p[0], "tokens": s.get("tokens")})
    return out


def holder(rows: list[dict], page: int) -> dict | None:
    """The deepest, latest-starting toc section holding `page` (find_text's rule)."""
    cover = [r for r in rows if r["start"] <= page <= r["end"]]
    return max(cover, key=lambda r: (r["level"], r["start"])) if cover else None


def find_row(rows: list[dict], sec: str) -> dict | None:
    """A section by its id, else by its title (case and punctuation ignored)."""
    import re
    norm = lambda s: re.sub(r"[^a-z0-9]+", "", (s or "").lower())
    return next((r for r in rows if r["id"] == sec), None) or next(
        (r for r in rows if norm(sec) and norm(r["title"]) == norm(sec)), None)


def section_key(docdir: Path, row: dict, shas: dict[int, str] | None = None) -> str:
    from . import freeze
    shas = shas if shas is not None else freeze.page_shas(docdir)
    pages = list(range(row["start"], row["end"] + 1))
    return freeze.unit_key(f"sec:{row['id']}", [shas.get(p, "missing") for p in pages], PROCEDURE)


def section_file(row_id: str) -> str:
    return f"s/{row_id}.md"


def statuses(atlas: Path, doc_id: str) -> dict[str, str]:
    """section file (`s/<id>.md`) -> frozen | redo, from the document's newest freeze."""
    from . import freeze
    entries = freeze.frozen_entries(atlas, doc_id)
    if not entries:
        return {}
    docdir = Path(atlas) / "docs" / doc_id
    _, e = entries[0]
    proc = e.get("procedure") or freeze.procedure("standard")
    shas = freeze.page_shas(docdir)
    out = {}
    for uid, u in (e.get("units") or {}).items():
        if uid.startswith("prose:s/"):
            same = freeze._key_for(uid, u["pages"], shas, proc) == u["key"]
            out[uid[len("prose:"):]] = "frozen" if same else "redo"
    return out


def load(docdir: Path) -> dict:
    try:
        wl = common.read_json(Path(docdir) / WORKLIST)
    except (OSError, ValueError):
        wl = {}
    return wl if isinstance(wl, dict) else {}


def listing(atlas: Path, doc_id: str) -> list[dict]:
    """The worklist's sections with pages, tokens, key and status (todo, frozen, redo)."""
    from . import freeze
    docdir = Path(atlas) / "docs" / doc_id
    rows = {r["id"]: r for r in toc_rows(docdir)}
    st = statuses(atlas, doc_id)
    shas = freeze.page_shas(docdir)
    out = []
    for s in load(docdir).get("sections") or []:
        row = rows.get(s["id"]) or {"id": s["id"], "title": s.get("title", ""), "start": s["pages"][0],
                                     "end": s["pages"][-1], "level": 1, "tokens": s.get("tokens")}
        f = section_file(s["id"])
        status = st.get(f) if (docdir / f).is_file() else None
        out.append({**s, "title": row["title"], "pages": [row["start"], row["end"]], "tokens": row.get("tokens"),
                    "key": section_key(docdir, row, shas), "status": status or "todo"})
    return out


def add(atlas: Path, doc_id: str, secs: list[str], why: str) -> tuple[list[str], list[str]]:
    """Add sections to the worklist (a reason is appended to one already there). (added, unknown)."""
    docdir = Path(atlas) / "docs" / doc_id
    rows = toc_rows(docdir)
    wl = load(docdir)
    have = {s["id"]: s for s in wl.get("sections") or []}
    added, unknown = [], []
    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for sec in secs:
        row = find_row(rows, sec)
        if row is None:
            unknown.append(sec)
            continue
        cur = have.get(row["id"])
        if cur is None:
            have[row["id"]] = {"id": row["id"], "title": row["title"], "pages": [row["start"], row["end"]],
                               "tokens": row.get("tokens"), "why": [why], "added": now}
            added.append(row["id"])
        elif why not in cur.get("why", []):
            cur.setdefault("why", []).append(why)
    order = {r["id"]: i for i, r in enumerate(rows)}
    wl = {"doc": doc_id, "procedure": PROCEDURE,
          "sections": sorted(have.values(), key=lambda s: order.get(s["id"], 10**6))}
    common.write_json(docdir / WORKLIST, wl)
    return added, unknown


def suggest(atlas: Path, doc_id: str, log: Path, level: int | None = None) -> list[dict]:
    """Sections not in the worklist, ranked by the lookups that landed in them: [{id, title, pages, tokens, calls}].
    `level` credits each hit to its ancestor at that toc level instead of the deepest section."""
    docdir = Path(atlas) / "docs" / doc_id
    rows = toc_rows(docdir)
    by_id = {r["id"]: r for r in rows}
    asked = {s["id"] for s in load(docdir).get("sections") or []}
    counts: dict[str, int] = {}
    try:
        lines = Path(log).read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("doc") != doc_id or row.get("tool") not in ("find_text", "doc_section"):
            continue
        page = row.get("page")
        r = by_id.get(str(row.get("section"))) if row.get("section") is not None else None
        if r is None and isinstance(page, int):
            r = holder(rows, page)
        if r is None or r["id"] in asked or (docdir / section_file(r["id"])).is_file():
            continue                             # a hit on a section asked for or distilled already
        if level is not None and r["level"] > level:
            up = [x for x in rows if x["level"] == level and x["start"] <= r["start"] <= x["end"]]
            r = max(up, key=lambda x: x["start"]) if up else r
        counts[r["id"]] = counts.get(r["id"], 0) + 1
    out = [{"id": i, "title": by_id[i]["title"], "pages": [by_id[i]["start"], by_id[i]["end"]],
            "tokens": by_id[i].get("tokens"), "calls": n}
           for i, n in counts.items() if i not in asked and not (docdir / section_file(i)).is_file()]
    order = {r["id"]: k for k, r in enumerate(rows)}
    out.sort(key=lambda s: (-s["calls"], order.get(s["id"], 10**6)))
    return out


def _pages(p: list[int]) -> str:
    return f"p.{p[0]}" + (f"-{p[1]}" if p[1] != p[0] else "")


def card_block(doc_id: str, secs: list[dict]) -> list[str]:
    done = [s for s in secs if s["status"] == "frozen"]
    other = [s for s in secs if s["status"] != "frozen"]
    out = [CARD_BLOCK, "Distilled sections (`worklist.json`, `embarch-atlas doc sections`): "
           + ("; ".join(f"`{s['id']}` {s['title']} ({_pages(s['pages'])}, `s/{s['id']}.md`)" for s in done) or "none yet")
           + "."]
    if other:
        out.append("Asked for, not distilled yet: " + "; ".join(f"`{s['id']}` {s['title']} ({s['status']})" for s in other) + ".")
    out += [f"Every other section is not distilled: `doc_toc doc={doc_id}` for the outline, "
            f"`find_text doc={doc_id} pattern=<words>` to search its pages, then cite the page you read.", CARD_END]
    return out


def refresh_card(atlas: Path, doc_id: str) -> bool:
    """Rewrite the card's generated `sections` block (append it when there is none). False without a card."""
    card = Path(atlas) / "docs" / doc_id / "card.md"
    if not card.is_file():
        return False
    lines = card.read_text(encoding="utf-8").split("\n")
    block = card_block(doc_id, listing(atlas, doc_id))
    if CARD_BLOCK in lines:
        i = lines.index(CARD_BLOCK)
        j = next((k for k in range(i + 1, len(lines)) if lines[k].strip() == CARD_END), None)
        new = lines[:i] + block + (lines[j + 1:] if j is not None else [])
    else:
        while lines and not lines[-1].strip():
            lines.pop()
        new = lines + ["", "## Sections", ""] + block + [""]
    text = "\n".join(new)
    if text != card.read_text(encoding="utf-8"):
        card.write_text(text, encoding="utf-8")
    return True


def is_standard(atlas: Path, doc_id: str) -> bool:
    try:
        return common.read_json(Path(atlas) / "docs" / doc_id / "doc.json").get("kind") == "standard"
    except (OSError, ValueError, AttributeError):
        return False


# ---------------------------------------------------------------- CLI

def cmd_sections(args) -> int:
    atlas = common.atlas_dir(args.atlas_dir)
    docdir = atlas / "docs" / args.doc_id
    if not (docdir / "doc.json").is_file():
        print(f"doc sections: no {docdir / 'doc.json'}: run `embarch-atlas doc prep` first", file=sys.stderr)
        return 2
    if not is_standard(atlas, args.doc_id):
        print(f"doc sections: {args.doc_id} is not a standard (doc.json kind); the worklist is a standard's "
              "(a reference manual's is `mcu rm-sections`)", file=sys.stderr)
        return 2
    if not toc_rows(docdir):
        print(f"doc sections: {args.doc_id} has no toc.json sections to choose from", file=sys.stderr)
        return 2
    if args.add:
        if not args.why:
            print("doc sections: --add needs --why (who asked, or what the firmware implements)", file=sys.stderr)
            return 2
        added, unknown = add(atlas, args.doc_id, [s.strip() for s in args.add.split(",") if s.strip()], args.why)
        if unknown:
            print(f"doc sections: not in the toc: {', '.join(unknown)} (doc_toc doc={args.doc_id} for the outline)",
                  file=sys.stderr)
        if added:
            print(f"added {', '.join(added)}")
    if args.suggest:
        log = Path(args.used) if args.used else atlas / "usage.jsonl"
        sug = suggest(atlas, args.doc_id, log, args.level)
        print(f"{args.doc_id}: sections not asked for, by lookups that landed in them ({log}):")
        for s in sug[:args.top]:
            print(f"  {s['calls']:5} calls  {s['id']} {s['title']} ({_pages(s['pages'])}, {s['tokens']} tok)")
        if not sug:
            print("  (no lookup in the log landed in a section that is not asked for)")
    secs = listing(atlas, args.doc_id)
    refresh_card(atlas, args.doc_id)
    if not args.suggest or args.add:
        print(f"{args.doc_id}: {len(secs)} sections in {WORKLIST} ({PROCEDURE})")
        for s in secs:
            print(f"  {s['status']:6} {s['id']} {s['title']} ({_pages(s['pages'])}, {s['tokens']} tok): {'; '.join(s.get('why') or [])}")
    return 1 if args.add and unknown else 0


def register(sub, groups) -> None:
    g = common.verb_group(sub, groups, "doc", "per-document steps: prep, check, render, regparse")
    p = g.add_parser("sections", help="a standard's worklist: add the sections asked for, list them with pages, "
                                      "tokens and status (todo, frozen, redo), or suggest sections from use")
    p.add_argument("doc_id")
    p.add_argument("--add", metavar="SEC[,SEC...]", help="sections to add, by toc id or title")
    p.add_argument("--why", help="with --add: who asked, or what the firmware implements")
    p.add_argument("--suggest", action="store_true", help="rank the sections not asked for by the lookups that "
                                                          "landed in them (find_text, doc_section tier=text)")
    p.add_argument("--used", metavar="LOG", help="with --suggest: the usage log (default <atlas dir>/usage.jsonl)")
    p.add_argument("--level", type=int, help="with --suggest: credit hits to their section at this toc level")
    p.add_argument("--top", type=int, default=20, help="with --suggest: how many to list (default 20)")
    p.set_defaults(func=cmd_sections)

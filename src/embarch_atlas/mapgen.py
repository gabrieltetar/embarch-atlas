"""MAP.md: the tier-0 index an agent loads every session (<= 5K tokens, byte-stable).

llms.txt-shaped: what the atlas is pinned to, the protocol line, where the
pin map and the checks are, the MCU and the peripherals in use (with the RM
chapters when a worklist exists), the boards and their interfaces, and the parts
on the boards with their document ids and card paths. No timestamps: the stamp
lives in atlas.json, so an unchanged atlas renders the same bytes.

When the atlas's commit equals the fw repo's HEAD, `current.json` is updated and
the MAP is copied to `<atlas_dir>/MAP.md`, the one stable path the session imports.
"""
from __future__ import annotations

import collections
import re
import shutil
import subprocess
from pathlib import Path

from embarch_atlas.common import count_tokens, read_json, write_json, write_text
from embarch_atlas.mismatch import dt_intent

BUDGET = 5000          # real (Claude) tokens
FACTOR = 1.4           # Claude tokens per o200k token (median measured)


def est_tokens(text: str) -> int:
    n = count_tokens(text)
    return int(round((n if n is not None else len(text) / 4) * FACTOR))


def _family(name: str) -> str:
    f = re.sub(r"\d+$", "", name)
    m = re.fullmatch(r"(GPIO)([A-Z])", f)
    return m[1] if m else f


def _compress(names: list[str]) -> str:
    """GPIOA, GPIOB, ... -> GPIOA-F; I2C1, I2C2 -> I2C1-2 (runs only)."""
    by = collections.defaultdict(list)
    for n in names:
        by[_family(n)].append(n)
    out = []
    for fam, ns in sorted(by.items()):
        sfx = [n[len(fam):] for n in ns]
        if len(ns) > 2 and all(len(s) == 1 and s.isalpha() for s in sfx):
            s = sorted(sfx)
            if ord(s[-1]) - ord(s[0]) + 1 == len(s):
                out.append(f"{fam}{s[0]}-{s[-1]}")
                continue
        out += sorted(ns, key=lambda x: (len(x), x))
    return ", ".join(out)


def render(ad: Path, atlas: Path, J, stamp: dict, pins: dict, report: dict, max_parts: int | None = None) -> str:
    aid = stamp["id"]
    rel = f"atlases/{aid}"
    L = [f"# {aid}: hardware atlas for target `{stamp['target']}`", "",
         f"> Zephyr board `{stamp['board']}`, app `{stamp.get('app')}`, firmware commit `{stamp['commit']}` "
         f"(built in its own worktree). Schematic boards: {', '.join(stamp.get('schematic_boards') or []) or 'none'}. "
         f"Stamp: `{rel}/atlas.json`. Stale when HEAD or a source hash differs: run `/atlas-update`.", "",
         "Protocol: MAP -> CARD -> section -> page. Read a part's card, then the section it points to, then the PDF page "
         "(`Read` with `pages=`) to verify. Cite every fact as `<doc_id> p<N>`; board keys are board-qualified (`B:U1-12`, "
         "net `B:3V3`). Mismatches are candidates for the engineer, never facts. Paths are relative to `embarch/atlas/`.", "",
         "Find: `grep -r <SYMBOL> embarch/atlas/docs/*/facts.txt` (one fact per line, with its cite and "
         "provenance). Tools: the `embarch-atlas` MCP server (part_card, doc_toc, doc_section, find_fact, "
         "register, pin_trace, net_trace, mismatch_report, config, dt_node), or the same as `embarch-atlas q <verb>`.", ""]
    c = pins.get("counts", {})
    L += ["## Files", "",
          f"- [{rel}/pins.md]({rel}/pins.md): every MCU pin -> net -> far end -> DT use -> driver; I2C identity; DT values "
          f"({c.get('package_pins', 0)} pins, {c.get('dt_pins', 0)} in DT, {c.get('reach', 0)}/{c.get('io_pins', 0)} I/O pins reach a part or connector)",
          f"- [{rel}/pin_slice.txt]({rel}/pin_slice.txt): one line per pin in use",
          f"- [{rel}/checks.md]({rel}/checks.md): {report['summary']}",
          f"- `{rel}/dt.json`, `{rel}/in_use.json`, `{rel}/pins.json`, `{rel}/mismatches.json`: machine forms", ""]
    # MCU
    per = (J.in_use.get("peripherals") or {}) if J else {}
    used = sorted(n for n, p in per.items() if p.get("instantiated") or p.get("touched_by_code"))
    dt_only = sorted(n for n, p in per.items() if p.get("dt_okay") and not (p.get("instantiated") or p.get("touched_by_code")))
    mcu = pins.get("mcu", {})
    L += ["## MCU", "", f"- {mcu.get('key') or '(not found on the schematic)'} {mcu.get('mpn') or ''} "
          f"({(stamp.get('mcu') or {}).get('part') or 'part not set'})".rstrip()]
    mdocs = mcu_docs(J, stamp) if J else []
    for d, kind, card in mdocs:
        L.append(f"- {kind}: `{d}`" + (f", card `{card}`" if card else ""))
    L.append(f"- In use (instantiated or touched by code): {_compress(used) or 'none'}")
    if dt_only:
        meant = {n for n in dt_only if dt_intent(J, per[n].get("node"), n)}
        L.append(f"- DT okay, no driver in this build: {', '.join(n + (' (on purpose)' if n in meant else '') for n in dt_only)}"
                 f" (see checks.md)")
    wl = worklist(J)
    if wl:
        chs = [f"{ch['chapter']} {ch['title']} (pp {ch['pages'][0]}-{ch['pages'][1]})" for ch in wl.get("chapters", [])]
        L.append(f"- RM chapters in use (`docs/{wl.get('doc')}/worklist.json`): " + "; ".join(chs))
    else:
        L.append("- RM chapters in use: not selected yet (`embarch-atlas mcu rm-sections " + rel + "`)")
    L.append("")
    # boards
    L += ["## Boards and interfaces", ""]
    for lab, B in sorted((J.boards if J else {}).items()):
        src = (B.nl.get("source") or {}).get("file") or "?"
        L.append(f"- {lab}: `boards/{lab}/netlist.json` from `{src}` ({len(B.comps)} parts, {len(B.nets)} nets)")
    for i in pins.get("interfaces", []):
        agree = f", {i['agree']}/{i['named']} names agree" if i.get("named") else ""
        L.append(f"- {i['a']} <-> {i['b']}: {i.get('rule') or 'pin map'} ({i['status']}{agree})")
    for f in (read_json(ad / "boards" / "system.json") if (ad / "boards" / "system.json").is_file() else {}).get("power_feeds") or []:
        via = f" via {' / '.join(f['via'])}" if f.get("via") else ""
        L.append(f"- {f['net']}: fed {'only ' if f.get('only') else ''}from {f.get('from', '?')}{via} "
                 f"({f.get('prov', 'engineer-declared')})")
    if J and not J.boards:
        L.append("- no netlist yet: `embarch-atlas schematic <pdf> --board <label>`")
    L.append("")
    # parts with documents
    rows = parts_table(J)
    with_doc = [r for r in rows if r["docs"]]
    without = [r for r in rows if not r["docs"]]
    L += ["## Parts on the boards with documents", ""]
    if with_doc:
        L += ["| part (MPN) | refs | doc | card |", "|---|---|---|---|"]
        for r in with_doc:
            refs = ", ".join(r["refs"][:6]) + (f" +{len(r['refs']) - 6}" if len(r["refs"]) > 6 else "")
            for d in r["docs"]:
                card = f"docs/{d}/card.md" if (ad / "docs" / d / "card.md").is_file() else "(no card yet)"
                L.append(f"| {r['mpn']} | {refs} | `{d}` | {card} |")
    else:
        L.append("None translated yet (`/atlas-translate-docs`).")
    L.append("")
    if without:
        show = without if max_parts is None else without[:max_parts]
        L += [f"## Active parts without a document ({len(without)})", "",
              "; ".join(f"{r['mpn']} ({', '.join(r['refs'][:3])}{'...' if len(r['refs']) > 3 else ''})" for r in show)
              + ("; ..." if len(show) < len(without) else ""), "",
              "`inventory/missing.md` lists them with the schematic's datasheet links."]
    return "\n".join(L).rstrip() + "\n"


def mcu_docs(J, stamp: dict) -> list[tuple[str, str, str | None]]:
    out = []
    mb = J.mcu
    ids = set(J.part_docs.get(mb, [])) if mb else set()
    for d, v in sorted(J.docs.docs.items()):
        kind = v["doc"].get("kind") or "doc"
        if d in ids or kind in ("reference-manual", "errata", "svd"):
            card = f"docs/{d}/card.md" if (v["dir"] / "card.md").is_file() else None
            out.append((d, kind, card))
    return out


def worklist(J) -> dict | None:
    if not J:
        return None
    for d, v in sorted(J.docs.docs.items()):
        wl = v["dir"] / "worklist.json"
        if wl.is_file():
            try:
                w = read_json(wl)
            except (OSError, ValueError):
                continue
            if w.get("atlas") and Path(w["atlas"]).name != J.atlas.name:
                continue                       # a worklist for another atlas
            w.setdefault("doc", d)
            return w
    return None


PASSIVE = re.compile(r"^(R|C|L|FB|TP|J|P|CN|X|MH|H|FID|SW|BT|Y|NT|JP|TVS)\d", re.I)


def parts_table(J) -> list[dict]:
    """Active parts (not passives, connectors, test points), grouped by MPN, with their documents."""
    if not J:
        return []
    by = collections.defaultdict(list)
    for lab, B in sorted(J.boards.items()):
        for ref in sorted(B.comps):
            mpn = B.mpn(ref)
            if not mpn or PASSIVE.match(ref) or B.dni(ref):
                continue
            by[mpn].append(f"{lab}:{ref}")
    out = []
    for mpn, refs in sorted(by.items(), key=lambda x: (-len(x[1]), x[0])):
        docs = sorted({d for r in refs for d in J.part_docs.get(tuple(r.split(":", 1)), [])})
        out.append({"mpn": mpn, "refs": refs, "docs": docs})
    return out


def update_current(ad: Path, atlas: Path, stamp: dict) -> bool:
    """current.json + <atlas_dir>/MAP.md when the atlas's commit is the fw repo's HEAD."""
    fw = (stamp.get("build") or {}).get("fw")
    head = None
    if fw:
        r = subprocess.run(["git", "-C", fw, "rev-parse", "HEAD"], capture_output=True, text=True)
        head = r.stdout.strip() if r.returncode == 0 else None
    if not head or head != stamp["commit"]:
        return False
    try:
        from embarch_atlas import project
        project.status(ad)                      # the owner of current.json's shape
    except Exception:                           # noqa: BLE001  (fall back to the same shape)
        cur_p = ad / "current.json"
        cur = read_json(cur_p) if cur_p.is_file() else {}
        if cur.get("commit") != head:
            cur = {"commit": head, "matches": []}
        if not any(m.get("atlas") == stamp["id"] for m in cur["matches"]):
            cur["matches"].append({"atlas": stamp["id"], "board": stamp.get("board"), "stale": []})
        write_json(cur_p, cur)
    shutil.copyfile(atlas / "MAP.md", ad / "MAP.md")
    return True


def run(ad: Path, atlas: Path, J, stamp: dict, pins: dict, report: dict) -> dict:
    text = render(ad, atlas, J, stamp, pins, report)
    tok = est_tokens(text)
    if tok > BUDGET:
        for n in (20, 10, 0):
            text = render(ad, atlas, J, stamp, pins, report, max_parts=n)
            tok = est_tokens(text)
            if tok <= BUDGET:
                break
    if tok > BUDGET:
        raise SystemExit(f"MAP.md is {tok} tokens (> {BUDGET}): the tier-0 budget is a hard cap")
    write_text(atlas / "MAP.md", text)
    current = update_current(ad, atlas, stamp)
    return {"tokens": tok, "current": current}

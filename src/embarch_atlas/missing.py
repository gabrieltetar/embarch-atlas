"""`missing`: which parts on the boards have no document, asked as questions.

Joins every board's `bom.json` / `netlist.json` to `inventory/manifest.json`
through `coverage` and writes `inventory/missing.md`. It opens with one table of
proposed matches, file to part, for the engineer to confirm or reject, so a file
already supplied is never asked for again. Then one question per part number
with no document and no proposal, ranked by how directly firmware sees the part,
plus the MCU's errata sheet and reference manual when the manifest has none, and
the Word PRDs to export. It closes with the documents nothing uses, which is how
a wrong file or a mis-typed document shows up. **Nothing is fetched**: for each
item the engineer supplies the file, marks it not needed, or accepts a
standard-function card (engineer-declared) for a jellybean part. The link a
part's own schematic parameters carry is listed as what the schematic says, not
as something to download.

Firmware visibility, from the netlist (the MCU is the part `[mcu] ref` names in
atlas.toml, else every part whose MPN starts with `[mcu] part`; `ref` is how a
module that carries the MCU, under the module's own MPN, is named the MCU):

- `direct`: shares a signal net with an MCU pin;
- `via-series`: one series resistor, ferrite bead or net tie away from one;
- `cross-board`: on another board, on a net that a board-to-board interface
  mates to one of those nets (engineer-declared first, else the best proposal
  from `interfaces`, which the question then says is unconfirmed);
- `not-visible`: none of these.

Supply nets are never signal nets: an MCU pin whose name is a supply
(VDD, VSS, VBAT, VREF, ...), a net named like a rail (GND, VDD, 3V3, ...) or any
net with RAIL_PINS pins or more.
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from . import answers as answersmod
from . import common, coverage, interfaces, inventory, project
from .coverage import category, family_regex, norm, ref_prefix  # noqa: F401 - family_regex is this module's API too

SERIES = {"R", "FB", "NT"}
RAIL_PINS = 30
SUPPLY_PIN = re.compile(r"^(V(DD|SS|BAT|REF|CAP|CC|EE|IN|DDA|SSA|DDIO)[A-Z0-9_+\-]*|A?GND\w*|[AD]V(DD|SS)\w*|EP|EPAD|PAD|"
                        r"THERMAL\w*|EXPOSED\w*)$", re.I)
RAIL_NET = re.compile(r"^([ADPSC]?GND(_?[A-Z0-9]{1,6})?|V(SS|DD|CC|EE)[A-Z0-9_]*|\+?\d+V\d*(_[A-Z0-9]+)?|"
                      r"[+-]?\d+V\d*)$", re.I)
TIER_ORDER = ("mcu", "direct", "via-series", "cross-board", "not-visible", "unknown")
RM_TOKEN = "reference-manual"       # `--confirm` / `--reject` argument for the MCU reference-manual row
KEPT_ANSWERS = ("not-needed", "standard-function")      # the engineer decided: no proposal reopens the part
UNUSED_KINDS = ("schematic", "prd", "spreadsheet", "notes")     # have a role of their own, or are read as they are


# ---------------------------------------------------------------- firmware visibility

def is_rail(net: str | None, nl: dict[str, Any]) -> bool:
    if not net:
        return True
    return bool(RAIL_NET.match(net)) or len(nl["nets"].get(net, {}).get("pins") or []) >= RAIL_PINS


def is_supply_pin(name: str | None) -> bool:
    return bool(name) and any(SUPPLY_PIN.match(part) for part in name.split("/") if part)


def part_nets(comp: dict[str, Any]) -> set[str]:
    return {p["net"] for p in comp["pins"].values() if p.get("net")}


def visibility(nls: dict[str, dict[str, Any]], mcu_refs: list[tuple[str, str]],
               mated: list[tuple[str, str, str]]) -> dict[str, dict[str, dict[str, Any]]]:
    """board -> {"direct": {net: why}, "via-series": {...}, "cross-board": {...}} (why = the MCU net it hangs off)."""
    vis: dict[str, dict[str, dict[str, Any]]] = {b: {"direct": {}, "via-series": {}, "cross-board": {}} for b in nls}
    for b, ref in mcu_refs:
        nl = nls[b]
        for pin, P in nl["components"][ref]["pins"].items():
            if P.get("net") and not is_supply_pin(P.get("name")) and not is_rail(P["net"], nl):
                vis[b]["direct"].setdefault(P["net"], {"mcu": f"{b}:{ref}-{pin}", "net": P["net"]})
    for b, nl in nls.items():
        direct = vis[b]["direct"]
        for ref, C in sorted(nl["components"].items()):
            if ref_prefix(ref) not in SERIES or len(C["pins"]) != 2:
                continue
            ns = [P.get("net") for P in C["pins"].values()]
            for x, y in (ns, ns[::-1]):
                if x in direct and y and y not in direct and not is_rail(y, nl):
                    vis[b]["via-series"].setdefault(y, {**direct[x], "via": f"{b}:{ref}"})
    for pa, pb, src in mated:
        for (x, y) in ((pa, pb), (pb, pa)):
            bx, rest_x = x.split(":", 1)
            by, rest_y = y.split(":", 1)
            if bx not in nls or by not in nls:
                continue
            rx, px = rest_x.rsplit("-", 1)
            ry, py = rest_y.rsplit("-", 1)
            nx = interfaces.net_of(nls[bx], rx, px)
            ny = interfaces.net_of(nls[by], ry, py)
            if not nx or not ny or is_rail(ny, nls[by]):
                continue
            hit = vis[bx]["direct"].get(nx) or vis[bx]["via-series"].get(nx)
            if hit and ny not in vis[by]["direct"] and ny not in vis[by]["via-series"]:
                vis[by]["cross-board"].setdefault(ny, {**hit, "through": f"{x}<->{y}", "mating": src})
    return vis


def mcu_by_ref(nls: dict[str, dict[str, Any]], ref: str | None) -> list[tuple[str, str]]:
    """`[mcu] ref` (`board:REF`) when it names a part on a board, as `join` reads it first."""
    b, _, r = (ref or "").strip().partition(":")
    return [(b, r)] if r and r in (nls.get(b) or {}).get("components", {}) else []


def tier_of(b: str, ref: str, comp: dict[str, Any], vis, mcu_set: set[tuple[str, str]], have_mcu: bool) -> tuple[str, list[dict]]:
    if (b, ref) in mcu_set:
        return "mcu", []
    if not have_mcu:
        return "unknown", []
    nets = part_nets(comp)
    for tier in ("direct", "via-series", "cross-board"):
        hits = [vis[b][tier][n] for n in sorted(nets) if n in vis[b][tier]]
        if hits:
            return tier, hits
    return "not-visible", []


# ---------------------------------------------------------------- the checklist

def analyse(atlas_dir: Path) -> dict[str, Any]:
    atlas_dir = Path(atlas_dir)
    cfg = project.load_config(atlas_dir) if (atlas_dir / project.CONFIG_NAME).is_file() else {}
    man_path = atlas_dir / "inventory" / "manifest.json"
    if not man_path.is_file():
        raise FileNotFoundError(f"{man_path} not found; run `embarch-atlas inventory` first")
    manifest = common.read_json(man_path)
    nls = interfaces.load_netlists(atlas_dir)
    if not nls:
        raise FileNotFoundError(f"no boards/*/netlist.json under {atlas_dir}; run `embarch-atlas schematic` per board")
    boms = {b: common.read_json(atlas_dir / "boards" / b / "bom.json") for b in nls
            if (atlas_dir / "boards" / b / "bom.json").is_file()}
    cov = coverage.load(atlas_dir)
    walks = _walks(atlas_dir)
    mcu_cfg = cfg.get("mcu") or {}
    mcu_part = (mcu_cfg.get("part") or "").strip()
    mcu_refs = mcu_by_ref(nls, mcu_cfg.get("ref")) or sorted(
        (b, r) for b, nl in nls.items() for r, c in nl["components"].items()
        if mcu_part and c.get("mpn") and norm(c["mpn"]).startswith(norm(mcu_part)))
    mated = interfaces.mated_pairs(atlas_dir, nls)
    vis = visibility(nls, mcu_refs, mated)
    mcu_set = set(mcu_refs)

    rows: dict[str, dict[str, Any]] = {}
    for b in sorted(nls):
        nl = nls[b]
        for e in boms.get(b, []):
            ref, mpn = e["ref"], e.get("mpn")
            cat = category(ref)
            if not mpn or cat == "passive" or e.get("in_bom") is False:
                continue
            comp = nl["components"].get(ref)
            if comp is None:
                continue
            R = rows.get(mpn)
            if R is None:
                st = cov.status(mpn)
                answer = answersmod.find(cov.given, mpn)
                answer = None if answer and answer["answer"] == "document" else answer
                keep = bool(answer) and answer["answer"] in KEPT_ANSWERS
                R = rows[mpn] = {"mpn": mpn, "refs": [], "category": cat, "tier": "unknown", "why": [],
                                 "answer": answer, "walks": walks.get(mpn, 0),
                                 "description": e.get("description"), "links": set(), "dni": [],
                                 "covered_by": [c["doc"]["file"] for c in st["covered"]],
                                 "covered_how": {c["doc"]["file"]: c for c in st["covered"] if c["how"] != "family"},
                                 "proposals": [] if keep else st["proposals"], "supporting": st["supporting"],
                                 "stale_confirm": st["stale"]}
            if cat == "active":
                R["category"] = "active"
            R["refs"].append(f"{b}:{ref}")
            if e.get("datasheet_link"):
                R["links"].add(e["datasheet_link"])
            if e.get("dni"):
                R["dni"].append(f"{b}:{ref}")
            tier, why = tier_of(b, ref, comp, vis, mcu_set, bool(mcu_refs))
            if TIER_ORDER.index(tier) < TIER_ORDER.index(R["tier"]):
                R["tier"], R["why"] = tier, why
            elif tier == R["tier"]:
                R["why"] += why
    for R in rows.values():
        R["refs"].sort(key=lambda s: (s.split(":")[0], _nat(s.split(":")[1])))
        R["links"] = sorted(R["links"])
        seen, why = set(), []
        for w in R["why"]:
            k = (w.get("net"), w.get("mcu"))
            if k not in seen:
                seen.add(k)
                why.append(w)
        R["why"] = why

    mcu_docs = {"errata": [], "reference-manual": [], "datasheet": []}
    # The MCU's own MPNs, and the `part` family too: a module's MPN finds the module's
    # datasheet, never the SoC's reference manual or errata.
    mcu_keys = sorted({nls[b]["components"][r]["mpn"] for b, r in mcu_refs if nls[b]["components"][r].get("mpn")})
    if mcu_part and not any(norm(m).startswith(norm(mcu_part)) for m in mcu_keys):
        mcu_keys.append(mcu_part + "XXXX")
    for kind in mcu_docs:
        found = {d["file"] for m in mcu_keys for d in cov.documents(m, (kind,))}
        if kind != "datasheet":     # a family name no part token can read still names the MCU; an override says so outright
            found |= {d["file"] for d, _ in cov.index if d["kind"] == kind and (
                (cov.overrides.get((d.get("sha256") or "")[:12]) or {}).get("kind") == kind or cov.names_mcu(mcu_part, d))}
        mcu_docs[kind] = sorted(found)
    if not mcu_docs["errata"] and not mcu_refs:
        mcu_docs["errata"] = [d["file"] for d, _ in cov.index if d["kind"] == "errata"]
    rm_rows = _rm_rows(cov, mcu_part) if not mcu_docs["reference-manual"] else []

    def key(R):
        return (-R["walks"], TIER_ORDER.index(R["tier"]), -len(R["refs"]), R["mpn"])
    ordered = sorted(rows.values(), key=key)
    passive_mpns = {e["mpn"] for b in boms.values() for e in b if e.get("mpn") and category(e["ref"]) == "passive"}
    return {"boards": sorted(nls), "mcu_part": mcu_part, "mcu_refs": [f"{b}:{r}" for b, r in mcu_refs],
            "mcu_mpns": sorted({nls[b]["components"][r]["mpn"] for b, r in mcu_refs}),
            "mcu_docs": mcu_docs, "rows": ordered, "rm_rows": rm_rows,
            "word_prds": [d["file"] for d in cov.docs if d.get("needs_export") and not d.get("superseded_by")],
            "unused": _unused(cov, ordered, mcu_docs, rm_rows, passive_mpns),
            "mating": sorted({src for _, _, src in mated}), "manifest_docs": len(manifest.get("documents") or [])}


def _rm_rows(cov: coverage.Coverage, mcu_part: str) -> list[dict[str, Any]]:
    """Long documents that name the MCU, when no reference manual does: some vendors publish one datasheet
    with the full register documentation and no separate manual. Longest first."""
    rows = []
    for d, _ in cov.index if mcu_part else []:
        if d["kind"] not in ("datasheet", "other") or int(d.get("pages") or 0) < coverage.MCU_RM_MIN_PAGES:
            continue
        if (cov.overrides.get((d.get("sha256") or "")[:12]) or {}).get("not_reference_manual"):
            continue
        where = cov.names_mcu(mcu_part, d)
        if where:
            rows.append({"file": d["file"], "sha256": d.get("sha256"), "pages": int(d["pages"]), "where": where})
    return sorted(rows, key=lambda r: (-r["pages"], r["file"]))


def _unused(cov: coverage.Coverage, rows: list[dict[str, Any]], mcu_docs: dict[str, list[str]],
            rm_rows: list[dict[str, Any]], passive_mpns: set[str]) -> list[dict[str, Any]]:
    """Documents covering no BOM part and no MCU role, and offered for none."""
    used = {f for fs in mcu_docs.values() for f in fs} | {r["file"] for r in rm_rows}
    for R in rows:
        used |= set(R["covered_by"]) | set(R["supporting"]) | {p["file"] for p in R["proposals"]}
    for mpn in passive_mpns:
        used |= {c["doc"]["file"] for c in cov.status(mpn)["covered"]}
    return [d for d in cov.docs if d["file"] not in used and d.get("kind") not in UNUSED_KINDS
            and not d.get("superseded_by") and not d.get("duplicate_of")]


def _walks(atlas_dir: Path) -> dict[str, int]:
    """MPN -> net_trace walks (one per rail) that stop at it for want of a datasheet; {} when the boards
    cannot be joined (no netlist)."""
    from . import nettrace
    try:
        return nettrace.walk_stops(nettrace.load(atlas_dir, None, common.read_json))
    except (OSError, ValueError, KeyError):
        return {}


def _nat(s: str) -> tuple:
    return tuple((0, int(t), "") if t.isdigit() else (1, 0, t) for t in re.split(r"(\d+)", s) if t)


def _refs(refs: list[str], n: int = 6) -> str:
    return ", ".join(refs[:n]) + (f", ... ({len(refs)} in all)" if len(refs) > n else "")


def _why(R: dict[str, Any]) -> str:
    if R["tier"] == "mcu":
        return "the MCU itself"
    if not R["why"]:
        return {"not-visible": "no MCU signal net within one series part",
                "unknown": "firmware visibility unknown (no `[mcu] part` in atlas.toml, or no part matches it)"}[R["tier"]]
    nets = sorted({w["net"] for w in R["why"]})
    mcu = sorted({w["mcu"].rsplit("-", 1)[0] for w in R["why"]})
    s = {"direct": "shares MCU net(s)", "via-series": "one series part from MCU net(s)",
         "cross-board": "across a board-to-board connector from MCU net(s)"}[R["tier"]]
    s += f" {', '.join(nets[:4])}{', ...' if len(nets) > 4 else ''} ({', '.join(mcu)})"
    vias = sorted({w["via"] for w in R["why"] if w.get("via")})
    if vias:
        s += f" via {', '.join(vias[:3])}"
    if R["tier"] == "cross-board":
        srcs = sorted({w.get("mating") for w in R["why"]})
        if "proposed" in srcs:
            s += "; the mating rule is a proposal, not yet engineer-declared"
    return s


ANSWERS = "Supply it, mark it not needed, or accept a standard-function card?"


def _answer_text(a: dict[str, Any]) -> str:
    if a["answer"] == "not-needed":
        return f"not needed: {a['why']}"
    if a["answer"] == "supplied":
        return "supplied; the file is not in the manifest yet (put it in the sources folder, run `embarch-atlas inventory`)"
    if a["answer"] == "document":
        return f"confirmed {a['file']}"
    pins = ", ".join(f"{k}={v}" for k, v in a["pins"].items())
    return f"generic card {a['function']} (engineer-declared; pins {pins})"


def _cell(s: Any) -> str:
    return str(s if s is not None else "").replace("|", "\\|")


def _proposal_section(a: dict[str, Any], proposed: list[dict[str, Any]]) -> list[str]:
    """The one ✓/✕ table. Rows of one part run strongest first; `--confirm-all` takes the first of each."""
    if not proposed and not a["rm_rows"]:
        return []
    L = ["## Proposed matches: confirm or reject", "",
         "Documents already in the sources folder that look like what a part or the MCU needs. Nothing here counts "
         "as covered until it is confirmed.", "",
         "| MPN | refs | proposed file | rung | evidence |", "|---|---|---|---|---|"]
    for R in proposed:
        for p in R["proposals"]:
            L.append("| " + " | ".join(map(_cell, (R["mpn"], _refs(R["refs"], 4), p["file"], p["rung"], p["evidence"]))) + " |")
    for r in a["rm_rows"]:
        L.append("| " + " | ".join(map(_cell, ("MCU reference manual", ", ".join(a["mcu_refs"]), r["file"], "long MCU document",
                                               f"{r['file']} serves as the reference manual ({r['pages']} pages, "
                                               f"names {a['mcu_part']} in {r['where']})"))) + " |")
    L += ["", "✓ all: `embarch-atlas missing --confirm-all` (the first row of each part).", "",
          "✓ one: `embarch-atlas missing --confirm <MPN> --file <file>`. ✕ one: `embarch-atlas missing --reject <MPN> "
          "--file <file>`, which holds until the file's content changes. The reference-manual row takes "
          f"`{RM_TOKEN}` for `<MPN>`.", ""]
    return L


def render_md(a: dict[str, Any]) -> str:
    rows = a["rows"]
    no_doc = [R for R in rows if not R["covered_by"]]
    proposed = [R for R in no_doc if R["proposals"]]
    answered = [R for R in no_doc if R["answer"] and not R["proposals"]]
    missing = [R for R in no_doc if not R["answer"] and not R["proposals"]]
    act = [R for R in missing if R["category"] == "active"]
    act_vis = [R for R in act if R["tier"] in ("mcu", "direct", "via-series", "cross-board")]
    act_rest = [R for R in act if R not in act_vis]
    other = [R for R in missing if R["category"] != "active"]
    other_vis = [R for R in other if R["tier"] in ("direct", "via-series", "cross-board")]
    covered = [R for R in rows if R["covered_by"]]
    n_act = sum(1 for R in rows if R["category"] == "active")
    n_cov = sum(1 for R in covered if R["category"] == "active")
    n_prop = sum(1 for R in proposed if R["category"] == "active")
    n_ans = sum(1 for R in answered if R["category"] == "active")
    mcu = ", ".join(a["mcu_refs"]) or "not found"
    L = ["# Missing documents: questions for the engineer", "",
         f"**{n_act} active part numbers (U/Q/D) on the boards: {n_cov} covered, {n_prop} proposed, {len(act)} missing, "
         f"{len(act_vis)} of them firmware-visible.** "
         + (f"{n_ans} more are answered in `inventory/answers.toml` (listed below). " if n_ans else "")
         + f"Other parts without a document: {len(other)} ({len(other_vis)} firmware-visible).", ""]
    L += _proposal_section(a, proposed)
    L += ["## How to answer", "",
         f"From boards {', '.join(a['boards'])} (`boards/*/bom.json`, the schematic's own component parameters) against "
         f"`inventory/manifest.json` ({a['manifest_docs']} files). The tool never fetches a document: each item is a "
         "question, and every answer is one of",
         "",
         "- **supply**: put the PDF in the sources folder and re-run `embarch-atlas inventory`;",
         "- **not needed**: say why (for example, firmware never touches the part);",
         "- **standard-function card**: for a jellybean part (logic gate, discrete MOSFET, diode), accept a generic "
         "card, recorded as engineer-declared.",
         "",
         f"MCU: {mcu} (`[mcu] part = \"{a['mcu_part']}\"`). Firmware visibility is read from the netlist: **direct** = "
         "shares a signal net with an MCU pin; **via-series** = one series resistor, bead or net tie away; "
         "**cross-board** = reaches an MCU net through a board-to-board connector. Supply nets never count.",
         "",
         "Open questions are ordered by how many `net_trace` walks (one per rail, upstream) stop at the part for "
         "want of its datasheet, then by firmware visibility. Answer one with `embarch-atlas missing --answer "
         "<MPN> supplied | not-needed --why <text> | standard-function <function> --pins <PART_PIN=CARD_PIN,...>`; "
         f"functions: {', '.join(answersmod.CARDS)}.", ""]
    q = 0

    def item(R):
        nonlocal q
        q += 1
        dni = f" Not fitted in this print: {_refs(R['dni'], 4)}." if R["dni"] else ""
        desc = f" \"{R['description']}\"." if R.get("description") else ""
        link = f" Link in the schematic's parameters (not fetched): {R['links'][0]}" if R["links"] else ""
        stops = (f" Stops {R['walks']} net_trace walk{'s' if R['walks'] != 1 else ''}." if R["walks"] else "")
        gone = (f" Confirmed earlier as {R['stale_confirm']['file']}, which is no longer in the sources folder."
                if R.get("stale_confirm") else "")
        note = f" Supporting material, not its datasheet: {', '.join(R['supporting'])}." if R["supporting"] else ""
        return (f"Q{q}. **{R['mpn']}** x{len(R['refs'])} ({_refs(R['refs'])}): {_why(R)}.{stops}{desc}{dni}{gone}{note} {ANSWERS}"
                + (f"\n    {link.strip()}" if link else ""))

    L += ["## MCU documents", ""]
    if not a["mcu_refs"]:
        q += 1
        L.append(f"Q{q}. No part on the boards matches `[mcu] part` in atlas.toml"
                 f"{' (' + a['mcu_part'] + ')' if a['mcu_part'] else ' (not set)'}. Which part is the MCU? "
                 "A module carrying it is named with `[mcu] ref = \"<board>:<refdes>\"`.")
    md = a["mcu_docs"]
    fam = a["mcu_part"] or "the MCU"
    if not md["errata"]:
        q += 1
        L.append(f"Q{q}. **MCU errata sheet**: no errata document for {fam} is in the manifest. "
                 "Supply the device errata sheet, or mark it not needed?")
    else:
        L.append(f"- errata: {', '.join(md['errata'])}")
    if a["rm_rows"]:
        L.append(f"- reference manual: none for {fam} in the manifest; {a['rm_rows'][0]['file']} is proposed above")
    elif not md["reference-manual"]:
        q += 1
        L.append(f"Q{q}. **MCU reference manual**: none for {fam} in the manifest. Supply it, or mark it not needed?")
    else:
        L.append(f"- reference manual: {', '.join(md['reference-manual'])}")
    if md["datasheet"]:
        L.append(f"- datasheet: {', '.join(md['datasheet'])}")
    L.append("")
    if a["word_prds"]:
        L += ["## PRD in Word", ""]
        for f in a["word_prds"]:
            q += 1
            L.append(f"Q{q}. **{f}** is a PRD in Word, which the procedure cannot read. Export it to PDF and put the PDF in "
                     "the sources folder under the same name, then run `embarch-atlas inventory`. The tool does not convert it.")
        L.append("")
    L += [f"## Active parts, firmware-visible, no document ({len(act_vis)})", ""] + [item(R) for R in act_vis] + [""]
    L += [f"## Active parts, not firmware-visible, no document ({len(act_rest)})", "",
          "Ranked by count. Not visible is not unimportant: logic between the MCU and a load can decide what a "
          "firmware mode means.", ""] + [item(R) for R in act_rest] + [""]
    L += [f"## Other parts without a document ({len(other)})", "",
          "Connectors, crystals, switches and the like; lower priority.", ""] + [item(R) for R in other] + [""]
    if answered:
        L += [f"## Answered ({len(answered)})", "",
              "The engineer's answers (`inventory/answers.toml`). A generic card is a function template, "
              "engineer-declared, never a limit or a timing.", ""]
        L += [f"- **{R['mpn']}** x{len(R['refs'])} ({_refs(R['refs'], 4)}): {_answer_text(R['answer'])}" for R in answered]
        L.append("")
    L += [f"## Covered ({len(covered)})", "", "A wrong row here is taken off with `embarch-atlas missing --reject <MPN> --file <file>`.",
          "", "| MPN | refs | tier | document |", "|---|---|---|---|"]
    for R in covered:
        L.append(f"| {R['mpn']} | {_refs(R['refs'], 4)} | {R['tier']} | {_covered_cell(R)} |")
    if a["unused"]:
        L += ["", f"## Supplied documents nothing uses ({len(a['unused'])})", "",
              "No part on the boards and no MCU role reads these. A file here is not wrong by itself (a standard, a guide), "
              "but this is where a wrong file or a mis-typed document shows up; `embarch-atlas missing --kind <file> <kind>` "
              "corrects a kind.", ""]
        for d in a["unused"]:
            pages = f", {d['pages']} page{'s' if d['pages'] != 1 else ''}" if d.get("pages") else ""
            parts = f"; part tokens read: {', '.join(d['parts'])}" if d.get("parts") else ""
            L.append(f"- {d['file']} ({d.get('kind')}{pages}){parts}")
    return "\n".join(L) + "\n"


def _covered_cell(R: dict[str, Any]) -> str:
    """The files, each with what covers it when that is not a part token; supporting material beside them."""
    cells = []
    for f in R["covered_by"]:
        c = R["covered_how"].get(f)
        cells.append(f if not c else f"{f} ({c['how']}{': ' + c['evidence'] if c['evidence'] else ''})")
    return _cell(", ".join(cells) + (f"; supporting: {', '.join(R['supporting'])}" if R["supporting"] else ""))


def run(atlas_dir: Path) -> dict[str, Any]:
    a = analyse(atlas_dir)
    common.write_text(Path(atlas_dir) / "inventory" / "missing.md", render_md(a))
    return a


# ---------------------------------------------------------------- confirm, reject, kind

class _Refused(ValueError):
    """Said to the engineer as it is, exit 2."""


def _row(a: dict[str, Any], mpn: str) -> dict[str, Any]:
    R = next((R for R in a["rows"] if norm(R["mpn"]) == norm(mpn)), None)
    if R is None:
        raise _Refused(f"no part on the boards has MPN {mpn!r}")
    return R


def _doc(cov: coverage.Coverage, name: str) -> dict[str, Any]:
    d = cov.doc_named(name)
    if d is None:
        raise _Refused(f"{name!r} is not in the manifest (or more than one file has that name: give its path in the "
                       "sources folder); put it there and run `embarch-atlas inventory`")
    if not d.get("sha256"):
        raise _Refused(f"the manifest has no sha256 for {d['file']}; run `embarch-atlas inventory`")
    return d


def _confirm_part(adir: Path, cov: coverage.Coverage, R: dict[str, Any], file: str | None) -> str:
    """Record the part's document: `file`, else its strongest proposal. Returns the file."""
    if file:
        d = _doc(cov, file)
    elif R["proposals"]:
        d = _doc(cov, R["proposals"][0]["file"])
    else:
        raise _Refused(f"{R['mpn']} has no proposal; name its document with --file")
    if d.get("kind") not in coverage.MATCH_KINDS:
        raise _Refused(f"{d['file']} is kind {d.get('kind')}, which never covers a part as its datasheet; if the kind is "
                       f"wrong, run `embarch-atlas missing --kind {d['file']} datasheet` first")
    rung = next((p["rung"] for p in R["proposals"] if p["file"] == d["file"]), "engineer")
    answersmod.record(adir, R["mpn"], {"answer": "document", "file": d["file"], "sha256": d["sha256"], "rung": rung})
    return d["file"]


def _confirm_rm(adir: Path, a: dict[str, Any], cov: coverage.Coverage, file: str | None) -> str:
    """The reference-manual row: the document's kind becomes `reference-manual`, through the kind override."""
    if file:
        d = _doc(cov, file)
    elif len(a["rm_rows"]) == 1:
        d = _doc(cov, a["rm_rows"][0]["file"])
    elif a["rm_rows"]:
        raise _Refused(f"{len(a['rm_rows'])} documents are proposed as the reference manual; name one with --file")
    else:
        raise _Refused("no document is proposed as the reference manual; name one with --file")
    answersmod.record_document(adir, d["sha256"], d["file"], kind="reference-manual")
    return d["file"]


def _refresh_manifest(adir: Path) -> None:
    """Write a kind override into the manifest now, so that everything reading it agrees."""
    try:
        man = common.read_json(adir / "inventory" / "manifest.json")
        inventory.run(adir, inventory.sources_of(adir, man))
    except FileNotFoundError as e:
        print(f"manifest not rewritten ({e}); the next `embarch-atlas inventory` applies the kind")
        return
    _sync_doc_json(adir)


def _sync_doc_json(adir: Path) -> None:
    """A prepped document's doc.json carries the manifest's kind: `doc prep` is the only other writer of it."""
    man = common.read_json(adir / "inventory" / "manifest.json")
    for rec in man.get("documents") or []:
        path = adir / "docs" / str(rec.get("doc_id")) / "doc.json"
        if not rec.get("kind") or not path.is_file():
            continue
        meta = common.read_json(path)
        if meta.get("sha256") != rec.get("sha256") or meta.get("kind") == rec["kind"]:
            continue
        old, meta["kind"] = meta.get("kind"), rec["kind"]
        common.write_json(path, meta)
        print(f"{path.relative_to(adir)}: kind {old} -> {rec['kind']}")


JOIN_HINT = "net_trace reads this now; `embarch-atlas join <atlas>` carries it into pins.json and MAP.md."


def _cmd_match(args, adir: Path) -> int:
    """`--confirm`, `--confirm-all`, `--reject`, `--kind`: record the engineer's word, then rewrite missing.md."""
    a = analyse(adir)
    cov = coverage.load(adir)
    refresh, said = False, []
    if args.kind:
        file, kind = args.kind
        if kind not in inventory.KINDS:
            raise _Refused(f"kind must be one of {', '.join(inventory.KINDS)} (got {kind!r})")
        d = _doc(cov, file)
        answersmod.record_document(adir, d["sha256"], d["file"], kind=kind)
        refresh = True
        said.append(f"recorded: {d['file']} is {kind} -> {answersmod.path_of(adir)}")
    elif args.confirm_all:
        n = 0
        for R in a["rows"]:
            if R["proposals"] and not R["covered_by"]:
                said.append(f"confirmed: {R['mpn']} -> {_confirm_part(adir, cov, R, None)}")
                n += 1
        if a["rm_rows"]:
            said.append(f"confirmed: {_confirm_rm(adir, a, cov, a['rm_rows'][0]['file'])} serves as the reference manual")
            refresh = True
            n += 1
        said.append(f"{n} proposal{'s' if n != 1 else ''} confirmed" if n else "nothing is proposed")
    elif args.confirm:
        if args.confirm == RM_TOKEN:
            said.append(f"confirmed: {_confirm_rm(adir, a, cov, args.file)} serves as the reference manual")
            refresh = True
        else:
            R = _row(a, args.confirm)
            said.append(f"confirmed: {R['mpn']} -> {_confirm_part(adir, cov, R, args.file)}")
    else:
        if not args.file:
            raise _Refused("--reject needs --file: the document that is not this part's")
        d = _doc(cov, args.file)
        if args.reject == RM_TOKEN:
            answersmod.record_document(adir, d["sha256"], d["file"], not_reference_manual=True)
            said.append(f"rejected: {d['file']} as the reference manual")
        else:
            R = _row(a, args.reject)
            answersmod.record_reject(adir, R["mpn"], d["file"], d["sha256"])
            said.append(f"rejected: {d['file']} for {R['mpn']}, until the file's content changes")
    if refresh:
        _refresh_manifest(adir)
    run(adir)
    for line in said:
        print(line)
    print(JOIN_HINT)
    return 0


def _cmd(args) -> int:
    adir = common.atlas_dir(args.atlas_dir)
    if args.file and not (args.confirm or args.reject):
        print("missing: --file goes with --confirm or --reject", file=sys.stderr)
        return 2
    if args.answer:
        return answersmod._cmd_answer(args, adir)
    try:
        if args.confirm or args.confirm_all or args.reject or args.kind:
            return _cmd_match(args, adir)
        a = run(adir)
    except (FileNotFoundError, ValueError) as e:
        print(f"missing: {e}", file=sys.stderr)
        return 2
    rows = a["rows"]
    act = [R for R in rows if R["category"] == "active"]
    n_cov = sum(1 for R in act if R["covered_by"])
    n_prop = sum(1 for R in act if not R["covered_by"] and R["proposals"])
    miss = [R for R in act if not R["covered_by"] and not R["answer"] and not R["proposals"]]
    vis = [R for R in miss if R["tier"] in ("mcu", "direct", "via-series", "cross-board")]
    tiers = defaultdict(int)
    for R in vis:
        tiers[R["tier"]] += 1
    other = [R for R in rows if R["category"] != "active" and not R["covered_by"] and not R["answer"] and not R["proposals"]]
    n_ans = sum(1 for R in rows if not R["covered_by"] and R["answer"] and not R["proposals"])
    print(f"MCU {', '.join(a['mcu_refs']) or 'not found'}; {len(act)} active MPNs, {n_cov} covered, {n_prop} proposed, "
          f"{len(miss)} without a document, "
          f"{len(vis)} firmware-visible ({', '.join(f'{v} {k}' for k, v in sorted(tiers.items()))}); "
          f"{len(other)} other parts without a document; {n_ans} answered; errata {'present' if a['mcu_docs']['errata'] else 'MISSING'} "
          f"-> {adir / 'inventory' / 'missing.md'}")
    return 0


def register(sub, groups) -> None:
    p = sub.add_parser("missing", help="proposed matches and missing-documents questions from the BOM vs the manifest (inventory/missing.md)")
    verb = p.add_mutually_exclusive_group()
    verb.add_argument("--answer", nargs="+", metavar="ARG",
                      help="record an answer instead of writing missing.md: MPN supplied | MPN not-needed --why TEXT | "
                           "MPN standard-function FUNCTION --pins 1=G,2=S,3=D")
    verb.add_argument("--confirm", metavar="MPN",
                      help=f"this part's document is --file, else its strongest proposal; `{RM_TOKEN}` confirms the MCU row")
    verb.add_argument("--confirm-all", action="store_true", help="accept the first proposed row of every part, and the MCU row")
    verb.add_argument("--reject", metavar="MPN", help=f"--file is not this part's document, until its content changes; `{RM_TOKEN}` for the MCU row")
    verb.add_argument("--kind", nargs=2, metavar=("FILE", "KIND"), help="override a document's kind, in the manifest from now on")
    p.add_argument("--file", help="with --confirm or --reject: the document, by its path in the sources folder or its file name")
    p.add_argument("--why", help="with `not-needed`: why the part needs no document")
    p.add_argument("--pins", help="with `standard-function`: the part's pin number or name = the card's pin, comma separated")
    p.set_defaults(func=_cmd)

"""`missing`: which parts on the boards have no document, asked as questions.

Joins every board's `bom.json` / `netlist.json` to `inventory/manifest.json` and
writes `inventory/missing.md`: one question per part number without a document,
ranked by how directly firmware sees the part, plus the MCU's errata sheet and
reference manual when the manifest has none. **Nothing is fetched**: for each
item the engineer supplies the file, marks it not needed, or accepts a
standard-function card (engineer-declared) for a jellybean part. The link a
part's own schematic parameters carry is listed as what the schematic says, not
as something to download.

Firmware visibility, from the netlist (the MCU is every part whose MPN starts
with `[mcu] part` in atlas.toml):

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

from . import common, interfaces, project

ACTIVE = {"U", "Q", "D", "IC", "VR", "LED", "ZD", "TVS", "DS"}
PASSIVE = {"R", "C", "L", "FB", "RN", "RA", "CR", "TP", "NT", "MH", "FID", "H", "LOGO", "MP", "E", "TH"}
SERIES = {"R", "FB", "NT"}
RAIL_PINS = 30
SUPPLY_PIN = re.compile(r"^(V(DD|SS|BAT|REF|CAP|CC|EE|IN|DDA|SSA|DDIO)[A-Z0-9_+\-]*|A?GND\w*|[AD]V(DD|SS)\w*|EP|EPAD|PAD|"
                        r"THERMAL\w*|EXPOSED\w*)$", re.I)
RAIL_NET = re.compile(r"^([ADPSC]?GND(_?[A-Z0-9]{1,6})?|V(SS|DD|CC|EE)[A-Z0-9_]*|\+?\d+V\d*(_[A-Z0-9]+)?|"
                      r"[+-]?\d+V\d*)$", re.I)
TIER_ORDER = ("mcu", "direct", "via-series", "cross-board", "not-visible", "unknown")
DOC_KINDS = ("datasheet", "reference-manual", "errata", "other")


def norm(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def ref_prefix(ref: str) -> str:
    m = re.match(r"[A-Za-z]+", ref)
    return m.group(0).upper() if m else ""


def category(ref: str) -> str:
    p = ref_prefix(ref)
    return "active" if p in ACTIVE else ("passive" if p in PASSIVE else "other")


# ---------------------------------------------------------------- document coverage

def family_regex(token: str) -> re.Pattern | None:
    """A part token from the manifest -> a regex over normalised MPNs.

    `ABC32F4xE/xG` (ST ordering family: base, one package letter, one of the
    listed flash-size letters) -> `^ABC32F4[A-Z0-9](E|G)`; a lower-case `x` is a
    wildcard (`ABC120x` -> `^ABC120[A-Z0-9]`); anything else is a prefix of at
    least 6 characters, so a bare vendor family (`ABC32`) never covers every part.
    """
    if "/x" in token and "x" in token.split("/")[0]:
        base, *alts = token.split("/")
        i = base.index("x")
        sizes = [base[i + 1:]] + [a.lstrip("x") for a in alts]
        sizes = [norm(s) for s in sizes if norm(s)]
        if not sizes:
            return None
        return re.compile("^" + re.escape(norm(base[:i])) + "[A-Z0-9](" + "|".join(map(re.escape, sizes)) + ")")
    head = token.split("/")[0]
    if "x" in head:
        rx = "".join("[A-Z0-9]" if c == "x" else re.escape(c.upper()) for c in head if c.isalnum())
        return re.compile("^" + rx) if len(rx) >= 5 else None
    n = norm(head)
    return re.compile("^" + re.escape(n)) if len(n) >= 6 and sum(c.isdigit() for c in n) >= 2 else None


def doc_patterns(doc: dict[str, Any]) -> list[re.Pattern]:
    toks = list(doc.get("parts") or [])
    stem = re.sub(r"^(DS|ds)_", "", Path(doc["file"]).stem)
    toks += [t for t in re.split(r"[-_ .]+", stem) if re.fullmatch(r"[A-Za-z]{2,}\d{2,}[A-Za-z0-9]*", t)]
    pats = []
    for t in toks:
        rx = family_regex(t)
        if rx is not None and rx.pattern not in {p.pattern for p in pats}:
            pats.append(rx)
    return pats


def coverage_index(manifest: dict[str, Any]) -> list[tuple[dict[str, Any], list[re.Pattern]]]:
    docs = [d for d in manifest.get("documents") or []
            if d.get("kind") in DOC_KINDS and not d.get("superseded_by") and not d.get("duplicate_of")]
    return [(d, doc_patterns(d)) for d in docs]


def covering_docs(mpn: str, index, kinds: tuple[str, ...] = ("datasheet", "reference-manual", "errata", "other")) -> list[dict[str, Any]]:
    n = norm(mpn)
    return [d for d, pats in index if d["kind"] in kinds and any(p.match(n) for p in pats)]


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
    index = coverage_index(manifest)
    mcu_part = ((cfg.get("mcu") or {}).get("part") or "").strip()
    mcu_refs = sorted((b, r) for b, nl in nls.items() for r, c in nl["components"].items()
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
            R = rows.setdefault(mpn, {"mpn": mpn, "refs": [], "category": cat, "tier": "unknown", "why": [],
                                      "description": e.get("description"), "links": set(), "dni": [],
                                      "covered_by": [d["file"] for d in covering_docs(mpn, index)]})
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
    if mcu_refs:
        mcu_mpns = sorted({nls[b]["components"][r]["mpn"] for b, r in mcu_refs})
        for kind in mcu_docs:
            mcu_docs[kind] = sorted({d["file"] for m in mcu_mpns for d in covering_docs(m, index, (kind,))})
    elif mcu_part:
        for kind in mcu_docs:
            mcu_docs[kind] = sorted({d["file"] for d in covering_docs(mcu_part + "XXXX", index, (kind,))})
    if not mcu_docs["errata"] and not mcu_refs:
        mcu_docs["errata"] = [d["file"] for d, _ in index if d["kind"] == "errata"]

    def key(R):
        return (TIER_ORDER.index(R["tier"]), -len(R["refs"]), R["mpn"])
    ordered = sorted(rows.values(), key=key)
    return {"boards": sorted(nls), "mcu_part": mcu_part, "mcu_refs": [f"{b}:{r}" for b, r in mcu_refs],
            "mcu_mpns": sorted({nls[b]["components"][r]["mpn"] for b, r in mcu_refs}),
            "mcu_docs": mcu_docs, "rows": ordered,
            "mating": sorted({src for _, _, src in mated}), "manifest_docs": len(manifest.get("documents") or [])}


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


def render_md(a: dict[str, Any]) -> str:
    rows = a["rows"]
    missing = [R for R in rows if not R["covered_by"]]
    act = [R for R in missing if R["category"] == "active"]
    act_vis = [R for R in act if R["tier"] in ("mcu", "direct", "via-series", "cross-board")]
    act_rest = [R for R in act if R not in act_vis]
    other = [R for R in missing if R["category"] != "active"]
    other_vis = [R for R in other if R["tier"] in ("direct", "via-series", "cross-board")]
    covered = [R for R in rows if R["covered_by"]]
    n_act = sum(1 for R in rows if R["category"] == "active")
    mcu = ", ".join(a["mcu_refs"]) or "not found"
    L = ["# Missing documents: questions for the engineer", "",
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
         f"**{n_act} active part numbers (U/Q/D) on the boards; {n_act - len(act)} have a document; {len(act)} missing, "
         f"{len(act_vis)} of them firmware-visible.** Other parts without a document: {len(other)} "
         f"({len(other_vis)} firmware-visible).", ""]
    q = 0

    def item(R):
        nonlocal q
        q += 1
        dni = f" Not fitted in this print: {_refs(R['dni'], 4)}." if R["dni"] else ""
        desc = f" \"{R['description']}\"." if R.get("description") else ""
        link = f" Link in the schematic's parameters (not fetched): {R['links'][0]}" if R["links"] else ""
        return (f"Q{q}. **{R['mpn']}** x{len(R['refs'])} ({_refs(R['refs'])}): {_why(R)}.{desc}{dni} {ANSWERS}"
                + (f"\n    {link.strip()}" if link else ""))

    L += ["## MCU documents", ""]
    if not a["mcu_refs"]:
        q += 1
        L.append(f"Q{q}. No part on the boards matches `[mcu] part` in atlas.toml"
                 f"{' (' + a['mcu_part'] + ')' if a['mcu_part'] else ' (not set)'}. Which part is the MCU?")
    md = a["mcu_docs"]
    fam = a["mcu_part"] or "the MCU"
    if not md["errata"]:
        q += 1
        L.append(f"Q{q}. **MCU errata sheet**: no errata document for {fam} is in the manifest. "
                 "Supply the device errata sheet, or mark it not needed?")
    else:
        L.append(f"- errata: {', '.join(md['errata'])}")
    if not md["reference-manual"]:
        q += 1
        L.append(f"Q{q}. **MCU reference manual**: none for {fam} in the manifest. Supply it, or mark it not needed?")
    else:
        L.append(f"- reference manual: {', '.join(md['reference-manual'])}")
    if md["datasheet"]:
        L.append(f"- datasheet: {', '.join(md['datasheet'])}")
    L.append("")
    L += [f"## Active parts, firmware-visible, no document ({len(act_vis)})", ""] + [item(R) for R in act_vis] + [""]
    L += [f"## Active parts, not firmware-visible, no document ({len(act_rest)})", "",
          "Ranked by count. Not visible is not unimportant: logic between the MCU and a load can decide what a "
          "firmware mode means.", ""] + [item(R) for R in act_rest] + [""]
    L += [f"## Other parts without a document ({len(other)})", "",
          "Connectors, crystals, switches and the like; lower priority.", ""] + [item(R) for R in other] + [""]
    L += [f"## Covered ({len(covered)})", "", "| MPN | refs | tier | document |", "|---|---|---|---|"]
    for R in covered:
        L.append(f"| {R['mpn']} | {_refs(R['refs'], 4)} | {R['tier']} | {', '.join(R['covered_by'])} |")
    return "\n".join(L) + "\n"


def run(atlas_dir: Path) -> dict[str, Any]:
    a = analyse(atlas_dir)
    common.write_text(Path(atlas_dir) / "inventory" / "missing.md", render_md(a))
    return a


def _cmd(args) -> int:
    adir = common.atlas_dir(args.atlas_dir)
    try:
        a = run(adir)
    except FileNotFoundError as e:
        print(f"missing: {e}", file=sys.stderr)
        return 2
    rows = a["rows"]
    act = [R for R in rows if R["category"] == "active"]
    miss = [R for R in act if not R["covered_by"]]
    vis = [R for R in miss if R["tier"] in ("mcu", "direct", "via-series", "cross-board")]
    tiers = defaultdict(int)
    for R in vis:
        tiers[R["tier"]] += 1
    other = [R for R in rows if R["category"] != "active" and not R["covered_by"]]
    print(f"MCU {', '.join(a['mcu_refs']) or 'not found'}; {len(act)} active MPNs, {len(miss)} without a document, "
          f"{len(vis)} firmware-visible ({', '.join(f'{v} {k}' for k, v in sorted(tiers.items()))}); "
          f"{len(other)} other parts without a document; errata {'present' if a['mcu_docs']['errata'] else 'MISSING'} "
          f"-> {adir / 'inventory' / 'missing.md'}")
    return 0


def register(sub, groups) -> None:
    p = sub.add_parser("missing", help="missing-documents questions from the BOM vs the manifest (inventory/missing.md)")
    p.set_defaults(func=_cmd)

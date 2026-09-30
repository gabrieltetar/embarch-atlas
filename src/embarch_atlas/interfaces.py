"""`interfaces`: propose how the boards' connectors mate, with a score; the engineer confirms.

Net names are not a join key across boards: two boards reuse local names (a
test-point net, a power-good line) for unrelated nets, and one signal can be
renamed across a connector. So an interface is a pair of connectors plus a pin
map, and the tool only *proposes* the map: for every connector pair it scores
each candidate mating rule by net-name agreement, k of n, where n counts the
contact pairs whose nets are both named (auto-named nets and mounting pads are
left out of the score) and k those whose names are equal.

Rules, for N numbered contacts:

- `identity`       n <-> n
- `mirror`         n <-> N+1-n   (an FFC or a header flipped end to end; for a
                                  dual-row header this is also "both flipped")
- `row-swap`       odd <-> even neighbour (n <-> n+1 for odd n): a dual-row
                   header numbered across the rows, rows exchanged
- `column-reverse` dual-row, columns reversed, rows kept: n <-> N-n (odd n), N+2-n (even n)

The two dual-row rules are scored only when N is even. Where the best rule
pairs two differently named nets, the pair is flagged and classified (an
active-low `n` prefix dropped, case, separators, or plain different).

Unit-to-unit contacts (single-pin spring contacts and their targets) are paired by name
role: equal names, or names equal under one token substitution found across
the set (the side's own name) with an optional RX/TX crossing. That is a
proposal only: which side of one unit meets which side of the next is not in a
schematic.

Everything goes to `boards/system.json` under `proposed` (and `name_check`,
the same-name nets no proposal joins). Entries the engineer wrote, in
`interfaces` or anywhere with `prov` `engineer-declared` / `engineer-confirmed`,
are never overwritten; the machine's score for a declared interface is written
beside it, in `proposed`, as a separate entry.
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from . import common

CONNECTOR_PREFIXES = {"J", "P", "CN", "CON", "X", "XS", "XP", "JP"}
ENGINEER_PROV = ("engineer-declared", "engineer-confirmed")
MIN_AGREE = 2          # a board-to-board proposal needs at least this many agreeing named pairs...
MIN_RATIO = 0.5        # ...and at least this share of the named pairs agreeing (unless the MPNs match)
CROSSINGS = (("RX", "TX"), ("RXD", "TXD"), ("MISO", "MOSI"), ("SDI", "SDO"), ("DIN", "DOUT"))
RULE_ORDER = ("identity", "mirror", "row-swap", "column-reverse")


# ---------------------------------------------------------------- inputs

def load_netlists(atlas_dir: Path) -> dict[str, dict[str, Any]]:
    out = {}
    for p in sorted((Path(atlas_dir) / "boards").glob("*/netlist.json")):
        nl = common.read_json(p)
        out[nl.get("board") or p.parent.name] = nl
    return out


def ref_prefix(ref: str) -> str:
    m = re.match(r"[A-Za-z]+", ref)
    return m.group(0).upper() if m else ""


def connectors(nl: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {r: c for r, c in nl["components"].items() if ref_prefix(r) in CONNECTOR_PREFIXES and c.get("pins")}


def numbered(comp: dict[str, Any]) -> tuple[list[int], list[str]]:
    """(numbered contacts, other pins: mounting / shield pads)."""
    nums = sorted(int(p) for p in comp["pins"] if p.isdigit())
    other = sorted(p for p in comp["pins"] if not p.isdigit())
    return nums, other


def net_of(nl: dict[str, Any], ref: str, pin: str) -> str | None:
    return (nl["components"].get(ref, {}).get("pins", {}).get(pin) or {}).get("net")


def is_auto(nl: dict[str, Any], net: str | None) -> bool:
    return bool(net) and bool(nl["nets"].get(net, {}).get("auto"))


# ---------------------------------------------------------------- mating rules

def rule_map(rule: str, n: int) -> dict[int, int] | None:
    """Contact n on side a -> contact on side b, for contacts 1..n; None when the rule does not apply."""
    if rule == "identity":
        return {k: k for k in range(1, n + 1)}
    if rule == "mirror":
        return {k: n + 1 - k for k in range(1, n + 1)}
    if n % 2:
        return None
    if rule == "row-swap":
        return {k: k + 1 if k % 2 else k - 1 for k in range(1, n + 1)}
    if rule == "column-reverse":
        return {k: n - k if k % 2 else n + 2 - k for k in range(1, n + 1)}
    return None


def classify_rename(a: str, b: str) -> str:
    """Why two names on one mated contact differ."""
    def strip_n(s: str) -> str:
        return re.sub(r"^[nN](?=[A-Z0-9_])|^[/!~#]|(_[nN]|_B|#|_L)$", "", s)
    if a.lower() == b.lower():
        return "case"
    if re.sub(r"[_\-. ]", "", a).lower() == re.sub(r"[_\-. ]", "", b).lower():
        return "separator"
    if strip_n(a) == strip_n(b) or strip_n(a) == b or a == strip_n(b):
        return "active-low-marker"
    return "different"


def score_pairs(A: dict[str, Any], ra: str, B: dict[str, Any], rb: str, pairs: list[tuple[str, str]]) -> dict[str, Any]:
    agree = named = 0
    rows = []
    for pa, pb in pairs:
        na, nb = net_of(A, ra, pa), net_of(B, rb, pb)
        both = bool(na and nb) and not is_auto(A, na) and not is_auto(B, nb)
        ok = both and na == nb
        named += both
        agree += ok
        rows.append({"a": f"{A['board']}:{ra}-{pa}", "a_net": na, "b": f"{B['board']}:{rb}-{pb}", "b_net": nb,
                     "match": "=" if ok else ("!" if both else "?")})
    return {"agree": agree, "named": named, "rows": rows}


def score_connector_pair(A: dict[str, Any], ra: str, B: dict[str, Any], rb: str) -> dict[str, Any] | None:
    ca, cb = A["components"][ra], B["components"][rb]
    na, oa = numbered(ca)
    nb, ob = numbered(cb)
    if len(na) < 2 or len(na) != len(nb):
        return None
    n = max(na + nb)
    rules = {}
    for rule in RULE_ORDER:
        m = rule_map(rule, n)
        if m is None:
            continue
        pairs = [(str(k), str(m[k])) for k in na if m.get(k) in nb]
        rules[rule] = score_pairs(A, ra, B, rb, pairs)
    best = max(rules, key=lambda r: (rules[r]["agree"], -RULE_ORDER.index(r)))
    pads = score_pairs(A, ra, B, rb, [(p, p) for p in oa if p in ob])
    return {"n": n, "rules": rules, "best": best, "pads": pads,
            "same_mpn": bool(ca.get("mpn")) and ca.get("mpn") == cb.get("mpn")}


def _flags(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**r, "class": classify_rename(r["a_net"], r["b_net"])} for r in rows if r["match"] == "!"]


def propose_board_to_board(nls: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    labels = sorted(nls)
    out = []
    for i, la in enumerate(labels):
        for lb in labels[i + 1:]:
            A, B = nls[la], nls[lb]
            for ra, ca in sorted(connectors(A).items()):
                for rb, cb in sorted(connectors(B).items()):
                    s = score_connector_pair(A, ra, B, rb)
                    if s is None:
                        continue
                    best = s["rules"][s["best"]]
                    ratio = best["agree"] / best["named"] if best["named"] else 0.0
                    if not (s["same_mpn"] or (best["agree"] >= MIN_AGREE and ratio >= MIN_RATIO)):
                        continue
                    out.append({
                        "id": f"{la}:{ra}~{lb}:{rb}",
                        "kind": "board-to-board",
                        "a": f"{la}:{ra}", "b": f"{lb}:{rb}",
                        "a_mpn": ca.get("mpn"), "b_mpn": cb.get("mpn"),
                        "a_dni": bool(ca.get("dni")), "b_dni": bool(cb.get("dni")),
                        "contacts": s["n"],
                        "rule": s["best"],
                        "score": f"{best['agree']}/{best['named']}",
                        "rules": {r: f"{v['agree']}/{v['named']}" for r, v in s["rules"].items()},
                        "pads": [{k: v for k, v in r.items()} for r in s["pads"]["rows"]],
                        "flags": _flags(best["rows"]),
                        "unnamed": [r for r in best["rows"] if r["match"] == "?"],
                        "prov": "deterministic",
                        "status": "proposed",
                        "note": "pin map inferred from net names; the engineer declares the real mating",
                    })
    # a connector in more than one proposal is ambiguous: say so on each
    seen = Counter(x for p in out for x in (p["a"], p["b"]))
    for p in out:
        amb = sorted(x for x in (p["a"], p["b"]) if seen[x] > 1)
        if amb:
            p["ambiguous"] = amb
    return sorted(out, key=lambda p: p["id"])


# ---------------------------------------------------------------- unit to unit (spring contacts)

def _tokens(name: str) -> list[str]:
    return name.split("_")


def _cross(tok: str) -> str:
    for x, y in CROSSINGS:
        if tok == x:
            return y
        if tok == y:
            return x
    return tok


def propose_unit_to_unit(nls: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Single-contact connectors on two boards, paired by name role."""
    labels = sorted(nls)
    out = []
    for i, la in enumerate(labels):
        for lb in labels[i + 1:]:
            A, B = nls[la], nls[lb]
            pa = {r: net_of(A, r, next(iter(c["pins"]))) for r, c in connectors(A).items() if len(c["pins"]) == 1}
            pb = {r: net_of(B, r, next(iter(c["pins"]))) for r, c in connectors(B).items() if len(c["pins"]) == 1}
            pa = {r: n for r, n in pa.items() if n and not is_auto(A, n)}
            pb = {r: n for r, n in pb.items() if n and not is_auto(B, n)}
            if not pa or not pb:
                continue
            # the one token substitution that explains most same-length name pairs
            subs: Counter = Counter()
            for na in pa.values():
                for nb in pb.values():
                    ta, tb = _tokens(na), _tokens(nb)
                    if len(ta) == len(tb):
                        d = [(x, y) for x, y in zip(ta, tb) if x != y and _cross(x) != y]
                        if len(d) == 1:
                            subs[d[0]] += 1
            sub = max(sorted(subs), key=lambda k: subs[k]) if subs else None

            def relation(na: str, nb: str) -> str | None:
                if na == nb:
                    return "same"
                ta, tb = _tokens(na), _tokens(nb)
                if len(ta) != len(tb):
                    return None
                crossed = renamed = False
                for x, y in zip(ta, tb):
                    if x == y:
                        continue
                    if sub and (x, y) == sub:
                        renamed = True
                    elif _cross(x) == y:
                        crossed = True
                    else:
                        return None
                return "crossed" if crossed else ("renamed" if renamed else "same")

            cands = sorted((0 if relation(na, nb) == "same" else 1, ra, rb, relation(na, nb))
                           for ra, na in pa.items() for rb, nb in pb.items() if relation(na, nb))
            used_a, used_b, pairs = set(), set(), []
            for _, ra, rb, rel in cands:
                if ra in used_a or rb in used_b:
                    continue
                used_a.add(ra)
                used_b.add(rb)
                pin_a, pin_b = next(iter(A["components"][ra]["pins"])), next(iter(B["components"][rb]["pins"]))
                pairs.append({"a": f"{la}:{ra}-{pin_a}", "a_net": pa[ra], "b": f"{lb}:{rb}-{pin_b}", "b_net": pb[rb],
                              "relation": rel, "a_dni": bool(A["components"][ra].get("dni")),
                              "b_dni": bool(B["components"][rb].get("dni"))})
            if not pairs:
                continue
            pairs.sort(key=lambda p: (p["a"], p["b"]))
            out.append({
                "id": f"{la}~{lb}:unit-to-unit",
                "kind": "unit-to-unit",
                "a": la, "b": lb,
                "substitution": list(sub) if sub else None,
                "score": f"{len(pairs)}/{min(len(pa), len(pb))}",
                "pairs": pairs,
                "unpaired": sorted([f"{la}:{r}" for r in pa if r not in used_a] + [f"{lb}:{r}" for r in pb if r not in used_b]),
                "prov": "deterministic",
                "status": "proposed",
                "note": "paired by net-name role only; which side of one unit meets which side of the next is not "
                        "in the schematics",
            })
    return out


# ---------------------------------------------------------------- pin maps (for other modules)

def interface_pairs(entry: dict[str, Any], nls: dict[str, dict[str, Any]]) -> list[tuple[str, str]]:
    """Mated pin ids (`A:J2-1`, `B:J5-4`) of a board-to-board entry: its explicit `pin_map`, else its `rule`."""
    if entry.get("kind", "board-to-board") != "board-to-board":
        return [(p["a"], p["b"]) for p in entry.get("pairs") or []]
    a, b = entry["a"], entry["b"]
    if entry.get("pin_map"):
        return sorted((f"{a}-{k}", f"{b}-{v}") for k, v in entry["pin_map"].items())
    la, ra = a.split(":", 1)
    lb, rb = b.split(":", 1)
    if la not in nls or lb not in nls or ra not in nls[la]["components"] or rb not in nls[lb]["components"]:
        return []
    na, oa = numbered(nls[la]["components"][ra])
    nb, ob = numbered(nls[lb]["components"][rb])
    m = rule_map(entry.get("rule", ""), max(na + nb) if na + nb else 0) or {}
    pairs = [(f"{a}-{k}", f"{b}-{m[k]}") for k in na if m.get(k) in nb]
    return pairs + [(f"{a}-{p}", f"{b}-{p}") for p in oa if p in ob]


def mated_pairs(atlas_dir: Path, nls: dict[str, dict[str, Any]] | None = None,
                include_proposed: bool = True) -> list[tuple[str, str, str]]:
    """(pin id, pin id, source) for every board-to-board mating: engineer entries first, then the
    best-scoring proposal for a connector no engineer entry covers. source is the entry's prov."""
    path = Path(atlas_dir) / "boards" / "system.json"
    if not path.is_file():
        return []
    nls = nls if nls is not None else load_netlists(atlas_dir)
    sysj = common.read_json(path)
    entries = [e for e in sysj.get("interfaces") or [] if e.get("kind", "board-to-board") == "board-to-board"]
    entries += [e for e in sysj.get("proposed") or [] if e.get("prov") in ENGINEER_PROV
                and e.get("kind", "board-to-board") == "board-to-board"]
    covered = {x for e in entries for x in (e.get("a"), e.get("b"))}
    out = [(pa, pb, e.get("prov", "engineer-declared")) for e in entries for pa, pb in interface_pairs(e, nls)]
    if include_proposed:
        props = [e for e in sysj.get("proposed") or [] if e.get("kind") == "board-to-board"
                 and e.get("prov") not in ENGINEER_PROV and not e.get("declared")]

        def key(e):
            k, n = map(int, e["score"].split("/"))
            return (-k, -(k / n if n else 0), e["id"])
        for e in sorted(props, key=key):
            if e["a"] in covered or e["b"] in covered:
                continue
            covered |= {e["a"], e["b"]}
            out += [(pa, pb, "proposed") for pa, pb in interface_pairs(e, nls)]
    return out


# ---------------------------------------------------------------- name check + system.json

def name_check(nls: dict[str, dict[str, Any]], joined: set[tuple[str, str]]) -> dict[str, Any]:
    """Named nets that exist on two boards: which ones a board-to-board interface joins (within one
    unit), and which share a name only. Unit-to-unit contacts are not joins: they meet another unit."""
    labels = sorted(nls)
    out = {}
    for i, la in enumerate(labels):
        for lb in labels[i + 1:]:
            shared = sorted(n for n in set(nls[la]["nets"]) & set(nls[lb]["nets"])
                            if not nls[la]["nets"][n].get("auto") and not nls[lb]["nets"][n].get("auto"))
            not_joined = [n for n in shared if (f"{la}:{n}", f"{lb}:{n}") not in joined]
            renamed = sorted({(a, b) for a, b in joined if a.startswith(la + ":") and b.startswith(lb + ":")
                              and a.split(":", 1)[1] != b.split(":", 1)[1]})
            out[f"{la}~{lb}"] = {"shared_names": len(shared), "joined_by_proposals": len(shared) - len(not_joined),
                                 "same_name_not_joined": not_joined,
                                 "joined_under_different_names": [list(x) for x in renamed]}
    return out


def _joined_nets(entries: list[dict[str, Any]], nls: dict[str, dict[str, Any]]) -> set[tuple[str, str]]:
    j = set()
    for e in entries:
        rows = []
        if e["kind"] == "board-to-board":
            for pa, pb in interface_pairs(e, nls):
                la, rest_a = pa.split(":", 1)
                lb, rest_b = pb.split(":", 1)
                ra, pna = rest_a.rsplit("-", 1)
                rb, pnb = rest_b.rsplit("-", 1)
                rows.append((la, net_of(nls[la], ra, pna), lb, net_of(nls[lb], rb, pnb)))
        else:
            for p in e["pairs"]:
                rows.append((p["a"].split(":")[0], p["a_net"], p["b"].split(":")[0], p["b_net"]))
        for la, na, lb, nb in rows:
            if na and nb:
                j.add((f"{la}:{na}", f"{lb}:{nb}"))
    return j


def run(atlas_dir: Path) -> dict[str, Any]:
    atlas_dir = Path(atlas_dir)
    nls = load_netlists(atlas_dir)
    if len(nls) < 2:
        raise FileNotFoundError(f"need at least two boards/*/netlist.json under {atlas_dir} (run `schematic` per board)")
    path = atlas_dir / "boards" / "system.json"
    sysj = common.read_json(path) if path.is_file() else {}
    declared = [e for e in sysj.get("interfaces") or []]
    kept = [e for e in sysj.get("proposed") or [] if e.get("prov") in ENGINEER_PROV]
    locked = {e.get("id") for e in declared + kept}
    b2b = propose_board_to_board(nls)
    u2u = propose_unit_to_unit(nls)
    fresh = []
    for p in b2b + u2u:
        if p["id"] in locked:
            continue
        fresh.append(p)
    # the machine's score for each declared board-to-board interface, beside it
    for e in declared + kept:
        if e.get("kind", "board-to-board") != "board-to-board" or ":" not in str(e.get("a")):
            continue
        pairs = interface_pairs(e, nls)
        la, ra = e["a"].split(":", 1)
        lb, rb = e["b"].split(":", 1)
        if la not in nls or lb not in nls:
            continue
        contacts = [(x.rsplit("-", 1)[1], y.rsplit("-", 1)[1]) for x, y in pairs]
        sc = score_pairs(nls[la], ra, nls[lb], rb, [(x, y) for x, y in contacts if x.isdigit() and y.isdigit()])
        best = next((p for p in b2b if p["a"] == e["a"] and p["b"] == e["b"]), None)
        fresh.append({"id": f"{e.get('id') or e['a'] + '~' + e['b']}:check", "kind": "board-to-board-check",
                      "declared": True, "a": e["a"], "b": e["b"], "declared_rule": e.get("rule") or "pin_map",
                      "declared_score": f"{sc['agree']}/{sc['named']}", "flags": _flags(sc["rows"]),
                      "machine_best": {"rule": best["rule"], "score": best["score"]} if best else None,
                      "prov": "deterministic", "status": "check"})
    proposed = kept + sorted(fresh, key=lambda p: p["id"])
    joined_entries = [e for e in declared + kept if e.get("kind", "board-to-board") == "board-to-board"]
    # for the name check, use each connector's best proposal unless an engineer entry covers it
    covered = {x for e in joined_entries for x in (e.get("a"), e.get("b"))}
    for p in sorted(b2b, key=lambda p: (-int(p["score"].split("/")[0]), p["id"])):
        if p["a"] not in covered and p["b"] not in covered:
            joined_entries.append(p)
            covered |= {p["a"], p["b"]}
    # unit-to-unit contacts join nets of *different* units, so they never explain a same-name pair
    out = dict(sysj)
    out["proposed"] = proposed
    out["name_check"] = name_check(nls, _joined_nets(joined_entries, nls))
    out["inputs"] = {b: nl["source"]["sha256"] for b, nl in sorted(nls.items())}
    out.setdefault("interfaces", [])
    common.write_json(path, out)
    return out


def _cmd(args) -> int:
    adir = common.atlas_dir(args.atlas_dir)
    try:
        out = run(adir)
    except FileNotFoundError as e:
        print(f"interfaces: {e}", file=sys.stderr)
        return 2
    for p in out["proposed"]:
        if p["kind"] == "board-to-board":
            print(f"{p['a']} <-> {p['b']}  rule={p['rule']} {p['score']} named pairs agree  "
                  f"(all rules: {', '.join(f'{k} {v}' for k, v in p['rules'].items())})"
                  f"{'  AMBIGUOUS' if p.get('ambiguous') else ''}  [proposed]")
            for f in p["flags"]:
                print(f"    ! {f['a']}={f['a_net']} <-> {f['b']}={f['b_net']}  ({f['class']})")
        elif p["kind"] == "unit-to-unit":
            print(f"{p['a']} ~ {p['b']} unit-to-unit: {p['score']} contacts paired by name role"
                  f"{' (substitution ' + '->'.join(p['substitution']) + ')' if p['substitution'] else ''}  [proposed]")
            for q in p["pairs"]:
                print(f"    {q['a']}={q['a_net']} <-> {q['b']}={q['b_net']}  ({q['relation']})")
        elif p["kind"] == "board-to-board-check":
            print(f"{p['a']} <-> {p['b']}  declared {p['declared_rule']}: {p['declared_score']}")
        else:
            print(f"{p.get('id')}  [{p.get('prov')}] kept")
    for k, v in out["name_check"].items():
        print(f"{k}: {v['shared_names']} net names on both boards, {v['joined_by_proposals']} joined; "
              f"same name, not joined: {', '.join(v['same_name_not_joined']) or '-'}")
    print(f"-> {adir / 'boards' / 'system.json'}")
    return 0


def register(sub, groups) -> None:
    p = sub.add_parser("interfaces", help="propose board-to-board mating rules with scores (boards/system.json)")
    p.set_defaults(func=_cmd)

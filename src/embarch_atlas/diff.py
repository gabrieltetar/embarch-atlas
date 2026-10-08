"""`diff <atlasA> <atlasB>`: what changed between two atlases.

Stamps (commit, west projects, source and schematic hashes, tool), the DT pin
uses (dt.json), the pin/net map (pins.json), peripherals in use, the mismatch
report (new and resolved candidates, check counts), the schematic netlists the
join read (`inputs/netlist_<label>.json`: components added/removed/re-MPN'd,
nets whose pin sets changed, and the designators that makes stale), and the
documents used. Prints a markdown summary; `--json` writes the full diff.
"""
from __future__ import annotations

import sys
from pathlib import Path

from embarch_atlas.common import read_json, write_json, write_text
from embarch_atlas.project import joined_stamp


def _load(atlas: Path, name: str, default=None):
    p = atlas / name
    return read_json(p) if p.is_file() else default


# ---------------------------------------------------------------- netlists (port of the revision diff)

def diff_netlist(old: dict, new: dict) -> dict:
    """Components added/removed/re-MPN'd, nets added/removed/changed, and the designators that go stale."""
    oc, nc = old.get("components", {}), new.get("components", {})
    on = {k: set(v.get("pins", [])) for k, v in old.get("nets", {}).items()}
    nn = {k: set(v.get("pins", [])) for k, v in new.get("nets", {}).items()}
    out = {
        "components_added": sorted(set(nc) - set(oc)),
        "components_removed": sorted(set(oc) - set(nc)),
        "mpn_changed": sorted([d, oc[d].get("mpn"), nc[d].get("mpn")] for d in set(oc) & set(nc)
                              if oc[d].get("mpn") != nc[d].get("mpn")),
        "value_changed": sorted([d, oc[d].get("value"), nc[d].get("value")] for d in set(oc) & set(nc)
                                if oc[d].get("value") != nc[d].get("value")),
        "dni_changed": sorted([d, bool(oc[d].get("dni")), bool(nc[d].get("dni"))] for d in set(oc) & set(nc)
                              if bool(oc[d].get("dni")) != bool(nc[d].get("dni"))),
        "nets_added": sorted(set(nn) - set(on)),
        "nets_removed": sorted(set(on) - set(nn)),
        "nets_changed": {n: {"+": sorted(nn[n] - on[n]), "-": sorted(on[n] - nn[n])}
                         for n in sorted(set(on) & set(nn)) if on[n] != nn[n]},
    }
    touched = set()
    for ch in out["nets_changed"].values():
        touched |= {p.rsplit("-", 1)[0] for p in ch["+"] + ch["-"]}
    touched |= set(out["components_added"]) | set(out["components_removed"])
    touched |= {x[0] for x in out["mpn_changed"] + out["value_changed"] + out["dni_changed"]}
    out["stale_designators"] = sorted(touched)
    return out


# ---------------------------------------------------------------- pieces

def diff_stamps(a: dict, b: dict) -> dict:
    wa = {p["name"]: p for p in a.get("west", [])}
    wb = {p["name"]: p for p in b.get("west", [])}
    west = {}
    for n in sorted(set(wa) | set(wb)):
        pa, pb = wa.get(n), wb.get(n)
        if pa is None or pb is None:
            west[n] = {"a": pa and pa.get("head"), "b": pb and pb.get("head"), "change": "added" if pa is None else "removed"}
        elif (pa.get("head"), pa.get("revision")) != (pb.get("head"), pb.get("revision")):
            west[n] = {"a": pa.get("head"), "b": pb.get("head"), "revision": [pa.get("revision"), pb.get("revision")]}
    sch = {}
    for lab in sorted(set(a.get("schematics") or {}) | set(b.get("schematics") or {})):
        sa, sb = (a.get("schematics") or {}).get(lab), (b.get("schematics") or {}).get(lab)
        if sa != sb:
            sch[lab] = [sa, sb]
    out = {"commit": [a.get("commit"), b.get("commit")], "west": west, "schematics": sch}
    for k in ("target", "board", "app", "sources"):
        if a.get(k) != b.get(k):
            out[k] = [a.get(k), b.get(k)]
    ta, tb = a.get("tool") or {}, b.get("tool") or {}
    if (ta.get("version"), ta.get("git")) != (tb.get("version"), tb.get("git")):
        out["tool"] = [ta, tb]
    if a.get("skill_versions") != b.get("skill_versions"):
        out["skill_versions"] = [a.get("skill_versions"), b.get("skill_versions")]
    return out


def _use_key(u: dict) -> tuple:
    return (u.get("pin"), u.get("node"), u.get("prop"), u.get("index"), u.get("kind"))


def _use_val(u: dict) -> str:
    return f"{u.get('af') or u.get('flags') or ''}"


def diff_dt(a: dict | None, b: dict | None) -> dict:
    if a is None or b is None:
        return {"missing": [a is None, b is None]}
    ua = {_use_key(u): u for u in a.get("uses", []) if not u.get("soc_defined")}
    ub = {_use_key(u): u for u in b.get("uses", []) if not u.get("soc_defined")}
    fmt = lambda k: f"{k[0]} {k[1]} {k[2]}" + (f"[{k[3]}]" if k[3] else "")
    return {
        "added": sorted(fmt(k) + f" {_use_val(ub[k])}" for k in set(ub) - set(ua)),
        "removed": sorted(fmt(k) + f" {_use_val(ua[k])}" for k in set(ua) - set(ub)),
        "changed": sorted(f"{fmt(k)}: {_use_val(ua[k])} -> {_use_val(ub[k])}" for k in set(ua) & set(ub)
                          if _use_val(ua[k]) != _use_val(ub[k])),
        "drivers_added": sorted(set(b.get("drivers", {})) - set(a.get("drivers", {}))),
        "drivers_removed": sorted(set(a.get("drivers", {})) - set(b.get("drivers", {}))),
        "devices": [len(a.get("instantiated_ordinals", [])), len(b.get("instantiated_ordinals", []))],
    }


def _far_set(r: dict) -> set[str]:
    out = set()
    for f in r.get("far", []):
        k = f["kind"]
        if k in ("part-pin", "connector", "testpoint"):
            out.add(f"{f['board']}:{f['pin']}")
        elif k in ("pull-up", "pull-down", "series", "cap"):
            out.add(f"{k} {f['board']}:{f['part']} {f.get('value')}{' DNI' if f.get('dni') else ''}")
        elif k == "board-to-board":
            out.add(f"{f['board']}:{f['pin']}->{f['to']}")
    return out


def _dt_set(r: dict) -> set[str]:
    return {f"{u['label']}.{u.get('func') or u['prop']} {u.get('af') or u.get('flags') or ''}".strip() for u in r.get("dt", [])}


def diff_pins(a: dict | None, b: dict | None) -> dict:
    if a is None or b is None:
        return {"missing": [a is None, b is None]}
    pa = {r["key"].split(":", 1)[-1]: r for r in a.get("pins", [])}
    pb = {r["key"].split(":", 1)[-1]: r for r in b.get("pins", [])}
    rows = []
    for k in sorted(set(pa) | set(pb), key=lambda x: (len(x), x)):
        ra, rb = pa.get(k), pb.get(k)
        if ra is None or rb is None:
            rows.append({"pin": k, "change": "added" if ra is None else "removed"})
            continue
        ch = {}
        if ra.get("net") != rb.get("net"):
            ch["net"] = [ra.get("net"), rb.get("net")]
        fa, fb = _far_set(ra), _far_set(rb)
        if fa != fb:
            ch["far"] = {"+": sorted(fb - fa), "-": sorted(fa - fb)}
        da, db = _dt_set(ra), _dt_set(rb)
        if da != db:
            ch["dt"] = {"+": sorted(db - da), "-": sorted(da - db)}
        if ch:
            rows.append({"pin": k, "port": rb.get("port") or ra.get("port"), **ch})
    counts = {k: [a.get("counts", {}).get(k), b.get("counts", {}).get(k)] for k in sorted(set(a.get("counts", {})) | set(b.get("counts", {})))
              if a.get("counts", {}).get(k) != b.get("counts", {}).get(k)}
    return {"pins": rows, "counts": counts}


def _cand_key(c: dict) -> tuple:
    return (c["class"], c.get("key"), c.get("summary"))


def diff_mismatches(a: dict | None, b: dict | None) -> dict:
    if a is None or b is None:
        return {"missing": [a is None, b is None]}
    ca = {_cand_key(c): c for c in a.get("candidates", [])}
    cb = {_cand_key(c): c for c in b.get("candidates", [])}
    ka = {c["check"]: c for c in a.get("checks", [])}
    kb = {c["check"]: c for c in b.get("checks", [])}
    checks = {}
    for n in sorted(set(ka) | set(kb)):
        x, y = ka.get(n, {}), kb.get(n, {})
        if (x.get("ran"), x.get("passed")) != (y.get("ran"), y.get("passed")):
            checks[n] = [f"{x.get('passed', 0)}/{x.get('ran', 0)}", f"{y.get('passed', 0)}/{y.get('ran', 0)}"]
    return {"new": [cb[k] for k in sorted(set(cb) - set(ca), key=str)],
            "resolved": [ca[k] for k in sorted(set(ca) - set(cb), key=str)],
            "checks": checks, "summary": [a.get("summary"), b.get("summary")]}


def diff_in_use(a: dict | None, b: dict | None) -> dict:
    if a is None or b is None:
        return {"missing": [a is None, b is None]}
    pa, pb = a.get("peripherals", {}), b.get("peripherals", {})
    out = {}
    for n in sorted(set(pa) | set(pb)):
        x, y = pa.get(n), pb.get(n)
        if x != y:
            out[n] = [x, y]
    return out


def diff_inputs(A: Path, B: Path) -> dict:
    ia, ib = _load(A, "inputs.json", {}), _load(B, "inputs.json", {})
    nets = {}
    for lab in sorted(set(ia.get("netlists", {})) | set(ib.get("netlists", {}))):
        if ia.get("netlists", {}).get(lab) == ib.get("netlists", {}).get(lab):
            continue
        fa, fb = A / "inputs" / f"netlist_{lab}.json", B / "inputs" / f"netlist_{lab}.json"
        if fa.is_file() and fb.is_file():
            nets[lab] = diff_netlist(read_json(fa), read_json(fb))
        else:
            nets[lab] = {"missing": [not fa.is_file(), not fb.is_file()]}
    da, db = ia.get("docs", {}), ib.get("docs", {})
    docs = {"added": sorted(set(db) - set(da)), "removed": sorted(set(da) - set(db)),
            "changed": sorted(d for d in set(da) & set(db) if da[d] != db[d])}
    sysj = [ia.get("system_json"), ib.get("system_json")] if ia.get("system_json") != ib.get("system_json") else None
    return {"netlists": nets, "docs": docs, "system_json": sysj}


def diff(A: Path, B: Path) -> dict:
    sa, sb = joined_stamp(A, read_json(A / "atlas.json")), joined_stamp(B, read_json(B / "atlas.json"))
    return {
        "a": sa["id"], "b": sb["id"],
        "stamp": diff_stamps(sa, sb),
        "dt": diff_dt(_load(A, "dt.json"), _load(B, "dt.json")),
        "pins": diff_pins(_load(A, "pins.json"), _load(B, "pins.json")),
        "in_use": diff_in_use(_load(A, "in_use.json"), _load(B, "in_use.json")),
        "mismatches": diff_mismatches(_load(A, "mismatches.json"), _load(B, "mismatches.json")),
        "inputs": diff_inputs(A, B),
    }


# ---------------------------------------------------------------- render

def _list(L: list[str], title: str, items: list[str], cap: int = 25) -> None:
    if not items:
        return
    L.append(f"- {title} ({len(items)}): " + "; ".join(items[:cap]) + ("; ..." if len(items) > cap else ""))


def render(d: dict) -> str:
    s = d["stamp"]
    L = [f"# Atlas diff: {d['a']} -> {d['b']}", "", "## Stamp", "",
         f"- commit: `{(s['commit'][0] or '')[:12]}` -> `{(s['commit'][1] or '')[:12]}`"]
    for k in ("target", "board", "app", "sources"):
        if k in s:
            L.append(f"- {k}: {s[k][0]} -> {s[k][1]}")
    for n, w in s["west"].items():
        L.append(f"- west {n}: {(w.get('a') or '-')[:12]} -> {(w.get('b') or '-')[:12]}" + (f" ({w['change']})" if w.get("change") else ""))
    for lab, (x, y) in s["schematics"].items():
        L.append(f"- schematic {lab}: {(x or '-')[:12]} -> {(y or '-')[:12]}")
    if "tool" in s:
        L.append(f"- tool: {s['tool'][0].get('version')} {(s['tool'][0].get('git') or '')[:12]} -> "
                 f"{s['tool'][1].get('version')} {(s['tool'][1].get('git') or '')[:12]}")
    if "skill_versions" in s:
        L.append("- skill versions changed")
    L += ["", "## Devicetree pin uses", ""]
    dt = d["dt"]
    if "missing" in dt:
        L.append("- dt.json missing in " + ("A" if dt["missing"][0] else "B"))
    else:
        _list(L, "added", dt["added"])
        _list(L, "removed", dt["removed"])
        _list(L, "changed", dt["changed"])
        _list(L, "drivers compiled, added", dt["drivers_added"])
        _list(L, "drivers compiled, removed", dt["drivers_removed"])
        if dt["devices"][0] != dt["devices"][1]:
            L.append(f"- devices instantiated: {dt['devices'][0]} -> {dt['devices'][1]}")
        if not any(dt[k] for k in ("added", "removed", "changed", "drivers_added", "drivers_removed")):
            L.append("- no change")
    L += ["", "## Pin/net map", ""]
    pm = d["pins"]
    if "missing" in pm:
        L.append("- pins.json missing in " + ("A" if pm["missing"][0] else "B"))
    else:
        for k, (x, y) in pm["counts"].items():
            L.append(f"- {k}: {x} -> {y}")
        for r in pm["pins"][:40]:
            bits = []
            if r.get("change"):
                bits.append(r["change"])
            if "net" in r:
                bits.append(f"net {r['net'][0]} -> {r['net'][1]}")
            if "dt" in r:
                bits.append(f"DT +{r['dt']['+']} -{r['dt']['-']}")
            if "far" in r:
                bits.append(f"far +{len(r['far']['+'])} -{len(r['far']['-'])}")
            L.append(f"- {r.get('port') or r['pin']}: " + "; ".join(bits))
        if len(pm["pins"]) > 40:
            L.append(f"- ... {len(pm['pins']) - 40} more pins (see --json)")
        if not pm["pins"] and not pm["counts"]:
            L.append("- no change")
    L += ["", "## Peripherals in use", ""]
    iu = d["in_use"]
    if "missing" in iu:
        L.append("- in_use.json missing")
    elif not iu:
        L.append("- no change")
    for n, (x, y) in ({} if "missing" in iu else iu).items():
        L.append(f"- {n}: {_state(x)} -> {_state(y)}")
    L += ["", "## Mismatch report", ""]
    mm = d["mismatches"]
    if "missing" in mm:
        L.append("- mismatches.json missing in " + ("A" if mm["missing"][0] else "B"))
    else:
        L.append(f"- A: {mm['summary'][0]}")
        L.append(f"- B: {mm['summary'][1]}")
        for n, (x, y) in mm["checks"].items():
            L.append(f"- check {n}: {x} -> {y}")
        _list(L, "new candidates", [f"[{c['class']}] {c['key']}: {c['summary']}" for c in mm["new"]], 30)
        _list(L, "resolved candidates", [f"[{c['class']}] {c['key']}: {c['summary']}" for c in mm["resolved"]], 30)
    L += ["", "## Inputs", ""]
    inp = d["inputs"]
    for lab, nd in inp["netlists"].items():
        if "missing" in nd:
            L.append(f"- netlist {lab}: snapshot missing")
            continue
        L.append(f"- netlist {lab}: +{len(nd['components_added'])}/-{len(nd['components_removed'])} parts, "
                 f"{len(nd['mpn_changed'])} MPN, {len(nd['value_changed'])} value, {len(nd['dni_changed'])} DNI changes; "
                 f"+{len(nd['nets_added'])}/-{len(nd['nets_removed'])} nets, {len(nd['nets_changed'])} changed; "
                 f"stale designators: {', '.join(nd['stale_designators'][:30]) or 'none'}")
    if not inp["netlists"]:
        L.append("- netlists: no change")
    _list(L, "documents added", inp["docs"]["added"])
    _list(L, "documents removed", inp["docs"]["removed"])
    _list(L, "documents changed", inp["docs"]["changed"])
    if inp["system_json"]:
        L.append("- boards/system.json changed")
    return "\n".join(L) + "\n"


def _state(p: dict | None) -> str:
    if not p:
        return "absent"
    if p.get("instantiated") or p.get("touched_by_code"):
        return "in use"
    return "DT okay, no driver" if p.get("dt_okay") else "off"


def cmd_diff(args) -> int:
    from embarch_atlas.build import resolve_atlas, resolve_atlas_dir
    ad = resolve_atlas_dir(args)
    A, B = resolve_atlas(ad, args.a), resolve_atlas(ad, args.b)
    d = diff(A, B)
    text = render(d)
    if args.out:
        write_text(Path(args.out), text)
    if args.json:
        write_json(Path(args.json), d)
    sys.stdout.write(text)
    return 0


def register(sub, groups) -> None:
    p = sub.add_parser("diff", help="what changed between two atlases")
    p.add_argument("a", help="the earlier atlas (id, dir, or target name)")
    p.add_argument("b", help="the later atlas")
    p.add_argument("--out", help="also write the markdown here")
    p.add_argument("--json", help="write the full diff as JSON here")
    p.set_defaults(func=cmd_diff)


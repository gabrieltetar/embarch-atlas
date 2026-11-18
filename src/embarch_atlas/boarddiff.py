"""`board-diff`: an Altium project's parts and pin nets against the `netlist.json` an atlas built from the
same project's PDF. Read-only: it writes no atlas state. The CAD side is `cad.read_project`, the PDF side
`<atlas>/boards/<label>/netlist.json`; both are compared through the joined board nets (`aliases` included),
never per sheet. Exit 0 when nothing differs, 1 when something does, 2 on a refusal."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from embarch_atlas import cad
from embarch_atlas.smartpdf import is_auto_net, natural_key, split_pin_id

NC = "NC"


def _norm(s: Any) -> str:
    return "".join(str(s or "").split()).lower()


def _pin_nets(nl: dict[str, Any]) -> dict[tuple[str, str], str]:
    out = {}
    for ref, c in (nl.get("components") or {}).items():
        for pin, P in (c.get("pins") or {}).items():
            out[(ref, pin)] = (P or {}).get("net") or NC
    return out


def _classes(nl: dict[str, Any]) -> dict[str, frozenset[str]]:
    return {n: frozenset([n, *(N.get("aliases") or [])]) for n, N in (nl.get("nets") or {}).items()}


def _auto(nl: dict[str, Any], name: str) -> bool:
    N = (nl.get("nets") or {}).get(name)
    if N is None:
        return False
    if "auto" in N:
        return bool(N["auto"])
    return is_auto_net(name, N.get("pins") or [])


def diff_netlists(pdf: dict[str, Any], cad_nl: dict[str, Any]) -> dict[str, Any]:
    """The differences between the PDF netlist and the CAD netlist (both `netlist.json`-shaped)."""
    pc, cc = pdf.get("components") or {}, cad_nl.get("components") or {}
    parts_pdf_only = sorted(set(pc) - set(cc), key=natural_key)
    parts_cad_only = sorted(set(cc) - set(pc), key=natural_key)
    common = sorted(set(pc) & set(cc), key=natural_key)
    changed = []
    for ref in common:
        for field in ("mpn", "value"):
            a, b = pc[ref].get(field), cc[ref].get(field)
            if a and b and _norm(a) != _norm(b):
                changed.append({"ref": ref, "field": field, "pdf": a, "cad": b})
    pn, cn = _pin_nets(pdf), _pin_nets(cad_nl)
    pins_pdf_only = sorted((k for k in pn if k[0] in cc and k not in cn), key=lambda k: natural_key(f"{k[0]}-{k[1]}"))
    pins_cad_only = sorted((k for k in cn if k[0] in pc and k not in pn), key=lambda k: natural_key(f"{k[0]}-{k[1]}"))
    both = sorted((k for k in pn if k in cn), key=lambda k: natural_key(f"{k[0]}-{k[1]}"))
    pcls, ccls = _classes(pdf), _classes(cad_nl)

    def same(a: str, b: str) -> bool:
        if a == b:
            return True
        return bool(pcls.get(a, frozenset([a])) & ccls.get(b, frozenset([b])))

    # pins of one PDF net / one CAD net, restricted to the pins both sides list
    by_p: dict[str, set] = {}
    by_c: dict[str, set] = {}
    for k in both:
        by_p.setdefault(pn[k], set()).add(k)
        by_c.setdefault(cn[k], set()).add(k)
    named_by_cad, renamed, cad_auto_named, handled = [], [], [], set()
    for p, S in sorted(by_p.items(), key=lambda kv: natural_key(kv[0])):
        if p == NC:
            continue
        c = cn[next(iter(S))]
        if c == NC or by_c[c] != S or same(p, c):
            continue
        handled |= S
        entry = {"pdf": p, "cad": c, "pins": [f"{r}-{q}" for r, q in sorted(S, key=lambda k: natural_key(f"{k[0]}-{k[1]}"))]}
        pa, ca = _auto(pdf, p), _auto(cad_nl, c)
        if pa and ca:
            continue  # both auto-named after a pin: the same net under Altium's own rule
        (named_by_cad if pa else cad_auto_named if ca else renamed).append(entry)
    pin_nets = [{"pin": f"{r}-{q}", "pdf": pn[(r, q)], "cad": cn[(r, q)]} for r, q in both
                if (r, q) not in handled and not same(pn[(r, q)], cn[(r, q)])
                and not (_auto(pdf, pn[(r, q)]) and _auto(cad_nl, cn[(r, q)]))
                and not (pn[(r, q)] == NC and cn[(r, q)] == NC)]
    fmt = lambda ks: [f"{r}-{q}" for r, q in ks]  # noqa: E731
    d = {"parts_pdf_only": parts_pdf_only, "parts_cad_only": parts_cad_only, "parts_changed": changed,
         "pins_pdf_only": fmt(pins_pdf_only), "pins_cad_only": fmt(pins_cad_only),
         "pin_nets": pin_nets, "nets_renamed": renamed, "nets_named_by_cad": named_by_cad,
         "nets_auto_named_by_cad": cad_auto_named}
    d["differs"] = any(d[k] for k in ("parts_pdf_only", "parts_cad_only", "parts_changed", "pins_pdf_only",
                                      "pins_cad_only", "pin_nets", "nets_renamed", "nets_auto_named_by_cad"))
    return d


def variant_view(pdf: dict[str, Any], cad_doc: dict[str, Any]) -> dict[str, Any] | None:
    v = cad_doc.get("variant")
    if not v:
        return None
    pc = pdf.get("components") or {}
    refs = [r["designator"] for r in v["not_fitted"]]
    return {"name": v["name"], "not_fitted_vs_pdf_fitted": [r for r in refs if r in pc and not pc[r].get("dni")],
            "not_fitted_not_in_pdf": [r for r in refs if r not in pc], "unknown_unique_ids": v["unknown_unique_ids"]}


def render(d: dict[str, Any], label: str, cad_doc: dict[str, Any]) -> str:
    src = cad_doc["source"]
    L = [f"board-diff {label}: CAD {src['file']}{' at ' + src['rev'] if src['rev'] else ''} against the PDF netlist: "
         + ("differs" if d["differs"] else "no difference"), ""]

    def sec(title: str, items: list[str]) -> None:
        if items:
            L.append(f"{title} ({len(items)}):")
            L.extend(f"    {i}" for i in items)

    sec("parts only in the PDF netlist", d["parts_pdf_only"])
    sec("parts only in the CAD project", d["parts_cad_only"])
    sec("parts whose part number or value differ", [f"{c['ref']} {c['field']}: PDF {c['pdf']!r}, CAD {c['cad']!r}" for c in d["parts_changed"]])
    sec("pins only in the PDF netlist", d["pins_pdf_only"])
    sec("pins only in the CAD project", d["pins_cad_only"])
    sec("pins on a different net", [f"{p['pin']}: PDF {p['pdf']}, CAD {p['cad']}" for p in d["pin_nets"]])
    sec("nets renamed (same pins, name only)", [f"PDF {n['pdf']} -> CAD {n['cad']} ({len(n['pins'])} pins)" for n in d["nets_renamed"]])
    sec("nets the CAD auto-names but the PDF names (same pins, name only)",
        [f"PDF {n['pdf']} -> CAD {n['cad']}" for n in d["nets_auto_named_by_cad"]])
    sec("nets the PDF left auto-named that the CAD names (not a difference)",
        [f"PDF {n['pdf']} -> CAD {n['cad']} ({len(n['pins'])} pins)" for n in d["nets_named_by_cad"]])
    v = d.get("variant")
    if v:
        L.append(f"variant {v['name']}: the PDF shows every part as fitted; the variant does not fit "
                 f"{len(v['not_fitted_vs_pdf_fitted'])} of them (not counted as differences):")
        L.extend(f"    {r}" for r in v["not_fitted_vs_pdf_fitted"])
        if v["not_fitted_not_in_pdf"]:
            L.append(f"  not-fitted parts the PDF netlist does not list: {', '.join(v['not_fitted_not_in_pdf'])}")
    return "\n".join(L)


def cmd_board_diff(args) -> int:
    from embarch_atlas.build import resolve_atlas_dir
    path = resolve_atlas_dir(args) / "boards" / args.board / "netlist.json"
    try:
        if not path.is_file():
            raise cad.CadError(f"no netlist for board {args.board!r} at {path}")
        try:
            pdf = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as e:
            raise cad.CadError(f"{path} is not JSON: {e}")
        doc = cad.read_project(args.cad, args.rev, args.variant)
    except cad.CadError as e:
        print(f"embarch-atlas board-diff: {e}", file=sys.stderr)
        return 2
    d = diff_netlists(pdf, doc)
    v = variant_view(pdf, doc)
    if v:
        d["variant"] = v
    print(json.dumps(d, indent=2) if args.json else render(d, args.board, doc))
    return 1 if d["differs"] else 0


def register(sub, groups) -> None:
    p = sub.add_parser("board-diff", help="diff an Altium project's parts and pin nets against a PDF-built atlas's "
                                          "netlist (read-only; exit 1 when they differ; needs the `cad` extra)")
    p.add_argument("--cad", required=True, help="the Altium project directory or its .PrjPcb file")
    p.add_argument("--board", required=True, help="the atlas board label (boards/<label>/netlist.json)")
    p.add_argument("--rev", help="read the CAD files at this commit with `git show`")
    p.add_argument("--variant", help="also report the parts this project variant does not fit, against the PDF's all-fitted view")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_board_diff)

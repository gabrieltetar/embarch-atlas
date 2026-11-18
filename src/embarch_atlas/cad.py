"""`cad`: read an Altium project's `.SchDoc` sources into parts, pin nets and variants.

Read-only and standalone: it writes no atlas state and feeds neither `schematic` nor `join`. It is rung 1
of the schematic ingest ladder used as a cross-check (DESIGN.md "Native EDA project"), so its output has
the shape of `boards/<label>/netlist.json` with `method: "altium-schdoc"`.

Every format fact beyond the record kinds the task names is an assumption not yet checked against a real
`.SchDoc`; DESIGN.md lists them, and the comments below say `ASSUMED` where the code relies on one.
"""
from __future__ import annotations

import configparser
import hashlib
import io
import json
import posixpath
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from .smartpdf import is_auto_net, join_board_nets, natural_key

METHOD = "altium-schdoc"
# ASSUMED: a coordinate is `LOCATION.X` in whole schematic units plus `LOCATION.X_FRAC` in 1/100000 of one.
FRAC = 100000
# ASSUMED: parameter names (compared case-insensitively, without spaces or underscores) that carry the part number.
MPN_PARAMS = ("mpn", "manufacturerpartnumber", "partnumber", "manufacturerpart", "mfrpartnumber")
VALUE_PARAMS = ("value", "comment")
NOT_FITTED = "1"
OLEFILE_MSG = "reading a .SchDoc needs the olefile package: uv pip install 'embarch-atlas[cad]'"


class CadError(Exception):
    """A refusal that is one line on stderr and exit 2."""


# ---------------------------------------------------------------- the container and its records

def _olefile():
    try:
        import olefile
    except ImportError:
        raise CadError(OLEFILE_MSG) from None
    return olefile


def file_header_stream(data: bytes, name: str) -> bytes:
    olefile = _olefile()
    try:
        ole = olefile.OleFileIO(io.BytesIO(data))
    except (OSError, ValueError):
        raise CadError(f"{name} is not an OLE compound document") from None
    try:
        if not ole.exists("FileHeader"):
            raise CadError(f"{name} has no FileHeader stream")
        return ole.openstream("FileHeader").read()
    finally:
        ole.close()


def parse_records(stream: bytes) -> list[dict[str, str]]:
    """The `FileHeader` stream's text records as dicts.

    ASSUMED: each record is a 4-byte little-endian word, whose high byte is a flag (0 text, other binary,
    skipped) and whose low 24 bits are the body length, then the body: `|KEY=VALUE|...`, NUL-terminated,
    in Windows-1252. The first record (the one with a `HEADER` key) is dropped, so a record's index here is
    what `OWNERINDEX` counts from (ASSUMED: from 0, after the header).
    """
    out: list[dict[str, str]] = []
    pos = 0
    while pos + 4 <= len(stream):
        word = int.from_bytes(stream[pos:pos + 4], "little")
        flag, size = word >> 24, word & 0xFFFFFF
        body = stream[pos + 4:pos + 4 + size]
        pos += 4 + size
        if flag != 0:
            continue
        rec: dict[str, str] = {}
        for part in body.rstrip(b"\0").decode("cp1252", errors="replace").split("|"):
            if "=" in part:
                k, v = part.split("=", 1)
                rec[k.strip().upper()] = v
        out.append(rec)
    if out and "HEADER" in out[0]:
        out.pop(0)
    return out


def _int(v: Any, default: int = 0) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def coord(rec: dict[str, str], key: str) -> int:
    """`LOCATION.X` plus `LOCATION.X_FRAC` as one integer in 1/FRAC units."""
    return _int(rec.get(key)) * FRAC + _int(rec.get(key + "_FRAC"))


def pin_ends(rec: dict[str, str]) -> list[tuple[int, int]]:
    """A pin's two ends. ASSUMED: `LOCATION` is one end, `PINLENGTH` (whole units) its extent, and the low two
    bits of `PINCONGLOMERATE` the direction from `LOCATION` towards the other end: 0 +x, 1 +y, 2 -x, 3 -y."""
    x, y = coord(rec, "LOCATION.X"), coord(rec, "LOCATION.Y")
    n = _int(rec.get("PINLENGTH")) * FRAC
    dx, dy = ((1, 0), (0, 1), (-1, 0), (0, -1))[_int(rec.get("PINCONGLOMERATE")) & 3]
    return [(x, y), (x + dx * n, y + dy * n)]


def _on_segment(p: tuple[int, int], a: tuple[int, int], b: tuple[int, int]) -> bool:
    (px, py), (ax, ay), (bx, by) = p, a, b
    if (bx - ax) * (py - ay) != (by - ay) * (px - ax):
        return False
    return min(ax, bx) <= px <= max(ax, bx) and min(ay, by) <= py <= max(ay, by)


# ---------------------------------------------------------------- one sheet

class _UF:
    def __init__(self) -> None:
        self.p: dict[Any, Any] = {}

    def find(self, x: Any) -> Any:
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: Any, b: Any) -> None:
        self.p[self.find(a)] = self.find(b)


def read_sheet(stream: bytes) -> dict[str, Any]:
    """-> {"components": [{designator, uid, part_id, lib, params, pins: {pin: {name, node}}}], "uf", "wires",
    "labels", "powers", "ports"}; nets are formed by `sheet_nets`."""
    recs = parse_records(stream)
    kinds = [r.get("RECORD") for r in recs]
    comps: dict[int, dict[str, Any]] = {}
    for i, r in enumerate(recs):
        if kinds[i] == "1":
            comps[i] = {"designator": "", "uid": r.get("UNIQUEID", ""), "part_id": _int(r.get("CURRENTPARTID"), 1),
                        "lib": r.get("LIBREFERENCE", ""), "params": {}, "pins": {}}
    uf = _UF()
    points: set[tuple[int, int]] = set()
    wires: list[list[tuple[int, int]]] = []
    labels: list[tuple[str, tuple[int, int]]] = []
    powers: list[tuple[str, tuple[int, int]]] = []
    ports: list[tuple[str, tuple[int, int]]] = []
    for i, r in enumerate(recs):
        k, owner = kinds[i], _int(r.get("OWNERINDEX"), -1)
        if k == "34" and owner in comps:
            comps[owner]["designator"] = r.get("TEXT", "").strip()
        elif k == "41" and owner in comps:
            comps[owner]["params"][r.get("NAME", "")] = r.get("TEXT", "")
        elif k == "2" and owner in comps and _int(r.get("OWNERPARTID"), -2) == comps[owner]["part_id"]:
            node = ("pin", i)
            for e in pin_ends(r):
                points.add(e)
                uf.union(node, e)
            comps[owner]["pins"].setdefault(r.get("DESIGNATOR", "").strip(), {"name": r.get("NAME", "").strip() or None, "node": node})
        elif k == "27":
            n = _int(r.get("LOCATIONCOUNT"))
            vs = [(coord(r, f"X{j}"), coord(r, f"Y{j}")) for j in range(1, n + 1)]
            if vs:
                wires.append(vs)
        elif k in ("25", "17"):
            at = (coord(r, "LOCATION.X"), coord(r, "LOCATION.Y"))
            (labels if k == "25" else powers).append((r.get("TEXT", "").strip(), at))
        elif k == "18":
            ports.append((r.get("NAME", "").strip(), (coord(r, "LOCATION.X"), coord(r, "LOCATION.Y"))))
    for _, at in labels + powers + ports:
        points.add(at)
    for wi, vs in enumerate(wires):
        for v in vs:
            points.add(v)
            uf.union(("wire", wi), v)
    # a point lying on a wire segment joins that wire (T-junctions, a pin end or label mid-wire)
    for pt in points:
        for wi, vs in enumerate(wires):
            if any(_on_segment(pt, a, b) for a, b in zip(vs, vs[1:])):
                uf.union(("wire", wi), pt)
    return {"components": list(comps.values()), "uf": uf, "labels": labels, "powers": powers, "ports": ports}


def sheet_nets(sh: dict[str, Any], sheet: str, folded_ref: dict[int, str], issues: list[str]) -> dict[str, Any]:
    """The sheet's nets in `join_board_nets`'s sheet shape: {"ports": [...], "nets": {name: {pins, labels, ports,
    port_names}}}. A net's name is a power port's, else a net label's, else a port's, else Altium's `Net<ref>_<pin>`."""
    uf = sh["uf"]
    by_root: dict[Any, dict[str, Any]] = defaultdict(lambda: {"pins": [], "power": [], "label": [], "port": []})
    for ci, c in enumerate(sh["components"]):
        ref = folded_ref[ci]
        for pin, P in c["pins"].items():
            by_root[uf.find(P["node"])]["pins"].append(f"{ref}-{pin}")
    for key, items in (("power", sh["powers"]), ("label", sh["labels"]), ("port", sh["ports"])):
        for text, at in items:
            if text:
                by_root[uf.find(at)][key].append(text)
    nets: dict[str, Any] = {}
    for R in by_root.values():
        names = list(dict.fromkeys(sorted(R["power"]) + sorted(R["label"]) + sorted(R["port"])))
        pins = sorted(R["pins"], key=natural_key)
        if not names:
            if not pins:
                continue
            ref, _, pin = pins[0].rpartition("-")
            names = [f"Net{ref}_{pin}"]
        if len(names) > 1:
            issues.append(f"{sheet}: one net carries several names {names}, kept {names[0]}")
        name = names[0]
        if name in nets:  # the same name on two unconnected wires of one sheet is one net by name
            N = nets[name]
            N["pins"] = sorted(set(N["pins"]) | set(pins), key=natural_key)
        else:
            N = nets[name] = {"pins": pins, "labels": 0, "ports": 0, "port_names": []}
        N["labels"] += len(R["power"]) + len(R["label"])
        N["ports"] += len(R["port"])
        N["port_names"] += sorted(R["port"])
    return {"ports": sorted({t for t, _ in sh["ports"] if t}), "nets": nets}


# ---------------------------------------------------------------- the project and where its files come from

def _last_line(raw: bytes) -> str:
    lines = raw.decode(errors="replace").strip().splitlines()
    return lines[-1] if lines else "git failed"


class Source:
    """Files of the project directory, in the working tree or at a commit (`git show`, never a checkout)."""

    def __init__(self, directory: Path, rev: str | None) -> None:
        self.dir, self.rev = directory, rev

    def read(self, rel: str) -> bytes:
        rel = posixpath.normpath(rel.replace("\\", "/"))
        if self.rev is None:
            f = self.dir / rel
            if not f.is_file():
                raise CadError(f"{rel}: no such file in {self.dir}")
            return f.read_bytes()
        r = subprocess.run(["git", "-C", str(self.dir), "show", f"{self.rev}:./{rel}"], capture_output=True)
        if r.returncode != 0:
            raise CadError(f"{rel}: not readable at {self.rev}: {_last_line(r.stderr)}")
        return r.stdout

    def project_files(self) -> list[str]:
        if self.rev is None:
            return sorted(p.name for p in self.dir.iterdir() if p.is_file() and p.suffix.lower() == ".prjpcb")
        r = subprocess.run(["git", "-C", str(self.dir), "ls-tree", "--name-only", self.rev], capture_output=True, text=True)
        if r.returncode != 0:
            raise CadError(f"cannot list {self.dir} at {self.rev}: {_last_line(r.stderr.encode())}")
        return sorted(n for n in r.stdout.splitlines() if n.lower().endswith(".prjpcb"))


def open_project(arg: str, rev: str | None) -> tuple[Source, str]:
    if rev is not None and (rev.startswith("-") or not rev.strip()):
        raise CadError(f"--rev {rev!r} is not a commit")
    p = Path(arg)
    if p.is_file() or (rev is not None and p.suffix.lower() == ".prjpcb"):
        if p.suffix.lower() != ".prjpcb":
            raise CadError(f"{arg} is not a .PrjPcb file or a project directory")
        return Source(p.resolve().parent, rev), p.name
    if not p.is_dir():
        raise CadError(f"{arg}: no such project directory or .PrjPcb file")
    src = Source(p.resolve(), rev)
    found = src.project_files()
    if len(found) != 1:
        raise CadError(f"{arg} holds {len(found)} .PrjPcb files{' (' + ', '.join(found) + ')' if found else ''}; name one")
    return src, found[0]


def parse_project(text: str) -> tuple[list[str], dict[str, list[dict[str, str]]]]:
    """-> (SchDoc paths in `[DocumentN]` order, {variant name: [{designator, uid, kind}]}).

    ASSUMED: `[DocumentN]` has `DocumentPath=`; `[ProjectVariantN]` has `Description=` as its name and
    `VariationN=Designator=…|UniqueId=…|Kind=…`, Kind 1 meaning not fitted; the project file is an INI file."""
    cp = configparser.ConfigParser(interpolation=None, strict=False, delimiters=("=",), comment_prefixes=(";",))
    try:
        cp.read_string(text)
    except configparser.Error as e:
        raise CadError(f"the .PrjPcb does not parse as an INI file: {str(e).splitlines()[0]}") from None

    def numbered(prefix: str) -> list[str]:
        secs = [s for s in cp.sections() if re.fullmatch(prefix + r"\d+", s, re.I)]
        return sorted(secs, key=lambda s: int(re.search(r"\d+$", s).group(0)))

    docs = []
    for s in numbered("Document"):
        path = cp.get(s, "documentpath", fallback="").strip()
        if path.lower().endswith(".schdoc"):
            docs.append(path)
    variants: dict[str, list[dict[str, str]]] = {}
    for s in numbered("ProjectVariant"):
        name = cp.get(s, "description", fallback="").strip() or s
        rows = []
        for key in sorted((k for k in cp.options(s) if re.fullmatch(r"variation\d+", k)), key=lambda k: int(k[9:])):
            kv = {}
            for part in cp.get(s, key).split("|"):
                if "=" in part:
                    a, b = part.split("=", 1)
                    kv[a.strip().lower()] = b.strip()
            rows.append({"designator": kv.get("designator", ""), "uid": kv.get("uniqueid", ""), "kind": kv.get("kind", "")})
        variants[name] = rows
    return docs, variants


def norm_uid(uid: str) -> str:
    """ASSUMED: a unique ID may be written as a path (`\\AB12CD34`); the last segment, upper-cased, is the ID."""
    return uid.strip().split("\\")[-1].upper()


# ---------------------------------------------------------------- the project

def read_project(arg: str, rev: str | None = None, variant: str | None = None) -> dict[str, Any]:
    _olefile()
    src, prj_name = open_project(arg, rev)
    prj_bytes = src.read(prj_name)
    docs, variants = parse_project(prj_bytes.decode("utf-8-sig", errors="replace"))
    if variant is not None and variant not in variants:
        known = ", ".join(sorted(variants)) or "none"
        raise CadError(f"unknown variant {variant!r}; the project defines: {known}")
    if not docs:
        raise CadError(f"{prj_name} lists no .SchDoc documents")
    stems = [posixpath.splitext(posixpath.basename(d.replace("\\", "/")))[0] for d in docs]
    names = [s if stems.count(s) == 1 else posixpath.splitext(d.replace("\\", "/"))[0] for s, d in zip(stems, docs)]
    issues: list[str] = []
    sheets_raw = {n: read_sheet(file_header_stream(src.read(d), d)) for n, d in zip(names, docs)}

    # fold records of one designator into one physical part (a multi-part symbol is several records)
    phys: dict[str, dict[str, Any]] = {}
    folded: dict[str, dict[int, str]] = {}
    for sname in sorted(sheets_raw, key=natural_key):
        folded[sname] = {}
        for ci, c in enumerate(sheets_raw[sname]["components"]):
            ref = c["designator"] or f"?{c['uid'] or sname + str(ci)}"
            if not c["designator"]:
                issues.append(f"{sname}: a component (unique ID {c['uid'] or 'none'}) has no designator")
            folded[sname][ci] = ref
            P = phys.setdefault(ref, {"mpn": None, "value": None, "uids": [], "part_ids": [], "sheets": set(), "pins": {}, "lib": c["lib"]})
            if c["part_id"] in P["part_ids"]:
                issues.append(f"{ref}: part {c['part_id']} is placed more than once (duplicate designator?)")
            P["part_ids"].append(c["part_id"])
            if c["uid"] and c["uid"] not in P["uids"]:
                P["uids"].append(c["uid"])
            P["sheets"].add(sname)
            norm = {re.sub(r"[ _]", "", k).lower(): v for k, v in c["params"].items()}
            P["mpn"] = P["mpn"] or next((norm[k] for k in MPN_PARAMS if norm.get(k)), None)
            P["value"] = P["value"] or next((norm[k] for k in VALUE_PARAMS if norm.get(k)), None)
            for pin, info in c["pins"].items():
                P["pins"].setdefault(pin, {"name": info["name"], "name_source": "schdoc", "net": None})

    sheet_in = {s: sheet_nets(sheets_raw[s], s, folded[s], issues) for s in sheets_raw}
    nets, label_only = join_board_nets(sheet_in, issues)
    for net, shs in sorted(label_only.items()):
        if len(nets[net]["sheets"]) > 1 and shs:
            issues.append(f"net {net} reaches sheet(s) {', '.join(sorted(shs, key=natural_key))} only through a net label "
                          "(no port): joined by name, which holds only for a global net-identifier scope")
    for net, B in nets.items():
        for pid in B["pins"]:
            ref, _, pin = pid.rpartition("-")
            P = phys.get(ref)
            if P is None or pin not in P["pins"]:
                issues.append(f"net {net} lists pin {pid}, which no component lists")
            elif P["pins"][pin]["net"] not in (None, net):
                issues.append(f"pin {pid} is on two nets: {P['pins'][pin]['net']}, {net}")
            else:
                P["pins"][pin]["net"] = net

    result_variant = None
    dni: dict[str, str] = {}
    if variant is not None:
        by_uid = {norm_uid(u): ref for ref, P in phys.items() for u in P["uids"]}
        fitted_not, unknown = [], []
        for v in variants[variant]:
            if v["kind"] != NOT_FITTED:
                issues.append(f"variant {variant}: variation for {v['designator'] or v['uid']} has Kind={v['kind'] or 'none'}, not read")
                continue
            ref = by_uid.get(norm_uid(v["uid"]))
            if ref is None:
                unknown.append({"designator": v["designator"], "unique_id": v["uid"]})
            else:
                fitted_not.append({"designator": ref, "unique_id": v["uid"]})
                dni[ref] = variant
                if v["designator"] and v["designator"] != ref:
                    issues.append(f"variant {variant}: variation names {v['designator']} but unique ID {v['uid']} is now {ref}")
        result_variant = {"name": variant, "not_fitted": fitted_not, "unknown_unique_ids": unknown}

    components = {}
    for ref in sorted(phys, key=natural_key):
        P = phys[ref]
        components[ref] = {"mpn": P["mpn"], "value": P["value"], "dni": ref in dni,
                           "dni_evidence": [f"variant:{dni[ref]}"] if ref in dni else [],
                           "unique_id": P["uids"][0] if P["uids"] else None, "unique_ids": P["uids"],
                           "part_ids": sorted(P["part_ids"]), "library_reference": P["lib"] or None,
                           "parts": [ref], "sheets": sorted(P["sheets"], key=natural_key),
                           "pins": {k: P["pins"][k] for k in sorted(P["pins"], key=natural_key)}}
    out_nets = {}
    for name in sorted(nets, key=natural_key):
        B = nets[name]
        pins = sorted(B["pins"], key=natural_key)
        out_nets[name] = {"pins": pins, "sheets": sorted(B["sheets"], key=natural_key), "labels": B["labels"],
                          "ports": B["ports"], "auto": is_auto_net(name, pins), "aliases": B["aliases"]}
    doc: dict[str, Any] = {
        "board": posixpath.splitext(prj_name)[0],
        "source": {"file": prj_name, "sha256": hashlib.sha256(prj_bytes).hexdigest(), "rev": rev},
        "method": METHOD,
        "sheets": {s: {"page": None, "parent": None, "file": d.replace("\\", "/")} for s, d in zip(names, docs)},
        "components": components, "nets": out_nets,
        "counts": {"geometry": {}}, "issues": issues}
    if result_variant is not None:
        doc["variant"] = result_variant
    return doc


# ---------------------------------------------------------------- the verb

def render(doc: dict[str, Any]) -> str:
    rev = doc["source"]["rev"]
    lines = [f"{doc['source']['file']}{' at ' + rev if rev else ''}: {len(doc['sheets'])} sheets, "
             f"{len(doc['components'])} components, {len(doc['nets'])} nets ({doc['method']})", ""]
    for ref, C in doc["components"].items():
        lines.append(f"{ref}  mpn={C['mpn'] or '-'}  value={C['value'] or '-'}  unique_id={','.join(C['unique_ids']) or '-'}"
                     f"{'  DNI (' + C['dni_evidence'][0] + ')' if C['dni'] else ''}")
        for pin, P in C["pins"].items():
            lines.append(f"    {pin}{' ' + P['name'] if P['name'] else ''} -> {P['net'] or '-'}")
    v = doc.get("variant")
    if v:
        lines += ["", f"variant {v['name']}: {len(v['not_fitted'])} part(s) not fitted"]
        lines += [f"    {r['designator']}  unique_id={r['unique_id']}" for r in v["not_fitted"]]
        if v["unknown_unique_ids"]:
            lines.append("  variations naming a unique ID that is no longer in the schematic:")
            lines += [f"    {r['designator'] or '-'}  unique_id={r['unique_id']}" for r in v["unknown_unique_ids"]]
    if doc["issues"]:
        lines += ["", "issues:"] + [f"    {i}" for i in doc["issues"]]
    return "\n".join(lines)


def cmd_cad(args) -> int:
    try:
        doc = read_project(args.project, args.rev, args.variant)
    except CadError as e:
        print(f"embarch-atlas cad: {e}", file=sys.stderr)
        return 2
    print(json.dumps(doc, indent=2) if args.json else render(doc))
    return 0


def register(sub, groups) -> None:
    p = sub.add_parser("cad", help="read an Altium project's .SchDoc sources: components, per-pin nets, a variant's not-fitted "
                                   "parts (read-only; needs the `cad` extra)")
    p.add_argument("project", help="the project directory or its .PrjPcb file")
    p.add_argument("--rev", help="read every file at this commit with `git show` (nothing is checked out)")
    p.add_argument("--variant", help="also list this project variant's not-fitted parts, matched by unique ID")
    p.add_argument("--json", action="store_true", help="netlist.json-shaped output")
    p.set_defaults(func=cmd_cad)

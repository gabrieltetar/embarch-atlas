"""`mcu svd`: CMSIS-SVD -> `register` / `field` records, peripheral sidecar, per-instance slices.

The SVD is the register source of truth for an MCU (DESIGN.md, "SVD for registers").
Everything here is deterministic: records carry `prov: deterministic`, `src: svd`,
and no page (an SVD has none).

Handled: device -> peripheral -> cluster -> register property inheritance (size,
access, resetValue, resetMask), `derivedFrom` on peripherals, clusters, registers
and fields, `<dim>` arrays and lists (`%s`, `[%s]`) on clusters, registers and
fields, bit ranges as bitOffset/bitWidth, lsb/msb or `[hi:lo]`.

A derived peripheral inherits registers, group, description and size from its
base, but not its interrupts: vendor SVDs list shared IRQs on one instance, and
inheriting would give a derived instance its base's IRQ number.

Outputs, under `docs/<svd_doc_id>/`:
  doc.json                  identity (file, sha256, kind=svd, part, rev)
  records/register.jsonl    one per register per instance (derived instances expanded)
  records/field.jsonl       one per field per instance
  peripherals.json          bases, sizes, groups, derivedFrom, IRQs, counts
  s/<PERIPH>.md             compact slice per instance (~1K tokens for a typical block)
  card.md                   instance table
"""
from __future__ import annotations

import copy
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from . import __version__
from .common import atlas_dir, count_tokens, read_json, read_jsonl, sha256_file, verb_group, write_json, write_jsonl, write_text

# --------------------------------------------------------------------------- parsing

_PROPS = ("size", "access", "resetValue", "resetMask")

# SVD access -> SystemRDL-style vocabulary used by `field.access` (DESIGN.md).
_ACCESS = {
    "read-write": "rw", "read-only": "ro", "write-only": "wo",
    "writeOnce": "w1", "read-writeOnce": "rw1",
}
_MWV = {
    "oneToClear": "woclr", "zeroToClear": "wzc", "oneToSet": "wos", "zeroToSet": "wzs",
    "oneToToggle": "wot", "zeroToToggle": "wzt", "clear": "wclr", "set": "wset",
}


def svd_int(s: str | None) -> int | None:
    """SVD scaledNonNegativeInteger: 0x.., #binary (x = don't care, read as 0), 0b.., decimal."""
    if s is None:
        return None
    s = s.strip().lower().replace("_", "")
    if not s:
        return None
    if s.startswith("0x"):
        return int(s[2:].replace("x", "0"), 16)
    if s.startswith("#"):
        return int(s[1:].replace("x", "0"), 2)
    if s.startswith("0b"):
        return int(s[2:].replace("x", "0"), 2)
    m = re.match(r"(\d+)([kmg])?$", s)
    if m:
        mult = {"k": 1 << 10, "m": 1 << 20, "g": 1 << 30}.get(m.group(2) or "", 1)
        return int(m.group(1)) * mult
    raise ValueError(f"not an SVD integer: {s!r}")


def _text(e: ET.Element | None, tag: str, default: str | None = None) -> str | None:
    if e is None:
        return default
    x = e.find(tag)
    if x is None or x.text is None:
        return default
    return x.text.strip()


def _desc(e: ET.Element, tag: str = "description") -> str:
    return " ".join((_text(e, tag) or "").split())


def _props(e: ET.Element, inherited: dict) -> dict:
    out = dict(inherited)
    for k in _PROPS:
        v = _text(e, k)
        if v is not None:
            out[k] = v
    return out


def _dim_names(e: ET.Element) -> list[tuple[str, int]] | None:
    """[(index_string, k)] for a dim element, None when the element is not an array."""
    dim = _text(e, "dim")
    if dim is None:
        return None
    n = svd_int(dim)
    idx = _text(e, "dimIndex")
    if idx:
        m = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", idx)
        ml = re.fullmatch(r"\s*([A-Za-z])\s*-\s*([A-Za-z])\s*", idx)
        if m:
            names = [str(i) for i in range(int(m.group(1)), int(m.group(2)) + 1)]
        elif ml:
            names = [chr(c) for c in range(ord(ml.group(1)), ord(ml.group(2)) + 1)]
        else:
            names = [x.strip() for x in idx.split(",") if x.strip()]
    else:
        names = [str(i) for i in range(n)]
    return [(s, k) for k, s in enumerate(names[:n])]


def _expand_name(name: str, idx: str) -> str:
    if "[%s]" in name:
        return name.replace("[%s]", idx)
    return name.replace("%s", idx)


def _access(access: str | None, mwv: str | None = None, read_action: str | None = None) -> str | None:
    if read_action == "clear":
        return "rclr"
    if mwv in _MWV:
        return _MWV[mwv]
    return _ACCESS.get(access or "", access)


def _enums(f: ET.Element, enum_index: dict) -> list[dict] | None:
    out: list[dict] = []
    for ev_set in f.findall("enumeratedValues"):
        src = ev_set
        der = ev_set.get("derivedFrom")
        if der and der.split(".")[-1] in enum_index:
            src = enum_index[der.split(".")[-1]]
        for ev in src.findall("enumeratedValue"):
            v = _text(ev, "value")
            is_default = _text(ev, "isDefault") == "true"
            try:
                val = svd_int(v) if v is not None else None
            except ValueError:
                val = None
            out.append({"name": _text(ev, "name"), "value": val, "desc": _desc(ev),
                        **({"default": True} if is_default else {})})
    return out or None


def _parse_fields(r: ET.Element, reg_props: dict, enum_index: dict) -> list[dict]:
    fs = r.find("fields")
    if fs is None:
        return []
    by_name: dict[str, ET.Element] = {}
    for f in fs.findall("field"):
        by_name[_text(f, "name") or ""] = f
    out: list[dict] = []
    for f in fs.findall("field"):
        src = f
        der = f.get("derivedFrom")
        if der and der.split(".")[-1] in by_name:
            base = copy.deepcopy(by_name[der.split(".")[-1]])
            for child in f:  # own elements override
                old = base.find(child.tag)
                if old is not None:
                    base.remove(old)
                base.append(child)
            src = base
        lsb, width = _bits(src)
        name = _text(src, "name") or ""
        acc = _access(_text(src, "access") or reg_props.get("access"),
                      _text(src, "modifiedWriteValues"), _text(src, "readAction"))
        item = {"name": name, "desc": _desc(src), "lsb": lsb, "width": width, "access": acc,
                "enums": _enums(src, enum_index)}
        dims = _dim_names(src)
        if dims:
            inc = svd_int(_text(src, "dimIncrement")) or width
            for idx, k in dims:
                d = dict(item, name=_expand_name(name, idx), lsb=lsb + k * inc)
                out.append(d)
        else:
            out.append(item)
    return out


def _bits(f: ET.Element) -> tuple[int, int]:
    if _text(f, "bitOffset") is not None:
        return svd_int(_text(f, "bitOffset")), svd_int(_text(f, "bitWidth", "1"))
    if _text(f, "lsb") is not None:
        lsb, msb = svd_int(_text(f, "lsb")), svd_int(_text(f, "msb"))
        return lsb, msb - lsb + 1
    br = _text(f, "bitRange")
    m = re.fullmatch(r"\[\s*(\d+)\s*:\s*(\d+)\s*\]", br or "")
    if not m:
        raise ValueError(f"field {_text(f, 'name')!r}: no bit position")
    hi, lo = int(m.group(1)), int(m.group(2))
    return lo, hi - lo + 1


def _merge_derived(el: ET.Element, base: ET.Element) -> ET.Element:
    """Copy of `base` with every child element that `el` specifies replaced by `el`'s."""
    out = copy.deepcopy(base)
    for child in el:
        for old in out.findall(child.tag):
            out.remove(old)
        out.append(copy.deepcopy(child))
    return out


def _walk_registers(container: ET.Element, props: dict, prefix: str, base_off: int,
                    enum_index: dict, cluster: str | None) -> list[dict]:
    """Registers under a <registers> or <cluster> element, flattened, offsets absolute to the peripheral."""
    out: list[dict] = []
    siblings = {(_text(c, "name") or ""): c for c in container if c.tag in ("register", "cluster")}
    for el in container:
        if el.tag not in ("register", "cluster"):
            continue
        der = el.get("derivedFrom")
        if der and der.split(".")[-1] in siblings and siblings[der.split(".")[-1]] is not el:
            el = _merge_derived(el, siblings[der.split(".")[-1]])
        name = _text(el, "name") or ""
        off = svd_int(_text(el, "addressOffset", "0"))
        eprops = _props(el, props)
        dims = _dim_names(el) or [(None, 0)]
        inc = svd_int(_text(el, "dimIncrement")) or 0
        for idx, k in dims:
            ename = _expand_name(name, idx) if idx is not None else name
            eoff = base_off + off + k * inc
            if el.tag == "cluster":
                cname = f"{prefix}{ename}"
                out += _walk_registers(el, eprops, cname + "_", eoff, enum_index,
                                       f"{cluster}.{ename}" if cluster else ename)
                continue
            reset = svd_int(eprops["resetValue"]) or 0 if eprops.get("resetValue") is not None else None  # None: the SVD gives none
            size = svd_int(eprops.get("size", "32")) or 32
            mask = svd_int(eprops.get("resetMask")) if eprops.get("resetMask") else (1 << size) - 1
            disp = _text(el, "displayName")
            reg = {
                "name": f"{prefix}{ename}",
                "disp": _expand_name(disp, idx) if (disp and idx is not None) else disp,
                "desc": _desc(el), "off": eoff, "size": size,
                "access": _access(eprops.get("access"), _text(el, "modifiedWriteValues"), _text(el, "readAction")),
                "reset": reset, "reset_mask": mask,
                "fields": _parse_fields(el, eprops, enum_index),
            }
            if cluster:
                reg["cluster"] = cluster
            out.append(reg)
    return out


def parse_svd(path: str | Path) -> dict:
    """Parse an SVD into a plain dict model (see module doc). Derived peripherals are resolved."""
    root = ET.parse(path).getroot()
    dev_props = _props(root, {})
    cpu = root.find("cpu")
    periph_els = root.find("peripherals")
    if periph_els is None:
        raise ValueError(f"{path}: no <peripherals>")
    els = {(_text(p, "name") or ""): p for p in periph_els.findall("peripheral")}
    # enumeratedValues by name, for enum derivedFrom (rare, device-wide lookup by leaf name)
    enum_index = {(_text(ev, "name") or ""): ev for ev in root.iter("enumeratedValues") if _text(ev, "name")}

    order: list[str] = []
    periphs: dict[str, dict] = {}

    def build(name: str, stack: tuple = ()) -> dict:
        if name in periphs:
            return periphs[name]
        el = els[name]
        der = el.get("derivedFrom")
        if der and der not in els:
            raise ValueError(f"{name}: derivedFrom unknown peripheral {der!r}")
        if der and der in stack:
            raise ValueError(f"derivedFrom cycle at {name}")
        base = build(der, stack + (name,)) if der else None
        props = _props(el, dev_props if base is None else base["_props"])
        ab = el.find("addressBlock")
        size = svd_int(_text(ab, "size")) if ab is not None else None
        p = {
            "name": name,
            "derived_from": der,
            "base": svd_int(_text(el, "baseAddress", "0")),
            "size": size if size is not None else (base["size"] if base else None),
            "group": _text(el, "groupName") or (base["group"] if base else None),
            "desc": _desc(el) or (base["desc"] if base else ""),
            "irqs": [{"name": _text(i, "name"), "value": svd_int(_text(i, "value")), "desc": _desc(i)}
                     for i in el.findall("interrupt")],
            "_props": props,
        }
        own = el.find("registers")
        own_regs = _walk_registers(own, props, "", 0, enum_index, None) if own is not None else []
        if base is not None:
            regs = [copy.deepcopy(r) for r in base["registers"]]
            names = {r["name"]: i for i, r in enumerate(regs)}
            for r in own_regs:
                if r["name"] in names:
                    regs[names[r["name"]]] = r
                else:
                    regs.append(r)
            p["registers"] = regs
            p["own_registers"] = len(own_regs)
        else:
            p["registers"] = own_regs
            p["own_registers"] = len(own_regs)
        periphs[name] = p
        return p

    for name in els:
        build(name)
        order.append(name)
    for p in periphs.values():
        p.pop("_props", None)
    return {
        "device": _text(root, "name"),
        "version": _text(root, "version"),
        "desc": _desc(root),
        "cpu": {c.tag: (c.text or "").strip() for c in cpu} if cpu is not None else {},
        "width": svd_int(_text(root, "width", "32")),
        "peripherals": [periphs[n] for n in order],
    }


# --------------------------------------------------------------------------- records

def part_slug(device: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (device or "mcu").lower())


def _full(r: dict) -> int:
    return (1 << r["size"]) - 1


def _defined(r: dict) -> int:
    """The register bits whose reset the SVD defines: none without a resetValue, else its resetMask."""
    return 0 if r["reset"] is None else r["reset_mask"] & _full(r)


def hexs(v: int | None, width: int = 32) -> str | None:
    if v is None:
        return None
    return f"0x{v:0{max(1, (width + 3) // 4)}X}"


# `access` vocabulary of schemas/record.schema.json; finer SVD semantics go to `note`
_SCHEMA_ACCESS = {"rw", "ro", "wo", "rclr", "woclr", "w1s", "rsvd"}
_ACCESS_FOLD = {"w1": "wo", "rw1": "rw", "wos": "w1s", "wzc": "rw", "wzs": "rw", "wot": "rw", "wzt": "rw",
                "wclr": "rw", "wset": "rw"}


def _schema_access(a: str | None) -> tuple[str | None, str | None]:
    """(schema access, note) for an access value from `_access`."""
    if a is None or a in _SCHEMA_ACCESS:
        return a, None
    return _ACCESS_FOLD.get(a, "rw"), f"SVD access: {a}"


def _enum_map(enums: list[dict] | None) -> dict | None:
    """[{name, value, desc}] -> {"<value>": "<NAME>: <desc>"} (the schema's `enum` shape)."""
    if not enums:
        return None
    out = {}
    for e in enums:
        key = str(e["value"]) if e["value"] is not None else (e["name"] or "?")
        out[key] = f"{e['name']}: {e['desc']}" if e.get("desc") else (e["name"] or "")
    return out


def build_records(dev: dict, doc_id: str) -> tuple[list[dict], list[dict]]:
    """`register` and `field` records, one per register/field per instance.

    Ids carry the instance and names (`<part>.reg.<PERIPH>.<REG>[.<FIELD>]`); a field's `reg` is its
    register's record id. Offsets, clusters and derivedFrom are in `peripherals.json`."""
    part = part_slug(dev["device"])
    regs: list[dict] = []
    fields: list[dict] = []
    for p in dev["peripherals"]:
        for r in p["registers"]:
            rid = f"{part}.reg.{p['name']}.{r['name']}"
            racc, rnote = _schema_access(r["access"])
            regs.append({
                "id": rid, "kind": "register", "doc": doc_id, "p": None, "q": None,
                "src": "svd", "prov": "deterministic", "note": rnote,
                "name": r["name"], "addr": hexs(p["base"] + r["off"]), "width": r["size"],
                "reset": hexs(r["reset"], r["size"]) if _defined(r) == _full(r) else None, "desc": r["desc"] or None,
                **({"access": racc} if racc else {}),  # none given: the fields carry it
            })
            for f in r["fields"]:
                hi, lo = f["lsb"] + f["width"] - 1, f["lsb"]
                fbits = ((1 << f["width"]) - 1) << lo
                freset = (r["reset"] >> lo) & ((1 << f["width"]) - 1) if r["reset"] is not None else None
                if _defined(r) & fbits != fbits:
                    freset = None  # a bit the SVD's resetMask leaves undefined: no claimed reset
                facc, fnote = _schema_access(f["access"] or r["access"])
                fields.append({
                    "id": f"{rid}.{f['name']}", "kind": "field", "doc": doc_id, "p": None, "q": None,
                    "src": "svd", "prov": "deterministic", "note": fnote,
                    "reg": rid, "name": f["name"], "bits": [hi, lo],
                    "reset": hex(freset) if freset is not None else None, "enum": _enum_map(f["enums"]), "desc": f["desc"] or None,
                    **({"access": facc} if facc else {}),
                })
    return regs, fields


def counts(dev: dict) -> dict:
    own = [p for p in dev["peripherals"] if not p["derived_from"]]
    defined_regs = [r for p in own for r in p["registers"]]
    irq_values = {i["value"] for p in dev["peripherals"] for i in p["irqs"]}
    return {
        "peripherals": len(dev["peripherals"]),
        "base_definitions": len(own),
        "derived": len(dev["peripherals"]) - len(own),
        "registers_defined": len(defined_regs) + sum(p["own_registers"] for p in dev["peripherals"] if p["derived_from"]),
        "fields_defined": sum(len(r["fields"]) for r in defined_regs),
        "enum_values_defined": sum(len(f["enums"] or []) for r in defined_regs for f in r["fields"]),
        "interrupt_entries": sum(len(p["irqs"]) for p in dev["peripherals"]),
        "interrupt_lines": len(irq_values),
        "register_instances": sum(len(p["registers"]) for p in dev["peripherals"]),
        "field_instances": sum(len(r["fields"]) for p in dev["peripherals"] for r in p["registers"]),
    }


def _bits_str(f: dict) -> str:
    hi, lo = f["lsb"] + f["width"] - 1, f["lsb"]
    return f"[{hi}:{lo}]" if hi != lo else f"[{lo}]"


def render_slice(dev: dict, p: dict) -> str:
    """Compact, grep-able register map of one instance: one line per register."""
    irq = ", ".join(f"{i['value']} {i['name']}" for i in p["irqs"]) or "none listed"
    head = [f"# {p['name']} ({dev['device']} SVD v{dev['version']})", "",
            f"base {hexs(p['base'])}, size {hexs(p['size'], 12) if p['size'] is not None else '?'}, "
            f"group {p['group'] or '-'}, IRQ {irq}"
            + (f", derived from {p['derived_from']}" if p["derived_from"] else ""), "",
            "`off REG access rst=reset | FIELD[hi:lo][/access when it differs]`, fields MSB first", ""]
    lines = []
    for r in sorted(p["registers"], key=lambda r: (r["off"], r["name"])):
        fs = sorted(r["fields"], key=lambda f: -f["lsb"])
        cells = []
        for f in fs:
            acc = f["access"] if f["access"] and f["access"] != r["access"] else None
            cells.append(f"{f['name']}{_bits_str(f)}" + (f"/{acc}" if acc else ""))
        lines.append(f"{hexs(r['off'], 12)} {r['name']} {r['access'] or '-'} rst={hexs(r['reset'], r['size'])}"
                     + (" | " + " ".join(cells) if cells else ""))
    return "\n".join(head + lines) + "\n"


def _tok(text: str) -> int:
    n = count_tokens(text)
    return n if n is not None else (len(text) + 3) // 4


def render_card(dev: dict, doc_id: str, info: dict, c: dict, src: dict) -> str:
    part = part_slug(dev["device"])
    lines = [
        f"# {dev['device']} SVD v{dev['version']} (`{doc_id}`)", "",
        f"Source `{Path(src['file']).name}` sha256 {src['sha256'][:12]}. Deterministic (`mcu svd`), no page cites: "
        f"cite as `{doc_id} <PERIPH>.<REG>[.<FIELD>]`.", "",
        f"{c['peripherals']} peripherals ({c['base_definitions']} definitions + {c['derived']} derived), "
        f"{c['registers_defined']} registers and {c['fields_defined']} fields defined, "
        f"{c['interrupt_entries']} interrupt entries on {c['interrupt_lines']} lines. "
        f"Record ids `{part}.reg.<PERIPH>.<REG>[.<FIELD>]`, one per instance "
        f"({c['register_instances']} registers, {c['field_instances']} fields). "
        "Per-instance map: `s/<PERIPH>.md`. SVD defects found by `mcu vote` are `conflict` records in the reference "
        "manual's doc (`docs/<rm>/records/conflict.jsonl`, `docs/<rm>/vote.md`); their `refs` point here.", "",
        "One row per register definition; derived instances share its map. IRQ = line number (instance).", "",
        "| Definition | Base | Group | Regs | Slice tok | Derived instances (base) | IRQ |", "|---|---|---|---|---|---|---|",
    ]
    for p in dev["peripherals"]:
        if p["derived_from"]:
            continue
        kids = [q for q in dev["peripherals"] if q["derived_from"] == p["name"]]
        irq = " ".join(f"{i['value']}" + (f" ({q['name']})" if q is not p else "")
                       for q in [p] + kids for i in q["irqs"]) or "-"
        der = ", ".join(f"{q['name']} {hexs(q['base'])}" for q in kids) or "-"
        lines.append(f"| {p['name']} | {hexs(p['base'])} | {p['group'] or '-'} | {len(p['registers'])} "
                     f"| {info[p['name']]['slice_tokens']} | {der} | {irq} |")
    return "\n".join(lines) + "\n"


def write_svd_doc(svd_path: Path, doc_id: str, root: Path) -> dict:
    """Parse and write every output under `root/docs/<doc_id>/`. Returns the counts."""
    dev = parse_svd(svd_path)
    d = root / "docs" / doc_id
    src = {"file": str(svd_path.resolve()), "sha256": sha256_file(svd_path)}
    regs, fields = build_records(dev, doc_id)
    write_jsonl(d / "records" / "register.jsonl", regs)
    write_jsonl(d / "records" / "field.jsonl", fields)
    info: dict[str, dict] = {}
    for p in dev["peripherals"]:
        text = render_slice(dev, p)
        write_text(d / "s" / f"{p['name']}.md", text)
        info[p["name"]] = {
            "base": hexs(p["base"]), "size": hexs(p["size"], 12) if p["size"] is not None else None,
            "group": p["group"], "derived_from": p["derived_from"], "desc": p["desc"],
            "irqs": [{"name": i["name"], "value": i["value"]} for i in p["irqs"]],
            "registers": len(p["registers"]), "fields": sum(len(r["fields"]) for r in p["registers"]),
            "slice": f"s/{p['name']}.md", "slice_tokens": _tok(text),
        }
    irq_lines: dict[int, dict] = {}
    for p in dev["peripherals"]:
        for i in p["irqs"]:
            e = irq_lines.setdefault(i["value"], {"value": i["value"], "names": [], "peripherals": [], "desc": []})
            if i["name"] not in e["names"]:
                e["names"].append(i["name"])
            e["peripherals"].append(p["name"])
            if i["desc"] and i["desc"] not in e["desc"]:
                e["desc"].append(i["desc"])
    c = counts(dev)
    write_json(d / "peripherals.json", {
        "doc": doc_id, "device": dev["device"], "version": dev["version"], "cpu": dev["cpu"],
        "part": part_slug(dev["device"]), "source": src, "counts": c,
        "peripherals": info, "interrupts": [irq_lines[k] for k in sorted(irq_lines)],
    })
    write_json(d / "doc.json", {
        "doc_id": doc_id, "kind": "svd", "file": src["file"], "sha256": src["sha256"],
        "part": dev["device"], "rev": dev["version"], "tool": f"embarch-atlas {__version__} mcu svd",
    })
    write_text(d / "card.md", render_card(dev, doc_id, info, c, src))
    return c


# --------------------------------------------------------------------------- loading (for vote / rm)

class SvdDoc:
    """Read-side view of an `mcu svd` output: peripherals, registers and fields by name."""

    def __init__(self, doc_id: str, periph_info: dict, regs: list[dict], fields: list[dict]):
        self.doc_id = doc_id
        self.info = periph_info
        self.device = periph_info.get("device")
        self.part = periph_info.get("part") or part_slug(self.device)
        self.peripherals: dict[str, dict] = periph_info.get("peripherals", {})
        self.regs: dict[str, dict[str, dict]] = {}
        for r in regs:
            per, name = r["id"].split(".")[2:4]
            base = int(self.peripherals.get(per, {}).get("base") or "0x0", 16)
            self.regs.setdefault(per, {})[name] = dict(r, periph=per, off=hex(int(r["addr"], 16) - base), fields={})
        for f in fields:
            per, reg = f["id"].split(".")[2:4]
            r = self.regs.get(per, {}).get(reg)
            if r is not None:
                r["fields"][f["name"]] = dict(f, periph=per)

    @classmethod
    def load(cls, root: Path, doc_id: str) -> "SvdDoc":
        d = root / "docs" / doc_id
        if not (d / "peripherals.json").exists():
            raise SystemExit(f"{d}: no SVD doc (run `embarch-atlas mcu svd <svd> --doc-id {doc_id}` first)")
        return cls(doc_id, read_json(d / "peripherals.json"),
                   list(read_jsonl(d / "records" / "register.jsonl")),
                   list(read_jsonl(d / "records" / "field.jsonl")))

    def base_of(self, name: str) -> str:
        """The instance that holds the register definitions (follows derivedFrom)."""
        seen = set()
        while name in self.peripherals and self.peripherals[name].get("derived_from") and name not in seen:
            seen.add(name)
            name = self.peripherals[name]["derived_from"]
        return name

    def instances_of(self, base: str) -> list[str]:
        return [n for n in self.peripherals if self.base_of(n) == base]

    def definitions(self) -> list[str]:
        return [n for n, p in self.peripherals.items() if not p.get("derived_from")]

    def has(self, ident: str, default_periphs: list[str] | None = None) -> bool | None:
        """`PERIPH.REG`, `PERIPH.REG.FIELD` or `REG.FIELD` (searched in default_periphs, else everywhere).

        None when the identifier cannot be an SVD name (wrong shape)."""
        parts = ident.split(".")
        if len(parts) == 3:
            per, reg, f = parts
            r = self.regs.get(per, {}).get(reg)
            return bool(r and f in r["fields"])
        if len(parts) != 2:
            return None
        a, b = parts
        if a in self.regs:
            return b in self.regs[a]
        pool = default_periphs or list(self.regs)
        for per in pool:
            r = self.regs.get(per, {}).get(a)
            if r and b in r["fields"]:
                return True
        return False


def find_svd_doc(root: Path) -> str | None:
    """The only SVD doc under docs/, if exactly one exists."""
    found = [p.parent.name for p in (root / "docs").glob("*/peripherals.json")]
    return found[0] if len(found) == 1 else None


# --------------------------------------------------------------------------- CLI

def cmd_svd(args) -> int:
    svd_path = Path(args.svd)
    if args.slice:
        dev = parse_svd(svd_path)
        p = next((p for p in dev["peripherals"] if p["name"] == args.slice), None)
        if p is None:
            print(f"no peripheral {args.slice!r} in {svd_path}", file=sys.stderr)
            return 2
        sys.stdout.write(render_slice(dev, p))
        return 0
    if not args.doc_id:
        print("--doc-id is required unless --slice is given", file=sys.stderr)
        return 2
    root = atlas_dir(args.atlas_dir)
    c = write_svd_doc(svd_path, args.doc_id, root)
    print(f"{args.doc_id}: {c['peripherals']} peripherals ({c['base_definitions']} definitions + {c['derived']} derived), "
          f"{c['registers_defined']} registers, {c['fields_defined']} fields, {c['enum_values_defined']} enum values defined; "
          f"{c['interrupt_entries']} interrupt entries on {c['interrupt_lines']} lines; "
          f"records: {c['register_instances']} registers, {c['field_instances']} fields "
          f"-> {root / 'docs' / args.doc_id}")
    return 0


def register(sub, groups) -> None:
    mcu = verb_group(sub, groups, "mcu", "MCU knowledge: SVD, register vote, reference-manual sections")
    p = mcu.add_parser("svd", help="CMSIS-SVD -> register/field records, peripheral sidecar, per-instance slices")
    p.add_argument("svd", help="the SVD file (local; never fetched)")
    p.add_argument("--doc-id", help="doc id for the SVD, e.g. demo-mcu-svd")
    p.add_argument("--slice", metavar="PERIPH", help="print one instance's slice to stdout and write nothing")
    p.set_defaults(func=cmd_svd)

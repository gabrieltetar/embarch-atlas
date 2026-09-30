"""dt.json and in_use.json from an atlas's build artifacts.

`dt.json`: every MCU pin use in the devicetree (pinctrl states with STM32_PINMUX
decoded, every GPIO phandle-array with its flags, raw cells for binding-less
nodes, GPIO-routed interrupts), bus devices with their address, chosen nodes,
aliases, scalar properties of okay nodes, the EXTI line of every GPIO pin used,
and the build's drivers: `DT_DRV_COMPAT` in the compiled TUs, label/alias
consumers, and which GPIO properties a driver arms as an interrupt.

`in_use.json`, per SoC peripheral instance: `dt_okay`, `instantiated` (its
`__device_dts_ord_N` is in the ELF), `touched_by_code` (its register block is
referenced from a literal pool or a device config object in the linked image),
`compatible`, `node`.

The edt.pickle needs Zephyr's python-devicetree (and PyYAML, which edtlib
imports). When this interpreter has no PyYAML, the extraction runs in the
build's own Python (`Python3_EXECUTABLE` from the build), with this file as
the script. Everything else here is stdlib only.
"""
from __future__ import annotations

import json
import os
import pickle
import re
import struct
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

if __package__ in (None, ""):   # run as a script by the build's Python
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from embarch_atlas.common import read_json, sha256_file, write_json  # noqa: E402

GPIO_FLAG_BITS = [(0x1, "ACTIVE_LOW"), (0x10, "PULL_UP"), (0x20, "PULL_DOWN")]
BIAS_PROPS = ("bias-pull-up", "bias-pull-down", "bias-disable", "drive-open-drain", "drive-push-pull",
              "output-low", "output-high", "slew-rate")
# ARMv6-M / ARMv7-M private peripheral bus blocks the SVD usually omits
CORE_BLOCKS = [("SysTick", 0xE000E010, 0x10), ("NVIC", 0xE000E100, 0x400),
               ("SCB", 0xE000ED00, 0x90), ("MPU", 0xE000ED90, 0x20)]


# ---------------------------------------------------------------- small helpers

def gpio_flags(f: int) -> list[str]:
    out = [n for b, n in GPIO_FLAG_BITS if f & b]
    if f & 0x2:
        out.append("OPEN_DRAIN" if f & 0x4 else "OPEN_SOURCE")
    return out


def flags_str(f: int) -> str:
    fl = gpio_flags(f)
    return "|".join(fl) if fl else "ACTIVE_HIGH"


def stm32_pinmux(v: int) -> dict:
    """STM32_PINMUX(port, line, mode): port bits 13:9, line 8:5, mode 4:0 (0x10 analog, 0x11 GPIO)."""
    port = chr(ord("A") + ((v >> 9) & 0x1F))
    line = (v >> 5) & 0xF
    mode = v & 0x1F
    af = "ANALOG" if mode == 0x10 else ("GPIO" if mode == 0x11 else f"AF{mode}")
    return {"pin": f"P{port}{line}", "port": port, "line": line, "af": af, "mode": mode}


def token(compat: str) -> str:
    """A compatible as DT_DRV_COMPAT spells it."""
    return re.sub(r"[^a-z0-9]", "_", compat.lower())


class Rel:
    """Absolute source paths -> short tagged ones (`fw:boards/...`, `zephyr:dts/...`)."""

    def __init__(self, bases: Iterable[tuple[str, str]] = ()):
        self.bases = sorted(((os.path.normpath(b), t) for b, t in bases if b), key=lambda x: -len(x[0]))

    def __call__(self, p: str | None) -> str | None:
        if not p:
            return p
        p = os.path.normpath(p)
        for base, tag in self.bases:
            if p == base or p.startswith(base + os.sep):
                return f"{tag}:{os.path.relpath(p, base)}"
        return p


def _prop_loc(node, name: str, rel: Rel) -> str | None:
    try:
        pr = node._node.props[name]
    except (AttributeError, KeyError):
        return None
    fn, ln = getattr(pr, "filename", None), getattr(pr, "lineno", None)
    return f"{rel(fn)}:{ln}" if fn else None


def _labels(n) -> list[str]:
    return list(getattr(n, "labels", []) or [])


def _compats(n) -> list[str]:
    return list(getattr(n, "compats", []) or [])


def _scalar(v: Any):
    if isinstance(v, (int, str, bool)) or v is None:
        return v
    if isinstance(v, bytes):
        return v.hex()
    if isinstance(v, list) and all(isinstance(x, (int, str)) for x in v):
        return v
    return None


# ---------------------------------------------------------------- edt -> raw dict

def extract_edt(edt, rel: Rel | None = None) -> dict:
    """Everything dt.json needs from an edtlib EDT (or anything shaped like one)."""
    rel = rel or Rel()
    gpio_ctrl: dict[str, str] = {}
    for n in edt.nodes:
        if any(c.endswith("-gpio") and c.startswith("st,stm32") for c in _compats(n)) or "st,stm32-gpio" in _compats(n):
            lab = next((l for l in _labels(n) if re.fullmatch(r"gpio[a-z]", l)), None)
            if lab:
                gpio_ctrl[n.path] = lab[-1].upper()
        elif getattr(n, "props", {}).get("gpio-controller") and _labels(n):
            lab = next((l for l in _labels(n) if re.fullmatch(r"gpio[a-z]", l)), None)
            if lab:
                gpio_ctrl[n.path] = lab[-1].upper()
    stm32 = any(c.startswith("st,stm32") for n in edt.nodes for c in _compats(n) if "pinctrl" in c)

    nodes, uses, devices = {}, [], []
    for n in edt.nodes:
        okay = getattr(n, "status", "okay") == "okay"
        parent = getattr(n, "parent", None)
        regs = [[r.addr, r.size] for r in (getattr(n, "regs", None) or [])]
        irqs = []
        for i in getattr(n, "interrupts", None) or []:
            irqs.append({"controller": i.controller.path, **{k: v for k, v in (i.data or {}).items()}})
        nodes[n.path] = {
            "labels": _labels(n), "compats": _compats(n), "okay": okay, "status": getattr(n, "status", "okay"),
            "binding": getattr(n, "matching_compat", None) is not None or bool(getattr(n, "props", {})),
            "ordinal": getattr(n, "dep_ordinal", None), "regs": regs, "irqs": irqs,
            "parent": parent.path if parent is not None else None,
            "bus": list(getattr(n, "buses", None) or []), "on_bus": list(getattr(n, "on_buses", None) or []),
            "props": {k: {"val": _scalar(p.val), "loc": _prop_loc(n, k, rel)}
                      for k, p in (getattr(n, "props", {}) or {}).items()
                      if okay and _scalar(p.val) is not None and not k.startswith("pinctrl-") and k != "compatible"},
        }
        if not okay:
            continue
        compat = (_compats(n) or (_compats(parent) if parent is not None else []) or [None])[0]
        compats = _compats(n) or (_compats(parent) if parent is not None else [])
        base = {"node": n.path, "labels": _labels(n), "compat": compat, "compats": compats}
        # pinctrl states
        for idx, st in enumerate(getattr(n, "pinctrls", None) or []):
            for conf in st.conf_nodes:
                for grp in [conf] + list(getattr(conf, "children", {}).values()):
                    pm = grp.props.get("pinmux")
                    if pm is None:
                        continue
                    vals = pm.val if isinstance(pm.val, list) else [pm.val]
                    for v in vals:
                        dec = stm32_pinmux(v) if stm32 else {"pin": None, "raw": v}
                        bias = [k for k in BIAS_PROPS if k in grp.props and grp.props[k].val not in (False, None)
                                and k != "slew-rate"]
                        uses.append({**base, "pin": dec.get("pin"), "kind": "pinctrl", "prop": f"pinctrl-{idx}",
                                     "state": st.name, "func": grp.name, "af": dec.get("af"), "raw": v,
                                     "flags": "|".join(bias) or None, "bias": bias,
                                     "loc": _prop_loc(n, f"pinctrl-{idx}", rel)})
        # GPIO phandle-arrays
        for pname, p in (getattr(n, "props", {}) or {}).items():
            if p.type != "phandle-array" or not isinstance(p.val, list):
                continue
            for idx, cad in enumerate(p.val):
                if cad is None or cad.controller.path not in gpio_ctrl:
                    continue
                pin, fl = cad.data.get("pin"), cad.data.get("flags", 0)
                loc = _prop_loc(n, pname, rel)
                uses.append({**base, "pin": f"P{gpio_ctrl[cad.controller.path]}{pin}", "kind": "gpio", "prop": pname,
                             "index": idx, "flags": flags_str(fl), "flags_raw": fl, "loc": loc,
                             "soc_defined": bool(loc and loc.startswith("zephyr:dts/"))})
        # binding-less nodes: edtlib leaves *-gpios untyped; read the raw dtlib cells
        if getattr(n, "matching_compat", None) is None and not getattr(n, "props", None) and hasattr(n, "_node"):
            for pname, rp in n._node.props.items():
                if not (pname == "gpios" or pname.endswith("-gpios")):
                    continue
                cells = [int.from_bytes(rp.value[j:j + 4], "big") for j in range(0, len(rp.value), 4)]
                i = k = 0
                while i < len(cells):
                    ctrl = edt._dt.phandle2node.get(cells[i])
                    cpath = ctrl.path if ctrl else None
                    if cpath not in gpio_ctrl:
                        break
                    nc = 2
                    gc = ctrl.props.get("#gpio-cells") if ctrl is not None else None
                    if gc is not None:
                        try:
                            nc = int.from_bytes(gc.value, "big")
                        except Exception:
                            nc = 2
                    pin, fl = cells[i + 1], (cells[i + 2] if nc >= 2 and i + 2 < len(cells) else 0)
                    loc = _prop_loc(n, pname, rel)
                    uses.append({**base, "compat": None, "compats": [], "pin": f"P{gpio_ctrl[cpath]}{pin}",
                                 "kind": "gpio", "prop": pname, "index": k, "flags": flags_str(fl), "flags_raw": fl,
                                 "loc": loc, "no_binding": True,
                                 "soc_defined": bool(loc and loc.startswith("zephyr:dts/"))})
                    i += 1 + nc
                    k += 1
        # interrupts routed to a GPIO controller
        for k2, i in enumerate(getattr(n, "interrupts", None) or []):
            if i.controller.path in gpio_ctrl:
                pin = (i.data or {}).get("pin", (i.data or {}).get("irq"))
                uses.append({**base, "pin": f"P{gpio_ctrl[i.controller.path]}{pin}", "kind": "interrupt",
                             "prop": "interrupts", "index": k2, "flags": None, "loc": _prop_loc(n, "interrupts", rel)})
        # bus devices
        if parent is not None and getattr(parent, "buses", None) and regs:
            devices.append({"node": n.path, "labels": _labels(n), "compat": (_compats(n) or [None])[0],
                            "bus": parent.path, "bus_labels": _labels(parent), "bus_kind": list(parent.buses),
                            "reg": regs[0][0], "ordinal": getattr(n, "dep_ordinal", None),
                            "loc": _prop_loc(n, "reg", rel)})
    for u in uses:
        if stm32 and u.get("pin") and u["kind"] in ("gpio", "interrupt"):
            u["exti_line"] = int(u["pin"][2:])
    dt = getattr(edt, "_dt", None)
    return {
        "gpio_controllers": gpio_ctrl,
        "stm32": stm32,
        "nodes": nodes,
        "uses": uses,
        "devices": devices,
        "chosen": {k: v.path for k, v in (getattr(edt, "chosen_nodes", {}) or {}).items()},
        "aliases": {a: n.path for a, n in (getattr(dt, "alias2node", {}) or {}).items()} if dt is not None else {},
    }


def load_edt(pickle_path: Path, zephyr_base: Path):
    src = zephyr_base / "scripts" / "dts" / "python-devicetree" / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    with open(pickle_path, "rb") as f:
        return pickle.load(f)


def raw_from_pickle(pickle_path: Path, zephyr_base: Path, rel_bases: list[tuple[str, str]],
                    python: str | None = None) -> dict:
    """extract_edt() on the pickle, in-process when PyYAML is importable, else in `python`."""
    try:
        import yaml  # noqa: F401  (edtlib imports it)
        return extract_edt(load_edt(pickle_path, zephyr_base), Rel(rel_bases))
    except ImportError:
        pass
    if not python:
        raise SystemExit("edt.pickle needs PyYAML: install it, or the build's Python must be known")
    r = subprocess.run([python, __file__, "--raw", str(pickle_path), str(zephyr_base), json.dumps(rel_bases)],
                       capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"edt extraction in {python} failed:\n{r.stderr[-2000:]}")
    return json.loads(r.stdout)


# ---------------------------------------------------------------- ELF (stdlib reader)

class Elf:
    """Just enough ELF: sections and symbols, 32/64-bit, either endianness."""

    def __init__(self, path: Path):
        self.data = Path(path).read_bytes()
        d = self.data
        if d[:4] != b"\x7fELF":
            raise ValueError(f"{path}: not an ELF file")
        self.is64 = d[4] == 2
        self.end = "<" if d[5] == 1 else ">"
        e = self.end
        if self.is64:
            shoff, = struct.unpack_from(e + "Q", d, 0x28)
            shentsize, shnum, shstrndx = struct.unpack_from(e + "HHH", d, 0x3A)
        else:
            shoff, = struct.unpack_from(e + "I", d, 0x20)
            shentsize, shnum, shstrndx = struct.unpack_from(e + "HHH", d, 0x2E)
        self.sections = []
        for i in range(shnum):
            o = shoff + i * shentsize
            if self.is64:
                name, typ, flags, addr, off, size, link, info, align, entsize = struct.unpack_from(e + "IIQQQQIIQQ", d, o)
            else:
                name, typ, flags, addr, off, size, link, info, align, entsize = struct.unpack_from(e + "IIIIIIIIII", d, o)
            self.sections.append({"name_off": name, "type": typ, "flags": flags, "addr": addr, "off": off,
                                  "size": size, "link": link, "entsize": entsize})
        if self.sections and shstrndx < len(self.sections):
            st = self.sections[shstrndx]
            for s in self.sections:
                s["name"] = self._str(st["off"], s["name_off"])
        self._syms = None

    def _str(self, tab_off: int, idx: int) -> str:
        end = self.data.index(b"\0", tab_off + idx)
        return self.data[tab_off + idx:end].decode("utf-8", "replace")

    def symbols(self) -> list[dict]:
        if self._syms is not None:
            return self._syms
        out = []
        for s in self.sections:
            if s["type"] != 2:   # SHT_SYMTAB
                continue
            strtab = self.sections[s["link"]]
            ent = s["entsize"] or (24 if self.is64 else 16)
            for i in range(s["size"] // ent):
                o = s["off"] + i * ent
                if self.is64:
                    name, info, other, shndx, value, size = struct.unpack_from(self.end + "IBBHQQ", self.data, o)
                else:
                    name, value, size, info, other, shndx = struct.unpack_from(self.end + "IIIBBH", self.data, o)
                out.append({"name": self._str(strtab["off"], name), "value": value, "size": size,
                            "type": info & 0xF, "bind": info >> 4, "shndx": shndx})
        self._syms = out
        return out

    def device_ordinals(self) -> set[int]:
        return {int(m[1]) for s in self.symbols() if (m := re.fullmatch(r"__device_dts_ord_(\d+)", s["name"]))}

    def words(self, sec: dict, lo: int, hi: int):
        """(address, value) of every aligned 32-bit word of `sec` in [lo, hi)."""
        lo = max(lo, sec["addr"])
        hi = min(hi, sec["addr"] + sec["size"])
        lo += (-lo) % 4
        base = sec["off"] - sec["addr"]
        for a in range(lo, hi - 3, 4):
            yield a, struct.unpack_from(self.end + "I", self.data, base + a)[0]


def mmio_refs(elf: Elf, blocks: list[tuple[str, int, int]]) -> dict:
    """Which register blocks the linked image references.

    Code: literal-pool words (ARM `$d` mapping-symbol regions inside executable
    sections), attributed to the containing function. Data: aligned words in
    allocated, non-executable PROGBITS sections, attributed to the containing
    object. Returns {block: {"code": {fn: [offsets]}, "data": {obj: [offsets]}}}.
    """
    blocks = sorted(blocks, key=lambda b: b[2])       # smallest first: nested blocks win
    starts = sorted({b[1] for b in blocks})

    def classify(v: int):
        for name, base, size in blocks:
            if base <= v < base + size:
                return name, v - base
        return None

    lo_all = min((b[1] for b in blocks), default=0)
    hi_all = max((b[1] + b[2] for b in blocks), default=0)
    syms = elf.symbols()
    funcs = sorted((s["value"] & ~1, s["size"], s["name"]) for s in syms if s["type"] == 2 and s["size"])
    objs = sorted((s["value"], s["size"], s["name"]) for s in syms if s["type"] == 1 and s["size"])
    maps = defaultdict(list)
    for s in syms:
        if s["name"] in ("$d", "$t", "$a", "$x") or s["name"].startswith(("$d.", "$t.", "$a.")):
            maps[s["shndx"]].append((s["value"] & ~1 if s["name"][1] != "d" else s["value"], s["name"][1]))
    import bisect
    fstarts = [f[0] for f in funcs]
    ostarts = [o[0] for o in objs]

    def owner(table, st, a):
        k = bisect.bisect_right(st, a) - 1
        if k >= 0 and table[k][0] <= a < table[k][0] + table[k][1]:
            return table[k][2]
        return None

    hits: dict = defaultdict(lambda: {"code": defaultdict(set), "data": defaultdict(set)})
    for idx, sec in enumerate(elf.sections):
        if not sec.get("addr") or sec["type"] != 1 or not (sec["flags"] & 2):   # PROGBITS + ALLOC
            continue
        if sec["flags"] & 4:                           # executable: only the $d regions
            ms = sorted(maps.get(idx, []))
            regions = []
            for j, (a, kind) in enumerate(ms):
                if kind == "d":
                    end = ms[j + 1][0] if j + 1 < len(ms) else sec["addr"] + sec["size"]
                    regions.append((a, end))
            for lo, hi in regions:
                for a, v in elf.words(sec, lo, hi):
                    if lo_all <= v < hi_all and (c := classify(v)):
                        hits[c[0]]["code"][owner(funcs, fstarts, a) or f"{sec['name']}@{a:#x}"].add(c[1])
        else:
            for a, v in elf.words(sec, sec["addr"], sec["addr"] + sec["size"]):
                if lo_all <= v < hi_all and (c := classify(v)):
                    o = owner(objs, ostarts, a)
                    if o:
                        hits[c[0]]["data"][o].add(c[1])
    del starts
    return {b: {"code": {k: sorted(v) for k, v in h["code"].items()}, "data": {k: sorted(v) for k, v in h["data"].items()}}
            for b, h in hits.items()}


# ---------------------------------------------------------------- SVD (stdlib reader)

def svd_peripherals(svd: Path) -> list[dict]:
    """[{name, base, size, group}] with derivedFrom resolved."""
    root = ET.parse(svd).getroot()
    raw = {}
    for p in root.iter("peripheral"):
        name = (p.findtext("name") or "").strip()
        if not name:
            continue
        ab = p.find("addressBlock")
        raw[name] = {"name": name, "base": int(p.findtext("baseAddress") or "0", 0),
                     "size": int(ab.findtext("size"), 0) if ab is not None and ab.findtext("size") else None,
                     "group": (p.findtext("groupName") or "").strip() or None, "derived": p.get("derivedFrom")}
    for r in raw.values():
        if r["size"] is None and r["derived"] in raw:
            r["size"] = raw[r["derived"]]["size"]
        if r["group"] is None and r["derived"] in raw:
            r["group"] = raw[r["derived"]]["group"]
    return [{"name": r["name"], "base": r["base"], "size": r["size"] or 0x400, "group": r["group"]} for r in raw.values()]


def family(name: str) -> str:
    """I2C1 -> I2C, USART2 -> USART, GPIOA -> GPIO."""
    f = re.sub(r"\d+$", "", name)
    m = re.fullmatch(r"(GPIO|PORT|P)([A-Z])", f)
    return m[1] if m else (f or name)


# ---------------------------------------------------------------- compile_commands

def scan_sources(files: Iterable[str], known_labels: set[str]) -> dict:
    """DT_DRV_COMPAT per TU, DT_NODELABEL/DT_ALIAS consumers, and interrupt-armed GPIO properties."""
    drivers, consumers, armed = defaultdict(set), defaultdict(set), defaultdict(set)
    for f in files:
        try:
            s = Path(f).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        comps = re.findall(r"#define\s+DT_DRV_COMPAT\s+(\w+)", s)
        for c in comps:
            drivers[c].add(f)
        for m in re.finditer(r"DT_NODELABEL\((\w+)\)", s):
            if m[1] in known_labels:
                consumers[m[1]].add(f)
        for m in re.finditer(r"DT_ALIAS\((\w+)\)", s):
            consumers["alias:" + m[1]].add(f)
        if comps and "gpio_pin_interrupt_configure_dt" in s:
            field2prop = dict(re.findall(
                r"\.(\w+)\s*=\s*(?:\{\s*)?(?:GPIO_DT_SPEC_INST_GET(?:_OR|_BY_IDX(?:_OR)?)?|DT_INST_FOREACH_PROP_ELEM_SEP)"
                r"\(\s*\w+,\s*(\w+)", s))
            alias = dict(re.findall(r"(\w+)\s*=\s*&(?:cfg|config)->(\w+)", s))
            for expr in re.findall(r"gpio_pin_interrupt_configure_dt\(\s*&?(?:(?:cfg|config)->)?(\w+)", s):
                fld = alias.get(expr, expr)
                if fld in field2prop:
                    for c in comps:
                        armed[c].add(field2prop[fld].replace("_", "-"))
    return {"drivers": {k: sorted(v) for k, v in drivers.items()},
            "consumers": {k: sorted(v) for k, v in consumers.items()},
            "armed": {k: sorted(v) for k, v in armed.items()}}


def tree_c_files(root: Path, skip: Iterable[str] = ("build", ".git", "embarch", "twister-out")) -> list[str]:
    out = []
    if not root.is_dir():
        return out
    skip = set(skip)
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in skip and not d.startswith("build")]
        out += [os.path.join(dp, f) for f in fn if f.endswith(".c")]
    return sorted(out)


# ---------------------------------------------------------------- assemble

def rel_bases(stamp: dict, ad: Path) -> list[tuple[str, str]]:
    b = stamp.get("build", {})
    out = []
    if b.get("worktree"):
        out.append((str((ad / b["worktree"]).resolve()), "fw"))
    for m in b.get("zephyr_modules", []):
        if b.get("worktree") and Path(m).resolve() == (ad / b["worktree"]).resolve():
            continue
        out.append((m, Path(m).name))
    if b.get("zephyr_base"):
        out.append((b["zephyr_base"], "zephyr"))
    if b.get("dir"):
        out.append((str((ad / b["dir"]).resolve()), "build"))
    return out


def instance_name(node: str, info: dict, svd_by_base: dict) -> str | None:
    regs = info.get("regs") or []
    if regs and regs[0][0] is not None and regs[0][0] in svd_by_base:
        return svd_by_base[regs[0][0]]
    for l in info.get("labels", []):
        if re.fullmatch(r"[a-z][a-z0-9_]*", l):
            u = l.upper()
            u = {"TIMERS": "TIM"}.get(re.sub(r"\d+$", "", u), re.sub(r"\d+$", "", u)) + (re.search(r"\d*$", u)[0])
            return u
    return None


def build_in_use(raw: dict, inst: set[int], mmio: dict, svd: list[dict] | None) -> dict:
    """{"peripherals": {INSTANCE: {dt_okay, instantiated, touched_by_code, compatible, node}}}"""
    svd_by_base = {p["base"]: p["name"] for p in (svd or [])}
    svd_by_base.update({base: name for name, base, _ in CORE_BLOCKS if base not in svd_by_base})
    periph: dict[str, dict] = {}
    node_of: dict[str, str] = {}
    for path, info in raw["nodes"].items():
        regs = info.get("regs") or []
        if not regs or regs[0][0] is None:
            continue
        addr = regs[0][0]
        if info.get("on_bus"):
            continue
        in_space = (addr in svd_by_base) if svd else (0x40000000 <= addr < 0x60000000 or addr >= 0xE0000000)
        in_space = in_space or addr >= 0xE0000000 and addr in svd_by_base
        if not in_space or not (path.startswith("/soc") or addr >= 0xE0000000):
            continue
        name = instance_name(path, info, svd_by_base)
        if not name:
            continue
        cur = periph.get(name)
        if cur:
            # two nodes on one block (pin-controller and gpioa): the one labelled like the instance wins,
            # then the shallower one
            def rank(pth, inf):
                return (name.lower() not in [l.lower() for l in inf.get("labels", [])], pth.count("/"))
            if rank(cur["node"], raw["nodes"][cur["node"]]) <= rank(path, info):
                continue
        periph[name] = {"dt_okay": info["okay"], "instantiated": info.get("ordinal") in inst,
                        "touched_by_code": False, "compatible": (info.get("compats") or [""])[0], "node": path}
        node_of[name] = path
    compat_of = {n: p["compatible"] for n, p in periph.items()}
    for name, h in mmio.items():
        p = periph.setdefault(name, {"dt_okay": False, "instantiated": False, "touched_by_code": False,
                                     "compatible": "", "node": ""})
        touched = False
        for fn in h.get("code", {}):
            # generic driver code that also touches a sibling of the same compatible is not evidence
            siblings = [o for o, oh in mmio.items() if o != name and fn in oh.get("code", {})]
            same = [o for o in siblings if p["compatible"] and compat_of.get(o) == p["compatible"]]
            fam = [o for o in siblings if family(o) == family(name)]
            if not same and len(fam) < 2:
                touched = True
                break
        if not touched:
            for obj in h.get("data", {}):
                others = [o for o, oh in mmio.items() if o != name and obj in oh.get("data", {})]
                if not others and re.search(r"(^|_)(cfg|config|conf|data|dev)(_|\d|$)", obj):
                    touched = True
                    break
        p["touched_by_code"] = touched
    return {"peripherals": dict(sorted(periph.items()))}


def run(atlas: Path, ad: Path, svd: str | None = None) -> tuple[dict, dict]:
    """Write dt.json and in_use.json for the atlas; return both."""
    stamp = read_json(atlas / "atlas.json")
    b = stamp.get("build", {})
    bd = atlas / "build"
    zb = Path(b.get("zephyr_base") or Path(b.get("west_topdir", ".")) / "zephyr")
    bases = rel_bases(stamp, ad)
    rel = Rel(bases)
    raw = raw_from_pickle(bd / "edt.pickle", zb, bases, b.get("tools", {}).get("Python3_EXECUTABLE"))
    labels = {l for n in raw["nodes"].values() for l in n["labels"]}
    cc = read_json(bd / "compile_commands.json") if (bd / "compile_commands.json").is_file() else []
    tus = sorted({e["file"] for e in cc if e.get("file", "").endswith(".c")})
    src = scan_sources(tus, labels)
    wt = (ad / b["worktree"]).resolve() if b.get("worktree") else None
    built = set(tus)
    extra = [f for f in tree_c_files(wt) if f not in built] if wt else []
    not_built = scan_sources(extra, labels)
    elf = Elf(bd / "zephyr.elf") if (bd / "zephyr.elf").is_file() else None
    inst = elf.device_ordinals() if elf else set()
    svd_path = svd or (stamp.get("mcu") or {}).get("svd")
    svdp = svd_peripherals(Path(svd_path)) if svd_path and Path(svd_path).is_file() else None
    blocks = [(p["name"], p["base"], p["size"]) for p in svdp] if svdp else []
    if not svdp:
        for path, info in raw["nodes"].items():
            regs = info.get("regs") or []
            if regs and regs[0][0] is not None and regs[0][1] and path.startswith("/soc") and not info.get("on_bus"):
                nm = instance_name(path, info, {})
                if nm and 0x40000000 <= regs[0][0] < 0x60000000:
                    blocks.append((nm, regs[0][0], regs[0][1]))
    blocks += [c for c in CORE_BLOCKS if c[0] not in {x[0] for x in blocks}]
    mmio = mmio_refs(elf, blocks) if elf else {}
    in_use = build_in_use(raw, inst, mmio, svdp)
    armed = {**not_built["armed"], **src["armed"]}
    exti = defaultdict(set)
    for u in raw["uses"]:
        if u.get("exti_line") is not None and not u.get("soc_defined"):
            exti[u["exti_line"]].add(u["pin"])
    dt = {
        "atlas": stamp.get("id"),
        "edt_sha256": sha256_file(bd / "edt.pickle"),
        **raw,
        "exti_lines": {f"EXTI{k}": sorted(v) for k, v in sorted(exti.items())},
        "drivers": {k: [rel(f) for f in v] for k, v in src["drivers"].items()},
        "drivers_not_built": {k: [rel(f) for f in v] for k, v in not_built["drivers"].items()},
        "consumers": {k: [rel(f) for f in v] for k, v in src["consumers"].items()},
        "armed": armed,
        "instantiated_ordinals": sorted(inst),
        "mmio": {k: {"code": sorted(v["code"]), "data": sorted(v["data"])} for k, v in sorted(mmio.items())},
        "n_tus": len(tus),
        "svd": bool(svdp),
    }
    write_json(atlas / "dt.json", dt)
    write_json(atlas / "in_use.json", in_use)
    return dt, in_use


if __name__ == "__main__" and len(sys.argv) >= 2 and sys.argv[1] == "--raw":
    _pk, _zb, _bases = sys.argv[2], sys.argv[3], json.loads(sys.argv[4])
    json.dump(extract_edt(load_edt(Path(_pk), Path(_zb)), Rel([tuple(x) for x in _bases])), sys.stdout)

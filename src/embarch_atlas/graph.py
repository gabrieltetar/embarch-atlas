"""graph.json: the whole atlas as one graph, for a human surface (DESIGN.md "graph.json").

The MCP tools answer one question at a time; a map needs everything at once. This
module joins what `join` already wrote (pins.json, mismatches.json, dt.json,
in_use.json, the board netlists) with a code model read from the build's ELF, and
writes one file per atlas:

- **nodes** on six layers: external systems, app modules, middleware, drivers,
  SoC peripherals + MCU pins, and every part on every board;
- **edges**: calls between modules, a module's use of a device, the driver-to-part
  binding (labelled with the DT node), DT gpio and pinctrl properties, MCU pin to
  far-end part (labelled with the net), mated board-to-board connectors;
- **nets** per board, each with the firmware's name for it where the MCU drives it;
- a **symbol** per part: a generic schematic shape (resistor, capacitor, IC box,
  connector...) with every pin's name, side, offset and net;
- **clusters** (feature areas) from a deterministic community pass, and a **layout**
  keyed by native IDs, so the same part keeps its place across rebuilds;
- **checks**, **problems** (mismatch candidates and unverified facts) as rows of
  claims per source, each with a structured citation.

Nothing here is new evidence: every status and citation comes from a file `join`
or `secondlook` wrote. The layout is presentation, but it lives here so that the
clusters are the same for every reader.
"""
from __future__ import annotations

import bisect
import collections
import math
import re
import shutil
import subprocess
from pathlib import Path

from embarch_atlas.common import read_json, read_jsonl, write_json

SCHEMA = 1

LAYERS = [
    {"id": "ext", "name": "External"},
    {"id": "app", "name": "App"},
    {"id": "mw", "name": "Middleware"},
    {"id": "drv", "name": "Drivers"},
    {"id": "soc", "name": "SoC peripherals + pins"},
    {"id": "hw", "name": "Components + signals"},
]
LAYER_IX = {l["id"]: i for i, l in enumerate(LAYERS)}

KINDS = {
    "ext": "External system", "module": "Module", "periph": "Peripheral", "pin": "MCU pin", "part": "Part",
}
STATUS = {
    "ok": "no finding", "mis": "mismatch", "unv": "unverified", "info": "expected or informational finding",
}
EDGE_KINDS = {
    "call": "calls", "device": "uses the device", "bind": "drives the part", "bus": "on the bus",
    "periph": "driver of the peripheral", "gpio": "DT gpio property", "pinctrl": "DT pinctrl function",
    "net": "net from the MCU pin", "pad": "the MCU's package pin", "mate": "mated connectors", "ext": "plugged into",
}
SYMBOLS = {
    "resistor": "Resistor", "capacitor": "Capacitor", "inductor": "Inductor", "ferrite": "Ferrite bead",
    "diode": "Diode", "led": "LED", "tvs": "TVS / Zener diode", "fuse": "Fuse", "crystal": "Crystal",
    "switch": "Switch", "nettie": "Net tie", "testpoint": "Test point", "transistor": "Transistor",
    "ic": "IC", "connector": "Connector",
}

# World units: one pin pitch is 10. Text is approximated in a monospace face (0.6 em).
P = 10.0
STUB = 8.0
FONT = 6.5
CH = FONT * 0.6
TWO_PIN_BODY = 20.0
HUB_NETS = 30             # a part on this many distinct signal nets is a hub, like the MCU
BUS_NET_PARTS = 6         # a net shared by more anchors than this is a bus: no clustering weight
ASPECT = 2.0              # the hw plane's target width : height

GROUND_NAME = re.compile(r"^((A|D|P|S|E)?GND\w*|VSS\w*|EP|PAD|EPAD|TAB|THERMAL\w*|PGND\d*)$", re.I)
POWER_NAME = re.compile(r"^(V(DD|CC|IN|BUS|BAT|SYS|S|REF|IO|M|PP|LDO|DDA|DDIO|CORE)\w*|(A|D|P)V(DD|CC|IN)\w*|PVIN\w*|VCC\w*|VDD\w*)$", re.I)


# ---------------------------------------------------------------- the code model (from the ELF)

NM_RE = re.compile(r"^([0-9a-fA-F]+)\s+(?:([0-9a-fA-F]+)\s+)?([A-Za-z])\s+(\S+)(?:\t(.+):(\d+))?\s*$")
FN_HEAD_RE = re.compile(r"^([0-9a-fA-F]+) <(.+)>:\s*$")
BRANCH_RE = re.compile(r"\t(?:bl|blx|b|beq|bne|bcs|bcc|bmi|bpl|bhi|bls|bge|blt|bgt|ble|cbz|cbnz)(?:\.w|\.n)?\t"
                       r"(?:\w+,\s*)?([0-9a-fA-F]+) <[^>]+>")
WORD_RE = re.compile(r"\.word\t0x([0-9a-fA-F]+)")


def parse_nm(text: str) -> list[dict]:
    """`nm -l --defined-only -S` lines -> symbols (addr, size, type, name, file, line)."""
    out = []
    for line in text.splitlines():
        m = NM_RE.match(line)
        if not m:
            continue
        out.append({"addr": int(m[1], 16), "size": int(m[2], 16) if m[2] else 0, "type": m[3], "name": m[4],
                    "file": m[5], "line": int(m[6]) if m[6] else None})
    return out


def parse_objdump(text: str) -> dict[int, dict]:
    """`objdump -d` -> {function address: {"name", "calls": {target address}, "words": {literal value}}}.

    By address, not name: two static functions may share a name in different files."""
    fns: dict[int, dict] = {}
    cur = None
    for line in text.splitlines():
        m = FN_HEAD_RE.match(line)
        if m:
            cur = fns.setdefault(int(m[1], 16), {"name": m[2], "calls": set(), "words": set()})
            continue
        if cur is None:
            continue
        b = BRANCH_RE.search(line)
        if b:
            cur["calls"].add(int(b[1], 16))
            continue
        w = WORD_RE.search(line)
        if w:
            cur["words"].add(int(w[1], 16))
    return fns


def _tool(stamp: dict, key: str, fallback: str) -> str | None:
    p = ((stamp.get("build") or {}).get("tools") or {}).get(key)
    if p and Path(p).is_file():
        return p
    return shutil.which(fallback)


class Roots:
    """Where a source path lives: the build's worktree (fw), the Zephyr tree, or elsewhere."""

    def __init__(self, ad: Path, stamp: dict):
        b = stamp.get("build") or {}
        self.fw: list[Path] = []
        if b.get("worktree"):
            wt = Path(b["worktree"])
            self.fw.append((ad / wt).resolve() if not wt.is_absolute() else wt.resolve())
        if b.get("fw"):
            self.fw.append(Path(b["fw"]).resolve())
        self.zephyr = Path(b["zephyr_base"]).resolve() if b.get("zephyr_base") else None
        self.fw_repo = Path(b["fw"]).resolve() if b.get("fw") else (self.fw[0] if self.fw else None)

    def split(self, path: str | None) -> tuple[str, str] | None:
        if not path:
            return None
        if path.startswith("fw:"):
            return "fw", path[3:]
        if path.startswith("zephyr:"):
            return "zephyr", path[7:]
        p = Path(path)
        for root in self.fw:
            try:
                return "fw", p.resolve().relative_to(root).as_posix()
            except (ValueError, OSError):
                pass
        if self.zephyr:
            try:
                return "zephyr", p.resolve().relative_to(self.zephyr).as_posix()
            except (ValueError, OSError):
                pass
        return None


def module_of(root: str, rel: str, driver_names: dict[str, str]) -> dict | None:
    """A source file -> its module: (id, layer, label, zephyr). None for code the map leaves out."""
    parts = rel.split("/")
    if root == "fw":
        if parts[0] == "apps" and len(parts) >= 3:
            stem = parts[-1]
            if not re.search(r"\.(c|cpp|cc|h|hpp)$", stem):
                return None
            mod_rel = re.sub(r"\.(h|hpp)$", ".c", rel)
            return {"id": f"code:fw:{mod_rel}", "layer": "app", "label": Path(mod_rel).name, "zephyr": False,
                    "path": mod_rel}
        if parts[0] == "lib" and len(parts) >= 3:
            d = "/".join(parts[:2])
            return {"id": f"code:fw:{d}", "layer": "mw", "label": d, "zephyr": False, "path": d + "/"}
        if parts[0] == "drivers" and len(parts) >= 2:
            d = "/".join(parts[:-1]) if len(parts) > 2 else parts[0]
            if parts[1] == "include":
                return None
            return {"id": f"code:fw:{d}", "layer": "drv", "label": driver_names.get(d) or parts[-2],
                    "zephyr": False, "path": d + "/"}
        if parts[0] == "boards":
            return {"id": "code:fw:boards", "layer": "drv", "label": "board", "zephyr": False, "path": "boards/"}
        if parts[0] in ("tests", "samples", "embarch", "build"):
            return None
        return {"id": f"code:fw:{parts[0]}", "layer": "mw", "label": parts[0], "zephyr": False, "path": parts[0] + "/"}
    if root == "zephyr":
        if parts[0] == "drivers" and len(parts) >= 3:
            stem = re.sub(r"\.c$", "", parts[-1])
            d = "/".join(parts[:-1])
            return {"id": f"code:zephyr:{d}/{stem}", "layer": "drv", "label": stem, "zephyr": True,
                    "path": f"{d}/{parts[-1]}"}
        if parts[0] == "subsys" and len(parts) >= 3:
            d = "/".join(parts[:2])
            return {"id": f"code:zephyr:{d}", "layer": "mw", "label": parts[1], "zephyr": True, "path": d + "/"}
    return None


def driver_names(dt: dict) -> dict[str, str]:
    """fw driver dir -> a label from its compatible (`vendor_part` -> `part`)."""
    out = {}
    for table in ("drivers", "drivers_not_built"):
        for tok, files in sorted((dt.get(table) or {}).items()):
            for f in files:
                if f.startswith("fw:drivers/"):
                    d = f[3:].rsplit("/", 1)[0]
                    out.setdefault(d, tok.split("_", 1)[1] if "_" in tok else tok)
    return out


def code_model(ad: Path, atlas: Path, stamp: dict, dt: dict, log=print, nm_text: str | None = None,
               objdump_text: str | None = None) -> dict:
    """Modules, their functions, calls between them and the devices each one references.

    Read from the build's ELF: `nm -l` places every function in a file, `objdump -d` gives
    direct calls and literal-pool words (a `DEVICE_DT_GET` is the address of
    `__device_dts_ord_<N>`; a callback registration is a function's address)."""
    roots = Roots(ad, stamp)
    names = driver_names(dt)
    elf = atlas / "build" / "zephyr.elf"
    if nm_text is None or objdump_text is None:
        nm, od = _tool(stamp, "CMAKE_NM", "arm-zephyr-eabi-nm"), _tool(stamp, "CMAKE_OBJDUMP", "arm-zephyr-eabi-objdump")
        if not elf.is_file() or not nm or not od:
            log(f"graph: no ELF or no nm/objdump ({elf}); the code layers are empty")
            return {"modules": {}, "calls": {}, "devices": {}, "note": "no ELF or toolchain binutils"}
        nm_text = subprocess.run([nm, "-l", "--defined-only", "-S", str(elf)], capture_output=True, text=True).stdout
        objdump_text = subprocess.run([od, "-d", "--no-show-raw-insn", str(elf)], capture_output=True, text=True).stdout
    syms = parse_nm(nm_text)
    fns = parse_objdump(objdump_text)

    modules: dict[str, dict] = {}
    fn_mod: dict[int, str] = {}          # function address -> module
    fn_size: dict[int, int] = {}
    for s in syms:
        if s["type"] not in "Tt" or not s["file"]:
            continue
        where = roots.split(s["file"])
        if not where:
            continue
        m = module_of(where[0], where[1], names)
        if not m:
            continue
        mod = modules.setdefault(m["id"], {**m, "root": where[0], "functions": [], "files": set()})
        mod["files"].add(where[1])
        mod["functions"].append({"name": s["name"], "public": s["type"] == "T", "path": where[1], "line": s["line"]})
        fn_mod[s["addr"] & ~1] = m["id"]
        fn_size[s["addr"] & ~1] = s["size"]

    # literal words -> the symbol they point into (device structs, functions)
    table = sorted((s["addr"], s["size"] or 1, s["name"]) for s in syms)
    addrs = [t[0] for t in table]

    def sym_at(v: int) -> str | None:
        for cand in (v, v & ~1):
            i = bisect.bisect_right(addrs, cand) - 1
            if i >= 0 and table[i][0] <= cand < table[i][0] + table[i][1]:
                return table[i][2]
        return None

    fn_starts = sorted(fn_mod)

    def fn_at(v: int) -> str | None:
        """The module of the function whose body holds address v; None outside every mapped function."""
        v &= ~1
        i = bisect.bisect_right(fn_starts, v) - 1
        if i < 0:
            return None
        start = fn_starts[i]
        return fn_mod[start] if v < start + max(fn_size.get(start, 0), 2) else None

    calls: dict[tuple[str, str], int] = collections.Counter()
    devices: dict[tuple[str, int], int] = collections.Counter()
    for addr, body in fns.items():
        src = fn_mod.get(addr & ~1)
        if not src:
            continue
        for target in body["calls"]:
            dst = fn_at(target)
            if dst and dst != src:
                calls[(src, dst)] += 1
        for w in body["words"]:
            name = sym_at(w)
            if not name:
                continue
            m = re.fullmatch(r"__device_dts_ord_(\d+)", name)
            if m:
                devices[(src, int(m[1]))] += 1
            elif (w & ~1) in fn_mod and fn_mod[w & ~1] != src:
                calls[(src, fn_mod[w & ~1])] += 1
    for mod in modules.values():
        mod["files"] = sorted(mod["files"])
        mod["functions"].sort(key=lambda f: (not f["public"], f["name"]))
    return {"modules": modules, "calls": dict(calls), "devices": dict(devices), "note": None}


# ---------------------------------------------------------------- parts and symbols

def symbol_kind(ref: str, comp: dict, bom: dict | None) -> str:
    prefix = (re.match(r"[A-Za-z]+", ref) or [""])[0].upper()
    n = len(comp.get("pins") or {})
    desc = " ".join(str((bom or {}).get(k) or "") for k in ("description", "comment")).upper()
    if prefix.startswith("TP"):
        return "testpoint"
    if prefix in ("NT",):
        return "nettie"
    if prefix in ("J", "P", "CN", "CON", "X") and not (prefix == "X" and "CRYSTAL" in desc):
        return "connector"
    if n == 2:
        if prefix == "R":
            return "resistor"
        if prefix == "C":
            return "capacitor"
        if prefix == "FB" or "FERRITE" in desc:
            return "ferrite"
        if prefix == "L":
            return "inductor"
        if prefix in ("LED",) or (prefix == "D" and "LED" in desc):
            return "led"
        if prefix == "D":
            return "tvs" if re.search(r"\bTVS\b|ZENER|ESD", desc) else "diode"
        if prefix == "F":
            return "fuse"
        if prefix in ("Y", "XTAL") or "CRYSTAL" in desc:
            return "crystal"
        if prefix in ("SW", "S", "BTN"):
            return "switch"
    if n == 3 and prefix == "Q":
        return "transistor"
    return "ic"


def text_w(s: str | None, font: float = FONT) -> float:
    return len(s or "") * font * 0.6


def _merge_pins(pins: list[dict]) -> list[dict]:
    """Pins with the same name on the same net are one stub (4x GND, 3x VDD)."""
    out: dict[tuple, dict] = {}
    for p in pins:
        if p["name"] and p["net"]:
            k = (p["name"], p["net"])
            if k in out:
                out[k]["nums"].append(p["num"])
                continue
            out[k] = {**p, "nums": [p["num"]]}
        else:
            out[(p["num"],)] = {**p, "nums": [p["num"]]}
    return list(out.values())


def _pin_order(num: str):
    m = re.fullmatch(r"([A-Za-z]*)(\d+)", str(num))
    return (m[1], int(m[2])) if m else (str(num), 0)


def _port_order(name: str | None):
    m = re.match(r"P([A-Z])(\d{1,2})", name or "")
    return (m[1], int(m[2])) if m else None


def layout_symbol(kind: str, pins: list[dict], ground: set[str]) -> dict:
    """Pin sides and offsets for a generic symbol. `pins`: [{num, name, net, dir}], `ground`: ground net ids.

    Offsets are the outer tip of each pin's stub, relative to the body's centre; `s` is the
    side the stub leaves from (l, r, t, b). Two-pin parts stand upright with a ground pin at the
    bottom; a diode's anode is on top. An IC is a box: supplies on top, grounds at the bottom,
    the rest split left/right (datasheet direction when known, MCU ports by port letter,
    otherwise by pin number)."""
    pins = sorted(pins, key=lambda p: _pin_order(p["num"]))
    if not pins:
        return {"w": 2 * P, "h": 2 * P, "pins": []}
    if kind == "testpoint":
        p = pins[0] if pins else {"num": "1", "name": None, "net": None}
        return {"w": 8, "h": 8, "pins": [{**p, "nums": [p["num"]], "s": "b", "x": 0, "y": 4 + STUB}]}
    if len(pins) == 2 and kind not in ("ic", "connector", "transistor"):
        a, b = pins
        if kind in ("diode", "led", "tvs"):
            if re.match(r"^(A|AN|ANODE|\+)$", str(b["name"] or ""), re.I) or \
                    (not re.match(r"^(A|AN|ANODE)$", str(a["name"] or ""), re.I) and str(a["num"]) == "1"):
                a, b = b, a          # anode on top; without names pin 1 is the cathode
        elif a["net"] in ground and b["net"] not in ground:
            a, b = b, a
        half = TWO_PIN_BODY / 2 + STUB
        return {"w": 10, "h": TWO_PIN_BODY, "pins": [{**a, "nums": [a["num"]], "s": "t", "x": 0, "y": -half},
                                                     {**b, "nums": [b["num"]], "s": "b", "x": 0, "y": half}]}
    if kind == "transistor":
        role = {}
        for p in pins:
            nm = str(p["name"] or "").upper()
            r = {"G": "g", "B": "g", "GATE": "g", "BASE": "g", "D": "d", "C": "d", "DRAIN": "d", "COLLECTOR": "d",
                 "S": "s", "E": "s", "SOURCE": "s", "EMITTER": "s"}.get(nm)
            if r:
                role[r] = p
        if len(role) == 3:
            R = 10
            return {"w": 2 * R, "h": 2 * R, "pins": [
                {**role["g"], "nums": [role["g"]["num"]], "s": "l", "x": -R - STUB, "y": 0},
                {**role["d"], "nums": [role["d"]["num"]], "s": "t", "x": 0, "y": -R - STUB},
                {**role["s"], "nums": [role["s"]["num"]], "s": "b", "x": 0, "y": R + STUB}]}
        kind = "ic"                  # no pin names: a box with numbered pins, never a guessed G/D/S
    merged = _merge_pins(pins) if kind == "ic" else [{**p, "nums": [p["num"]]} for p in pins]
    top, bot, left, right = [], [], [], []
    if kind == "connector":
        if len(merged) <= 12:
            right = merged
        else:
            for p in merged:
                m = re.search(r"(\d+)$", str(p["num"]))
                (left if m and int(m[1]) % 2 else right).append(p)
    else:
        rest = []
        for p in merged:
            nm = str(p["name"] or "")
            if GROUND_NAME.match(nm) or (not nm and p["net"] in ground):
                bot.append(p)
            elif POWER_NAME.match(nm):
                top.append(p)
            else:
                rest.append(p)
        if len(top) > 12 or len(bot) > 12:            # a pin-dense part keeps supplies on the sides
            rest, top, bot = rest + top + bot, [], []
        ports = [p for p in rest if _port_order(p["name"])]
        if len(ports) >= 8:                           # an MCU: ports split by letter, the rest at the left foot
            ports.sort(key=lambda p: _port_order(p["name"]))
            other = [p for p in rest if not _port_order(p["name"])]
            half = (len(ports) + 1) // 2
            letters = [_port_order(p["name"])[0] for p in ports]
            cut = half
            for d in range(0, half):                  # nearest port-letter boundary to the half
                for c in (half - d, half + d):
                    if 0 < c < len(ports) and letters[c] != letters[c - 1]:
                        cut = c
                        break
                else:
                    continue
                break
            left, right = ports[:cut] + other, ports[cut:]
        elif any(p.get("dir") for p in rest):
            for p in rest:
                (right if p.get("dir") in ("out", "power-out") else left).append(p)
        else:
            half = (len(rest) + 1) // 2
            left, right = rest[:half], rest[half:]
    inner_l = max((text_w(_inner(p, kind)) for p in left), default=0)
    inner_r = max((text_w(_inner(p, kind)) for p in right), default=0)
    w = max(4 * P, inner_l + inner_r + 2.5 * P if left and right else max(inner_l, inner_r) + 2 * P,
            (len(top) + 1) * P, (len(bot) + 1) * P)
    w = math.ceil(w / P) * P
    h = max(2 * P, (max(len(left), len(right)) + 1) * P)
    out = []
    for i, p in enumerate(left):
        out.append({**p, "s": "l", "x": -w / 2 - STUB, "y": -h / 2 + P * (i + 1)})
    for i, p in enumerate(right):
        out.append({**p, "s": "r", "x": w / 2 + STUB, "y": -h / 2 + P * (i + 1)})
    for i, p in enumerate(top):
        out.append({**p, "s": "t", "x": -w / 2 + w * (i + 1) / (len(top) + 1), "y": -h / 2 - STUB})
    for i, p in enumerate(bot):
        out.append({**p, "s": "b", "x": -w / 2 + w * (i + 1) / (len(bot) + 1), "y": h / 2 + STUB})
    return {"w": w, "h": h, "pins": out}


def _inner(p: dict, kind: str) -> str:
    """What is written inside the box at a pin: its name; a connector (or a nameless pin) its number."""
    if kind == "connector" or not p.get("name"):
        return ",".join(p.get("nums") or [p["num"]])
    return str(p["name"])


def place_label(sym: dict, label: str, net_text: dict[str, str]) -> tuple[dict, tuple[float, float, float, float]]:
    """Where the part's own label goes, and the drawn extent (left, top, right, bottom) around the centre.

    An upright two-pin part (or a test point) carries its label to the right of the body; a box
    carries it centred above, clear of any top pin's rotated net label. The browser draws at
    exactly these coordinates, so the extent here is what the packer reserves."""
    upright = sym["w"] <= 12
    l, t, r, b = -sym["w"] / 2, -sym["h"] / 2, sym["w"] / 2, sym["h"] / 2
    for p in sym["pins"]:
        txt = text_w(net_text.get(p.get("net") or "", "")) + 3
        x, y = p["x"], p["y"]
        if p["s"] == "l":
            l = min(l, x - txt)
        elif p["s"] == "r":
            r = max(r, x + txt)
        elif upright:
            if p["s"] == "t":
                t = min(t, y - FONT * 1.4)
            else:
                b = max(b, y + FONT * 1.6)
            l, r = min(l, -txt / 2), max(r, txt / 2)
        elif p["s"] == "t":
            t = min(t, y - txt)
        else:
            b = max(b, y + txt)
    lines = label.split("\n")
    if upright:
        lab = {"x": sym["w"] / 2 + 3, "y": -FONT * 0.3, "a": "start"}
        r = max(r, lab["x"] + max(text_w(x) for x in lines))
    else:
        lab = {"x": 0.0, "y": round(min(t, -sym["h"] / 2) - 3 - FONT * (len(lines) - 1) * 1.2, 2), "a": "middle"}
        t = lab["y"] - FONT
        half = max(text_w(x) for x in lines) / 2
        l, r = min(l, -half), max(r, half)
    return lab, (l - 3, t - 3, r + 3, b + 3)


# ---------------------------------------------------------------- clustering (Louvain, deterministic)

def louvain(adj: dict[str, dict[str, float]], resolution: float = 1.0, max_passes: int = 60) -> dict[str, int]:
    """Communities of an undirected weighted graph. Node order is sorted, so the result is stable."""
    graph = {n: {m: w for m, w in adj[n].items() if m in adj} for n in sorted(adj)}
    members = {n: [n] for n in graph}
    while True:
        m2 = sum(w for nb in graph.values() for w in nb.values())
        if m2 <= 0:
            break
        k = {n: sum(nb.values()) for n, nb in graph.items()}
        com = {n: n for n in graph}
        tot = dict(k)
        moved_any = False
        for _ in range(max_passes):
            improved = False
            for n in sorted(graph):
                c0 = com[n]
                links: dict[str, float] = collections.defaultdict(float)
                for nb, w in graph[n].items():
                    if nb != n:
                        links[com[nb]] += w
                tot[c0] -= k[n]
                best, best_gain = c0, links.get(c0, 0.0) - resolution * tot[c0] * k[n] / m2
                for c in sorted(links):
                    gain = links[c] - resolution * tot[c] * k[n] / m2
                    if gain > best_gain + 1e-12:
                        best, best_gain = c, gain
                tot[best] += k[n]
                if best != c0:
                    com[n] = best
                    improved = moved_any = True
            if not improved:
                break
        if not moved_any:
            break
        new_graph: dict[str, dict[str, float]] = collections.defaultdict(lambda: collections.defaultdict(float))
        new_members: dict[str, list] = collections.defaultdict(list)
        for n, nbs in graph.items():
            new_members[com[n]] += members[n]
            new_graph[com[n]]
            for nb, w in nbs.items():
                new_graph[com[n]][com[nb]] += w
        graph = {c: dict(v) for c, v in new_graph.items()}
        members = dict(new_members)
    out = {}
    for i, (_c, ms) in enumerate(sorted(members.items(), key=lambda kv: min(kv[1]))):
        for n in ms:
            out[n] = i
    return out


# ---------------------------------------------------------------- citations

def doc_cite(docs_dir: Path, doc: str, page: int | None, q: str | None = None) -> dict:
    c = {"t": "doc", "doc": doc, "p": page}
    if q:
        c["q"] = q
    if page:
        sec = section_of(docs_dir / doc, page)
        if sec:
            c["sec"], c["sec_title"], c["file"] = sec
        card = card_block(docs_dir / doc, page)
        if card:
            c["card"] = card
    return c


_toc_cache: dict[Path, list] = {}


def section_of(d: Path, page: int) -> tuple[str, str, str | None] | None:
    if d not in _toc_cache:
        try:
            _toc_cache[d] = read_json(d / "toc.json").get("sections", [])
        except (OSError, ValueError):
            _toc_cache[d] = []
    secs = _toc_cache[d]
    by_id = {s["id"]: s for s in secs}
    best = None
    for s in secs:
        pg = s.get("pages") or []
        if len(pg) == 2 and pg[0] <= page <= pg[1]:
            if best is None or s.get("level", 0) > best.get("level", 0):
                best = s
    if not best:
        return None
    cur, f = best, None
    while cur and not f:
        f = cur.get("file") or (f"s/{cur['id']}.md" if (d / "s" / f"{cur['id']}.md").is_file() else None)
        cur = by_id.get(cur.get("parent"))
    return best["id"], best.get("title") or "", f


def card_block(d: Path, page: int) -> str | None:
    """The card's paragraph or heading block that cites this page."""
    try:
        text = (d / "card.md").read_text(encoding="utf-8")
    except OSError:
        return None
    pat = re.compile(rf"\bp\.?\s?{page}\b(?!\d)")
    blocks = re.split(r"\n(?=## )|\n\n", text)
    for b in blocks:
        if pat.search(b):
            return b.strip()[:900]
    return None


EVID_FW = re.compile(r"fw:([\w./+-]+?):(\d+)")
EVID_DOC = re.compile(r"\(?([\w.+-]+@[\w.+-]+) p(\d+)")
BOARD_KEY = re.compile(r"\b([A-Z]):([A-Z]+\w*?\d+\w*)(?:-([A-Za-z]*\d+))?\b")


def evidence_row(s: str, docs_dir: Path, sch_page) -> dict:
    """One evidence string of a mismatch candidate -> {src, says, cite}."""
    fw = EVID_FW.search(s)
    if fw:
        says = (s[:fw.start()].strip(" @") + " " + s[fw.end():].strip()).strip()
        return {"src": "Firmware", "says": says or s, "cite": {"t": "code", "root": "fw", "path": fw[1], "line": int(fw[2])}}
    d = EVID_DOC.search(s)
    if d:
        return {"src": "Datasheet", "says": s[:d.start()].strip(" (") or s, "cite": doc_cite(docs_dir, d[1], int(d[2]))}
    if s.startswith(("schematic", "netlist", "mating rule", "interface")):
        k = BOARD_KEY.search(s)
        cite = sch_page(k[1], k[2]) if k else None
        return {"src": "Schematic", "says": re.sub(r"^schematic(/BOM)?\s*", "", s), "cite": cite}
    if s.startswith(("ELF", "compile_commands", "interrupt arming")):
        return {"src": "Build", "says": s, "cite": None}
    k = BOARD_KEY.search(s)
    if k:
        return {"src": "Schematic", "says": s, "cite": sch_page(k[1], k[2])}
    return {"src": "Atlas", "says": s, "cite": None}


# ---------------------------------------------------------------- the build

class Graph:
    def __init__(self):
        self.nodes: dict[str, dict] = {}
        self.edges: list[dict] = []
        self._edge_keys: set = set()

    def node(self, nid: str, layer: str, kind: str, label: str, **kw) -> dict:
        n = self.nodes.get(nid)
        if n is None:
            n = {"id": nid, "layer": layer, "kind": kind, "label": label, "status": "ok"}
            self.nodes[nid] = n
        n.update({k: v for k, v in kw.items() if v is not None})
        return n

    def edge(self, a: str, b: str, kind: str, label: str | None = None, **kw) -> None:
        if a not in self.nodes or b not in self.nodes or a == b:
            return
        key = (a, b, kind, label)
        if key in self._edge_keys:
            return
        self._edge_keys.add(key)
        e = {"a": a, "b": b, "kind": kind, "status": "ok"}
        if label:
            e["label"] = label
        e.update({k: v for k, v in kw.items() if v is not None})
        self.edges.append(e)


def _short_func(func: str | None, port: str | None) -> str | None:
    if not func:
        return None
    f = re.sub(rf"_p{port[1].lower()}{port[2:]}$", "", func) if port else func
    return f


def _humanize(sheet: str | None) -> str:
    return re.sub(r"_+", " ", sheet or "").strip()


def build(ad: Path, atlas: Path, J, stamp: dict, pins: dict, report: dict, code: dict, log=print) -> dict:
    G = Graph()
    dt = J.dt
    docs_dir = ad / "docs"
    boards = J.boards
    mcu = J.mcu                                   # (board, ref) or None
    bom = {}
    for lab in boards:
        bp = ad / "boards" / lab / "bom.json"
        if bp.is_file():
            for row in read_json(bp):
                bom[(lab, row.get("ref"))] = row

    # --- nets, and the firmware's name for a net
    nets: dict[str, dict] = {}
    for lab, B in boards.items():
        for name, n in B.nets.items():
            nets[f"{lab}:{name}"] = {"board": lab, "name": name, "pins": sorted(n.get("pins", [])),
                                     "rail": J.is_rail(lab, name), "ground": J.is_ground(name)}
    for itf in J.interfaces:
        ba, ra = itf["a"].split(":", 1)
        bb, rb = itf["b"].split(":", 1)
        for pa, pb in itf["map"].items():
            na, nb = boards[ba].net(f"{ra}-{pa}"), boards[bb].net(f"{rb}-{pb}")
            if na and nb:
                nets[f"{ba}:{na}"].setdefault("mates", []).append(f"{bb}:{nb}")
                nets[f"{bb}:{nb}"].setdefault("mates", []).append(f"{ba}:{na}")
    for n in nets.values():
        if "mates" in n:
            n["mates"] = sorted(set(n["mates"]))

    fw_of_net: dict[str, dict] = {}
    for r in pins.get("pins", []):
        if not r.get("port") or not r.get("net") or r.get("rail"):
            continue
        uses = []
        nodes_ = []
        for u in r.get("dt", []):
            if u.get("kind") == "pinctrl":
                uses.append(_short_func(u.get("func"), r["port"]) or u.get("prop"))
            else:
                uses.append(u.get("prop"))
            nodes_.append(u.get("label"))
        fw = {"port": r["port"], "uses": sorted(set(x for x in uses if x)), "dt": sorted(set(x for x in nodes_ if x))}
        mb = (r.get("key") or "").split(":")[0]
        reached = [f"{mb}:{r['net']}"]
        for f in r.get("far", []):
            if f["kind"] in ("board-to-board", "series", "passthrough") and f.get("to_net"):
                ob = f["to"].split(":")[0] if f["kind"] == "board-to-board" else f["board"]
                reached.append(f"{ob}:{f['to_net']}")
        for nid in reached:
            if nid in nets and not nets[nid]["rail"]:
                cur = fw_of_net.setdefault(nid, {"port": [], "uses": [], "dt": []})
                cur["port"].append(fw["port"])
                cur["uses"] += fw["uses"]
                cur["dt"] += fw["dt"]
    for nid, fw in fw_of_net.items():
        fw = {k: sorted(set(v)) for k, v in fw.items()}
        fw["text"] = " · ".join(filter(None, [",".join(fw["port"]), ", ".join(fw["uses"])]))
        nets[nid]["fw"] = fw

    # --- parts, with symbols
    ground_ids = {nid for nid, n in nets.items() if n["ground"]}
    sheets_of = {}
    for lab, B in boards.items():
        for ref, c in sorted(B.comps.items()):
            key = f"{lab}:{ref}"
            kind = symbol_kind(ref, c, bom.get((lab, ref)))
            plist = []
            for num, p in (c.get("pins") or {}).items():
                net = p.get("net") or B.net(f"{ref}-{num}")
                name = J.part_pin_name(lab, f"{ref}-{num}") if kind not in ("resistor", "capacitor", "testpoint") else None
                rec = J.part_pin_record(lab, f"{ref}-{num}") if kind == "ic" else None
                plist.append({"num": str(num), "name": name, "net": f"{lab}:{net}" if net else None,
                              "dir": (rec[1].get("dir") if rec else None)})
            sym = layout_symbol(kind, plist, ground_ids)
            b = bom.get((lab, ref)) or {}
            docs = J.part_docs.get((lab, ref), [])
            sheets = c.get("sheets") or []
            sheets_of[key] = sheets
            sub = c.get("mpn") or b.get("comment")
            G.node(f"hw:{key}", "hw", "part", key, board=lab, ref=ref, sym_kind=kind,
                   value=c.get("value") or None, mpn=c.get("mpn") or None, desc=b.get("description") or None,
                   dni=bool(c.get("dni")), sheet=sheets[0] if sheets else None, pages=c.get("pages") or [],
                   docs=docs or None, sub=sub,
                   box=part_box(ad, lab, ref, (c.get("pages") or [None])[0]),
                   sym={"w": sym["w"], "h": sym["h"],
                        "pins": [{"n": ",".join(p["nums"]), "nm": p.get("name"), "s": p["s"], "x": round(p["x"], 2),
                                  "y": round(p["y"], 2), "net": p.get("net")} for p in sym["pins"]]})
    if mcu:
        G.nodes[f"hw:{mcu[0]}:{mcu[1]}"]["mcu"] = True

    # --- external systems: off-board connectors, and the neighbouring unit
    mated = set()
    for itf in J.interfaces:
        mated.add(itf["a"])
        mated.add(itf["b"])
    unit_conns: set[str] = set()
    sysj = ad / "boards" / "system.json"
    if sysj.is_file():
        for pr in read_json(sysj).get("proposed", []):
            if pr.get("kind") == "unit-to-unit":
                for pair in pr.get("pairs", []):
                    for end in (pair.get("a"), pair.get("b")):
                        if end:
                            unit_conns.add(end.rsplit("-", 1)[0])
    if unit_conns:
        G.node("ext:unit", "ext", "ext", "Neighbouring unit", sub="unit-to-unit interface",
               prov="name-inferred", status="unv")
        for k in sorted(unit_conns):
            G.edge("ext:unit", f"hw:{k}", "ext", status="unv", prov="name-inferred")
    for nid, n in list(G.nodes.items()):
        if n["kind"] == "part" and n["sym_kind"] == "connector":
            key = n["label"]
            if key in mated or key in unit_conns:
                continue
            sheet = _humanize(n.get("sheet"))
            G.node(f"ext:{key}", "ext", "ext", f"{key} · {sheet}" if sheet else key, sub=n.get("desc") or n.get("mpn"))
            G.edge(f"ext:{key}", nid, "ext")
    for itf in J.interfaces:
        st = "ok" if itf.get("status") == "engineer-declared" else "unv"
        G.edge(f"hw:{itf['a']}", f"hw:{itf['b']}", "mate", f"{itf.get('rule')} · {itf.get('agree')}/{itf.get('named')} names",
               status=st, prov=itf.get("status"))

    # --- code modules
    fw_files: dict[str, str] = {}
    for mid, m in code["modules"].items():
        fns = m["functions"]
        G.node(mid, m["layer"], "module", m["label"], zephyr=m["zephyr"], path=m["path"], root=m["root"],
               functions=[{"name": f["name"], "public": f["public"], "path": f["path"], "line": f["line"]} for f in fns[:80]],
               n_functions=len(fns), n_public=sum(1 for f in fns if f["public"]), files=m["files"])
        for f in m["files"]:
            fw_files[f"{m['root']}:{f}"] = mid
    names = driver_names(dt)
    for tok, files in sorted((dt.get("drivers_not_built") or {}).items()):
        for f in files:
            root, rel = f.split(":", 1)
            m = module_of(root, rel, names)
            if m and m["id"] not in G.nodes:
                G.node(m["id"], m["layer"], "module", m["label"], zephyr=m["zephyr"], path=m["path"], root=root,
                       built=False, sub="not in the build", functions=[], n_functions=0, n_public=0, files=[rel])
            if m:
                fw_files[f"{root}:{rel}"] = m["id"]

    def module_for(file_ref: str | None) -> str | None:
        if not file_ref:
            return None
        if file_ref in fw_files and fw_files[file_ref] in G.nodes:
            return fw_files[file_ref]
        root, _, rel = file_ref.partition(":")
        m = module_of(root, rel, names)
        return m["id"] if m and m["id"] in G.nodes else None

    # zephyr modules only when the fw reaches them, or the DT instantiates them
    zephyr_wanted = set()
    for (a, b), _n in code["calls"].items():
        if a in G.nodes and b in G.nodes and not G.nodes[a]["zephyr"] and G.nodes[b]["zephyr"]:
            zephyr_wanted.add(b)
    for tok, files in (dt.get("drivers") or {}).items():
        for f in files:
            mid = module_for(f)
            if mid:
                zephyr_wanted.add(mid)
    ord_node = {n.get("ordinal"): path for path, n in dt.get("nodes", {}).items()}

    def driver_of_node(path: str | None) -> str | None:
        node = dt.get("nodes", {}).get(path or "", {})
        for c in node.get("compats", []):
            tok = re.sub(r"[^a-z0-9]", "_", c.lower())
            for table in ("drivers", "drivers_not_built"):
                for f in (dt.get(table) or {}).get(tok, []):
                    mid = module_for(f)
                    if mid:
                        return mid
        return None

    for (src, ordn), _n in code["devices"].items():
        path = ord_node.get(ordn)
        drv = driver_of_node(path)
        if drv and src in G.nodes:
            if G.nodes[drv]["zephyr"] and not G.nodes[src]["zephyr"]:
                zephyr_wanted.add(drv)
    for mid in list(G.nodes):
        n = G.nodes[mid]
        if n["kind"] == "module" and n.get("zephyr") and mid not in zephyr_wanted:
            del G.nodes[mid]
    for (a, b), cnt in sorted(code["calls"].items()):
        G.edge(a, b, "call", weight=cnt)
    for (src, ordn), cnt in sorted(code["devices"].items()):
        path = ord_node.get(ordn)
        drv = driver_of_node(path)
        if drv:
            lab = (dt["nodes"].get(path, {}).get("labels") or [path.rsplit("/", 1)[-1]])[0]
            G.edge(src, drv, "device", lab, node=path)

    # --- peripherals
    periph_of_node: dict[str, str] = {}
    for name, p in sorted((J.in_use.get("peripherals") or {}).items()):
        if not (p.get("instantiated") or p.get("touched_by_code") or p.get("dt_okay")):
            continue
        pid = f"periph:{name}"
        G.node(pid, "soc", "periph", name, node=p.get("node") or None, compat=p.get("compatible") or None,
               instantiated=bool(p.get("instantiated")), dt_okay=bool(p.get("dt_okay")),
               touched_by_code=bool(p.get("touched_by_code")),
               sub="instantiated" if p.get("instantiated") else ("DT okay, no device" if p.get("dt_okay") else "touched by code"))
        if p.get("node"):
            periph_of_node[p["node"]] = pid
            drv = driver_of_node(p["node"])
            if drv:
                G.edge(drv, pid, "periph", (dt["nodes"].get(p["node"], {}).get("labels") or [None])[0])
    for d in dt.get("devices", []):
        drv = driver_of_node(d["node"])
        bus = periph_of_node.get(d.get("bus"))
        if drv and bus:
            G.edge(drv, bus, "bus", (d.get("bus_labels") or [None])[0])

    # --- MCU pins
    for r in pins.get("pins", []):
        if not r.get("port"):
            continue
        pid = f"pin:{r['port']}"
        fws = [(u.get("func") and _short_func(u["func"], r["port"])) or u.get("prop") for u in r.get("dt", [])]
        G.node(pid, "soc", "pin", r["port"], key=r["key"], num=r["num"], net=f"{r['key'].split(':')[0]}:{r['net']}" if r.get("net") else None,
               io=r.get("io"), sub=", ".join(x for x in fws if x) or None, rail=bool(r.get("rail")),
               uses=[{k: u.get(k) for k in ("node", "label", "prop", "kind", "func", "af", "flags", "loc", "compat")} for u in r.get("dt", [])])
        G.edge(pid, f"hw:{r['key'].rsplit('-', 1)[0]}", "pad", r["num"])
        for u in r.get("dt", []):
            if u.get("kind") == "pinctrl":
                per = periph_of_node.get(u.get("owner") or u.get("node")) or periph_of_node.get(u.get("node"))
                if per:
                    G.edge(per, pid, "pinctrl", _short_func(u.get("func"), r["port"]), af=u.get("af"))
            else:
                drv = None
                for f in ((u.get("driver") or {}).get("files") or []):
                    drv = module_for(f)
                    if drv:
                        break
                label = u.get("prop") + (f", {u['flags']}" if u.get("flags") else "")
                if drv:
                    G.edge(drv, pid, "gpio", label, node=u.get("node"), dt=u.get("label"))
        for f in r.get("far", []):
            if f["kind"] in ("part-pin", "connector"):
                G.edge(pid, f"hw:{f['board']}:{f['part']}", "net", f.get("net"))
            elif f["kind"] == "board-to-board":
                G.edge(pid, f"hw:{f['board']}:{f['pin'].rsplit('-', 1)[0]}", "net", f.get("net"))

    # --- driver -> part bindings: the DT node rides on the edge
    for ident in (pins.get("i2c") or {}).get("identity", []):
        drv = driver_of_node(ident.get("node"))
        if drv and ident.get("part"):
            st = "ok" if ident.get("status") == "identified" else "unv"
            G.edge(drv, f"hw:{ident['part']}", "bind", ident["node"].rsplit("/", 1)[-1], node=ident["node"], status=st,
                   prov="strap" if st == "ok" else ident.get("status"))
    by_node: dict[str, set[str]] = collections.defaultdict(set)
    for r in pins.get("pins", []):
        for u in r.get("dt", []):
            if u.get("kind") == "gpio":
                for f in r.get("far", []):
                    if f["kind"] == "part-pin" and not re.match(r"(R|C|L|FB|TP|D)\d", f.get("part") or ""):
                        by_node[u["node"]].add(f"{f['board']}:{f['part']}")
    for node, parts in sorted(by_node.items()):
        drv = driver_of_node(node)
        if drv and len(parts) == 1 and not any(e["kind"] == "bind" and e.get("node") == node for e in G.edges):
            G.edge(drv, f"hw:{next(iter(parts))}", "bind", node.rsplit("/", 1)[-1], node=node, prov="by-pin")

    # --- problems
    def sch_page(board: str, ref: str) -> dict | None:
        B = boards.get(board)
        if not B or ref not in B.comps:
            return None
        pages = B.pages(ref)
        box = part_box(ad, board, ref, pages[0] if pages else None)
        return {"t": "doc", "doc": f"sch:{board}", "p": pages[0] if pages else None,
                "sec": (B.comps[ref].get("sheets") or [None])[0], "box": box}

    problems, info = [], collections.defaultdict(list)
    for c in report.get("candidates", []):
        sev = {"check": "mis", "info": "info"}.get(c.get("severity"))
        if not sev:
            continue
        ids = problem_nodes(c, G, mcu, module_for)
        rows = [evidence_row(e, docs_dir, sch_page) for e in c.get("evidence", [])]
        p = {"sev": sev, "cls": c["class"], "key": c.get("key"), "title": c["summary"], "settle": c.get("settle"),
             "nodes": ids, "rows": rows}
        if sev == "mis":
            problems.append(p)
        else:
            info[c["class"]].append(p)
        for i in ids:
            n = G.nodes[i]
            if sev == "mis":
                n["status"] = "mis"
            elif n["status"] == "ok":
                n["status"] = "info"
    for p in unverified_problems(atlas, report, pins, docs_dir, mcu):
        p["nodes"] = [i for i in p["nodes"] if i in G.nodes]
        if not p["nodes"]:
            continue
        problems.append(p)
        for i in p["nodes"]:
            if G.nodes[i]["status"] in ("ok", "info"):
                G.nodes[i]["status"] = "unv"
    problems.sort(key=lambda p: (p["sev"] != "mis", p["cls"], p["title"]))
    seq = collections.Counter()
    for p in problems:
        seq[p["sev"]] += 1
        p["id"] = f"{'M' if p['sev'] == 'mis' else 'U'}{seq[p['sev']]}"
    for e in G.edges:
        a, b = G.nodes[e["a"]], G.nodes[e["b"]]
        if e["status"] == "ok" and "mis" in (a["status"], b["status"]) and e["kind"] in ("gpio", "bind", "net") \
                and (a["kind"] != "part" or b["kind"] != "part"):
            e["status"] = "mis" if a["status"] == "mis" and b["status"] == "mis" else e["status"]

    return {"G": G, "nets": nets, "problems": problems, "info": info}


def part_box(ad: Path, board: str, ref: str, page: int | None) -> list[float] | None:
    """Bounding box of a part's pins on its schematic page, in PDF points (top-left origin)."""
    if page is None:
        return None
    g = _geometry(ad, board)
    pg = (g.get("pages") or {}).get(str(page)) if g else None
    if not pg:
        return None
    pts = [p["at"] for k, p in (pg.get("pins") or {}).items() if p.get("part") == ref and p.get("at")]
    if not pts:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return [round(min(xs) - 12, 1), round(min(ys) - 12, 1), round(max(xs) + 12, 1), round(max(ys) + 12, 1)]


_geom_cache: dict[tuple, dict] = {}


def _geometry(ad: Path, board: str) -> dict | None:
    k = (ad, board)
    if k not in _geom_cache:
        p = ad / "boards" / board / "geometry.json"
        try:
            _geom_cache[k] = read_json(p) if p.is_file() else {}
        except (OSError, ValueError):
            _geom_cache[k] = {}
    return _geom_cache[k]


def problem_nodes(c: dict, G: Graph, mcu, module_for) -> list[str]:
    """The map entities a candidate marks: MCU pins, parts, driver modules, peripherals."""
    out: list[str] = []

    def add(i):
        if i in G.nodes and i not in out:
            out.append(i)

    key = str(c.get("key") or "")
    text = " ".join([c.get("summary") or ""] + list(c.get("evidence") or []))
    if re.fullmatch(r"P[A-Z]\d{1,2}", key):
        add(f"pin:{key}")
    if re.fullmatch(r"EXTI\d+", key):
        for port in re.findall(r"\b(P[A-Z]\d{1,2})\b", text):
            add(f"pin:{port}")
    for m in BOARD_KEY.finditer(f"{key} {text}"):
        add(f"hw:{m[1]}:{m[2]}")
    for m in EVID_FW.finditer(text):
        add(module_for(f"fw:{m[1]}"))
    for m in re.finditer(r"(/soc/[\w@/,.-]+)", text):
        for e in G.edges:
            if e.get("node") == m[1]:
                add(e["a"])
    if mcu:
        out = [i for i in out if i != f"hw:{mcu[0]}:{mcu[1]}"] or out
    return out


def unverified_problems(atlas: Path, report: dict, pins: dict, docs_dir: Path, mcu) -> list[dict]:
    """Facts the atlas uses whose second look did not confirm them, grouped per document."""
    sl = atlas / "second_look.jsonl"
    if not sl.is_file():
        return []
    latest: dict[str, dict] = {}
    for r in read_jsonl(sl):
        if r.get("id"):
            latest[r["id"]] = r
    folded = report.get("folded") or {}
    unresolved = set(folded.get("second look unresolved (record stays llm-unverified)") or [])
    gone = set(folded.get("second look of a record id no longer in the docs (renamed or removed)") or []) | \
        set(folded.get("second look of an earlier version of the record (re-queued)") or [])
    by_doc = collections.defaultdict(list)
    for rid, r in sorted(latest.items()):
        if rid in gone:
            continue
        if not r.get("doc"):
            continue
        if r.get("verdict") == "conflict" or (r.get("verdict") == "unresolved" and (not unresolved or rid in unresolved)):
            by_doc[r["doc"]].append(r)
    parts_of_doc = collections.defaultdict(list)
    for part, ds in (pins.get("part_docs") or {}).items():
        for d in ds:
            parts_of_doc[d].append(f"hw:{part}")
    out = []
    for doc, rs in sorted(by_doc.items()):
        nodes = parts_of_doc.get(doc) or ([f"hw:{mcu[0]}:{mcu[1]}"] if mcu else [])
        nconf = sum(1 for r in rs if r.get("verdict") == "conflict")
        rows = []
        for r in rs[:40]:
            p = r.get("p")
            page = p[0] if isinstance(p, list) and p else p
            first = (r.get("first") or {}).get("value")
            says = f"{r['id']}: {first if first is not None else (r.get('q') or '')}"
            if r.get("verdict") == "conflict":
                says += f" — second reading: {r.get('value')}"
            rows.append({"src": "Datasheet", "says": says[:400], "verdict": r.get("verdict"),
                         "cite": doc_cite(docs_dir, doc, page if isinstance(page, int) else None, r.get("q"))})
        title = f"{len(rs)} fact(s) in {doc} not confirmed by the second look" + (f" ({nconf} conflict)" if nconf else "")
        out.append({"sev": "unv", "cls": "second-look", "key": doc, "title": title,
                    "settle": "read the cited pages: the first and second readings disagree or the second could not decide",
                    "nodes": nodes, "rows": rows})
    return out


# ---------------------------------------------------------------- clusters and layout

def anchors_and_satellites(G: Graph, nets: dict, ad: Path) -> tuple[dict[str, str], set[str]]:
    """Every small part is a satellite of one anchor (an IC, connector, transistor): the anchor it shares a
    signal net with, else the nearest anchor on its schematic page. Returns ({satellite: anchor}, anchors)."""
    parts = {i: n for i, n in G.nodes.items() if n["kind"] == "part"}
    small = {"resistor", "capacitor", "inductor", "ferrite", "diode", "led", "tvs", "fuse", "crystal", "switch",
             "nettie", "testpoint"}
    anchors = {i for i, n in parts.items() if n["sym_kind"] not in small}
    pin_parts: dict[str, set[str]] = collections.defaultdict(set)
    for i, n in parts.items():
        for p in n["sym"]["pins"]:
            if p.get("net"):
                pin_parts[p["net"]].add(i)
    centre = {}
    for i, n in parts.items():
        g = _geometry(ad, n["board"])
        for pg in (n.get("pages") or [])[:1]:
            page = (g or {}).get("pages", {}).get(str(pg)) or {}
            pts = [p["at"] for p in (page.get("pins") or {}).values() if p.get("part") == n["ref"] and p.get("at")]
            if pts:
                centre[i] = (pg, sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
    hubs = {i for i in anchors if G.nodes[i].get("mcu")}
    sat: dict[str, str] = {}
    for i in sorted(parts):
        if i in anchors:
            continue
        n = parts[i]
        cand = collections.Counter()
        for p in n["sym"]["pins"]:
            net = p.get("net")
            if not net or nets.get(net, {}).get("rail"):
                continue
            for j in pin_parts.get(net, ()):
                if j in anchors and parts[j]["board"] == n["board"]:
                    cand[j] += 1 if j not in hubs else 0.5
        if cand:
            best = max(cand.values())
            tied = sorted(j for j, v in cand.items() if v == best)
            if len(tied) > 1 and i in centre:
                tied.sort(key=lambda j: _dist(centre.get(i), centre.get(j)))
            sat[i] = tied[0]
            continue
        if i in centre:
            pg = centre[i][0]
            near = sorted((j for j in anchors if centre.get(j, (None,))[0] == pg and parts[j]["board"] == n["board"]),
                          key=lambda j: (_dist(centre[i], centre[j]), j))
            if near:
                sat[i] = near[0]
    return sat, anchors


def _dist(a, b) -> float:
    if not a or not b or a[0] != b[0]:
        return 1e9
    return math.hypot(a[1] - b[1], a[2] - b[2])


def clusters(G: Graph, nets: dict, sat: dict[str, str], anchors: set[str]) -> tuple[dict[str, int], list[dict]]:
    """Feature areas: Louvain over modules, peripherals, MCU pins and anchor parts; satellites follow
    their anchor; hubs (the MCU, board-to-board connectors, in-tree Zephyr modules, bus peripherals)
    go where their strongest neighbour went, the MCU and wide connectors into a first column."""
    W = collections.defaultdict(lambda: collections.defaultdict(float))

    def link(a, b, w):
        if a != b:
            W[a][b] += w
            W[b][a] += w

    signal_nets = collections.defaultdict(set)
    for i in anchors:
        n = G.nodes[i]
        for p in n["sym"]["pins"]:
            if p.get("net") and not nets.get(p["net"], {}).get("rail"):
                signal_nets[i].add(p["net"])
    for s, a in sat.items():                       # a series part joins its anchor's nets
        for p in G.nodes[s]["sym"]["pins"]:
            if p.get("net") and not nets.get(p["net"], {}).get("rail"):
                signal_nets[a].add(p["net"])
    mated = {e["a"] for e in G.edges if e["kind"] == "mate"} | {e["b"] for e in G.edges if e["kind"] == "mate"}
    hub_parts = {i for i in anchors if G.nodes[i].get("mcu") or i in mated or len(signal_nets[i]) >= HUB_NETS}
    net_anchors = collections.defaultdict(set)
    for i, ns in signal_nets.items():
        for net in ns:
            net_anchors[net].add(i)
            for m in nets.get(net, {}).get("mates", []):
                net_anchors[m].add(i)
    for net, ps in net_anchors.items():
        ps = sorted(p for p in ps if p not in hub_parts)
        if 2 <= len(ps) <= BUS_NET_PARTS:
            for x in range(len(ps)):
                for y in range(x + 1, len(ps)):
                    link(ps[x], ps[y], 1.0)
    hubs = set(hub_parts)
    for i, n in G.nodes.items():
        if n["kind"] == "module" and n.get("zephyr"):
            hubs.add(i)
        if n["kind"] == "periph":
            hubs.add(i)
    weights = {"call": 1.0, "device": 2.0, "bind": 3.0, "gpio": 2.0, "pinctrl": 1.0, "net": 2.0, "ext": 2.0,
               "periph": 1.0, "bus": 1.0, "mate": 0.5}
    for e in G.edges:
        if e["a"] in hubs or e["b"] in hubs:
            continue
        w = weights.get(e["kind"], 1.0)
        if e["kind"] == "call":
            w = 1.0 + math.log2(1 + e.get("weight", 1)) / 2
        link(e["a"], e["b"], w)
    core = [i for i in G.nodes if i not in hubs and i not in sat and (i in W or G.nodes[i]["kind"] != "part")]
    adj = {i: dict(W.get(i, {})) for i in core}
    for i in core:
        adj[i] = {j: w for j, w in adj[i].items() if j in adj}
    comm = louvain(adj) if adj else {}
    # tiny communities (isolated nodes) fold into "unclustered"
    sizes = collections.Counter(comm.values())
    for i, c in list(comm.items()):
        if sizes[c] < 2:
            del comm[i]
    # hubs and isolated anchors: strongest non-hub neighbour, else the hub column (-2) / unclustered (-1)
    allw = collections.defaultdict(lambda: collections.defaultdict(float))
    for e in G.edges:
        allw[e["a"]][e["b"]] += weights.get(e["kind"], 1.0)
        allw[e["b"]][e["a"]] += weights.get(e["kind"], 1.0)
    for i in sorted(G.nodes):
        if i in comm or i in sat:
            continue
        if i in hub_parts:
            comm[i] = -2
            continue
        score = collections.Counter()
        for j, w in allw[i].items():
            if j in comm and comm[j] >= 0:
                score[comm[j]] += w
        comm[i] = max(sorted(score), key=lambda c: score[c]) if score else -1
    for s, a in sat.items():
        comm[s] = comm.get(a, -1)
    for i in G.nodes:
        comm.setdefault(i, -1)

    # renumber: hub column first, then by size, unclustered last
    order = sorted(set(comm.values()), key=lambda c: (c != -2, c == -1, -sum(1 for v in comm.values() if v == c), c))
    remap = {c: k for k, c in enumerate(order)}
    comm = {i: remap[c] for i, c in comm.items()}
    out = []
    for c, k in sorted(remap.items(), key=lambda kv: kv[1]):
        members = [i for i, v in comm.items() if v == k]
        out.append({"id": f"c{k}", "ix": k, "label": cluster_label(G, members, c), "size": len(members),
                    "kind": "hub" if c == -2 else ("rest" if c == -1 else "cluster")})
    return comm, out


def cluster_label(G: Graph, members: list[str], c: int) -> str:
    if c == -2:
        return "MCU · interconnect"
    if c == -1:
        return "unclustered"
    deg = collections.Counter()
    ms = set(members)
    for e in G.edges:
        if e["a"] in ms and e["b"] in ms:
            deg[e["a"]] += 1
            deg[e["b"]] += 1
    drivers = sorted((i for i in members if G.nodes[i]["kind"] == "module" and G.nodes[i]["layer"] == "drv"
                      and not G.nodes[i].get("zephyr")), key=lambda i: (-deg[i], i))
    def two(ids, name):
        out = []
        for i in ids:
            if name(i) not in out:
                out.append(name(i))
            if len(out) == 2:
                break
        return " · ".join(out)

    if drivers:
        return two(drivers, lambda i: G.nodes[i]["label"])
    mods = sorted((i for i in members if G.nodes[i]["kind"] == "module"), key=lambda i: (-deg[i], i))
    if mods:
        return two(mods, lambda i: G.nodes[i]["label"])
    parts = sorted((i for i in members if G.nodes[i]["kind"] == "part" and G.nodes[i].get("mpn")
                    and G.nodes[i]["sym_kind"] in ("ic", "connector", "transistor")),
                   key=lambda i: (G.nodes[i]["sym_kind"] != "ic", -deg[i], i))
    if parts:
        return two(parts, lambda i: G.nodes[i]["mpn"])
    return f"cluster {c}"


def box_size(n: dict) -> tuple[float, float]:
    """A code-layer node's box: label width, a subtitle line when there is one."""
    w = max(56.0, len(n["label"]) * 6.3 + 20)
    if n["kind"] == "pin":
        return 22.0, 22.0
    return w, 32.0 if n.get("sub") else 22.0


def layout(G: Graph, nets: dict, comm: dict[str, int], cl: list[dict], sat: dict[str, str],
           boards: list[str]) -> dict:
    """World positions. Columns are clusters (same x-range on every layer); the hw layer has one band per
    board. Within a column: code-layer nodes in rows; parts as groups (anchor, its satellites under it)
    shelf-packed by width."""
    net_text = {nid: n["name"] + (f" · {n['fw']['text']}" if n.get("fw") else "") for nid, n in nets.items()}
    foot = {}
    for i, n in G.nodes.items():
        if n["kind"] == "part":
            text = part_label(n)
            lab, foot[i] = place_label(n["sym"], text, net_text)
            n["sym"]["lab"] = {**lab, "t": text}
    ncl = len(cl)
    groups_of = collections.defaultdict(list)       # (cluster, board) -> [(anchor, [satellites])]
    sats_of = collections.defaultdict(list)
    for s, a in sat.items():
        sats_of[a].append(s)
    for i, n in sorted(G.nodes.items()):
        if n["kind"] != "part" or i in sat:
            continue
        groups_of[(comm[i], n["board"])].append(i)

    def group_block(a: str, width_cap: float) -> tuple[float, float, list]:
        """Place an anchor and its satellites; returns (w, h, [(id, x, y)]) with x,y = centre offsets."""
        fl, ft, fr, fb = foot[a]
        aw, ah = fr - fl, fb - ft
        items = [(a, -fl, -ft)]
        ss = sorted(sats_of.get(a, []), key=lambda s: _sat_order(G, a, s))
        x, y, rowh = 0.0, ah + 6, 0.0
        W = max(aw, min(width_cap, sum(foot[s][2] - foot[s][0] for s in ss) + 4 * len(ss)))
        for s in ss:
            sl, st, sr, sb = foot[s]
            sw, sh = sr - sl, sb - st
            if x > 0 and x + sw > W:
                x, y, rowh = 0.0, y + rowh + 4, 0.0
            items.append((s, x - sl, y - st))
            x += sw + 4
            rowh = max(rowh, sh)
        h = y + rowh if ss else ah
        return W, h, items

    code_layers = [l["id"] for l in LAYERS if l["id"] != "hw"]
    code_nodes = collections.defaultdict(list)
    for i, n in sorted(G.nodes.items(), key=lambda kv: (kv[1]["kind"], kv[1]["label"])):
        if n["layer"] in code_layers:
            code_nodes[(comm[i], n["layer"])].append(i)

    # Column widths: wide enough for the widest code row and the widest part group, and otherwise
    # sized so the whole hw plane comes out about ASPECT times wider than tall.
    areas, widest, code_w = [], [], []
    for c in range(ncl):
        area, wmax = 0.0, 0.0
        for b in boards:
            for a in groups_of.get((c, b), []):
                w, h, _ = group_block(a, 420)
                area += (w + 16) * (h + 16)
                wmax = max(wmax, w)
        cw = 0.0
        for L in code_layers:
            ws = [box_size(G.nodes[i])[0] + 14 for i in code_nodes.get((c, L), [])]
            cw = max(cw, min(sum(ws), max(ws, default=0) * 3))
        areas.append(area)
        widest.append(wmax)
        code_w.append(cw)
    band_h = math.sqrt(sum(areas) / ASPECT) if sum(areas) else 0
    col_w = []
    for c in range(ncl):
        w = max(200.0, widest[c] + 24, code_w[c] + 24, areas[c] / band_h * 1.15 if band_h else 0)
        col_w.append(round(w))
    x0 = []
    acc = 0.0
    for w in col_w:
        x0.append(acc)
        acc += w + 40
    scene_w = acc - 40 if col_w else 0

    pos = {}
    depth = {}
    for L in code_layers:
        h_max = 0.0
        for c in range(ncl):
            ids = code_nodes.get((c, L), [])
            x, y, rowh = 12.0, 14.0, 0.0
            for i in ids:
                bw, bh = box_size(G.nodes[i])
                if x > 12 and x + bw > col_w[c] - 12:
                    x, y, rowh = 12.0, y + rowh + 14, 0.0
                pos[i] = (x0[c] + x + bw / 2, y + bh / 2)
                x += bw + 14
                rowh = max(rowh, bh)
            h_max = max(h_max, y + rowh + 14 if ids else 0)
        depth[L] = max(110.0, h_max)

    band = {}
    hw_y = 18.0
    for b in boards:
        h_max = 0.0
        for c in range(ncl):
            x, y, rowh = 10.0, 0.0, 0.0
            for a in sorted(groups_of.get((c, b), []), key=lambda a: _group_order(G, a)):
                w, h, items = group_block(a, min(420, col_w[c] - 20))
                if x > 10 and x + w > col_w[c] - 10:
                    x, y, rowh = 10.0, y + rowh + 16, 0.0
                for i, dx, dy in items:
                    pos[i] = (x0[c] + x + dx, hw_y + y + dy)
                x += w + 16
                rowh = max(rowh, h)
            h_max = max(h_max, y + rowh)
        band[b] = [hw_y - 18, hw_y + h_max + 10]
        hw_y += h_max + 46
    depth["hw"] = max(110.0, hw_y - 28)

    for i, (x, y) in pos.items():
        G.nodes[i]["x"], G.nodes[i]["y"] = round(x, 1), round(y, 1)
    for k, c in enumerate(cl):
        c["x"], c["w"] = round(x0[k], 1), col_w[k]
    return {"scene_w": round(scene_w, 1), "depth": {k: round(v, 1) for k, v in depth.items()},
            "stub": STUB, "font": FONT, "pitch": P,
            "bands": [{"board": b, "name": f"board {b}", "y0": round(band[b][0], 1), "y1": round(band[b][1], 1)}
                      for b in boards]}


def part_label(n: dict) -> str:
    """The designator, and under it the value (passives) or the part number (everything else)."""
    second = n.get("value") if n["sym_kind"] in ("resistor", "capacitor", "inductor", "ferrite", "fuse", "crystal") \
        else n.get("mpn")
    return n["label"].split(":", 1)[-1] + (f"\n{second}" if second else "")


def _group_order(G: Graph, a: str):
    n = G.nodes[a]
    order = {"ic": 0, "transistor": 1, "connector": 2}
    return (not n.get("mcu"), order.get(n["sym_kind"], 3), n.get("sheet") or "", _pin_order(n["ref"]))


def _sat_order(G: Graph, a: str, s: str):
    """A satellite sits under the anchor pin it shares a net with, in that pin's order."""
    an = G.nodes[a]
    nets = {p.get("net") for p in G.nodes[s]["sym"]["pins"]}
    for k, p in enumerate(sorted(an["sym"]["pins"], key=lambda p: ({"l": 0, "t": 1, "r": 2, "b": 3}[p["s"]], p["y"], p["x"]))):
        if p.get("net") in nets:
            return (0, k, s)
    return (1, 0, _pin_order(G.nodes[s]["ref"]))


# ---------------------------------------------------------------- checks, docs, run

def checks_summary(report: dict, info: dict) -> list[dict]:
    rows = []
    for c in report.get("checks", []):
        rows.append({"check": c.get("check"), "ran": c.get("ran"), "passed": c.get("passed"),
                     "candidates": c.get("candidates"), "sources": c.get("sources")})
    return rows


def docs_table(ad: Path, J) -> dict:
    out = {}
    for d in sorted((ad / "docs").glob("*/doc.json")):
        try:
            meta = read_json(d)
        except (OSError, ValueError):
            continue
        pdf = meta.get("path")
        pdf_abs = (d.parent / pdf).resolve() if pdf and not Path(pdf).is_absolute() else (Path(pdf) if pdf else None)
        if pdf and not (pdf_abs and pdf_abs.is_file()):
            alt = (ad / pdf).resolve()
            pdf_abs = alt if alt.is_file() else pdf_abs
        out[meta.get("doc_id") or d.parent.name] = {
            "title": meta.get("title"), "kind": meta.get("kind"), "part": meta.get("part"), "pages": meta.get("pages"),
            "file": meta.get("file"), "pdf": str(pdf_abs) if pdf_abs and pdf_abs.is_file() else None,
            "card": (d.parent / "card.md").is_file()}
    try:
        from embarch_atlas.project import load_config
        cfg = load_config(ad)
    except Exception:
        cfg = {}
    src = (cfg.get("project") or {}).get("sources") or "embarch/hardware"
    fw = Path((cfg.get("project") or {}).get("fw") or ad.parent.parent)
    for lab, B in J.boards.items():
        f = (B.nl.get("source") or {}).get("file")
        p = (fw / src / f) if f else None
        out[f"sch:{lab}"] = {"title": f"board {lab} schematic", "kind": "schematic", "part": None,
                             "pages": len(B.nl.get("sheets") or {}) or None, "file": f,
                             "pdf": str(p.resolve()) if p and p.is_file() else None, "card": False,
                             "sheets": {k: v.get("page") for k, v in (B.nl.get("sheets") or {}).items()}}
    return out


def run(ad: Path, atlas: Path, J, stamp: dict, pins: dict, report: dict, log=print,
        code: dict | None = None) -> dict:
    dt = J.dt
    if code is None:
        code = code_model(ad, atlas, stamp, dt, log=log)
    res = build(ad, atlas, J, stamp, pins, report, code, log=log)
    G, nets = res["G"], res["nets"]
    sat, anchors = anchors_and_satellites(G, nets, ad)
    for s, a in sat.items():
        G.nodes[s]["of"] = f"{a}"
    comm, cl = clusters(G, nets, sat, anchors)
    for i, c in comm.items():
        G.nodes[i]["cluster"] = f"c{c}"
    boards = sorted(J.boards)
    lay = layout(G, nets, comm, cl, sat, boards)
    counts = collections.Counter(n["kind"] for n in G.nodes.values())
    out = {
        "schema": SCHEMA,
        "atlas": stamp["id"], "target": stamp.get("target"), "board": stamp.get("board"), "app": stamp.get("app"),
        "commit": stamp.get("commit"), "created": stamp.get("created"),
        "fw": str(Roots(ad, stamp).fw_repo) if Roots(ad, stamp).fw_repo else None,
        "zephyr": (stamp.get("build") or {}).get("zephyr_base"),
        "vocab": {"layers": LAYERS, "kinds": KINDS, "status": STATUS, "edges": EDGE_KINDS, "symbols": SYMBOLS},
        "layout": lay,
        "clusters": cl,
        "nodes": sorted(G.nodes.values(), key=lambda n: (LAYER_IX[n["layer"]], n["id"])),
        "edges": sorted(G.edges, key=lambda e: (e["kind"], e["a"], e["b"], e.get("label") or "")),
        "nets": nets,
        "problems": res["problems"],
        "info": {k: v for k, v in sorted(res["info"].items())},
        "checks": checks_summary(report, res["info"]),
        "summary": report.get("summary"),
        "docs": docs_table(ad, J),
        "code_note": code.get("note"),
        "counts": {"nodes": len(G.nodes), "edges": len(G.edges), "nets": len(nets), **dict(sorted(counts.items())),
                   "mismatches": sum(1 for p in res["problems"] if p["sev"] == "mis"),
                   "unverified": sum(1 for p in res["problems"] if p["sev"] == "unv")},
    }
    write_json(atlas / "graph.json", out)
    log(f"graph: {len(G.nodes)} nodes, {len(G.edges)} edges, {len(cl)} clusters, "
        f"{out['counts']['mismatches']} mismatches, {out['counts']['unverified']} unverified groups")
    return out


def cmd_graph(args) -> int:
    from embarch_atlas.build import resolve_atlas, resolve_atlas_dir
    from embarch_atlas.join import load_join
    ad = resolve_atlas_dir(args)
    atlas = resolve_atlas(ad, args.atlas)
    stamp = read_json(atlas / "atlas.json")
    for f in ("dt.json", "in_use.json", "pins.json", "mismatches.json"):
        if not (atlas / f).is_file():
            raise SystemExit(f"{atlas}: no {f} (run `join` first)")
    dt, in_use = read_json(atlas / "dt.json"), read_json(atlas / "in_use.json")
    J, _root, _sysj = load_join(ad, atlas, stamp, dt, in_use, log=lambda *a: None)
    run(ad, atlas, J, stamp, read_json(atlas / "pins.json"), read_json(atlas / "mismatches.json"))
    return 0


def register(sub, groups) -> None:
    p = sub.add_parser("graph", help="graph.json: the whole atlas as one graph, for the embarch-ui Atlas tab (also run by join)")
    p.add_argument("atlas", help="atlas id (<target>@<commit12>), its dir, or a target name")
    p.set_defaults(func=cmd_graph)

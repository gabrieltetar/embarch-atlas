"""`footprint` and `stacks`: where a build's flash and RAM go, and its thread stacks.

Read from the build's own `zephyr.elf` with its own binutils (`CMAKE_NM`, `CMAKE_OBJDUMP` from the
build): `objdump -h` gives the sections and their flags, `nm -l -S` every symbol with its size and
the file:line its debug info places it at. A section that is loaded (`LOAD`) is in the flash image,
and one that is allocated and writable is in RAM; initialised data is in both, as the linker counts
it. Capacities come from the devicetree: the `zephyr,flash`, `zephyr,sram` and
`zephyr,code-partition` chosen nodes. The code partition matters on its own: an image that fits
the flash can still overflow the slot a bootloader gives it.

A thread stack is a symbol in `noinit` whose name says stack (`*_stack`, `*_stacks`,
`_k_thread_stack_*`, `*stack_area*`). Each is listed with its size and the line that defined it,
next to the `CONFIG_*_STACK_SIZE` options of the same value whose name shares a word with it (a
pairing by inference, said so). How much of each stack a running board uses is a live question
(`kernel thread stacks`), not this one.

The parse of one ELF is cached under `cache/footprint/` by the ELF's hash.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from pathlib import Path

from . import kconfig as KC
from .common import read_json, write_json

SEC_RE = re.compile(r"^\s*\d+\s+(\S+)\s+([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\s+[0-9a-fA-F]+\s+2\*\*\d+")
NM_RE = re.compile(r"^([0-9a-fA-F]+)\s+([0-9a-fA-F]+)\s+(\w)\s+(\S+)(?:\t(.*):(\d+))?\s*$")
STACK_NAME = re.compile(r"(^_k_thread_stack_|stacks?$|stack_area|_stk$)", re.I)
TOP = 25


# ---------------------------------------------------------------- reading one ELF

def _tool(path: str | None, fallback: str) -> str | None:
    if path and Path(path).is_file():
        return path
    return shutil.which(fallback)


def build_tools(cmake_cache: Path) -> dict[str, str]:
    out = {}
    if cmake_cache.is_file():
        for line in cmake_cache.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"(CMAKE_NM|CMAKE_OBJDUMP):\w+=(.*)", line)
            if m:
                out[m[1]] = m[2]
    return out


def parse_sections(text: str) -> list[dict]:
    """`objdump -h` -> [{name, size, vma, lma, flags}]; the flags are on the line after each section."""
    out, lines = [], text.splitlines()
    for i, line in enumerate(lines):
        m = SEC_RE.match(line)
        if not m:
            continue
        flags = {f.strip() for f in lines[i + 1].split(",")} if i + 1 < len(lines) else set()
        out.append({"name": m[1], "size": int(m[2], 16), "vma": int(m[3], 16), "lma": int(m[4], 16),
                    "flags": sorted(flags)})
    return out


def parse_symbols(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        m = NM_RE.match(line)
        if m and int(m[2], 16):
            out.append({"a": int(m[1], 16), "s": int(m[2], 16), "t": m[3], "n": m[4], "f": m[5],
                        "l": int(m[6]) if m[6] else None})
    return out


def image(elf: Path, tools: dict[str, str], cache: Path, rel) -> dict:
    """{sections, symbols} of one ELF, symbol files made relative with `rel`, cached by the ELF hash."""
    data = elf.read_bytes()
    key = hashlib.sha256(data).hexdigest()[:16]
    cf = cache / "footprint" / f"{key}.json"
    if cf.is_file():
        return read_json(cf)
    nm = _tool(tools.get("CMAKE_NM"), "arm-zephyr-eabi-nm")
    od = _tool(tools.get("CMAKE_OBJDUMP"), "arm-zephyr-eabi-objdump")
    if not nm or not od:
        raise FileNotFoundError("no nm/objdump for this build (CMAKE_NM, CMAKE_OBJDUMP)")
    secs = parse_sections(subprocess.run([od, "-h", str(elf)], capture_output=True, text=True, check=True).stdout)
    syms = parse_symbols(subprocess.run([nm, "-l", "--defined-only", "-S", str(elf)], capture_output=True,
                                        text=True, check=True).stdout)
    for s in syms:
        if s["f"]:
            s["f"] = rel(s["f"])
    out = {"elf_sha256": key, "sections": secs, "symbols": syms}
    write_json(cf, out)
    return out


# ---------------------------------------------------------------- classifying

def _kind(sec: dict) -> tuple[bool, bool]:
    """(counts in the flash image, counts in RAM) for a section."""
    f = set(sec["flags"])
    if "ALLOC" not in f:
        return False, False
    if "THREAD_LOCAL" in f and "LOAD" not in f:     # .tbss: a template, its threads' copies live in their stacks
        return False, False
    return "LOAD" in f, "READONLY" not in f


def totals(img: dict) -> dict:
    rom = ram = 0
    by = {"rom": [], "ram": []}
    for s in img["sections"]:
        r, w = _kind(s)
        if r:
            rom += s["size"]
            by["rom"].append((s["name"], s["size"]))
        if w:
            ram += s["size"]
            by["ram"].append((s["name"], s["size"]))
    return {"rom": rom, "ram": ram, "sections": by}


def _section_of(img: dict):
    secs = sorted((s for s in img["sections"] if "ALLOC" in s["flags"] and s["size"]), key=lambda s: s["vma"])

    def find(addr: int) -> dict | None:
        lo, hi = 0, len(secs) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            s = secs[mid]
            if addr < s["vma"]:
                hi = mid - 1
            elif addr >= s["vma"] + s["size"]:
                lo = mid + 1
            else:
                return s
        return None
    return find


def _group(f: str | None, by: str) -> str:
    if not f:
        return "(no debug file: libc, linker-made, assembly)"
    if by == "file":
        return f
    tag, _, path = f.partition(":") if ":" in f.split("/")[0] else ("", "", f)
    parts = path.split("/")
    depth = 3 if parts[0] in ("drivers", "subsys", "lib", "modules", "soc", "arch") else 2
    return (f"{tag}:" if tag else "") + "/".join(parts[:min(depth, max(1, len(parts) - 1))])


def aggregate(img: dict, by: str) -> dict[str, list[int]]:
    """group -> [rom bytes, ram bytes]; `by` is dir, file or symbol."""
    find = _section_of(img)
    out: dict[str, list[int]] = {}
    for s in img["symbols"]:
        sec = find(s["a"])
        if not sec:
            continue
        r, w = _kind(sec)
        key = s["n"] if by == "symbol" else _group(s["f"], by)
        acc = out.setdefault(key, [0, 0])
        acc[0] += s["s"] if r else 0
        acc[1] += s["s"] if w else 0
    return out


# ---------------------------------------------------------------- capacities from the devicetree

def capacities(dt: dict | None) -> dict[str, tuple[str, int]]:
    """name -> (node path, bytes) for the flash, the SRAM and the code partition."""
    out = {}
    if not dt:
        return out
    nodes, chosen = dt.get("nodes") or {}, dt.get("chosen") or {}
    for key, name in (("zephyr,flash", "flash"), ("zephyr,sram", "sram"), ("zephyr,code-partition", "code partition")):
        path = chosen.get(key)
        reg = (((nodes.get(path) or {}).get("props") or {}).get("reg") or {}).get("val")
        if path and isinstance(reg, list) and len(reg) >= 2:
            out[name] = (path, int(reg[1]))
    return out


# ---------------------------------------------------------------- answers

def _kb(n: int) -> str:
    return f"{n} B ({n / 1024:.1f} KiB)"


def _signed(n: int) -> str:
    return f"{n:+d}" if n else "0"


def footprint_answer(img: dict, dt: dict | None, by: str = "dir", vs: dict | None = None, vs_label: str = "",
                     top: int = TOP) -> str:
    t = totals(img)
    caps = capacities(dt)
    lines = [f"flash image {_kb(t['rom'])}, RAM {_kb(t['ram'])}"]
    for name, (path, size) in caps.items():
        used = t["ram"] if name == "sram" else t["rom"]
        lines.append(f"  {name} {_kb(size)}: {100 * used / size:.1f}% used, {size - used} B left  ({path})")
    if not caps:
        lines.append("  (no devicetree for this build: capacities unknown)")
    if vs:
        tv = totals(vs)
        lines += ["", f"against {vs_label}: flash image {_signed(t['rom'] - tv['rom'])} B, RAM {_signed(t['ram'] - tv['ram'])} B"]
        a, b = aggregate(img, by), aggregate(vs, by)
        rows = []
        for k in set(a) | set(b):
            ra, wa = a.get(k, [0, 0])
            rb, wb = b.get(k, [0, 0])
            if ra != rb or wa != wb:
                rows.append((abs(ra - rb) + abs(wa - wb), k, ra - rb, wa - wb))
        rows.sort(key=lambda r: (-r[0], r[1]))
        lines.append(f"largest changes by {by} (flash, RAM):")
        lines += [f"  {_signed(dr):>7} {_signed(dw):>7}  {k}" for _, k, dr, dw in rows[:top]]
        if len(rows) > top:
            lines.append(f"  ({len(rows) - top} more changed)")
        return "\n".join(lines)
    agg = aggregate(img, by)
    for col, idx, whole in (("flash", 0, t["rom"]), ("RAM", 1, t["ram"])):
        rows = sorted(((v[idx], k) for k, v in agg.items() if v[idx]), reverse=True)
        total = sum(v for v, _ in rows) or 1
        lines += ["", f"largest by {by}, {col} ({whole - total} B of the {col} total sit in no sized symbol: "
                  "padding, alignment, section fill):"]
        lines += [f"  {v:>7} B {100 * v / total:4.1f}%  {k}" for v, k in rows[:top]]
        if len(rows) > top:
            lines.append(f"  ({len(rows) - top} more, {sum(v for v, _ in rows[top:])} B)")
    return "\n".join(lines)


def _words(s: str) -> set[str]:
    w = {x for x in re.split(r"[^a-z0-9]+", s.lower()) if x and x not in
         ("k", "thread", "stack", "stacks", "size", "area", "config", "z", "workq", "workqueue", "my", "tid", "g")}
    return w | {x[:3] for x in w if len(x) > 3}          # log / logging, pd / power...


def _dir(path: str) -> str:
    """`tag:dir/file[:line]` -> `tag:dir`."""
    return re.sub(r":\d+$", "", path).rsplit("/", 1)[0]


def stack_rows(img: dict, dotconfig: dict[str, str], ksyms: dict | None = None) -> list[dict]:
    """Every thread stack symbol in noinit, largest first, each with the *_STACK_SIZE options it most
    likely comes from (`opts`, and `how` they were matched: an inference)."""
    find = _section_of(img)
    rows = []
    for s in img["symbols"]:
        sec = find(s["a"])
        if not sec or "noinit" not in sec["name"] or not STACK_NAME.search(s["n"]):
            continue
        rows.append(dict(s))
    rows.sort(key=lambda s: (-s["s"], s["n"]))
    cfg = stack_options(dotconfig)
    for s in rows:
        sdir = _dir(s["f"]) if s["f"] else None
        same = [k for k, v in cfg.items() if v == s["s"]]
        # an option of the same size defined in the stack's own directory (needs kconfig.json), else one
        # of the same size sharing a name word
        match, how = [k for k in same if sdir and any(_dir(at) in (sdir, sdir.rsplit("/", 1)[0])
                                                      for at in ((ksyms or {}).get(k) or {}).get("at") or [])], "defined beside it"
        named = [k for k in same if _words(k) & _words(s["n"])]
        if len(match) > 1 and set(match) & set(named):
            match, how = [k for k in match if k in named], "defined beside it, name word"
        elif not match:
            match, how = named, "name word"
        s["opts"], s["how"] = match, how
    return rows


def stack_options(dotconfig: dict[str, str]) -> dict[str, int]:
    return {k: int(v, 0) for k, v in dotconfig.items() if k.endswith("STACK_SIZE") and re.fullmatch(r"(0x)?[0-9a-fA-F]+", v)}


def stacks_answer(img: dict, dotconfig: dict[str, str], ksyms: dict | None = None) -> str:
    rows = stack_rows(img, dotconfig, ksyms)
    cfg = stack_options(dotconfig)
    used: set[str] = set()
    lines = [f"{len(rows)} stacks, {sum(s['s'] for s in rows)} B in noinit (sizes include alignment)", ""]
    for s in rows:
        match, how = s["opts"], s["how"]
        used.update(match)
        where = f"{s['f']}:{s['l']}" if s["f"] else "(no debug file)"
        lines.append(f"  {s['s']:>6} B  {s['n']:<34} {where}"
                     + (f"   ~ CONFIG_{', CONFIG_'.join(match)} (same size, {how}: inference)" if match else ""))
    rest = sorted(k for k in cfg if k not in used)
    if rest:
        lines += ["", "other *_STACK_SIZE options in .config: "
                  + ", ".join(f"CONFIG_{k}={cfg[k]}" for k in rest)]
    lines += ["", "Live high-water marks: `live_stacks` (or `kernel thread stacks` on the board)"
              + (" (CONFIG_THREAD_STACK_INFO=y in this build)" if dotconfig.get("THREAD_STACK_INFO") == "y" else "")]
    return "\n".join(lines)


def dotconfig_of(path: Path) -> dict[str, str]:
    return KC.read_dotconfig(path) if path.is_file() else {}

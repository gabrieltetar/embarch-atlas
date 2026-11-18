"""shell.json: the Zephyr shell commands a build's image really has, and how each is called.

Read from the build's own `zephyr.elf`, never from the sources: a subcommand guarded by a Kconfig
that is off is in the source and not in the image, and only the image says which it is. The ELF
holds the shell's tables as data, in the layout of Zephyr 4.3 `include/zephyr/shell/shell.h`: every root command is a
`union shell_cmd_entry` in the `shell_root_cmds` iterable section, and that union is one pointer;
for a root it points at a `struct shell_static_entry` (syntax, help, subcommand set, handler, then
the mandatory and optional argument counts as two bytes, then `Z_SHELL_STATIC_ENTRY_PADDING`, 24
bytes on a 64-bit x86 image and 0 elsewhere). A subcommand set is a pointer to such a union, and
which kind it is decides the section the union sits in, as Zephyr's `is_dynamic_cmd` does: in
`shell_subcmds` the pointer is an array of static entries ending in an all-zero one, in
`shell_dynamic_subcmds` it is a getter function called at run time. The older layout, a bool
before the union, is not read. The tables are read with the stdlib ELF reader of `dt.py`; the source file and line of each
table or getter come from `nm -l` through `footprint.image`, which `stacks` already uses.

A dynamic set is not run. Its getter is named, and when it is the one that walks the device list
(its name says `device`) the devices of the image are listed: every object of the `device_area`
section, by the name its `struct device` carries. The getter's own filter and the devices'
init state are runtime facts and are not evaluated.

`mandatory` counts the command word itself, as Zephyr does. An optional count of 254 is the raw
rest of the line (`SHELL_OPT_ARG_RAW`) and 255 is not limited, unchecked (`SHELL_OPT_ARG_CHECK_SKIP`).

The record carries the ELF's size and modification time. A build directory rebuilt after the
atlas was made changes the answer, and `stale()` says so by comparing them with the file now.
A build with no shell compiled in gets a record that says so, not an error.
"""
from __future__ import annotations

import hashlib
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .dt import Elf

SCHEMA = 1
MAX_DEPTH = 6
MAX_STR = 1024
MAX_ENTRIES = 4096
OPT_RAW = 0xFE
OPT_SKIP_CHECK = 0xFF
EM_X86_64 = 62
X86_64_ENTRY_PADDING = 24       # Z_SHELL_STATIC_ENTRY_PADDING on a 64-bit x86 image
ROOT_SECTION = "shell_root_cmds"
DYNAMIC_SECTION = "shell_dynamic_subcmds"
DEVICE_SECTION = "device_area"
SHT_PROGBITS = 1
SHF_ALLOC = 2


class _Image:
    """Bytes of an ELF by virtual address."""

    def __init__(self, elf: Elf):
        self.elf = elf
        self.p = 8 if elf.is64 else 4
        self.e = elf.end
        self.secs = [s for s in elf.sections if s["type"] == SHT_PROGBITS and s["flags"] & SHF_ALLOC and s["size"]]

    def read(self, addr: int, n: int) -> bytes | None:
        for s in self.secs:
            if s["addr"] <= addr and addr + n <= s["addr"] + s["size"]:
                o = s["off"] + addr - s["addr"]
                return self.elf.data[o:o + n]
        return None

    def ptr(self, addr: int) -> int | None:
        b = self.read(addr, self.p)
        return struct.unpack(self.e + ("Q" if self.p == 8 else "I"), b)[0] if b else None

    def cstr(self, addr: int | None) -> str | None:
        if not addr:
            return None
        for s in self.secs:
            if s["addr"] <= addr < s["addr"] + s["size"]:
                o = s["off"] + addr - s["addr"]
                end = min(s["off"] + s["size"], o + MAX_STR)
                z = self.elf.data.find(b"\0", o, end)
                return self.elf.data[o:z if z >= 0 else end].decode("utf-8", "replace")
        return None

    @property
    def entry_size(self) -> int:
        """sizeof(struct shell_static_entry): four pointers, two count bytes, `Z_SHELL_STATIC_ENTRY_PADDING`, then
        the struct's own tail padding to a pointer."""
        pad = X86_64_ENTRY_PADDING if self.elf.is64 and self.machine == EM_X86_64 else 0
        return (4 * self.p + 2 + pad + self.p - 1) // self.p * self.p

    @property
    def machine(self) -> int:
        return struct.unpack_from(self.e + "H", self.elf.data, 18)[0]

    def section_name(self, addr: int) -> str:
        for s in self.elf.sections:
            if s["flags"] & SHF_ALLOC and s["size"] and s["addr"] <= addr < s["addr"] + s["size"]:
                return str(s.get("name", ""))
        return ""


def _where(lines: dict[int, tuple[str, int]], addr: int | None) -> str | None:
    if addr is None:
        return None
    hit = lines.get(addr) or lines.get(addr & ~1)
    return f"{hit[0]}:{hit[1]}" if hit and hit[0] else None


def _walk(im: _Image, funcs: dict[int, str], lines: dict[int, tuple[str, int]], devices: list[str],
          cmd_entry: int | None, depth: int, seen: set[int]) -> tuple[list[dict] | None, dict | None]:
    """A `union shell_cmd_entry` at `cmd_entry` -> (static subcommands, or None) and (dynamic info, or None)."""
    if not cmd_entry:
        return None, None
    target = im.ptr(cmd_entry)
    if not target:
        return None, None
    if im.section_name(cmd_entry).startswith(DYNAMIC_SECTION):
        name = funcs.get(target & ~1) or f"0x{target:x}"
        info: dict[str, Any] = {"getter": name, "at": _where(lines, target)}
        if "device" in name.lower():
            info["offers"] = "devices"
            info["devices"] = list(devices)
        return None, info
    if target in seen or depth > MAX_DEPTH:
        return None, None
    seen = seen | {target}
    subs = []
    addr = target
    for _ in range(MAX_ENTRIES):
        e = _entry(im, funcs, lines, devices, addr, depth, seen)
        if e is None:
            break
        subs.append(e)
        addr += im.entry_size
    return subs, None


def _entry(im: _Image, funcs: dict[int, str], lines: dict[int, tuple[str, int]], devices: list[str],
           addr: int, depth: int, seen: set[int]) -> dict | None:
    """One `struct shell_static_entry`, or None at the all-zero terminator (or when the bytes are not in the image)."""
    syn = im.ptr(addr)
    if not syn:
        return None
    name = im.cstr(syn)
    if name is None:
        return None
    p = im.p
    sub, handler = im.ptr(addr + 2 * p), im.ptr(addr + 3 * p)
    counts = im.read(addr + 4 * p, 2)
    mand, opt = (counts[0], counts[1]) if counts else (0, 0)
    out: dict[str, Any] = {"name": name, "help": im.cstr(im.ptr(addr + p)) or "", "mandatory": mand, "optional": opt,
                           "handler": funcs.get(handler & ~1) or (f"0x{handler:x}" if handler else None)}
    subs, dyn = _walk(im, funcs, lines, devices, sub, depth + 1, seen)
    if subs is not None:
        out["subcommands"] = subs
        out["at"] = _where(lines, im.ptr(sub))
    if dyn is not None:
        out["dynamic"] = dyn
        out["at"] = dyn["at"]
    return out


def elf_stamp(elf: Path) -> dict:
    st = Path(elf).stat()
    return {"name": Path(elf).name, "mtime": st.st_mtime, "size": st.st_size,
            "sha256": hashlib.sha256(Path(elf).read_bytes()).hexdigest()[:16]}


def inventory(elf: Path, lines: dict[int, tuple[str, int]] | None = None, config: dict[str, str] | None = None) -> dict:
    """The shell inventory of one ELF. `lines` maps an address to (file, line), `config` is the build's .config."""
    lines = lines or {}
    e = Elf(elf)
    im = _Image(e)
    syms = e.symbols()
    funcs = {s["value"] & ~1: s["name"] for s in syms if s["type"] == 2 and s["name"]}

    def in_section(s: dict, prefix: str) -> bool:
        return 0 < s["shndx"] < len(e.sections) and str(e.sections[s["shndx"]].get("name", "")).startswith(prefix)

    devices = sorted({im.cstr(im.ptr(s["value"])) or s["name"] for s in syms
                      if s["type"] == 1 and s["size"] and in_section(s, DEVICE_SECTION)})
    roots = sorted({s["value"] for s in syms if s["type"] == 1 and s["size"] and in_section(s, ROOT_SECTION)})
    shell_cfg = (config or {}).get("SHELL")
    out: dict[str, Any] = {"schema": SCHEMA, "elf": elf_stamp(elf), "commands": {},
                           "config": {"SHELL": shell_cfg if shell_cfg is not None else "unknown"}}
    for addr in roots:
        entry = im.ptr(addr)
        root = _entry(im, funcs, lines, devices, entry, 0, {entry}) if entry else None
        if root is None:
            continue
        root.setdefault("at", _where(lines, entry))
        out["commands"][root["name"]] = root
    out["shell"] = bool(out["commands"])
    if not out["shell"]:
        out["why"] = ("no shell is compiled into this image: no `shell_root_cmds` entry in the ELF"
                      + (f" (CONFIG_SHELL={shell_cfg})" if shell_cfg is not None else ""))
    else:
        out["devices"] = devices
    return out


def lines_from_image(img: dict) -> dict[int, tuple[str, int]]:
    """address -> (file, line) from a `footprint.image` parse (`nm -l`)."""
    return {s["a"]: (s["f"], s["l"]) for s in img.get("symbols", []) if s.get("f") and s.get("l")}


def for_build(elf: Path, tools: dict[str, str], cache: Path, rel, dotconfig: Path | None) -> dict:
    """The inventory of a build's ELF with source lines from its own `nm -l`; without binutils, without lines."""
    from . import footprint as F
    from . import kconfig as KC
    try:
        lines = lines_from_image(F.image(elf, tools, cache, rel))
    except Exception:       # no nm, or nm failing on an odd ELF must not lose the tables, which need no binutils
        lines = {}
    config = KC.read_dotconfig(dotconfig) if dotconfig and Path(dotconfig).is_file() else {}
    return inventory(elf, lines, config)


def stale(rec: dict, elf: Path | None, label: str) -> str | None:
    """Why `rec` no longer describes the ELF at `elf`, or None when its size and mtime still match."""
    if not elf or not Path(elf).is_file():
        return None
    st = Path(elf).stat()
    was = rec.get("elf") or {}
    if st.st_size != was.get("size") or abs(st.st_mtime - float(was.get("mtime") or 0)) > 1e-3:
        return (f"STALE: {label} changed since this inventory was read (mtime {_iso(st.st_mtime)}, {st.st_size} B; "
                f"read at {_iso(was.get('mtime'))}, {was.get('size')} B)")
    return None


def _iso(t: Any) -> str:
    try:
        return datetime.fromtimestamp(float(t), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


# ---------------------------------------------------------------- the answer

def _args(e: dict) -> str:
    o = e["optional"]
    opt = "raw rest of line" if o == OPT_RAW else "unchecked" if o == OPT_SKIP_CHECK else str(o)
    return f"{e['mandatory']} mandatory, {opt} optional"


def _rows(e: dict, path: str, out: list[str]) -> None:
    full = f"{path} {e['name']}".strip()
    at = f" [{e['at']}]" if e.get("at") else ""
    h = f" - {e['help']}" if e.get("help") else ""
    out.append(f"- `{full}` ({_args(e)}){h}{at}")
    dyn = e.get("dynamic")
    if dyn:
        what = f"dynamic, getter `{dyn['getter']}`" + (f" [{dyn['at']}]" if dyn.get("at") else "")
        if dyn.get("offers") == "devices":
            devs = dyn.get("devices") or []
            out.append(f"  - {what}: offers the image's devices ({len(devs)}): " + (", ".join(devs) or "none")
                       + "; the getter's own filter and the devices' init state are not evaluated")
        else:
            out.append(f"  - {what}: its names are produced at run time and not listed")
    for s in e.get("subcommands") or []:
        _rows(s, full, out)


def answer(rec: dict, command: str | None) -> tuple[str, str]:
    """(head extra for the src line, markdown body) for one root command, or the list of roots."""
    el = rec.get("elf") or {}
    extra = f"elf={el.get('name', '?')} mtime={_iso(el.get('mtime'))}"
    if not rec.get("shell"):
        return extra, rec.get("why") or "No shell is compiled into this image."
    cmds = rec["commands"]
    if not command:
        rows = []
        for n in sorted(cmds):
            c = cmds[n]
            subs = c.get("subcommands")
            kind = "dynamic" if c.get("dynamic") else f"{len(subs)} subcommands" if subs else "no subcommands"
            rows.append(f"- `{n}` ({kind}){' - ' + c['help'] if c.get('help') else ''}"
                        f"{' [' + c['at'] + ']' if c.get('at') else ''}")
        return extra, f"{len(cmds)} root shell commands in the image:\n" + "\n".join(rows)
    c = cmds.get(command.strip())
    if c is None:
        return extra, (f"No root shell command `{command}` in the image. Present: " + ", ".join(sorted(cmds)))
    out: list[str] = []
    _rows(c, "", out)
    return extra, "\n".join(out)

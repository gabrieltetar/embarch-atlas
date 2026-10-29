"""CMSIS struct-member names for registers: `UCPD1->CFG1` is the SVD's `UCPD_CFGR1`.

Firmware names an MCU register by the device header's `<PERIPH>_TypeDef` member (`UCPD1->CFG1`,
`RX_PAYSZ`), and the SVD names it its own way, so `register UCPD1.CFG1` misses. The two agree on the
offset: a member's offset is computed from the typedef's layout, the instance's address is the
header's `<INSTANCE>_BASE`, and the SVD register at that address is the one meant.

The header is the one `mcu vote --header` read: its path is recorded in the RM doc's `vote*.json`,
so no new configuration exists. Without a recorded header, or without the file, nothing aliases.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

SIZES = {"uint8_t": 1, "int8_t": 1, "uint16_t": 2, "int16_t": 2, "uint32_t": 4, "int32_t": 4,
         "uint64_t": 8, "int64_t": 8}
_MEMBER = re.compile(r"^(?:(?:__\w+|volatile|const)\s+)*(u?int(?:8|16|32|64)_t)\s+(.+)$", re.S)
_NAME = re.compile(r"^(\w+)\s*(?:\[\s*(\d+)\s*\])?$")


def parse_structs(text: str) -> dict[str, list[tuple[str, int]]]:
    """`<TYPE>_TypeDef` typedefs -> [(member, byte offset)], natural alignment. A typedef with a
    member this does not understand (a nested struct, a union) is left out whole: a wrong offset
    would alias the wrong register."""
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"//[^\n]*", " ", text)
    out: dict[str, list[tuple[str, int]]] = {}
    for m in re.finditer(r"typedef\s+struct\s*(?:\w+\s*)?\{([^{}]*)\}\s*(\w+)_TypeDef\s*;", text):
        off, members, ok = 0, [], True
        for stmt in m.group(1).split(";"):
            stmt = " ".join(stmt.split())
            if not stmt:
                continue
            mm = _MEMBER.match(stmt)
            if not mm:
                ok = False
                break
            size = SIZES[mm.group(1)]
            for part in mm.group(2).split(","):
                nm = _NAME.match(part.strip())
                if not nm:
                    ok = False
                    break
                off = (off + size - 1) // size * size
                members.append((nm.group(1), off))
                off += size * int(nm.group(2) or 1)
            if not ok:
                break
        if ok:
            out[m.group(2)] = members
    return out


def header_path(atlas_dir: Path) -> Path | None:
    """The CMSIS header `mcu vote` recorded, if it is still on disk."""
    from .common import read_json
    for f in sorted((Path(atlas_dir) / "docs").glob("*/vote*.json")):
        try:
            h = (read_json(f) or {}).get("header")
        except (OSError, ValueError):
            continue
        p = Path((h or {}).get("file") or "")
        if p.name and p.is_file():
            return p
    return None


def member_address(header: Path, instance: str, member: str) -> int | None:
    """The address of `<instance>-><member>`: the instance's base plus the member's offset."""
    from .vote import parse_header
    h = parse_header(header)
    typ = h["types"].get(instance)
    base = h["bases"].get(f"{instance}_BASE")
    if typ is None or base is None:
        return None
    structs = parse_structs(header.read_text(encoding="utf-8", errors="replace"))
    for nm, off in structs.get(typ, []):
        if nm.upper() == member.upper():
            return base[0] + off
    return None

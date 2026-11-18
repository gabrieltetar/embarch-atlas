"""shell.json and the shell_commands lookup over a synthetic ELF (invented commands, no client build)."""
import os
import struct
from pathlib import Path

import pytest

from embarch_atlas import query, shellcmds
from embarch_atlas.dt import Elf
from embarch_atlas.common import read_json, write_json, write_text

RODATA, ROOTS, DEVS, TEXT, SUBS, DYNS = 0x1000, 0x2000, 0x3000, 0x4000, 0x5000, 0x6000
P = 4
ENTRY = 20      # four pointers, two count bytes, no Z_SHELL_STATIC_ENTRY_PADDING on ARM, padded to a pointer
# the sections in file order; their ELF index is the position + 1
SECTIONS = [(".rodata", 2, RODATA), ("shell_root_cmds_area", 2, ROOTS), (".text", 6, TEXT), ("device_area", 2, DEVS),
            ("shell_subcmds_area", 2, SUBS), ("shell_dynamic_subcmds_area", 2, DYNS)]


class Img:
    """Lays out shell tables as Zephyr 4.3 include/zephyr/shell/shell.h does, in an ELF32 little-endian file:
    a `union shell_cmd_entry` is one pointer, in `shell_root_cmds` for a root, `shell_subcmds` for a static
    set and `shell_dynamic_subcmds` for a getter."""

    def __init__(self):
        self.buf = {n: bytearray() for n, _, _ in SECTIONS}
        self.base = {n: a for n, _, a in SECTIONS}
        self.syms: list[tuple[str, int, int, int, str]] = []     # name, value, size, type, section name
        self.strs: dict[str, int] = {}

    def _at(self, sec: str) -> int:
        b = self.buf[sec]
        while len(b) % 4:
            b.append(0)
        return self.base[sec] + len(b)

    def put(self, sec: str, data: bytes) -> int:
        addr = self._at(sec)
        self.buf[sec] += data
        return addr

    def string(self, s: str) -> int:
        if s not in self.strs:
            self.strs[s] = self.put(".rodata", s.encode() + b"\0")
        return self.strs[s]

    def entry_bytes(self, syntax, help_="", sub=0, handler=0, mand=0, opt=0) -> bytes:
        return struct.pack("<IIIIBBxx", self.string(syntax) if syntax else 0, self.string(help_) if help_ else 0,
                           sub, handler, mand, opt)

    def static_set(self, name, entries: list[dict]) -> int:
        """`SHELL_STATIC_SUBCMD_SET_CREATE`: the array, ended by a zero entry, and the union that points at it."""
        blobs = [self.entry_bytes(**e) for e in entries] + [b"\0" * ENTRY]
        arr = self.put(".rodata", b"".join(blobs))
        self.syms.append((f"shell_{name}", arr, len(blobs) * ENTRY, 1, ".rodata"))
        return self.put("shell_subcmds_area", struct.pack("<I", arr))

    def dynamic_set(self, getter: str, at: int) -> int:
        self.syms.append((getter, at | 1, 8, 2, ".text"))      # a Thumb function
        return self.put("shell_dynamic_subcmds_area", struct.pack("<I", at | 1))

    def root(self, name, help_, sub=0, mand=1, opt=0) -> None:
        st = self.put(".rodata", self.entry_bytes(name, help_, sub, 0, mand, opt))
        self.syms.append((f"_shell_{name}", st, ENTRY, 1, ".rodata"))
        ra = self.put("shell_root_cmds_area", struct.pack("<I", st))
        self.syms.append((f"shell_cmd_{name}", ra, P, 1, "shell_root_cmds_area"))

    def device(self, label: str) -> None:
        name = self.string(label)
        da = self.put("device_area", struct.pack("<IIIII", name, 0, 0, 0, 0))
        self.syms.append((f"__device_{label}", da, 20, 1, "device_area"))

    def write(self, path: Path) -> None:
        n_sec = len(SECTIONS)
        names = [n for n, _, _ in SECTIONS] + [".symtab", ".strtab", ".shstrtab"]
        shstr = b"\0" + b"".join(n.encode() + b"\0" for n in names)
        name_off = {n: shstr.index(b"\0" + n.encode() + b"\0") + 1 for n in names}
        index = {n: i + 1 for i, n in enumerate(names)}
        strtab, sym = b"\0", b"\0" * 16
        for n, v, sz, typ, sec in self.syms:
            sym += struct.pack("<IIIBBH", len(strtab), v, sz, typ, 0, index[sec])
            strtab += n.encode() + b"\0"
        secs = [(n, 1, fl, a, bytes(self.buf[n]) if n != ".text" else b"\0" * 16) for n, fl, a in SECTIONS]
        secs += [(".symtab", 2, 0, 0, sym), (".strtab", 3, 0, 0, strtab), (".shstrtab", 3, 0, 0, shstr)]
        body, offs, o = [], [], 52
        for *_, data in secs:
            offs.append(o)
            body.append(data)
            o += len(data)
        pad = b"\0" * ((-o) % 4)
        o += len(pad)
        sh = b"\0" * 40
        for (n, typ, flags, addr, data), off in zip(secs, offs):
            sh += struct.pack("<10I", name_off[n], typ, flags, addr, off, len(data),
                              index[".strtab"] if typ == 2 else 0, 0, 4, 16 if typ == 2 else 0)
        hdr = b"\x7fELF" + bytes([1, 1, 1]) + b"\0" * 9
        hdr += struct.pack("<HHIIIIIHHHHHH", 2, 40, 1, 0, 0, o, 0, 52, 0, 0, 40, len(secs) + 1, index[".shstrtab"])
        path.write_bytes(hdr + b"".join(body) + pad + sh)


def build_elf(path: Path, with_speed: bool = False, shell: bool = True) -> None:
    im = Img()
    im.device("sensor0")
    im.device("i2c1")
    if shell:
        speed = [dict(syntax="speed", help_="Set the bus speed", mand=2, opt=0)] if with_speed else []
        i2c = im.static_set("sub_i2c", [
            dict(syntax="scan", help_="Scan the bus", mand=2, opt=0),
            dict(syntax="read", help_="Read registers", mand=4, opt=1),
            *speed,
            dict(syntax="write", help_="Write a register", mand=4, opt=0xFE),
            dict(syntax="poke", help_="Write, unchecked", mand=2, opt=0xFF)])
        im.root("i2c", "I2C commands", i2c)
        devs = im.dynamic_set("shell_device_name_get", TEXT)
        im.root("device", "Device commands", im.static_set("sub_device", [
            dict(syntax="list", help_="List devices", mand=1, opt=0),
            dict(syntax="names", help_="Device name", sub=devs, mand=1, opt=1)]))
        kern = im.static_set("sub_thread", [dict(syntax="stacks", help_="Thread stacks", mand=1, opt=0)])
        im.root("kernel", "Kernel commands", im.static_set("sub_kernel", [
            dict(syntax="thread", help_="Thread tools", sub=kern, mand=1, opt=0)]))
        im.root("echo", "Echo")
    im.write(path)


def inventory(tmp_path, **kw):
    elf = tmp_path / "zephyr.elf"
    build_elf(elf, **kw)
    return elf, shellcmds.inventory(elf, None, {"SHELL": "y"})


def test_root_commands_with_digits_and_each_subcommands_counts(tmp_path):
    elf, rec = inventory(tmp_path)
    assert rec["shell"] and sorted(rec["commands"]) == ["device", "echo", "i2c", "kernel"]
    subs = {s["name"]: s for s in rec["commands"]["i2c"]["subcommands"]}
    assert set(subs) == {"scan", "read", "write", "poke"}
    assert (subs["read"]["mandatory"], subs["read"]["optional"], subs["read"]["help"]) == (4, 1, "Read registers")
    assert subs["write"]["optional"] == 0xFE and subs["poke"]["optional"] == 0xFF
    assert rec["commands"]["i2c"]["help"] == "I2C commands"


def test_a_subcommand_compiled_out_is_absent_and_one_compiled_in_is_present(tmp_path):
    _, off = inventory(tmp_path)
    assert "speed" not in {s["name"] for s in off["commands"]["i2c"]["subcommands"]}
    on = tmp_path / "on" / "zephyr.elf"
    on.parent.mkdir()
    build_elf(on, with_speed=True)
    rec = shellcmds.inventory(on, None, {"SHELL": "y"})
    assert "speed" in {s["name"] for s in rec["commands"]["i2c"]["subcommands"]}
    out = shellcmds.answer(off, "i2c")[1]
    assert "speed" not in out and "`i2c read` (4 mandatory, 1 optional) - Read registers" in out
    assert "254" not in out and "255" not in out and "raw rest of line" in out and "unchecked" in out


def test_nested_and_dynamic_sets_and_the_device_list(tmp_path):
    _, rec = inventory(tmp_path)
    kern = shellcmds.answer(rec, "kernel")[1]
    assert "`kernel thread stacks`" in kern
    dev = rec["commands"]["device"]["subcommands"][1]
    assert dev["dynamic"]["getter"] == "shell_device_name_get"
    assert dev["dynamic"]["devices"] == ["i2c1", "sensor0"]
    out = shellcmds.answer(rec, "device")[1]
    assert "offers the image's devices (2): i2c1, sensor0" in out and "not evaluated" in out
    assert rec["devices"] == ["i2c1", "sensor0"]


def test_source_lines_come_from_the_symbol_addresses(tmp_path):
    elf = tmp_path / "zephyr.elf"
    build_elf(elf)
    probe = shellcmds.inventory(elf, None, None)
    arr = next(s for s in Elf(elf).symbols() if s["name"] == "shell_sub_i2c")
    rec = shellcmds.inventory(elf, {arr["value"]: ("fw:src/i2c_shell.c", 88)}, None)
    assert rec["commands"]["i2c"]["at"] == "fw:src/i2c_shell.c:88"
    assert probe["commands"]["i2c"]["at"] is None
    assert "[fw:src/i2c_shell.c:88]" in shellcmds.answer(rec, "i2c")[1]


def test_a_build_with_no_shell_says_so_and_is_not_an_error(tmp_path):
    elf = tmp_path / "zephyr.elf"
    build_elf(elf, shell=False)
    rec = shellcmds.inventory(elf, None, {"SHELL": "n"})
    assert rec["shell"] is False and rec["commands"] == {}
    extra, body = shellcmds.answer(rec, "i2c")
    assert "no shell is compiled into this image" in body and "CONFIG_SHELL=n" in body


def test_an_unknown_root_names_the_ones_present(tmp_path):
    _, rec = inventory(tmp_path)
    body = shellcmds.answer(rec, "spi")[1]
    assert "No root shell command `spi`" in body and "i2c" in body
    roots = shellcmds.answer(rec, None)[1]
    assert roots.startswith("4 root shell commands") and "`i2c` (4 subcommands)" in roots


def test_stale_when_the_elf_changes(tmp_path):
    elf, rec = inventory(tmp_path)
    assert shellcmds.stale(rec, elf, "zephyr.elf") is None
    st = elf.stat()
    os.utime(elf, (st.st_atime, st.st_mtime + 100))
    assert shellcmds.stale(rec, elf, "zephyr.elf").startswith("STALE: zephyr.elf changed")
    assert rec["elf"]["mtime"] == st.st_mtime and rec["elf"]["size"] == st.st_size


@pytest.fixture()
def atlas_dir(tmp_path: Path) -> Path:
    a = tmp_path / "fw" / "embarch" / "atlas"
    at = a / "atlases" / "core@000000000009"
    write_json(at / "atlas.json", {"id": "core@000000000009", "commit": "0" * 40, "build": {}})
    write_text(at / "build" / ".config", "CONFIG_SHELL=y\n")
    build_elf(at / "build" / "zephyr.elf")
    write_json(a / "current.json", {"matches": [{"atlas": "core@000000000009"}]})
    return a


def test_query_reads_shell_json_and_flags_itself_stale(atlas_dir):
    at = atlas_dir / "atlases" / "core@000000000009"
    elf = at / "build" / "zephyr.elf"
    write_json(at / "shell.json", shellcmds.inventory(elf, None, {"SHELL": "y"}))
    a = query.Atlas(atlas_dir)
    out = query.shell_commands(a, "i2c")
    assert out.startswith("src: atlas core@000000000009 zephyr.elf elf=zephyr.elf mtime=")
    assert "STALE" not in out and "`i2c scan` (2 mandatory, 0 optional) - Scan the bus" in out
    st = elf.stat()
    os.utime(elf, (st.st_atime, st.st_mtime + 60))
    stale = query.shell_commands(a, "i2c")
    assert stale.splitlines()[0].endswith(" STALE") and "STALE: the atlas's zephyr.elf changed" in stale


def test_query_without_shell_json_reads_the_elf_and_says_so(atlas_dir):
    a = query.Atlas(atlas_dir)
    out = query.shell_commands(a, "device")
    assert "has no shell.json" in out and "`device list`" in out


def test_shell_commands_is_a_read_only_mcp_tool_and_a_q_verb(atlas_dir):
    import asyncio

    from embarch_atlas.mcp_server import build_server

    tools = {t.name: t for t in asyncio.run(build_server(atlas_dir, live=False).list_tools())}
    assert tools["shell_commands"].annotations.read_only_hint
    assert query.TOOLS["shell_commands"] is query.shell_commands

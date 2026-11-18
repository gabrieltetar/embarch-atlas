"""shell.json and the shell_commands lookup over a synthetic ELF (invented commands, no client build)."""
import os
import struct
from pathlib import Path

import pytest

from embarch_atlas import query, shellcmds
from embarch_atlas.dt import Elf
from embarch_atlas.common import read_json, write_json, write_text

RODATA, ROOTS, DEVS, TEXT = 0x1000, 0x2000, 0x3000, 0x4000
P = 4
ENTRY = 20      # four pointers and two bytes, padded to a pointer


class Img:
    """Lays out shell tables the way Zephyr's shell.h does, in an ELF32 little-endian file."""

    def __init__(self):
        self.ro = bytearray()
        self.roots = bytearray()
        self.devs = bytearray()
        self.syms: list[tuple[str, int, int, int, int]] = []     # name, value, size, type, section index
        self.strs: dict[str, int] = {}

    def _at(self, buf: bytearray, base: int) -> int:
        while len(buf) % 4:
            buf.append(0)
        return base + len(buf)

    def string(self, s: str) -> int:
        if s not in self.strs:
            self.strs[s] = self._at(self.ro, RODATA)
            self.ro += s.encode() + b"\0"
        return self.strs[s]

    def entry_bytes(self, syntax, help_="", sub=0, handler=0, mand=0, opt=0) -> bytes:
        return struct.pack("<IIIIBBxx", self.string(syntax) if syntax else 0, self.string(help_) if help_ else 0,
                           sub, handler, mand, opt)

    def static_set(self, name, entries: list[dict]) -> int:
        """`SHELL_STATIC_SUBCMD_SET_CREATE`: the array, ended by a zero entry, and the cmd_entry that points at it."""
        blobs = [self.entry_bytes(**e) for e in entries] + [b"\0" * ENTRY]
        arr = self._at(self.ro, RODATA)
        self.ro += b"".join(blobs)
        self.syms.append((f"shell_{name}", arr, len(blobs) * ENTRY, 1, 2))
        ce = self._at(self.ro, RODATA)
        self.ro += struct.pack("<BxxxI", 0, arr)
        return ce

    def dynamic_set(self, getter: str, at: int) -> int:
        self.syms.append((getter, at | 1, 8, 2, 4))      # a Thumb function
        ce = self._at(self.ro, RODATA)
        self.ro += struct.pack("<BxxxI", 1, at | 1)
        return ce

    def root(self, name, help_, sub=0, mand=1, opt=0) -> None:
        blob = self.entry_bytes(name, help_, sub, 0, mand, opt)      # its strings first, so the entry's address is final
        st = self._at(self.ro, RODATA)
        self.ro += blob
        self.syms.append((f"_shell_{name}", st, ENTRY, 1, 2))
        ra = self._at(self.roots, ROOTS)
        self.roots += struct.pack("<BxxxI", 0, st)
        self.syms.append((f"shell_cmd_{name}", ra, 8, 1, 3))

    def device(self, label: str) -> None:
        name = self.string(label)
        da = self._at(self.devs, DEVS)
        self.devs += struct.pack("<IIIII", name, 0, 0, 0, 0)
        self.syms.append((f"__device_{label}", da, 20, 1, 5))

    def write(self, path: Path) -> None:
        names = ["", "shell_x", ".rodata", "shell_root_cmds_area", ".text", "device_area", ".symtab", ".strtab", ".shstrtab"]
        shstr = b"\0" + b"".join(n.encode() + b"\0" for n in names[2:])
        idx = {n: shstr.index(n.encode() + b"\0") for n in names[2:]}
        strtab = b"\0"
        sym = b"\0" * 16
        for n, v, sz, typ, sh in self.syms:
            sym += struct.pack("<IIIBBH", len(strtab), v, sz, typ, 0, sh)
            strtab += n.encode() + b"\0"
        text = b"\0" * 16
        secs = [(".rodata", 1, 2, RODATA, bytes(self.ro)), ("shell_root_cmds_area", 1, 2, ROOTS, bytes(self.roots)),
                (".text", 1, 6, TEXT, text), ("device_area", 1, 2, DEVS, bytes(self.devs)),
                (".symtab", 2, 0, 0, sym), (".strtab", 3, 0, 0, strtab), (".shstrtab", 3, 0, 0, shstr)]
        body, offs, o = [], [], 52
        for *_, data in secs:
            offs.append(o)
            body.append(data)
            o += len(data)
        o += (-o) % 4
        pad = b"\0" * (o - 52 - sum(len(b) for b in body))
        sh = b"\0" * 40
        for (n, typ, flags, addr, data), off in zip(secs, offs):
            link = 6 if typ == 2 else 0
            sh += struct.pack("<10I", idx[n], typ, flags, addr, off, len(data), link, 0, 4, 16 if typ == 2 else 0)
        # sections: 1 .rodata 2 roots 3 .text 4 device_area 5 symtab 6 strtab 7 shstrtab; symbols above use 2=rodata,
        # 3=roots, 4=text, 5=device_area, so remap the section indexes here
        hdr = b"\x7fELF" + bytes([1, 1, 1]) + b"\0" * 9
        hdr += struct.pack("<HHIIIIIHHHHHH", 2, 40, 1, 0, 0, o, 0, 52, 0, 0, 40, len(secs) + 1, 7)
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
            dict(syntax="write", help_="Write a register", mand=4, opt=255)])
        im.root("i2c", "I2C commands", i2c)
        devs = im.dynamic_set("shell_device_name_get", TEXT)
        im.root("device", "Device commands", im.static_set("sub_device", [
            dict(syntax="list", help_="List devices", mand=1, opt=0),
            dict(syntax="names", help_="Device name", sub=devs, mand=1, opt=1)]))
        kern = im.static_set("sub_thread", [dict(syntax="stacks", help_="Thread stacks", mand=1, opt=0)])
        im.root("kernel", "Kernel commands", im.static_set("sub_kernel", [
            dict(syntax="thread", help_="Thread tools", sub=kern, mand=1, opt=0)]))
        im.root("echo", "Echo")
    # section indexes in the symbol table: .rodata 1, roots 2, .text 3, device_area 4
    im.syms = [(n, v, s, t, {2: 1, 3: 2, 4: 3, 5: 4}[sh]) for n, v, s, t, sh in im.syms]
    im.write(path)


def inventory(tmp_path, **kw):
    elf = tmp_path / "zephyr.elf"
    build_elf(elf, **kw)
    return elf, shellcmds.inventory(elf, None, {"SHELL": "y"})


def test_root_commands_with_digits_and_each_subcommands_counts(tmp_path):
    elf, rec = inventory(tmp_path)
    assert rec["shell"] and sorted(rec["commands"]) == ["device", "echo", "i2c", "kernel"]
    subs = {s["name"]: s for s in rec["commands"]["i2c"]["subcommands"]}
    assert set(subs) == {"scan", "read", "write"}
    assert (subs["read"]["mandatory"], subs["read"]["optional"], subs["read"]["help"]) == (4, 1, "Read registers")
    assert subs["write"]["optional"] == 255
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
    assert "255" not in out and "raw rest of line" in out


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
    assert roots.startswith("4 root shell commands") and "`i2c` (3 subcommands)" in roots


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

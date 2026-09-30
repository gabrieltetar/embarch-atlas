"""dt: pin uses from an edt-like object, the stdlib ELF reader, SVD naming, in_use.json.

The EDT is a fake with the attributes edtlib exposes. A real edt.pickle is read
only when EMBARCH_ATLAS_REAL_ATLAS names a built atlas dir (and its atlas dir's
parent is given by EMBARCH_ATLAS_REAL_ATLAS_DIR).
"""
from __future__ import annotations

import json
import os
import struct
from pathlib import Path

import pytest

from embarch_atlas import dt
from embarch_atlas.common import read_json, write_json


# ---------------------------------------------------------------- a fake edtlib

class P:
    def __init__(self, type_, val):
        self.type, self.val = type_, val


class RawProp:
    def __init__(self, value=b"", filename="/fw/board.dts", lineno=1):
        self.value, self.filename, self.lineno = value, filename, lineno


class RawNode:
    def __init__(self, props):
        self.props = props


class Reg:
    def __init__(self, addr, size):
        self.addr, self.size = addr, size


class CAD:
    def __init__(self, controller, data):
        self.controller, self.data = controller, data


class PinCtrl:
    def __init__(self, name, conf_nodes):
        self.name, self.conf_nodes = name, conf_nodes


class N:
    def __init__(self, path, labels=(), compats=(), status="okay", props=None, raw=None, regs=(), parent=None,
                 buses=(), on_buses=(), ordinal=0, pinctrls=(), interrupts=(), binding=True, children=None):
        self.path, self.labels, self.compats, self.status = path, list(labels), list(compats), status
        self.name = path.rsplit("/", 1)[-1]
        self.props = props or {}
        self._node = RawNode(raw or {k: RawProp(lineno=10 + i) for i, k in enumerate(self.props)})
        self.regs = [Reg(a, s) for a, s in regs]
        self.parent, self.buses, self.on_buses = parent, list(buses), list(on_buses)
        self.dep_ordinal, self.pinctrls, self.interrupts = ordinal, list(pinctrls), list(interrupts)
        self.matching_compat = (self.compats[0] if self.compats else None) if binding else None
        self.children = children or {}


class DTraw:
    def __init__(self):
        self.alias2node, self.phandle2node = {}, {}


class EDT:
    def __init__(self, nodes):
        self.nodes, self.chosen_nodes, self._dt = nodes, {}, DTraw()


def pinmux(port: str, line: int, mode: int) -> int:
    return ((ord(port) - ord("A")) << 9) | (line << 5) | mode


def fake_edt() -> EDT:
    soc = N("/soc", compats=["simple-bus"])
    pc = N("/soc/pin-controller@50000000", compats=["st,stm32-pinctrl"], parent=soc, regs=[(0x50000000, 0x2000)])
    ga = N("/soc/pin-controller@50000000/gpio@50000000", ["gpioa"], ["st,stm32-gpio"], parent=pc,
           regs=[(0x50000000, 0x400)], ordinal=5, props={"gpio-controller": P("boolean", True)})
    gb = N("/soc/pin-controller@50000000/gpio@50000400", ["gpiob"], ["st,stm32-gpio"], parent=pc,
           regs=[(0x50000400, 0x400)], ordinal=6, props={"gpio-controller": P("boolean", True)})
    scl = N("/soc/pin-controller@50000000/i2c1_scl_pb6", parent=pc,
            props={"pinmux": P("int", pinmux("B", 6, 6)), "bias-pull-up": P("boolean", True), "drive-open-drain": P("boolean", True),
                   "bias-disable": P("boolean", False)})
    ain = N("/soc/pin-controller@50000000/adc1_in0_pa0", parent=pc, props={"pinmux": P("int", pinmux("A", 0, 0x10))})
    i2c = N("/soc/i2c@40005400", ["i2c1"], ["st,stm32-i2c-v2"], parent=soc, regs=[(0x40005400, 0x400)], buses=["i2c"],
            ordinal=20, pinctrls=[PinCtrl("default", [scl])],
            props={"pinctrl-0": P("phandles", None), "clock-frequency": P("int", 400000)})
    mon = N("/soc/i2c@40005400/mon@44", ["mon0"], ["acme,mon"], parent=i2c, regs=[(0x44, None)], on_buses=["i2c"], ordinal=21,
            props={"alert-gpios": P("phandle-array", [CAD(ga, {"pin": 3, "flags": 1})]),
                   "shunt-milliohm": P("int", 5), "reg": P("array", [0x44])})
    adc = N("/soc/adc@40012400", ["adc1"], ["st,stm32-adc"], parent=soc, regs=[(0x40012400, 0x400)], ordinal=30,
            pinctrls=[PinCtrl("default", [ain])])
    off = N("/soc/spi@40013000", ["spi1"], ["st,stm32-spi"], status="disabled", parent=soc, regs=[(0x40013000, 0x400)], ordinal=31)
    # binding-less node: the gpios property only exists as raw cells (phandle, pin, flags)
    raw = N("/misc/sel", parent=None, binding=False, ordinal=40,
            raw={"select-gpios": RawProp(struct.pack(">III", 7, 9, 0x11), "/fw/board.dts", 77)})
    raw.props = {}
    gb.props["#gpio-cells"] = P("int", 2)
    nodes = [soc, pc, ga, gb, scl, ain, i2c, mon, adc, off, raw]
    e = EDT(nodes)
    e._dt.phandle2node = {7: type("X", (), {"path": gb.path, "props": {"#gpio-cells": RawProp(struct.pack(">I", 2))}})()}
    e._dt.alias2node = {"sensor0": mon}
    e.chosen_nodes = {"zephyr,console": i2c}
    return e


# ---------------------------------------------------------------- tests

def test_pinmux_and_flags():
    assert dt.stm32_pinmux(pinmux("B", 6, 6)) == {"pin": "PB6", "port": "B", "line": 6, "af": "AF6", "mode": 6}
    assert dt.stm32_pinmux(pinmux("A", 0, 0x10))["af"] == "ANALOG"
    assert dt.stm32_pinmux(pinmux("C", 13, 0x11))["af"] == "GPIO"
    assert dt.flags_str(0) == "ACTIVE_HIGH"
    assert dt.flags_str(0x1 | 0x10) == "ACTIVE_LOW|PULL_UP"
    assert dt.flags_str(0x6) == "OPEN_DRAIN"


def test_extract_edt():
    raw = dt.extract_edt(fake_edt(), dt.Rel([("/fw", "fw")]))
    assert raw["stm32"] is True
    assert set(raw["gpio_controllers"].values()) == {"A", "B"}
    by = {(u["pin"], u["kind"], u["prop"]): u for u in raw["uses"]}
    scl = by[("PB6", "pinctrl", "pinctrl-0")]
    assert scl["af"] == "AF6" and scl["func"] == "i2c1_scl_pb6" and set(scl["bias"]) == {"bias-pull-up", "drive-open-drain"}
    alert = by[("PA3", "gpio", "alert-gpios")]
    assert alert["flags"] == "ACTIVE_LOW" and alert["exti_line"] == 3 and alert["loc"].startswith("fw:board.dts:")
    assert by[("PA0", "pinctrl", "pinctrl-0")]["af"] == "ANALOG"
    sel = by[("PB9", "gpio", "select-gpios")]
    assert sel["no_binding"] and sel["flags"] == "ACTIVE_LOW|PULL_UP" and sel["loc"] == "fw:board.dts:77"
    assert [d["reg"] for d in raw["devices"]] == [0x44]
    assert raw["devices"][0]["bus_labels"] == ["i2c1"]
    assert raw["nodes"]["/soc/spi@40013000"]["okay"] is False
    assert raw["nodes"]["/soc/i2c@40005400/mon@44"]["props"]["shunt-milliohm"]["val"] == 5
    assert raw["aliases"] == {"sensor0": "/soc/i2c@40005400/mon@44"}
    assert raw["chosen"] == {"zephyr,console": "/soc/i2c@40005400"}
    json.dumps(raw)                                             # dt.json must be plain JSON


def test_scan_sources(tmp_path):
    drv = tmp_path / "mon.c"
    drv.write_text(
        "#define DT_DRV_COMPAT acme_mon\n"
        "static const struct mon_cfg cfg0 = { .irq = GPIO_DT_SPEC_INST_GET(0, alert_gpios), };\n"
        "static int init(const struct device *dev) {\n"
        "  const struct gpio_dt_spec *s = &cfg->irq;\n"
        "  gpio_pin_interrupt_configure_dt(s, GPIO_INT_EDGE_TO_ACTIVE);\n"
        "  DT_NODELABEL(mon0); DT_ALIAS(sensor0); DT_NODELABEL(unknown);\n}\n")
    out = dt.scan_sources([str(drv), str(tmp_path / "missing.c")], {"mon0"})
    assert out["drivers"] == {"acme_mon": [str(drv)]}
    assert out["armed"] == {"acme_mon": ["alert-gpios"]}
    assert set(out["consumers"]) == {"mon0", "alias:sensor0"}


def test_svd_and_family(tmp_path):
    svd = tmp_path / "x.svd"
    svd.write_text("""<device><peripherals>
      <peripheral><name>I2C1</name><groupName>I2C</groupName><baseAddress>0x40005400</baseAddress>
        <addressBlock><offset>0</offset><size>0x400</size></addressBlock></peripheral>
      <peripheral derivedFrom="I2C1"><name>I2C2</name><baseAddress>0x40005800</baseAddress></peripheral>
      <peripheral><name>SYSCFG</name><baseAddress>0x40010000</baseAddress>
        <addressBlock><offset>0</offset><size>0x30</size></addressBlock></peripheral>
    </peripherals></device>""")
    ps = {p["name"]: p for p in dt.svd_peripherals(svd)}
    assert ps["I2C2"]["size"] == 0x400 and ps["I2C2"]["group"] == "I2C"
    assert dt.family("I2C2") == "I2C" and dt.family("GPIOB") == "GPIO" and dt.family("USART10") == "USART"


# ---------------------------------------------------------------- a tiny ARM ELF

def make_elf(path: Path, text_words: list[int], data_words: list[int], syms: list[tuple]) -> None:
    """ELF32 LE: .text at 0x08000000 (a $t region then a $d literal pool), .rodata at 0x08001000, .symtab, .strtab."""
    text = struct.pack("<2I", 0xBF00BF00, 0xBF00BF00) + b"".join(struct.pack("<I", w) for w in text_words)
    rodata = b"".join(struct.pack("<I", w) for w in data_words)
    strtab = b"\0"
    names = {}
    for s in syms:
        names[s[0]] = len(strtab)
        strtab += s[0].encode() + b"\0"
    shstr = b"\0.text\0.rodata\0.symtab\0.strtab\0.shstrtab\0"
    off = {n: shstr.index(n.encode()) for n in (".text", ".rodata", ".symtab", ".strtab", ".shstrtab")}
    symtab = b"\0" * 16
    for name, value, size, typ, shndx in syms:
        symtab += struct.pack("<IIIBBH", names[name], value, size, typ, 0, shndx)
    body = [text, rodata, symtab, strtab, shstr]
    pos, offs = 52, []
    for b in body:
        offs.append(pos)
        pos += len(b)
    shoff = pos
    hdr = b"\x7fELF" + bytes([1, 1, 1]) + b"\0" * 9 + struct.pack("<HHIIIIIHHHHHH", 2, 40, 1, 0, 0, shoff, 0, 52, 0, 0, 40, 6, 5)
    sh = b"\0" * 40
    sh += struct.pack("<10I", off[".text"], 1, 0x6, 0x08000000, offs[0], len(text), 0, 0, 4, 0)
    sh += struct.pack("<10I", off[".rodata"], 1, 0x2, 0x08001000, offs[1], len(rodata), 0, 0, 4, 0)
    sh += struct.pack("<10I", off[".symtab"], 2, 0, 0, offs[2], len(symtab), 4, 0, 4, 16)
    sh += struct.pack("<10I", off[".strtab"], 3, 0, 0, offs[3], len(strtab), 0, 0, 1, 0)
    sh += struct.pack("<10I", off[".shstrtab"], 3, 0, 0, offs[4], len(shstr), 0, 0, 1, 0)
    path.write_bytes(hdr + b"".join(body) + sh)


def demo_elf(path: Path) -> None:
    # generic driver init touches three USART-like instances (an instance check); soc init touches SYSCFG only
    make_elf(path, text_words=[0x40010000, 0x40013800, 0x40004400, 0x40004800],
             data_words=[0x40005400, 0x00000001, 0x40010000, 0x40000000],
             syms=[("$t", 0x08000000, 0, 0, 1), ("$d", 0x08000008, 0, 0, 1),
                   ("soc_init", 0x08000001, 0x0C, 2, 1), ("uart_init", 0x0800000C, 0x0C, 2, 1),
                   ("i2c_cfg_0", 0x08001000, 8, 1, 2), ("POW_TABLE", 0x08001008, 8, 1, 2),
                   ("__device_dts_ord_20", 0x08001000, 4, 1, 2), ("__device_dts_ord_5", 0x08001004, 4, 1, 2)])


BLOCKS = [("I2C1", 0x40005400, 0x400), ("SYSCFG", 0x40010000, 0x30), ("USART1", 0x40013800, 0x400),
          ("USART2", 0x40004400, 0x400), ("USART3", 0x40004800, 0x400), ("TIM2", 0x40000000, 0x400)]


def test_elf_reader_and_mmio(tmp_path):
    f = tmp_path / "z.elf"
    demo_elf(f)
    e = dt.Elf(f)
    assert e.device_ordinals() == {20, 5}
    refs = dt.mmio_refs(e, BLOCKS)
    assert refs["SYSCFG"]["code"] == {"soc_init": [0]}
    assert set(refs["USART1"]["code"]) == {"uart_init"}
    assert refs["I2C1"]["data"] == {"i2c_cfg_0": [0]}
    assert set(refs["TIM2"]["data"]) == {"POW_TABLE"} and set(refs["SYSCFG"]["data"]) == {"POW_TABLE"}


def test_build_in_use(tmp_path):
    f = tmp_path / "z.elf"
    demo_elf(f)
    raw = dt.extract_edt(fake_edt())
    refs = dt.mmio_refs(dt.Elf(f), BLOCKS)
    svd = [{"name": n, "base": b, "size": s} for n, b, s in BLOCKS] + [{"name": "ADC", "base": 0x40012400, "size": 0x400}]
    iu = dt.build_in_use(raw, {20, 5}, refs, svd)["peripherals"]
    assert iu["I2C1"] == {"dt_okay": True, "instantiated": True, "touched_by_code": True, "compatible": "st,stm32-i2c-v2",
                          "node": "/soc/i2c@40005400"}
    assert iu["ADC"]["dt_okay"] and not iu["ADC"]["instantiated"] and not iu["ADC"]["touched_by_code"]
    assert iu["SYSCFG"]["touched_by_code"] and iu["SYSCFG"]["node"] == ""           # soc code, one block only
    assert not iu["USART1"]["touched_by_code"]                                        # a driver's instance check
    assert not iu["TIM2"]["touched_by_code"]                                          # a numeric table, not a config object
    # without an SVD, names come from labels
    iu2 = dt.build_in_use(raw, {20}, {}, None)["peripherals"]
    assert "I2C1" in iu2 and "ADC1" in iu2 and "SPI1" in iu2 and not iu2["SPI1"]["dt_okay"]


def test_run_writes_dt_and_in_use(tmp_path, monkeypatch):
    ad = tmp_path / "atlas"
    atlas = ad / "atlases" / "demo@000000000001"
    bd = atlas / "build"
    bd.mkdir(parents=True)
    src = tmp_path / "wt" / "drivers" / "mon.c"
    src.parent.mkdir(parents=True)
    src.write_text("#define DT_DRV_COMPAT acme_mon\n")
    notbuilt = tmp_path / "wt" / "drivers" / "other.c"
    notbuilt.write_text("#define DT_DRV_COMPAT acme_other\n")
    write_json(bd / "compile_commands.json", [{"file": str(src), "command": "cc"}])
    demo_elf(bd / "zephyr.elf")
    (bd / "edt.pickle").write_bytes(b"not read: raw_from_pickle is replaced")
    write_json(atlas / "atlas.json", {"id": "demo@000000000001", "mcu": {},
                                      "build": {"worktree": str(tmp_path / "wt"), "zephyr_base": str(tmp_path / "zb")}})
    monkeypatch.setattr(dt, "raw_from_pickle", lambda *a, **k: dt.extract_edt(fake_edt()))
    d, iu = dt.run(atlas, ad)
    assert d["drivers"] == {"acme_mon": ["fw:drivers/mon.c"]}
    assert d["drivers_not_built"] == {"acme_other": ["fw:drivers/other.c"]}
    assert d["instantiated_ordinals"] == [5, 20]
    assert d["exti_lines"] == {"EXTI3": ["PA3"], "EXTI9": ["PB9"]}
    assert read_json(atlas / "in_use.json") == iu
    assert (atlas / "dt.json").is_file()


REAL = os.environ.get("EMBARCH_ATLAS_REAL_ATLAS")


@pytest.mark.skipif(not REAL, reason="needs a built atlas (EMBARCH_ATLAS_REAL_ATLAS=<atlas dir>)")
def test_real_edt(tmp_path):
    atlas = Path(REAL)
    ad = atlas.parent.parent
    d, iu = dt.run(atlas, ad)
    assert d["uses"] and iu["peripherals"]

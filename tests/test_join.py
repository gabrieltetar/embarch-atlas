"""join: pin/net map, board-to-board tracing, I2C identity by strap, DT values, DT comments.

Everything here is synthetic: a two-board product ("M" main, "S" sensor) with a
6-pin connector mated mirrored, an MCU with STM32-style port names on M, two SNS701-style current monitors
(one per board), and a hand-written dt.json. `make_project` is shared with the
mismatch, mapgen, secondlook and diff tests.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from embarch_atlas import join
from embarch_atlas.common import read_json, write_json, write_jsonl


def _comp(mpn, value=None, pins=None, dni=False, pages=(1,)):
    return {"mpn": mpn, "value": value, "dni": dni, "pages": list(pages),
            "pins": {str(k): {"name": n, "net": net} for k, (n, net) in (pins or {}).items()}}


def _netlist(board: str, comps: dict) -> dict:
    nets: dict[str, dict] = {}
    for ref, c in comps.items():
        for num, p in c["pins"].items():
            if p["net"]:
                nets.setdefault(p["net"], {"pins": [], "sheets": ["S1"], "labels": 0, "ports": 0})["pins"].append(f"{ref}-{num}")
    for n in nets.values():
        n["pins"].sort()
    return {"board": board, "source": {"file": f"{board}.pdf", "sha256": "0" * 64}, "method": "synthetic",
            "sheets": {"S1": {"page": 1}}, "components": comps, "nets": nets}


def main_board() -> dict:
    c = {
        "U1": _comp("MCU0-QFP32", pins={
            1: ("PB6", "BUS0_SCL"), 2: ("PB7", "BUS0_SDA"), 3: ("PA5", "LED_STATUS"), 4: ("PA1", "nMON_ALERT"),
            5: ("VDD", "3V3"), 6: ("VSS", "GND"), 7: ("PA13", "SWDIO"), 8: ("PA2", "AUX_OUT"), 9: ("PC6", "PC6"),
            10: ("PA0", "BOARD_REV"), 11: ("PB1", "nKEY0"), 12: ("PF2-NRST", "nRST_LINE")}),
        "R1": _comp(None, "2.2kΩ", {1: (None, "BUS0_SCL"), 2: (None, "3V3")}),
        "R2": _comp(None, "2.2kΩ", {1: (None, "BUS0_SDA"), 2: (None, "3V3")}),
        "R3": _comp(None, "10kΩ", {1: (None, "nMON_ALERT"), 2: (None, "3V3")}),
        "R4": _comp(None, "680Ω", {1: (None, "NetD1_1"), 2: (None, "3V3")}),
        "D1": _comp("LED-RED-0603", pins={1: ("A", "NetD1_1"), 2: ("K", "LED_STATUS")}),
        "R5": _comp(None, "0.005 Ω", {1: (None, "SENSE_P"), 2: (None, "SENSE_N")}),
        "R6": _comp(None, "10kΩ", {1: (None, "BOARD_REV"), 2: (None, "3V3")}),
        "R7": _comp(None, "10kΩ", {1: (None, "BOARD_REV"), 2: (None, "GND")}, dni=True),
        "U2": _comp("LOGIC-INV1", pins={2: ("A", "AUX_OUT"), 4: ("Y", "INV_OUT"), 5: ("VCC", "3V3"), 3: ("GND", "GND")}),
        "U3": _comp("SNS701QR", pins={
            "B3": ("SDA", "BUS0_SDA"), "C3": ("SCL", "BUS0_SCL"), "B2": ("A0", "GND"), "C2": ("ALERT", "nMON_ALERT"),
            "C1": ("VS", "3V3"), "B1": ("GND", "GND"), "A3": ("IN+", "SENSE_P"), "A1": ("IN-", "SENSE_N")}),
        "J1": _comp("CONN-6", pins={1: (None, "BUS0_SCL"), 2: (None, "BUS0_SDA"), 3: (None, "nMON_ALERT"),
                                    4: (None, "GND"), 5: (None, "3V3"), 6: (None, "GND")}),
        "J2": _comp("BTN-HDR", pins={1: (None, "nKEY0"), 2: (None, "GND")}),
        "J3": _comp("SWD-HDR", pins={1: (None, "SWDIO"), 2: (None, "nRST_LINE"), 3: (None, "GND")}),
        "TP1": _comp(None, pins={1: (None, "PC6")}),
    }
    return _netlist("M", c)


def sensor_board() -> dict:
    c = {
        "J1": _comp("CONN-6", pins={6: (None, "BUS0_SCL"), 5: (None, "BUS0_SDA"), 4: (None, "MON_ALERT"),
                                    3: (None, "GND"), 2: (None, "3V3"), 1: (None, "GND")}),
        "U1": _comp("SNS701QR", pins={
            "B3": ("SDA", "BUS0_SDA"), "C3": ("SCL", "BUS0_SCL"), "B2": ("A0", "3V3"), "C2": ("ALERT", "MON_ALERT"),
            "C1": ("VS", "3V3"), "B1": ("GND", "GND"), "A3": ("IN+", "LOAD_P"), "A1": ("IN-", "LOAD_N")}),
        "R1": _comp(None, "0.005 Ω", {1: (None, "LOAD_P"), 2: (None, "LOAD_N")}),
        "C1": _comp(None, "100nF", {1: (None, "3V3"), 2: (None, "GND")}),
    }
    return _netlist("S", c)


GPIOA, GPIOB = "/soc/pin-controller@50000000/gpio@50000000", "/soc/pin-controller@50000000/gpio@50000400"
I2C = "/soc/i2c@40005400"


def _use(pin, node, kind, prop, labels=(), compat=None, compats=None, **kw):
    u = {"pin": pin, "node": node, "kind": kind, "prop": prop, "labels": list(labels), "compat": compat,
         "compats": compats if compats is not None else ([compat] if compat else []), "loc": kw.pop("loc", "fw:board.dts:1")}
    u.update(kw)
    return u


def synthetic_dt() -> dict:
    nodes = {
        GPIOA: {"labels": ["gpioa"], "compats": ["st,stm32-gpio"], "okay": True, "ordinal": 10, "regs": [[0x50000000, 0x400]], "parent": None, "props": {}},
        GPIOB: {"labels": ["gpiob"], "compats": ["st,stm32-gpio"], "okay": True, "ordinal": 11, "regs": [[0x50000400, 0x400]], "parent": None, "props": {}},
        I2C: {"labels": ["i2c1"], "compats": ["st,stm32-i2c-v2"], "okay": True, "ordinal": 20, "regs": [[0x40005400, 0x400]], "parent": "/soc", "props": {}},
        f"{I2C}/mon-a@44": {"labels": ["mon_a"], "compats": ["acme,sns7xx-alert"], "okay": True, "ordinal": 21, "regs": [[0x44, None]],
                            "parent": I2C, "props": {"sense-resistor-milliohm": {"val": 5, "loc": "fw:board.dts:30"}}},
        f"{I2C}/mon-b@45": {"labels": ["mon_b"], "compats": ["acme,sns7xx-alert"], "okay": True, "ordinal": 22, "regs": [[0x45, None]],
                            "parent": I2C, "props": {"sense-resistor-milliohm": {"val": 10, "loc": "fw:board.dts:40"}}},
        "/leds": {"labels": [], "compats": ["gpio-leds"], "okay": True, "ordinal": 30, "regs": [], "parent": "/", "props": {}},
        "/leds/led_0": {"labels": ["status_led"], "compats": [], "okay": True, "ordinal": 31, "regs": [], "parent": "/leds", "props": {}},
        "/buttons": {"labels": [], "compats": ["gpio-keys"], "okay": True, "ordinal": 32, "regs": [], "parent": "/", "props": {}},
        "/buttons/btn_0": {"labels": ["btn0"], "compats": [], "okay": True, "ordinal": 33, "regs": [], "parent": "/buttons", "props": {}},
        "/soc/adc@40012400": {"labels": ["adc1"], "compats": ["st,stm32-adc"], "okay": True, "ordinal": 40,
                              "regs": [[0x40012400, 0x400]], "parent": "/soc", "props": {}},
    }
    uses = [
        _use("PB6", I2C, "pinctrl", "pinctrl-0", ["i2c1"], "st,stm32-i2c-v2", func="i2c1_scl_pb6", af="AF6", flags="bias-pull-up"),
        _use("PB7", I2C, "pinctrl", "pinctrl-0", ["i2c1"], "st,stm32-i2c-v2", func="i2c1_sda_pb7", af="AF6", flags="bias-pull-up"),
        _use("PA0", "/soc/adc@40012400", "pinctrl", "pinctrl-0", ["adc1"], "st,stm32-adc", func="adc1_in0_pa0", af="ANALOG"),
        _use("PA5", "/leds/led_0", "gpio", "gpios", ["status_led"], "gpio-leds", ["gpio-leds"], flags="ACTIVE_LOW", index=0, exti_line=5,
             loc="fw:board.dts:12"),
        _use("PA1", f"{I2C}/mon-a@44", "gpio", "alert-gpios", ["mon_a"], "acme,sns7xx-alert", flags="ACTIVE_LOW", index=0, exti_line=1,
             loc="fw:board.dts:31"),
        _use("PA1", f"{I2C}/mon-b@45", "gpio", "alert-gpios", ["mon_b"], "acme,sns7xx-alert", flags="ACTIVE_LOW", index=0, exti_line=1,
             loc="fw:board.dts:41"),
        _use("PB1", "/buttons/btn_0", "gpio", "gpios", ["btn0"], "gpio-keys", ["gpio-keys"], flags="ACTIVE_LOW|PULL_UP", index=0, exti_line=1,
             loc="fw:board.dts:20"),
    ]
    devices = [
        {"node": f"{I2C}/mon-a@44", "labels": ["mon_a"], "compat": "acme,sns7xx-alert", "bus": I2C, "bus_labels": ["i2c1"],
         "bus_kind": ["i2c"], "reg": 0x44, "ordinal": 21, "loc": "fw:board.dts:29"},
        {"node": f"{I2C}/mon-b@45", "labels": ["mon_b"], "compat": "acme,sns7xx-alert", "bus": I2C, "bus_labels": ["i2c1"],
         "bus_kind": ["i2c"], "reg": 0x45, "ordinal": 22, "loc": "fw:board.dts:39"},
    ]
    return {"atlas": "demo@000000000001", "gpio_controllers": {GPIOA: "A", GPIOB: "B"}, "stm32": True, "nodes": nodes,
            "uses": uses, "devices": devices, "chosen": {}, "aliases": {"led0": "/leds/led_0"},
            "exti_lines": {"EXTI1": ["PA1", "PB1"], "EXTI5": ["PA5"]},
            "drivers": {"acme_sns7xx_alert": ["fw:drivers/sns.c"], "st_stm32_i2c_v2": ["zephyr:drivers/i2c/i2c_ll_stm32.c"],
                        "gpio_keys": ["zephyr:drivers/input/input_gpio_keys.c"]},
            "drivers_not_built": {}, "consumers": {"alias:led0": ["fw:lib/led.c"]},
            "armed": {"acme_sns7xx_alert": ["alert-gpios"], "gpio_keys": ["gpios"]},
            "instantiated_ordinals": [10, 11, 20, 21, 22, 32, 33], "mmio": {}, "n_tus": 3, "svd": False}


def synthetic_in_use() -> dict:
    return {"peripherals": {
        "GPIOA": {"dt_okay": True, "instantiated": True, "touched_by_code": True, "compatible": "st,stm32-gpio", "node": GPIOA},
        "I2C1": {"dt_okay": True, "instantiated": True, "touched_by_code": True, "compatible": "st,stm32-i2c-v2", "node": I2C},
        "ADC": {"dt_okay": True, "instantiated": False, "touched_by_code": False, "compatible": "st,stm32-adc", "node": "/soc/adc@40012400"},
        "SYSCFG": {"dt_okay": False, "instantiated": False, "touched_by_code": True, "compatible": "", "node": ""},
    }}


def ina_doc(ad: Path, conflict: bool = False) -> None:
    d = ad / "docs" / "sns701@a"
    write_json(d / "doc.json", {"doc_id": "sns701@a", "kind": "datasheet", "part": "SNS701", "file": "sns701.pdf",
                                "path": "../src/sns701.pdf", "sha256": "1" * 64, "pages": 30})
    write_jsonl(d / "records" / "strap.jsonl", [
        {"id": f"sns701.strap.A0.{c.lower()}", "kind": "strap", "doc": "sns701@a", "p": 21, "q": f"A0 {c} {v}", "src": "table",
         "prov": "llm-verified", "pin": "A0", "condition": c, "value": v}
        for c, v in (("GND", "0x44"), ("VS", "0x45"), ("SDA", "0x46"), ("SCL", "0x47"))])
    write_jsonl(d / "records" / "pin.jsonl", [
        {"id": "sns701.pin.ALERT", "kind": "pin", "doc": "sns701@a", "p": 3, "q": "C2 ALERT O open-drain", "src": "table",
         "prov": "llm-verified", "pin": "C2", "name": "ALERT", "dir": "out", "type": "od", "active": "low"}])
    write_jsonl(d / "records" / "limit.jsonl", [
        {"id": "sns701.ec.VS", "kind": "limit", "doc": "sns701@a", "p": 5, "q": "VS Supply voltage 2.7 5.5 V", "src": "table",
         "prov": "llm-verified", "group": "recommended", "sym": "VS", "param": "Supply voltage", "min": 2.7, "max": 5.5, "unit": "V"},
        {"id": "sns701.ec.IQ.active", "kind": "limit", "doc": "sns701@a", "p": 6, "q": "IQ Quiescent current 60 80", "src": "table",
         "prov": "llm-verified", "group": "ec", "sym": "IQ", "param": "Quiescent current", "cond": "active mode",
         "typ": 60, "max": 80, "unit": "µA"},
        {"id": "sns701.abs.VS", "kind": "limit", "doc": "sns701@a", "p": 4, "q": "VS -0.3 6 V", "src": "table",
         "prov": "deterministic", "group": "abs", "sym": "VS", "min": -0.3, "max": 6, "unit": "V"}])
    write_jsonl(d / "records" / "quirk.jsonl", [
        {"id": "sns701.quirk.alert_default", "kind": "quirk", "doc": "sns701@a", "p": 3, "q": None, "src": "figure",
         "prov": "llm-unverified", "class": "silent-default", "text": "ALERT is active low by default"}])
    if conflict:
        write_jsonl(d / "records" / "conflict.jsonl", [
            {"id": "sns701.conflict.alert_latch", "kind": "conflict", "doc": "sns701@a", "p": 22, "q": None, "src": "text",
             "prov": "llm-verified", "refs": ["sns701.quirk.alert_default"], "sources": ["driver: fw:drivers/sns.c:88", "sns701@a p22"],
             "text": "the driver assumes ALERT latches; the datasheet says it is transparent by default"}])
    for p, text in ((3, "C2 ALERT O open-drain, active low by default"), (5, "VS Supply voltage 2.7 5.5 V"),
                    (6, "IQ Quiescent current active mode 60 80 uA"), (21, "A0 GND 0x44 VS 0x45 SDA 0x46 SCL 0x47")):
        (d / "pages").mkdir(parents=True, exist_ok=True)
        (d / "pages" / f"p{p:03d}.txt").write_text(text + "\n", encoding="utf-8")


def git(repo: Path, *argv: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *argv], capture_output=True, text=True, check=True).stdout.strip()


def fw_repo(tmp: Path) -> tuple[Path, str]:
    fw = tmp / "fw"
    fw.mkdir(parents=True, exist_ok=True)
    git(fw, "init", "-q")
    git(fw, "config", "user.email", "t@example.com")
    git(fw, "config", "user.name", "t")
    (fw / "board.dts").write_text(
        "/ {\n"
        "  leds { led_0: led { gpios = <&gpioa 5 GPIO_ACTIVE_LOW>; /* LED_WRONG */ }; };\n"
        "  mon { alert-gpios = <&gpioa 1 GPIO_ACTIVE_LOW>; /* nMON_ALERT */ };\n"
        "  btn { gpios = <&gpiob 1 (GPIO_ACTIVE_LOW | GPIO_PULL_UP)>; // nKEY0\n"
        "};\n", encoding="utf-8")
    git(fw, "add", "board.dts")
    git(fw, "commit", "-q", "-m", "init")
    return fw, git(fw, "rev-parse", "HEAD")


def make_project(tmp: Path, conflict: bool = False, with_docs: bool = True) -> tuple[Path, Path]:
    """(atlas_dir, atlas) for the synthetic product. The fw repo is a real git repo, so HEAD matches."""
    fw, head = fw_repo(tmp)
    ad = fw / "embarch" / "atlas"
    write_json(ad / "boards" / "M" / "netlist.json", main_board())
    write_json(ad / "boards" / "S" / "netlist.json", sensor_board())
    if with_docs:
        ina_doc(ad, conflict)
    aid = f"demo@{head[:12]}"
    atlas = ad / "atlases" / aid
    stamp = {"id": aid, "target": "demo", "board": "demo_board", "app": "app", "schematic_boards": ["M", "S"],
             "commit": head, "dirty": False, "west": [], "sources": None, "schematics": {},
             "mcu": {"part": "MCU0", "svd": None, "ref": None},
             "tool": {"name": "embarch-atlas", "version": "0", "git": None}, "skill_versions": {}, "created": "2026-01-01T00:00:00Z",
             "build": {"fw": str(fw), "worktree": "..", "from_worktree": {"dts_inputs_in_worktree": ["board.dts"]}}}
    # the worktree is the fw repo itself here: rel path from the atlas dir
    stamp["build"]["worktree"] = str(Path("..") / "..")
    write_json(atlas / "atlas.json", stamp)
    dt = synthetic_dt()
    dt["atlas"] = aid
    write_json(atlas / "dt.json", dt)
    write_json(atlas / "in_use.json", synthetic_in_use())
    return ad, atlas


def run_join(ad: Path, atlas: Path) -> dict:
    return join.run(ad, atlas, log=lambda *a: None)


# ---------------------------------------------------------------- units

def test_ohms_parses_bom_spellings():
    assert join.ohms("10kΩ") == 10_000
    assert join.ohms("4K7") == 4700
    assert join.ohms("6.19 kΩ") == 6190
    assert abs(join.ohms("0.005 Ω") - 0.005) < 1e-12
    assert abs(join.ohms("5mΩ") - 0.005) < 1e-12
    assert join.ohms("1M") == 1e6
    assert join.ohms("DNI") is None


def test_part_pattern_prefix_wildcard_and_range():
    assert join._part_pattern("SNS701").match("sns701qr")
    assert join._part_pattern("MCU0x1").match("mcu071rb")
    assert not join._part_pattern("MCU0x1").match("mcu0b0")
    rng = join._part_pattern("ABC201-ABC215")
    assert rng.match("abc205gt") and not rng.match("abc216")
    assert not join._part_pattern("ABC").match("abd")


def test_strap_value_forms():
    assert join._strap_value("0x44") == 0x44
    assert join._strap_value("6Bh") == 0x6B
    assert join._strap_value("1000100b") == 0x44
    assert join._strap_value(68) == 68


def test_rails_and_grounds():
    J = join.Join(Path("."), Path("."), {"M": join.Board("M", main_board())}, synthetic_dt(), {}, join.Docs(Path("/nonexistent")),
                  [], {}, [], "MCU0", None)
    for g in ("GND", "X_AGND", "AGND", "GND_2_EXT", "VSS"):
        assert J.is_ground(g), g
    for r in ("3V3", "5V0", "VDD_IO", "1V8"):
        assert J.is_rail("M", r), r
    assert not J.is_rail("M", "BUS0_SDA")
    assert not J.is_ground("nMON_ALERT")
    assert J.mcu == ("M", "U1")


def test_prop_to_pin_heuristics():
    nl = _netlist("X", {"U9": _comp("PWR100", pins={14: ("FLT/INT", "N1"), 1: ("EN/UV", "N2"), 2: ("ALRT2", "N3"),
                                                         3: ("RDY_OK", "N4")})})
    J = join.Join(Path("."), Path("."), {"X": join.Board("X", nl)}, {"uses": [], "nodes": {}}, {}, join.Docs(Path("/nonexistent")),
                  [], {}, [], None, None)
    assert join.prop_to_pin(J, "acme,x", "intn-gpios", "X", "U9")[0] == "U9-14"
    assert join.prop_to_pin(J, "acme,x", "en-gpios", "X", "U9")[0] == "U9-1"
    assert join.prop_to_pin(J, "acme,x", "alert-gpios", "X", "U9")[0] == "U9-2"
    assert join.prop_to_pin(J, "acme,x", "rdy-ok-gpios", "X", "U9")[0] == "U9-3"
    J.bindings = {"prop_pins": {"acme,x": {"alert-gpios": "RDY_OK"}}}
    assert join.prop_to_pin(J, "acme,x", "alert-gpios", "X", "U9") == ("U9-3", "declared")


def test_infer_interfaces_mirror():
    boards = {"M": join.Board("M", main_board()), "S": join.Board("S", sensor_board())}
    itf = join.infer_interfaces(boards)
    assert len(itf) == 1
    i = itf[0]
    assert (i["a"], i["b"], i["rule"]) == ("M:J1", "S:J1", "mirror")
    assert (i["agree"], i["named"]) == (5, 6)
    assert i["status"].startswith("inferred")


def test_interfaces_from_system_declared_wins():
    boards = {"M": join.Board("M", main_board()), "S": join.Board("S", sensor_board())}
    sj = {"interfaces": [{"a": "M:J1", "b": "S:J1", "rule": "mirror", "prov": "engineer-declared"}]}
    itf = join.interfaces_from_system(sj, boards)
    assert itf[0]["status"] == "engineer-declared" and itf[0]["map"]["1"] == "6"


# ---------------------------------------------------------------- the whole join

def test_join_pin_map(tmp_path):
    ad, atlas = make_project(tmp_path)
    pins = run_join(ad, atlas)
    rows = {r["port"]: r for r in pins["pins"] if r["port"]}
    c = pins["counts"]
    assert c["package_pins"] == 12 and c["io_pins"] == 10
    assert c["dt_pins"] == 6
    # PF2-NRST keeps its port from the symbol name
    assert "PF2" in rows
    # SDA crosses the connector (mirrored) and reaches the sensor board's monitor
    sda = rows["PB7"]
    assert sda["cross"]
    assert any(f["kind"] == "part-pin" and f["board"] == "S" and f["pin"] == "U1-B3" for f in sda["far"])
    assert any(f["kind"] == "board-to-board" and f["to"] == "S:J1-5" for f in sda["far"])
    pulls = [f for f in rows["PB6"]["far"] if f["kind"] == "pull-up"]
    assert [(f["part"], f["rail"]) for f in pulls] == [("R1", "3V3")]
    # a DNI resistor stays on the far list, marked
    assert any(f.get("dni") and f["part"] == "R7" for f in rows["PA0"]["far"])
    # DT use with driver and IRQ arming
    alert = rows["PA1"]["dt"]
    assert {u["node"] for u in alert} == {f"{I2C}/mon-a@44", f"{I2C}/mon-b@45"}
    assert all(u["irq_armed"] and u["driver"]["compiled"] for u in alert)
    led = rows["PA5"]["dt"][0]
    assert led["owner"] == "/leds" and led["driver"]["consumer"]
    assert rows["PC6"]["reach"] is False
    md = (atlas / "pins.md").read_text()
    assert "| 2 | PB7 |" in md and "S:U1-B3 SDA" in md
    sl = (atlas / "pin_slice.txt").read_text().splitlines()
    assert any(l.startswith("PB7 BUS0_SDA i2c1.i2c1_sda_pb7") for l in sl)
    assert not any(l.startswith("PA2 ") for l in sl)          # connected but no DT use: not in the slice


def test_join_i2c_identity_and_values(tmp_path):
    ad, atlas = make_project(tmp_path)
    pins = run_join(ad, atlas)
    ids = {d["node"].rsplit("/", 1)[-1]: d for d in pins["i2c"]["identity"]}
    assert ids["mon-a@44"]["status"] == "identified" and ids["mon-a@44"]["part"] == "M:U3"
    assert ids["mon-b@45"]["part"] == "S:U1"                  # A0 tied to VS -> 0x45, on the other board
    assert "sns701@a p21" in ids["mon-a@44"]["addr_evidence"]
    for d in ids.values():
        assert [s["status"] for s in d["signals"]] == ["agree"]
    assert ids["mon-b@45"]["signals"][0]["path"]               # reached PA1 through the connector
    vals = {v["node"].rsplit("/", 1)[-1]: v for v in pins["values"]}
    assert vals["mon-a@44"]["status"] == "agree"
    assert vals["mon-b@45"]["status"] == "check"               # DT says 10 mOhm, the board has 5 mOhm
    com = {c["pin"]: c for c in pins["comments"]}
    assert com["PA1"]["status"] == "agree" and com["PA5"]["status"] == "disagree"
    assert com["PB1"]["status"] == "agree"                     # a // comment after a parenthesised flag expression
    assert pins["part_docs"]["M:U3"] == ["sns701@a"]


def test_join_uses_engineer_straps_without_docs(tmp_path):
    ad, atlas = make_project(tmp_path, with_docs=False)
    pins = run_join(ad, atlas)
    assert all(d["status"] != "identified" for d in pins["i2c"]["identity"])
    write_jsonl(ad / "boards" / "straps.jsonl", [
        {"part": "SNS701", "pin": "A0", "condition": "GND", "value": "0x44", "cite": "engineer table"},
        {"part": "SNS701", "pin": "A0", "condition": "VS", "value": "0x45", "cite": "engineer table"}])
    pins = run_join(ad, atlas)
    assert all(d["status"] == "identified" for d in pins["i2c"]["identity"])


def test_join_snapshots_inputs(tmp_path):
    ad, atlas = make_project(tmp_path)
    run_join(ad, atlas)
    inp = read_json(atlas / "inputs.json")
    assert set(inp["netlists"]) == {"M", "S"}
    assert (atlas / "inputs" / "netlist_M.json").is_file()
    assert "sns701@a" in inp["docs"]
    assert json.loads((atlas / "pins.json").read_text())["interfaces"][0]["rule"] == "mirror"
    # what the join read, for staleness: the manifest it saw and the PDF each netlist came from
    assert inp["sources"] is None and inp["source_files"] is None          # no inventory in this project
    assert set(inp["schematics"]) == {"M", "S"}
    write_json(ad / "inventory" / "manifest.json", {"documents": [{"file": "a.pdf", "sha256": "11"}]})
    run_join(ad, atlas)
    inp = read_json(atlas / "inputs.json")
    assert inp["source_files"] == {"a.pdf": "11"} and inp["sources"]


def test_mcu_port_from_datasheet_pin_records(tmp_path):
    ad, atlas = make_project(tmp_path)
    nl = read_json(ad / "boards" / "M" / "netlist.json")
    nl["components"]["U1"]["pins"]["12"]["name"] = "NRST"            # the symbol drops the port name
    write_json(ad / "boards" / "M" / "netlist.json", nl)
    assert "PF2" not in {r["port"] for r in run_join(ad, atlas)["pins"]}
    d = ad / "docs" / "mcu0@1"
    write_json(d / "doc.json", {"doc_id": "mcu0@1", "kind": "datasheet", "part": "MCU0"})
    write_jsonl(d / "records" / "pin.jsonl", [{"id": "mcu0.pin.12", "kind": "pin", "doc": "mcu0@1", "p": 40, "q": "12 PF2-NRST I/O FT",
                                              "src": "table", "prov": "deterministic", "pin": "12", "name": "PF2-NRST", "type": "FT"}])
    rows = {r["num"]: r for r in run_join(ad, atlas)["pins"]}
    assert rows["12"]["port"] == "PF2" and rows["12"]["io"] == "FT" and rows["12"]["ds_cite"] == "mcu0@1 p40"


def test_pin_norm_keeps_polarity():
    from embarch_atlas.join import pin_norm
    assert pin_norm("IN+") != pin_norm("IN-")
    assert pin_norm("IN-") == pin_norm("IN–") == pin_norm("in-")
    assert pin_norm("SDA") == "sda"


def test_card_aka_parts_family_forms(tmp_path):
    from embarch_atlas.join import card_aka_parts, _part_pattern, norm
    card = tmp_path / "card.md"
    card.write_text("# X\n\naka: DEMO120x, DEMO121x, fuel gauge, I2C 0x36, SHA-256, DEMO1205ABC+T\n")
    parts = card_aka_parts(card)
    assert "DEMO120x" in parts and "DEMO1205ABC+T" in parts
    assert "fuel gauge" not in parts and "SHA-256" not in parts
    assert _part_pattern("DEMO120x").match(norm("DEMO1205G+T"))


def test_prop_to_pin_ignores_the_active_low_marker():
    nl = _netlist("X", {"U9": _comp("PWR100", pins={4: ("FB/INT-bar", "N1"), 5: ("FAULT-bar", "N2"), 6: ("~RDY", "N3"),
                                                        7: ("EN", "N4"), 8: ("EN-bar", "N5")})})
    J = join.Join(Path("."), Path("."), {"X": join.Board("X", nl)}, {"uses": [], "nodes": {}}, {}, join.Docs(Path("/nonexistent")),
                  [], {}, [], None, None)
    assert join.prop_to_pin(J, "acme,x", "int-gpios", "X", "U9")[0] == "U9-4"
    assert join.prop_to_pin(J, "acme,x", "fault-gpios", "X", "U9")[0] == "U9-5"
    assert join.prop_to_pin(J, "acme,x", "rdy-gpios", "X", "U9")[0] == "U9-6"
    assert join.prop_to_pin(J, "acme,x", "en-gpios", "X", "U9")[0] is None      # EN and EN-bar: ambiguous, not guessed
    assert join.plain_name("FB/INT-bar") == "FB/INT" and join.plain_name("CS#") == "CS"


def test_a_declared_board_without_a_netlist_is_named_everywhere_the_session_reads(tmp_path):
    from embarch_atlas import project, query
    ad, atlas = make_project(tmp_path)
    (ad / "boards" / "S" / "netlist.json").unlink()
    logs = []
    join.run(ad, atlas, log=logs.append)
    assert any("without a netlist" in l and "S" in l for l in logs)
    assert read_json(atlas / "inputs.json")["missing_boards"] == ["S"]
    assert "S: declared in the stamp, NO netlist" in (atlas / "MAP.md").read_text(encoding="utf-8")
    st = project.status(ad)
    (row,) = [r for r in st["atlases"] if r["atlas"] == atlas.name]
    assert any("board S is declared but has no netlist" in w for w in row["stale"])
    out = query.mismatch_report(query.Atlas(ad))
    assert "board S is declared but has no netlist" in out.split("\n", 1)[0]
    # the netlist arrives and the join runs again: covered, and no reason
    write_json(ad / "boards" / "S" / "netlist.json", sensor_board())
    run_join(ad, atlas)
    assert read_json(atlas / "inputs.json")["missing_boards"] == []
    st = project.status(ad)
    assert not any("no netlist" in w for r in st["atlases"] for w in r["stale"])

"""join on a synthetic Nordic-family project: MCU pins spelled P0.xx / P1.xx and a Nordic dt.json.

Nothing here is a real part or board. The STM32 fixture in test_join.py is reused for the project scaffolding
(stamp, fw repo, docs) and only the netlist, dt.json and in_use.json are replaced.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_join import _comp, _netlist, _use, make_project, run_join  # noqa: E402

from embarch_atlas.common import read_json, write_json  # noqa: E402

GPIO0, GPIO1 = "/soc/peripheral@50000000/gpio@842500", "/soc/peripheral@50000000/gpio@842800"
UART = "/soc/peripheral@50000000/uart@a000"
LED = "/leds/led_0"


def nordic_board() -> dict:
    c = {
        "U1": _comp("MCU0-QFP32", pins={
            1: ("P0.04", "LED_STATUS"), 2: ("P0.5/AIN2", "nKEY0"), 3: ("P1.02", "UART_TX"), 4: ("P1.03", "UART_RX"),
            5: ("P1.10", "TP_ONLY"), 6: ("VDD", "3V3"), 7: ("VSS", "GND")}),
        "R1": _comp(None, "680Ω", {1: (None, "NetD1_1"), 2: (None, "3V3")}),
        "D1": _comp("LED-RED-0603", pins={1: ("A", "NetD1_1"), 2: ("K", "LED_STATUS")}),
        "J1": _comp("CONN-4", pins={1: (None, "UART_TX"), 2: (None, "UART_RX"), 3: (None, "GND"), 4: (None, "3V3")}),
        "J2": _comp("BTN-HDR", pins={1: (None, "nKEY0"), 2: (None, "GND")}),
        "TP1": _comp(None, pins={1: (None, "TP_ONLY")}),
    }
    return _netlist("M", c)


def nordic_dt() -> dict:
    def gpio(label, addr, ordinal):
        return {"labels": [label], "compats": ["nordic,nrf-gpio"], "okay": True, "ordinal": ordinal,
                "regs": [[addr, 0x300]], "parent": "/soc", "props": {}}
    nodes = {
        GPIO0: gpio("gpio0", 0x842500, 10), GPIO1: gpio("gpio1", 0x842800, 11),
        UART: {"labels": ["uart20"], "compats": ["nordic,nrf-uarte"], "okay": True, "ordinal": 20, "regs": [[0xA000, 0x1000]],
               "parent": "/soc", "props": {}},
        "/leds": {"labels": [], "compats": ["gpio-leds"], "okay": True, "ordinal": 30, "regs": [], "parent": "/", "props": {}},
        LED: {"labels": ["status_led"], "compats": [], "okay": True, "ordinal": 31, "regs": [], "parent": "/leds", "props": {}},
        "/buttons": {"labels": [], "compats": ["gpio-keys"], "okay": True, "ordinal": 32, "regs": [], "parent": "/", "props": {}},
        "/buttons/btn_0": {"labels": ["btn0"], "compats": [], "okay": True, "ordinal": 33, "regs": [], "parent": "/buttons", "props": {}},
    }
    uses = [
        _use("P1.02", UART, "pinctrl", "pinctrl-0", ["uart20"], "nordic,nrf-uarte", func="UART_TX"),
        _use("P1.03", UART, "pinctrl", "pinctrl-0", ["uart20"], "nordic,nrf-uarte", func="UART_RX"),
        _use("P0.04", LED, "gpio", "gpios", ["status_led"], "gpio-leds", ["gpio-leds"], flags="ACTIVE_LOW", index=0,
             loc="fw:board.dts:12"),
        _use("P0.05", "/buttons/btn_0", "gpio", "gpios", ["btn0"], "gpio-keys", ["gpio-keys"], flags="ACTIVE_LOW|PULL_UP", index=0,
             loc="fw:board.dts:20"),
    ]
    return {"atlas": "demo@000000000001", "gpio_controllers": {GPIO0: "0", GPIO1: "1"}, "stm32": False, "pin_decoder": "nordic",
            "nodes": nodes, "uses": uses, "devices": [], "chosen": {}, "aliases": {}, "exti_lines": {},
            "drivers": {"nordic_nrf_uarte": ["zephyr:drivers/serial/uart_nrfx_uarte.c"],
                        "gpio_keys": ["zephyr:drivers/input/input_gpio_keys.c"]},
            "drivers_not_built": {}, "consumers": {"alias:led0": ["fw:lib/led.c"]}, "armed": {},
            "instantiated_ordinals": [10, 11, 20, 32, 33], "mmio": {}, "n_tus": 1, "svd": False}


def test_join_on_a_nordic_project_fills_the_dt_column_and_runs_the_pin_checks(tmp_path):
    ad, atlas = make_project(tmp_path, with_docs=False)
    write_json(ad / "boards" / "M" / "netlist.json", nordic_board())
    stamp = read_json(atlas / "atlas.json")
    stamp["schematic_boards"] = ["M"]
    write_json(atlas / "atlas.json", stamp)
    dt = nordic_dt()
    dt["atlas"] = stamp["id"]
    write_json(atlas / "dt.json", dt)
    write_json(atlas / "in_use.json", {"peripherals": {}})
    run_join(ad, atlas)

    pj = read_json(atlas / "pins.json")
    pins = {r["name"]: r for r in pj["pins"]}
    assert pj["counts"]["dt_pins"] == 4
    assert {n: bool(pins[n]["dt"]) for n in ("P0.04", "P0.5/AIN2", "P1.02", "P1.03", "P1.10")} == {
        "P0.04": True, "P0.5/AIN2": True, "P1.02": True, "P1.03": True, "P1.10": False}
    assert pins["P0.5/AIN2"]["port"] == "P0.05"          # the symbol's P0.5 and the devicetree's P0.05 are one pin

    rep = read_json(atlas / "mismatches.json")
    assert rep["pin_checks_no_input"] is None
    chk = {c["check"]: c for c in rep["checks"]}
    assert chk["DT pin reaches the board"]["ran"] >= 4
    assert chk["connected pin has a DT use"]["ran"] > 0

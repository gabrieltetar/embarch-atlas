"""The live module: name -> ranges, the read-clear guard, and the decode, against a fake embarch-api."""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

from embarch_atlas import live
from embarch_atlas.common import write_json, write_jsonl

# A fake `embarch-api --config C --json <verb> ...`: `status` answers, `mem-read` serves words from
# memory.json and logs every request to calls.jsonl.
FAKE_API = """#!{python}
import json, sys
from pathlib import Path
here = Path(__file__).parent
args = sys.argv[1:]
verb = args[3]
if (here / "down").exists():
    print(json.dumps({{"success": False, "error": "embarch-core unreachable"}})); sys.exit(1)
if verb == "status":
    print(json.dumps({{"success": True, "status": "ok", "probes": []}})); sys.exit(0)
mem = {{int(k, 16): v for k, v in json.loads((here / "memory.json").read_text()).items()}}
ranges = [a.split(":") for i, a in enumerate(args) if i and args[i - 1] == "--range"]
with open(here / "calls.jsonl", "a") as f:
    f.write(json.dumps(ranges) + "\\n")
out = []
for a, n in ranges:
    base = int(a, 16)
    out.append({{"address": base, "words": [mem.get(base + 4 * i, 0) for i in range(int(n))]}})
chip = (here / "chip").read_text() if (here / "chip").exists() else "DEMO1"
print(json.dumps({{"success": True, "read": {{"role": "dut", "chip": chip, "probe_serial": "P1",
                  "ranges": out, "halted": True, "halted_us": 850}}}}))
"""

BASE = 0x4000_8000


def reg(name, off, reset="0x00000000", access="rw", width=32, periph="UART1"):
    return {"kind": "register", "id": f"demo.reg.{periph}.{name}", "name": name, "addr": f"0x{BASE + off:08X}",
            "reset": reset, "access": access, "width": width, "doc": "demo-svd"}


def field(regname, name, hi, lo, access="rw", enum=None, periph="UART1"):
    f = {"kind": "field", "id": f"demo.reg.{periph}.{regname}.{name}", "reg": f"demo.reg.{periph}.{regname}",
         "name": name, "bits": [hi, lo], "access": access, "doc": "demo-svd"}
    if enum:
        f["enum"] = enum
    return f


def make(tmp: Path, memory: dict[int, int]) -> Path:
    ad = tmp / "fw" / "embarch" / "atlas"
    svd = ad / "docs" / "demo-svd"
    write_json(svd / "doc.json", {"doc_id": "demo-svd", "kind": "svd"})
    write_jsonl(svd / "records" / "register.jsonl", [
        reg("CR1_FIFO_ENABLED", 0x00, reset="0x00000001"), reg("CR1_FIFO_DISABLED", 0x00, reset="0x00000001"),
        reg("BRR", 0x0C), reg("ISR", 0x1C, reset="0x000000C0", access="ro"), reg("RDR", 0x24, access="ro"),
        reg("TDR", 0x28), reg("ECR", 0x2C, access="ro"), reg("KEY", 0x30, access="wo"),
    ])
    write_jsonl(svd / "records" / "field.jsonl", [
        field("CR1_FIFO_ENABLED", "UE", 0, 0, enum={"0": "B_0x0: disabled", "1": "B_0x1: enabled"}),
        field("CR1_FIFO_ENABLED", "M", 3, 2),
        field("BRR", "BRR", 15, 0),
        field("ISR", "RXNE", 5, 5, access="ro"), field("ISR", "TXE", 7, 7, access="ro"),
        field("RDR", "RDR", 8, 0, access="ro"), field("TDR", "TDR", 8, 0),
        field("ECR", "CEL", 7, 0, access="rclr"),
    ])
    rm = ad / "docs" / "demo-rm"
    write_json(rm / "doc.json", {"doc_id": "demo-rm", "kind": "reference-manual"})
    write_json(rm / "rm_regs.json", [{"name": "UART_ISR", "page": 40, "fields": [
        {"name": "RXNE", "page": 41, "text": "Read data register not empty. It is cleared by reading the UART_RDR register."},
        {"name": "TXE", "page": 41, "text": "Set by hardware when the data has been moved out of TDR."},
    ]}])
    api = ad / "fake-api"
    api.write_text(FAKE_API.format(python=sys.executable))
    api.chmod(api.stat().st_mode | stat.S_IEXEC)
    (ad / "memory.json").write_text(json.dumps({f"0x{k:08X}": v for k, v in memory.items()}))
    (ad / "atlas.toml").write_text(f'[project]\nfw = "{tmp / "fw"}"\n\n[live]\napi = "{api}"\napi_config = "x.toml"\n')
    return ad


def set_chips(ad: Path, atlas: str, core: str) -> None:
    write_json(ad / "docs" / "demo-svd" / "peripherals.json", {"device": atlas, "peripherals": {}})
    (ad / "chip").write_text(core)


def test_a_read_from_another_chip_is_refused_and_names_both(tmp_path):
    ad = make(tmp_path, {BASE + 0x0C: 0x0000_01A1})
    set_chips(ad, "ABC200", "XYZ700")
    out = live.reg_read(ad, "UART1.BRR", role="dev-bench")
    assert "Refused" in out and "XYZ700" in out and "ABC200" in out and "dev-bench" in out
    assert "0x000001A1" not in out and "BRR[" not in out
    set_chips(ad, "ABC200", "ABC200")
    ok = live.reg_read(ad, "UART1.BRR")
    assert not ok.startswith("src: none") and "0x000001A1" in ok


def test_chip_names_compare_across_package_and_suffix_variants():
    assert live.same_chip("abc200", "ABC200XY")
    assert live.same_chip("ABC200XY", "abc200")
    assert live.same_chip("XYZ700", "XYZ700-QFAA")
    assert live.same_chip(None, "anything")
    assert not live.same_chip("ABC200", "XYZ700")
    assert not live.same_chip("ABC200", "ABC300")


def test_live_stacks_says_different_boards_not_another_build_on_a_chip_mismatch(tmp_path):
    ad = make(tmp_path, {})
    set_chips(ad, "ABC200", "XYZ700")
    elf = tmp_path / "z.elf"
    tiny_elf(elf, 0x0800_0000, bytes(64))
    import pytest
    with pytest.raises(live.ChipMismatch):
        live.image_check(live.settings(ad), "dut", elf, ad)
    from embarch_atlas import buildq

    class Con:
        def __init__(self, *a): self.s, self.role = live.settings(ad), "dut"
        def run(self, *a): return STACKS_OUT
        def src(self): return "console"
    import types
    src = types.SimpleNamespace(label="b", elf=elf)
    mp = pytest.MonkeyPatch()
    mp.setattr(live, "Console", Con)
    mp.setattr(buildq, "source", lambda *a: src)
    mp.setattr(buildq, "_image", lambda s: {"symbols": []})
    try:
        out = live.live_stacks(ad)
    finally:
        mp.undo()
    assert "different boards" in out and "another build" not in out


def calls(ad: Path) -> list:
    p = ad / "calls.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def test_one_register_is_decoded_field_by_field_with_what_differs_from_reset(tmp_path):
    ad = make(tmp_path, {BASE + 0x0C: 0x0000_01A1})
    out = live.reg_read(ad, "UART1.BRR")
    assert out.startswith("src: live dut DEMO1 via embarch-core (probe P1), core halted 850 us")
    assert "UART1.BRR @ 0x4000800C = 0x000001A1  reset 0x00000000" in out
    assert "* BRR[15:0] = 0x1a1 (reset 0x0)" in out
    assert calls(ad) == [[["0x4000800C", "1"]]]


def test_alternate_views_at_one_address_are_all_decoded_and_enums_are_named(tmp_path):
    ad = make(tmp_path, {BASE: 0x0000_0009})
    out = live.reg_read(ad, "uart1.cr1")
    assert "UART1.CR1_FIFO_ENABLED" in out and "UART1.CR1_FIFO_DISABLED" in out
    assert "  UE[0] = 0x1  enabled" in out
    assert "* M[3:2] = 0x2 (reset 0x0)" in out
    assert calls(ad) == [[["0x40008000", "1"]]]


def test_a_register_whose_read_clears_a_flag_is_refused_unless_allowed(tmp_path):
    ad = make(tmp_path, {BASE + 0x24: 0x41})
    out = live.reg_read(ad, "UART1.RDR")
    assert out.startswith("src: none") and "Refused" in out
    assert "reading it clears UART_ISR.RXNE (demo-rm p41)" in out
    assert calls(ad) == []
    out = live.reg_read(ad, "UART1.RDR", allow_clear=True)
    assert "UART1.RDR @ 0x40008024 = 0x00000041" in out
    assert "! this read cleared: reading it clears UART_ISR.RXNE" in out
    assert calls(ad) == [[["0x40008024", "1"]]]


def test_the_svd_read_action_guards_too(tmp_path):
    ad = make(tmp_path, {})
    out = live.reg_read(ad, "UART1.ECR")
    assert "Refused" in out and "CEL is cleared by the read (demo-svd: readAction)" in out


def test_a_whole_peripheral_is_one_snapshot_that_leaves_out_what_a_read_would_clear(tmp_path):
    ad = make(tmp_path, {BASE: 0x1, BASE + 0x1C: 0x0000_00E0, BASE + 0x24: 0x41, BASE + 0x28: 0x0})
    out = live.reg_read(ad, "UART1")
    # one request, split around RDR (0x24) and ECR (0x2C), the gap before ISR spanned
    assert calls(ad) == [[["0x40008000", "8"], ["0x40008028", "1"], ["0x40008030", "1"]]]
    assert "(also viewed as CR1_FIFO_ENABLED at 0x40008000)" in out
    assert "* RXNE[5] = 0x1 (reset 0x0)" in out
    assert "TXE" not in out.split("UART1.ISR", 1)[1].split("UART1.", 1)[0]
    assert "(1 field at reset)" in out
    assert "UART1.KEY @ 0x40008030 = 0x00000000  reset 0x00000000  (write-only" in out
    tail = out.split("Not read", 1)[1]
    assert "- RDR: reading it clears UART_ISR.RXNE" in tail and "- ECR: CEL is cleared by the read" in tail


def test_bits_outside_every_field_are_flagged(tmp_path):
    ad = make(tmp_path, {BASE + 0x1C: 0x0001_0080})
    out = live.reg_read(ad, "UART1.ISR")
    assert "! bits outside every field are set: 0x10000" in out


def test_unknown_names_say_what_exists(tmp_path):
    ad = make(tmp_path, {})
    assert "no register 'NOPE' in UART1; it has: BRR, CR1_FIFO_DISABLED" in live.reg_read(ad, "UART1.NOPE")
    out = live.reg_read(ad, "charger.ChargeOption0")
    assert "no MCU peripheral, DT label or part named 'charger'" in out


def test_a_core_that_does_not_answer_keeps_the_tool_off(tmp_path):
    ad = make(tmp_path, {})
    assert live.available(ad) is None
    (ad / "down").write_text("")
    assert live.available(ad) == "embarch-core unreachable"
    assert live.reg_read(ad, "UART1.BRR") == "src: none\n\nThe read failed: embarch-core unreachable\n"


def test_plan_merges_across_short_gaps_only_and_never_over_a_skipped_word():
    regs = [{"addr": hex(BASE + o)} for o in (0x0, 0x4, 0x10, 0x60)]
    assert live.plan(regs, set()) == [(BASE, 5), (BASE + 0x60, 1)]
    assert live.plan(regs, {BASE + 0x8}) == [(BASE, 2), (BASE + 0x10, 1), (BASE + 0x60, 1)]


def test_sixteen_bit_registers_at_a_half_word_take_their_half():
    words = {BASE: 0xBEEF_1234}
    assert live.reg_value(words, {"addr": hex(BASE + 2), "width": 16}) == 0xBEEF
    assert live.reg_value(words, {"addr": hex(BASE), "width": 16}) == 0x1234


def test_rm_clear_on_read_wordings():
    import re
    texts = {
        "It is cleared by reading the UART_RDR register.": ("UART", "RDR"),
        "It is automatically cleared upon reading PDC_RXDR.": ("PDC", "RXDR"),
        "cleared when the SPIx_SR register is read": ("SPI", "SR"),
        "Every read operation from the UART_RDR frees a location": ("UART", "RDR"),
        "or by reading the TIMx_CCR1 register": ("TIM", "CCR1"),
    }
    for text, key in texts.items():
        hits = [m.group(1) for p in live.CLEAR_ON_READ for m in p.finditer(text)]
        assert hits, text
        prefix, _, short = hits[0].partition("_")
        assert (prefix.rstrip("x").rstrip("0123456789").upper(), short.upper()) == key, text
    assert not any(p.search("Set by hardware when the data has been moved out of TDR.") for p in live.CLEAR_ON_READ)


# ---------------------------------------------------------------- external parts, through the shell

# A fake console: `list-signals` names one; `signal-exchange` answers `i2c read_byte`, `i2c read` and
# `device list` from devices.json ({"bus/addr": {"reg": byte, ...}, "_off": ["node"]}), echoing the
# command after a stale prompt and some log first, as a Zephyr shell over CDC ACM does.
FAKE_SHELL = """#!{python}
import json, sys
from pathlib import Path
here = Path(__file__).parent
args = sys.argv[1:]
verb = args[3]
if verb == "list-signals":
    print(json.dumps({{"success": True, "signals": [
        {{"name": "bench-console", "origin_role": "dev-bench", "direction": "bidirectional", "route": {{"kind": "direct"}}}},
        {{"name": "dut-console", "origin_role": "dut", "direction": "bidirectional", "route": {{"kind": "direct"}}}}]}}))
    sys.exit(0)
cmd = args[args.index("--write") + 1]
with open(here / "shell.log", "a") as f:
    f.write(cmd + "\\n")
devs = json.loads((here / "devices.json").read_text())
words = cmd.split()
out = ""
if words[:2] == ["device", "list"]:
    out = "devices:\\r\\n" + "".join("- %s (off)\\r\\n" % n for n in devs.get("_off", []))
elif words[0] == "i2c" and devs.get("_no_i2c"):
    out = "i2c: command not found\\r\\n"
elif words[0] == "i2c":
    regs = devs.get("%s/%s" % (words[2], words[3].lower()))
    reg = int(words[4], 16)
    if regs is None:
        out = "\\x1b[1;31mFailed to read from device: %s\\x1b[0m\\r\\n" % words[3]
    elif words[1] == "read_byte":
        out = "Output: 0x%x\\r\\n" % regs.get(str(reg), 0)
    else:
        n = int(words[5], 16)
        data = regs.get(str(reg), [0] * n)
        out = "00000000: " + " ".join("%02x" % b for b in data) + " |....|\\r\\n"
text = "log line\\r\\n\\x1b[1;32muart:~$ \\x1b[m" + cmd + "\\r\\n" + out + "\\x1b[1;32muart:~$ "
print(json.dumps({{"success": True, "exchange": {{"signal": "dut-console", "port": "COM9", "text": text,
                  "until_seen": True, "truncated": False}}}}))
"""


def make_ext(tmp: Path, devices: dict) -> Path:
    ad = make(tmp, {})
    api = ad / "fake-api"
    api.write_text(FAKE_SHELL.format(python=sys.executable))
    (ad / "devices.json").write_text(json.dumps(devices))
    atlas = ad / "atlases" / "core@000000000000"
    write_json(atlas / "atlas.json", {"created": "2026-10-06T00:00:00Z"})
    write_json(atlas / "pins.json", {"part_docs": {"U5": ["sense@A"], "U6": ["sense@A"], "U9": ["gauge@B"]},
        "i2c": {"buses": [{"bus": "/soc/i2c@40005800", "families": [
            {"compat": "acme,sense", "dt": [{"node": "/soc/i2c@40005800/vbus-sense@44", "reg": 0x44},
                                             {"node": "/soc/i2c@40005800/vsys-sense@45", "reg": 0x45}],
             "parts": [{"part": "U5", "addrs": [0x44]}, {"part": "U6", "addrs": [0x45]}]},
            {"compat": "acme,gauge", "dt": [{"node": "/soc/i2c@40005800/gauge@36", "reg": 0x36}],
             "parts": [{"part": "U9", "addrs": [0x36, 0x0B]}]}]}]}})
    write_json(atlas / "dt.json", {"nodes": {
        "/soc/i2c@40005800/vbus-sense@44": {"labels": ["sense_vbus"]},
        "/soc/i2c@40005800/vsys-sense@45": {"labels": ["sense_vsys"]},
        "/soc/i2c@40005800/gauge@36": {"labels": ["gauge"]}}})
    write_json(atlas / "kconfig.json", {"symbols": {"SHELL_PROMPT_UART": {"v": "uart:~$ "}}})
    sense = ad / "docs" / "sense@A"
    write_json(sense / "doc.json", {"doc_id": "sense@A", "kind": "datasheet", "part": "SENSE7"})
    write_jsonl(sense / "records" / "register.jsonl", [
        {"kind": "register", "id": "sense.reg.CONFIG", "name": "CONFIG", "addr": "0x00", "width": 16, "reset": "0x0000", "doc": "sense@A"},
        {"kind": "register", "id": "sense.reg.VBUS", "name": "VBUS", "addr": "0x05", "width": 16, "reset": "0x0000", "doc": "sense@A"},
        {"kind": "register", "id": "sense.reg.ENERGY", "name": "ENERGY", "addr": "0x09", "width": 40, "reset": "0x0", "doc": "sense@A"},
        {"kind": "register", "id": "sense.reg.DIAG", "name": "DIAG", "addr": "0x0B", "width": 16, "reset": "0x0001", "doc": "sense@A"},
        {"kind": "register", "id": "sense.reg.LIM_LSB", "name": "LIM_LSB", "addr": "0x20", "width": 8, "reset": "0x34", "doc": "sense@A"},
        {"kind": "register", "id": "sense.reg.LIM_MSB", "name": "LIM_MSB", "addr": "0x21", "width": 8, "reset": "0x12", "doc": "sense@A"}])
    write_jsonl(sense / "records" / "field.jsonl", [
        {"kind": "field", "id": "sense.reg.CONFIG.RST", "reg": "sense.reg.CONFIG", "name": "RST", "bits": [15, 15],
         "enum": {"0h": "Normal", "1h": "Reset"}, "doc": "sense@A", "p": 20},
        {"kind": "field", "id": "sense.reg.CONFIG.AVG", "reg": "sense.reg.CONFIG", "name": "AVG", "bits": [2, 0], "doc": "sense@A", "p": 20},
        {"kind": "field", "id": "sense.reg.DIAG.OVF", "reg": "sense.reg.DIAG", "name": "OVF", "bits": [3, 3], "access": "ro",
         "desc": "overflow; when LATCH = 1, cleared by reading DIAG", "doc": "sense@A", "p": 29},
        {"kind": "field", "id": "sense.reg.LIM_MSB.HI", "reg": "sense.reg.LIM_MSB", "name": "HI", "bits": [7, 0], "doc": "sense@A", "p": 31},
        {"kind": "field", "id": "sense.reg.LIM_LSB.LO", "reg": "sense.reg.LIM_LSB", "name": "LO", "bits": [7, 0], "doc": "sense@A", "p": 31}])
    write_jsonl(sense / "records" / "bus.jsonl", [
        {"kind": "bus", "id": "sense.bus.i2c", "iface": "i2c", "byte_order": "msb-first", "reg_addr_bytes": 1, "doc": "sense@A", "p": 22}])
    gauge = ad / "docs" / "gauge@B"
    write_json(gauge / "doc.json", {"doc_id": "gauge@B", "kind": "datasheet", "part": "GAUGE5"})
    write_jsonl(gauge / "records" / "register.jsonl", [
        {"kind": "register", "id": "gauge.reg.Status", "name": "Status", "addr": "0x000", "width": 16, "reset": "0x0002", "doc": "gauge@B"},
        {"kind": "register", "id": "gauge.reg.nCycles", "name": "nCycles", "addr": "0x1A4", "width": 16, "reset": "0x0000", "doc": "gauge@B"}])
    write_jsonl(gauge / "records" / "bus.jsonl", [
        {"kind": "bus", "id": "gauge.bus.i2c", "iface": "i2c", "byte_order": "lsb-first", "reg_addr_bytes": 1,
         "windows": [{"from": "0x000", "to": "0x0FF", "addr": "0x36"}, {"from": "0x100", "to": "0x1FF", "addr": "0x0B", "base": "0x100"}],
         "doc": "gauge@B", "p": 100}])
    return ad


def shell_log(ad: Path) -> list[str]:
    p = ad / "shell.log"
    return p.read_text().splitlines() if p.exists() else []


SENSE = {"i2c@40005800/0x44": {"0": [0x80, 0x03], "5": [0x06, 0x63], "9": [0, 0, 0, 1, 0], "11": [0, 8],
                                "32": 0x34, "33": 0x56},
         "i2c@40005800/0x36": {"0": [0x02, 0x80]}, "i2c@40005800/0x0b": {"164": [0x10, 0x00]}}


def test_the_shell_answers_the_command_alone_on_the_roles_console(tmp_path):
    ad = make_ext(tmp_path, SENSE)
    out = live.shell(ad, "device list")
    assert out == "src: live dut shell via embarch-core (signal dut-console, COM9)\n\ndevices:\n"


def test_shell_read_runs_listed_reads_and_refuses_the_rest(tmp_path):
    ad = make_ext(tmp_path, SENSE)
    assert "devices:" in live.shell_read(ad, "device list")
    assert "src: live" in live.shell_read(ad, "i2c read i2c@40005800 0x44 0x00 2")       # glob
    for bad in ("gpio set gpio@1 3 1", "i2c scan i2c@40005800", "device list; gpio set g 1 1", "kernel uptime | x"):
        out = live.shell_read(ad, bad)
        assert "Refused" in out and "shell_run" in out, bad
    assert shell_log(ad) == ["device list", "i2c read i2c@40005800 0x44 0x00 2"]


def test_the_read_only_list_is_per_project(tmp_path):
    ad = make_ext(tmp_path, SENSE)
    assert live.settings(ad)["read_only"] == live.DEFAULT_READ_ONLY
    assert "i2c scan" not in " ".join(live.DEFAULT_READ_ONLY) and "kernel uptime" in live.DEFAULT_READ_ONLY
    assert live.is_read_only("foo  bar", ["foo *"]) and not live.is_read_only("foo", ["foo *"])


def test_an_external_register_is_read_on_its_bus_and_decoded_msb_first(tmp_path):
    ad = make_ext(tmp_path, SENSE)
    out = live.reg_read(ad, "sense_vbus.CONFIG")
    assert shell_log(ad) == ["i2c read i2c@40005800 0x44 0x00 2"]
    assert "sense_vbus = /soc/i2c@40005800/vbus-sense@44 at 0x44 on i2c@40005800 (U5); decoded with sense@A" in out
    assert "sense_vbus.CONFIG @ 0x00 = 0x8003  reset 0x0000" in out
    assert "* RST[15] = 0x1 (reset 0x0)  Reset" in out and "* AVG[2:0] = 0x3 (reset 0x0)" in out


def test_a_lsb_msb_pair_is_one_value_read_a_byte_at_a_time(tmp_path):
    ad = make_ext(tmp_path, SENSE)
    out = live.reg_read(ad, "sense_vbus.LIM")
    assert shell_log(ad) == ["i2c read_byte i2c@40005800 0x44 0x20", "i2c read_byte i2c@40005800 0x44 0x21"]
    assert "sense_vbus.LIM @ 0x20+0x21 = 0x5634  reset 0x1234" in out
    assert "* HI[15:8] = 0x56 (reset 0x12)" in out and "  LO[7:0] = 0x34" in out


def test_a_wide_register_takes_all_its_bytes(tmp_path):
    ad = make_ext(tmp_path, SENSE)
    out = live.reg_read(ad, "sense_vbus.ENERGY")
    assert shell_log(ad) == ["i2c read i2c@40005800 0x44 0x09 5"]
    assert "= 0x0000000100" in out


def test_a_part_with_several_devices_asks_which(tmp_path):
    ad = make_ext(tmp_path, SENSE)
    out = live.reg_read(ad, "SENSE7.VBUS")
    assert "'SENSE7' is 2 devices on the board; name one by its DT label: sense_vbus (i2c@40005800 0x44), sense_vsys" in out
    assert shell_log(ad) == []


def test_a_read_clearing_register_of_a_part_is_refused_and_left_out(tmp_path):
    ad = make_ext(tmp_path, SENSE)
    out = live.reg_read(ad, "sense_vbus.DIAG")
    assert "Refused" in out and "OVF clears on the read (sense@A p29)" in out and shell_log(ad) == []
    out = live.reg_read(ad, "sense_vbus")
    assert "i2c read i2c@40005800 0x44 0x0B 2" not in shell_log(ad)
    assert "- DIAG: OVF clears on the read (sense@A p29)" in out.split("Not read", 1)[1]
    assert "sense_vbus.VBUS @ 0x05 = 0x0663" in out


def test_windows_route_a_register_to_its_bus_address_and_lsb_first(tmp_path):
    ad = make_ext(tmp_path, SENSE)
    out = live.reg_read(ad, "gauge.nCycles")
    assert shell_log(ad) == ["i2c read i2c@40005800 0x0B 0xA4 2"]
    assert "gauge.nCycles @ 0x1A4 = 0x0010" in out
    out = live.reg_read(ad, "gauge.Status")
    assert shell_log(ad)[-1] == "i2c read i2c@40005800 0x36 0x00 2" and "= 0x8002" in out


def test_a_device_that_does_not_answer_says_what_the_board_lists_it_as(tmp_path):
    ad = make_ext(tmp_path, {"_off": ["vsys-sense@45"]})
    out = live.reg_read(ad, "sense_vsys.VBUS")
    assert "Failed to read from device: 0x45; the board lists vsys-sense@45 as (off)" in out


def test_multi_byte_registers_need_a_bus_record(tmp_path):
    ad = make_ext(tmp_path, SENSE)
    (ad / "docs" / "sense@A" / "records" / "bus.jsonl").unlink()
    out = live.reg_read(ad, "sense_vbus.VBUS")
    assert "no `bus` record that states their byte order" in out and shell_log(ad) == []
    assert "= 0x5634" in live.reg_read(ad, "sense_vbus.LIM")   # single bytes need none


def test_read_clear_wordings_of_a_part():
    yes = ["when ALATCH = 1, cleared by reading ALERT_DIAG", "Does not reset until it is read.",
           "reading 07h clears them", "It is cleared upon a read of the register"]
    no = ["cleared by a system reset", "This bit can be read any time.", "set after all conversions complete"]
    assert all(live.READ_CLEARS_TEXT.search(t) for t in yes)
    assert not any(live.READ_CLEARS_TEXT.search(t) for t in no)


# ---------------------------------------------------------------- live stacks

STACKS_OUT = """0x20000b48 link_tx                          (real size 1024):\tunused  292\tusage  732 / 1024 (71 %)
0x20000c00 link_rx                          (real size 1024):\tunused   28\tusage  996 / 1024 (97 %)
0x20001998 usb@40005c00                     (real size 1024):\tunused  700\tusage  324 / 1024 (31 %)
0x200034a0 idle                             (real size  256):\tunused  168\tusage   88 /  256 (34 %)
0x20008938 IRQ 00                           (real size 2048):\tunused 1716\tusage  332 / 2048 (16 %)
"""


def tiny_elf(path: Path, paddr: int, payload: bytes) -> None:
    """An ELF32 LE with one PT_LOAD segment of `payload` at `paddr`."""
    import struct
    ehdr = b"\x7fELF" + bytes([1, 1, 1]) + bytes(9) + struct.pack("<HHIIIIIHHHHHH", 2, 40, 1, 0, 52, 0, 0, 52, 32, 1, 0, 0, 0)
    phdr = struct.pack("<IIIIIIII", 1, 84, paddr, paddr, len(payload), len(payload), 5, 4)
    path.write_bytes(ehdr + phdr + payload)


def test_thread_rows_are_parsed():
    rows = live.parse_thread_stacks(STACKS_OUT)
    assert [r["name"] for r in rows] == ["link_tx", "link_rx", "usb@40005c00", "idle", "IRQ 00"]
    assert rows[1] == {"addr": 0x20000C00, "name": "link_rx", "size": 1024, "unused": 28, "used": 996, "pct": 97}


def test_the_image_check_compares_three_places_of_flash(tmp_path):
    payload = bytes(range(256)) * 4
    elf = tmp_path / "z.elf"
    tiny_elf(elf, 0x0800_0000, payload)
    segs = live.elf_segments(elf)
    assert segs == [(0x0800_0000, 84, 1024)]
    assert live.probe_ranges(segs) == [(0x0800_0000, 16), (0x0800_0200, 16), (0x0800_03C0, 16)]
    words = {0x0800_0000 + i: int.from_bytes(payload[i:i + 4], "little") for i in range(0, 1024, 4)}
    ad = make(tmp_path, words)
    s = live.settings(ad)
    assert live.image_check(s, "dut", elf) == (True, "flash matches the ELF at 3 places")
    words[0x0800_0204] ^= 1
    (ad / "memory.json").write_text(json.dumps({f"0x{k:08X}": v for k, v in words.items()}))
    assert live.image_check(s, "dut", elf) == (False, "flash differs from the ELF at 1 of 3 places")


def test_threads_join_their_stacks_by_definition_then_name_never_by_size_alone():
    img = {"symbols": [
        {"a": 0x20000B00, "s": 0x100, "n": "_k_thread_obj_link_tx"},
        {"a": 0x20000C00, "s": 0x100, "n": "_k_thread_obj_link_rx"},
        {"a": 0x200016A8, "s": 0x3F8, "n": "udc169_priv"},
        {"a": 0x20003400, "s": 0x100, "n": "z_idle_threads"},
        {"a": 0x20008900, "s": 0x100, "n": "_kernel"}]}
    rows = [{"n": "_k_thread_stack_link_tx", "s": 1024, "f": "fw:src/link.c", "l": 10, "opts": []},
            {"n": "_k_thread_stack_link_rx", "s": 1024, "f": "fw:src/link.c", "l": 12, "opts": []},
            {"n": "udc169_thr_stk", "s": 1024, "f": "zephyr:udc.c", "l": 9, "opts": ["UDC_STACK_SIZE"]},
            {"n": "z_main_stack", "s": 1024, "f": "zephyr:init.c", "l": 55, "opts": ["MAIN_STACK_SIZE"]},
            {"n": "z_idle_stacks", "s": 256, "f": "zephyr:init.c", "l": 62, "opts": []},
            {"n": "z_interrupt_stacks", "s": 2048, "f": "zephyr:init.c", "l": 149, "opts": ["ISR_STACK_SIZE"]}]
    out = {j["name"]: j for j in live.join_stacks(live.parse_thread_stacks(STACKS_OUT), img, rows)}
    assert out["link_rx"]["stack"]["n"] == "_k_thread_stack_link_rx" and out["link_rx"]["how"] == "K_THREAD_DEFINE"
    assert out["usb@40005c00"]["stack"]["n"] == "udc169_thr_stk"          # via the thread object's name
    assert out["idle"]["stack"]["n"] == "z_idle_stacks"
    assert out["IRQ 00"]["stack"]["n"] == "z_interrupt_stacks"            # irq ~ interrupt
    assert all(j["stack"] is None or j["stack"]["n"] != "z_main_stack" for j in out.values())


# ---------------------------------------------------------------- write-one-to-clear and undefined reset bits

def _extend(ad: Path, doc: str, regs: list[dict], fields: list[dict]) -> None:
    d = ad / "docs" / doc / "records"
    for name, add in (("register.jsonl", regs), ("field.jsonl", fields)):
        rows = [json.loads(x) for x in (d / name).read_text().splitlines() if x.strip()]
        write_jsonl(d / name, rows + add)


def test_a_write_one_to_clear_field_does_not_guard_its_register_on_the_mcu(tmp_path):
    ad = make(tmp_path, {BASE + 0x34: 0x0000_0003})
    _extend(ad, "demo-svd", [reg("SR", 0x34, access="rw")],
            [field("SR", "OVR", 1, 1, access="woclr"), field("SR", "FE", 0, 0, access="wclr")])
    out = live.reg_read(ad, "UART1.SR")
    assert "Refused" not in out and "UART1.SR @ 0x40008034 = 0x00000003" in out
    assert "OVR[1] = 0x1" in out
    whole = live.reg_read(ad, "UART1")                       # read, not left out under "Not read"
    assert "UART1.SR @ 0x40008034" in whole and "- SR:" not in whole
    assert "Refused" in live.reg_read(ad, "UART1.ECR")        # a read-clear (rclr) still guards


def test_a_write_one_to_clear_field_does_not_guard_an_external_register(tmp_path):
    ad = make_ext(tmp_path, SENSE)
    _extend(ad, "sense@A", [], [])
    d = ad / "docs" / "sense@A" / "records"
    rows = [json.loads(x) for x in (d / "field.jsonl").read_text().splitlines()]
    for r in rows:
        if r["name"] == "OVF":
            r["access"], r["desc"] = "woclr", "overflow flag; write 1 to clear"
    write_jsonl(d / "field.jsonl", rows)
    out = live.reg_read(ad, "sense_vbus.DIAG")
    assert "Refused" not in out and "i2c read i2c@40005800 0x44 0x0B 2" in shell_log(ad)


def test_fields_whose_reset_the_svd_leaves_undefined_are_not_marked_against_reset(tmp_path):
    ad = make(tmp_path, {BASE + 0x38: 0x0000_00FF})
    _extend(ad, "demo-svd", [reg("PART", 0x38, reset=None)],
            [dict(field("PART", "LO", 3, 0), reset="0x5"), dict(field("PART", "HI", 7, 4), reset=None)])
    out = live.reg_read(ad, "UART1.PART")
    assert "reset 0x" not in out.split("LO")[0]              # no register reset claimed
    assert "* LO[3:0] = 0xf (reset 0x5)" in out
    hi = next(ln for ln in out.splitlines() if "HI[7:4]" in ln)
    assert hi.strip() == "HI[7:4] = 0xf"                      # no star, no reset claimed


def test_a_field_reset_is_field_relative_and_null_falls_back_to_the_register(tmp_path):
    ad = make(tmp_path, {BASE + 0x38: 0x50})
    _extend(ad, "demo-svd", [reg("RST", 0x38, reset="0x00000050")],
            [dict(field("RST", "HI", 7, 4), reset="0x5"), dict(field("RST", "LO", 3, 0), reset=None)])
    out = live.reg_read(ad, "UART1.RST")
    assert "* HI" not in out and "* LO" not in out and "(reset" not in out
    ad2 = make(tmp_path / "b", {BASE + 0x38: 0x60})
    _extend(ad2, "demo-svd", [reg("RST", 0x38, reset="0x00000050")],
            [dict(field("RST", "HI", 7, 4), reset="0x5"), dict(field("RST", "LO", 3, 0), reset=None)])
    off = live.reg_read(ad2, "UART1.RST")
    assert "* HI[7:4] = 0x6 (reset 0x5)" in off and "* LO" not in off


def test_a_board_without_the_i2c_shell_is_told_so_not_given_a_bus_diagnosis(tmp_path):
    ad = make_ext(tmp_path, {"_no_i2c": True, "_off": ["vsys-sense@45"]})
    out = live.reg_read(ad, "sense_vsys.VBUS")
    assert "no I2C shell" in out and "CONFIG_I2C_SHELL=y" in out
    assert "Nothing answered" not in out and "the board lists" not in out
    assert shell_log(ad) == ["i2c read i2c@40005800 0x45 0x05 2"]        # no `device list`
    assert "Nothing answered at 0x45" in live.reg_read(make_ext(tmp_path / "b", {}), "sense_vsys.VBUS")


def charger_doc(tmp: Path) -> Path:
    """Records shaped like the parts whose read-to-clear wordings differ: a config bit that names
    another register's flag, a plain read-clear flag, a latch, a W1C flag."""
    doc = tmp / "chg@C"
    r = lambda n, a, w=16: {"kind": "register", "id": f"c.reg.{n}", "name": n, "addr": a, "width": w, "doc": "chg@C", "p": 9}
    f = lambda reg, n, **kw: {"kind": "field", "id": f"c.reg.{reg}.{n}", "reg": f"c.reg.{reg}", "name": n,
                              "bits": [0, 0], "doc": "chg@C", "p": 47, **kw}
    write_jsonl(doc / "records" / "register.jsonl", [
        r("ChargeOption0", "0x00"), r("ProchotStatus", "0x22"), r("ChargerStatus", "0x20"),
        r("ChargeOption4", "0x3C"), r("ALERT_DIAG", "0x0B"), r("STATUS", "0x07", 8), r("Plain", "0x10")])
    write_jsonl(doc / "records" / "field.jsonl", [
        f("ChargeOption0", "EN_CMP_LATCH", access="rw",
          desc="Enable comparator latch. When set, the comparator output STAT_COMP (REG0x22) stays set until read by host. "
               "The default is 0."),
        f("ProchotStatus", "STAT_COMP", access="ro", desc="Comparator trip flag"),
        f("ChargerStatus", "FAULT_OCP", access="ro", desc="Fault flag, cleared when read by host."),
        f("ChargeOption4", "STAT_IDCHG2", access="ro", desc="Latched until read by the host."),
        f("ALERT_DIAG", "OVF", access="ro", desc="Overflow; cleared by reading ALERT_DIAG."),
        f("STATUS", "OCP", access="ro", desc="Latched fault, reading STATUS clears it."),
        f("Plain", "EN", access="rw", desc="Enable.")])
    return doc


def test_a_clear_on_read_sentence_naming_another_register_is_charged_to_that_one(tmp_path):
    doc = charger_doc(tmp_path)
    regs = {r["name"]: r for r in live._jsonl(doc / "records" / "register.jsonl")}
    eff = {n: live._ext_effects(doc, [r]) for n, r in regs.items()}
    assert eff["ChargeOption0"] == [] and eff["Plain"] == []
    assert len(eff["ProchotStatus"]) == 1 and eff["ProchotStatus"][0].startswith("STAT_COMP clears on the read (chg@C p47)")
    assert "stays set until read by host" in eff["ProchotStatus"][0]          # quotes the sentence it matched
    assert "EN_CMP_LATCH" not in " ".join(sum(eff.values(), []))
    for n in ("ChargerStatus", "ChargeOption4", "ALERT_DIAG", "STATUS"):
        assert eff[n] and "clears on the read" in eff[n][0], n
    assert [n for n, e in eff.items() if e] == ["ProchotStatus", "ChargerStatus", "ChargeOption4", "ALERT_DIAG", "STATUS"]


def test_the_guard_over_every_external_part_in_the_fixture_atlases_is_pinned(tmp_path):
    ad = make_ext(tmp_path, SENSE)
    docs = {"sense@A": ad / "docs" / "sense@A", "gauge@B": ad / "docs" / "gauge@B", "chg@C": charger_doc(tmp_path)}
    guarded = sorted(f"{d}:{r['name']}" for d, p in docs.items()
                     for r in live._jsonl(p / "records" / "register.jsonl") if live._ext_effects(p, [r]))
    assert guarded == ["chg@C:ALERT_DIAG", "chg@C:ChargeOption4", "chg@C:ChargerStatus", "chg@C:ProchotStatus",
                       "chg@C:STATUS", "sense@A:DIAG"]

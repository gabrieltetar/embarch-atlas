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
print(json.dumps({{"success": True, "read": {{"role": "dut", "chip": "DEMO1", "probe_serial": "P1",
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
    assert "no peripheral 'charger'" in out and "live shell" in out


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

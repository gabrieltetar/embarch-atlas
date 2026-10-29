"""`live core-state`: bit names, symbols from a fixture ELF's symbol table, and the refusal when the
flash is not that ELF. Against a fake embarch-api serving a canned `core-state` answer."""
from __future__ import annotations

import json
import subprocess
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from embarch_atlas import buildq, corestate, live  # noqa: E402
from test_live import make, tiny_elf  # noqa: E402

SYMS = [{"a": 0x0800_0101, "s": 0x40, "t": "T", "n": "main_loop", "f": "src/main.c", "l": 40},
        {"a": 0x0800_0200, "s": 0x20, "t": "T", "n": "spin_wait", "f": "src/wait.c", "l": 12},
        {"a": 0x2000_0000, "s": 0x40, "t": "D", "n": "a_variable", "f": "src/main.c", "l": 3}]


def dhcsr(raw):
    return {"raw": raw}


def state(**kw):
    base = {"role": "dut", "chip": "DEMO1", "probe_serial": "P1", "found": "running", "left": "running",
            "halt_count": 0, "halted_us": 0, "dhcsr": dhcsr(0x0301_0001), "dhcsr_after": dhcsr(0x0301_0001),
            "pc_samples": [0x0800_0204, 0x0800_0204, 0x0800_0204, 0x0800_0110, None],
            "pc_note": None, "registers": None, "scb_icsr": None}
    base.update(kw)
    return base


def setup(tmp_path, monkeypatch, st, flash_ok=True):
    payload = bytes(range(256)) * 4
    elf = tmp_path / "z.elf"
    tiny_elf(elf, 0x0800_0000, payload)
    words = {0x0800_0000 + i: int.from_bytes(payload[i:i + 4], "little") for i in range(0, 1024, 4)}
    if not flash_ok:
        words[0x0800_0004] ^= 1
    ad = make(tmp_path, words)
    (ad / "state.json").write_text(json.dumps(st))
    src = types.SimpleNamespace(label="b", elf=elf, tools={}, rel=lambda p: p)
    monkeypatch.setattr(buildq, "source", lambda *a: src)
    monkeypatch.setattr(buildq, "_image", lambda s: {"symbols": SYMS})
    monkeypatch.setattr(corestate, "_addr2line_of", lambda t: None)
    return ad


def test_dhcsr_and_icsr_are_decoded_by_field_name():
    d = corestate.decode_dhcsr(0x0307_0001)
    assert "halted" in d and "S_HALT" in d and "S_REGRDY" in d and "S_RESET_ST" in d and "C_DEBUGEN" in d
    assert "lockup" in corestate.decode_dhcsr(1 << 19)
    assert "sleeping" in corestate.decode_dhcsr(1 << 18) and "running" in corestate.decode_dhcsr(1)
    i = corestate.decode_icsr((1 << 26) | (1 << 22) | (3 << 12) | 15)
    assert "VECTACTIVE 15 (SysTick)" in i and "VECTPENDING 3 (HardFault)" in i and "PENDSTSET" in i
    assert "ISRPENDING" in i
    assert "IRQ 5" in corestate.decode_icsr(21) and "thread mode" in corestate.decode_icsr(0)
    assert "unnamed bits" in corestate.decode_icsr(1 << 30)


def test_samples_become_a_histogram_of_functions_with_the_flash_checked(tmp_path, monkeypatch):
    ad = setup(tmp_path, monkeypatch, state())
    out = corestate.core_state(ad)
    assert out.startswith("src: embarch-core core-state + b zephyr.elf (flash matches the ELF at 3 places)")
    assert "3 x  75%  spin_wait" in out and "1 x  25%  main_loop" in out
    assert "spin_wait+0x4 src/wait.c:12 (definition line)" in out
    assert "1 sample(s) empty" in out and "retired in between" in out and "running" in out


def test_halted_registers_resolve_pc_and_lr_and_an_exc_return_lr_is_not_resolved(tmp_path, monkeypatch):
    regs = {"r": [0] * 13, "sp": 0x2000_0100, "lr": 0x0800_0121, "pc": 0x0800_0208, "xpsr": 0x0100_0000,
            "control": 0, "primask": 0}
    ad = setup(tmp_path, monkeypatch, state(pc_samples=[], registers=regs, found="running", left="running",
                                            halt_count=1, halted_us=900, scb_icsr={"raw": 3}))
    out = corestate.core_state(ad, halt=True)
    assert "PC  0x08000208  spin_wait+0x8" in out and "LR  0x08000121  main_loop+0x1e" in out
    assert "VECTACTIVE 3 (HardFault)" in out
    assert "--halt" in json.loads((ad / "calls.jsonl").read_text().splitlines()[0])
    regs["lr"] = 0xFFFF_FFF9
    (ad / "state.json").write_text(json.dumps(state(pc_samples=[], registers=regs)))
    assert "EXC_RETURN to thread mode, MSP" in corestate.core_state(ad, halt=True)


def test_a_flash_that_is_not_this_elf_refuses_by_name_and_leaves_addresses_raw(tmp_path, monkeypatch):
    ad = setup(tmp_path, monkeypatch, state(), flash_ok=False)
    out = corestate.core_state(ad)
    assert "Refused to name symbols" in out and "flash differs from the ELF" in out and "build other than b" in out
    assert "0x08000204 x3" in out and "spin_wait" not in out
    assert "DHCSR 0x03010001" in out          # the bit names need no ELF


def test_a_halted_core_with_no_samples_does_not_touch_the_flash(tmp_path, monkeypatch):
    ad = setup(tmp_path, monkeypatch, state(found="halted", left="halted", pc_samples=[],
                                            pc_note="the core is halted: use halt=true", dhcsr=dhcsr(1 << 17)))
    out = corestate.core_state(ad)
    assert "halted" in out and "use halt=true" in out
    assert all(c[0] == "core-state" for c in map(json.loads, (ad / "calls.jsonl").read_text().splitlines()))


def test_no_elf_is_a_named_refusal_not_a_crash(tmp_path, monkeypatch):
    ad = setup(tmp_path, monkeypatch, state())
    monkeypatch.setattr(buildq, "source", lambda *a: (_ for _ in ()).throw(LookupError("no atlas")))
    out = corestate.core_state(ad)
    assert "Refused to name symbols: no ELF to check the flash against (no atlas)" in out and "0x08000204" in out


def test_another_chip_is_refused_before_anything_is_decoded(tmp_path, monkeypatch):
    ad = setup(tmp_path, monkeypatch, state(chip="XYZ700"))
    from test_live import set_chips
    set_chips(ad, "ABC200", "XYZ700")
    out = corestate.core_state(ad)
    assert out.startswith("src: none") and "XYZ700" in out and "ABC200" in out


def test_addr2line_output_is_parsed_and_used_for_the_exact_line():
    assert corestate.parse_addr2line("0x08000204\nspin_wait\nsrc/wait.c:15\n0x1\n??\n??:0\n", 2) == [
        ("spin_wait", "src/wait.c:15"), None]

    def run(cmd, **kw):
        assert cmd[0] == "a2l" and "0x08000204" in cmd
        return subprocess.CompletedProcess(cmd, 0, "0x08000204\nspin_wait\nsrc/wait.c:15 (discriminator 2)\n", "")
    mp = pytest.MonkeyPatch()
    mp.setattr(corestate, "_addr2line_of", lambda t: "a2l")
    try:
        r = corestate.resolve([0x0800_0204], {"symbols": SYMS}, Path("z.elf"), {}, run=run)
    finally:
        mp.undo()
    assert r[0x0800_0204] == {"fn": "spin_wait", "where": "src/wait.c:15", "exact": True, "off": 4}


def test_the_cli_verb_and_mcp_tool_exist(tmp_path, monkeypatch, capsys):
    ad = setup(tmp_path, monkeypatch, state())
    from embarch_atlas import cli
    assert cli.main(["--atlas-dir", str(ad), "live", "core-state", "--samples", "5"]) == 0
    assert "spin_wait" in capsys.readouterr().out
    assert json.loads((ad / "calls.jsonl").read_text().splitlines()[0])[:5] == [
        "core-state", "--role", "dut", "--samples", "5"]

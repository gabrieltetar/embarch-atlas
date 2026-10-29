"""footprint and stacks over canned objdump/nm output (invented sections, symbols and files)."""
import hashlib
from pathlib import Path

import pytest

from embarch_atlas import footprint as F, query
from embarch_atlas.common import write_json, write_text

OBJDUMP_H = """
zephyr.elf:     file format elf32-littlearm

Sections:
Idx Name          Size      VMA       LMA       File off  Algn
  0 text          00001000  08000000  08000000  000000f4  2**2
                  CONTENTS, ALLOC, LOAD, READONLY, CODE
  1 rodata        00000400  08001000  08001000  000010f4  2**2
                  CONTENTS, ALLOC, LOAD, READONLY, DATA
  2 tbss          00000008  08001400  08001400  000014f4  2**2
                  ALLOC, THREAD_LOCAL
  3 datas         00000100  20000000  08001400  000014f4  2**2
                  CONTENTS, ALLOC, LOAD, DATA
  4 bss           00000200  20000100  20000100  000015f4  2**3
                  ALLOC
  5 noinit        00000c00  20000300  20000300  000015f4  2**3
                  ALLOC
  6 .debug_info   00009999  00000000  00000000  000015f4  2**0
                  CONTENTS, READONLY, DEBUGGING, OCTETS
"""

NM = "\n".join([
    "08000000 00000800 T main_loop\t/w/fw/app/src/main.c:10",
    "08000800 00000400 T k_sched\t/w/zephyr/kernel/sched.c:5",
    "08000c00 00000100 t memcpy",
    "08001000 00000200 R table\t/w/fw/app/src/main.c:3",
    "20000000 00000080 D counter\t/w/fw/app/src/main.c:4",
    "20000100 00000100 b buf\t/w/zephyr/kernel/sched.c:9",
    "20000300 00000800 b demo_workq_stack\t/w/fw/drivers/demo/demo.c:20",
    "20000b00 00000400 B z_main_stack\t/w/zephyr/kernel/init.c:55",
])

REL = [("/w/fw", "fw"), ("/w/zephyr", "zephyr")]


def _rel(p):
    for base, tag in REL:
        if p.startswith(base + "/"):
            return f"{tag}:{p[len(base) + 1:]}"
    return p


def _img(sections=OBJDUMP_H, nm=NM):
    syms = F.parse_symbols(nm)
    for s in syms:
        if s["f"]:
            s["f"] = _rel(s["f"])
    return {"sections": F.parse_sections(sections), "symbols": syms}


DT = {"chosen": {"zephyr,flash": "/flash@8000000", "zephyr,sram": "/memory@20000000",
                 "zephyr,code-partition": "/flash@8000000/partitions/partition@c000"},
      "nodes": {"/flash@8000000": {"props": {"reg": {"val": [0x08000000, 0x10000]}}},
                "/memory@20000000": {"props": {"reg": {"val": [0x20000000, 0x4000]}}},
                "/flash@8000000/partitions/partition@c000": {"props": {"reg": {"val": [0xc000, 0x1800]}}}}}


def test_sections_and_totals_count_data_in_both_and_skip_tbss_and_debug():
    t = F.totals(_img())
    assert t["rom"] == 0x1000 + 0x400 + 0x100
    assert t["ram"] == 0x100 + 0x200 + 0xc00


def test_footprint_answer_capacities_and_largest_by_dir():
    out = F.footprint_answer(_img(), DT, "dir")
    assert "flash image 5376 B" in out
    assert "code partition 6144 B (6.0 KiB): 87.5% used, 768 B left" in out
    assert "largest by dir, flash" in out and "fw:app/src" in out and "(no debug file" in out
    by_sym = F.footprint_answer(_img(), DT, "symbol")
    assert "2048 B" in by_sym and "main_loop" in by_sym


def test_footprint_diff_against_another_build():
    nm2 = NM.replace("08000000 00000800 T main_loop", "08000000 00000600 T main_loop")
    secs2 = OBJDUMP_H.replace("0 text          00001000", "0 text          00000e00")
    out = F.footprint_answer(_img(), DT, "file", vs=_img(secs2, nm2), vs_label="old")
    assert "against old: flash image +512 B, RAM 0 B" in out
    assert "+512       0  fw:app/src/main.c" in out


def test_stacks_pairs_by_kconfig_dir_then_name_word():
    dotconfig = {"MAIN_STACK_SIZE": "1024", "WORKQ_THING_STACK_SIZE": "1024", "DEMO_WORKQUEUE_STACK_SIZE": "2048",
                 "OTHER_STACK_SIZE": "512"}
    ksyms = {"DEMO_WORKQUEUE_STACK_SIZE": {"at": ["fw:drivers/demo/Kconfig:3"]},
             "MAIN_STACK_SIZE": {"at": ["zephyr:kernel/Kconfig:160"]},
             "WORKQ_THING_STACK_SIZE": {"at": ["zephyr:kernel/Kconfig:200"]}}
    out = F.stacks_answer(_img(), dotconfig, ksyms)
    assert "2 stacks, 3072 B" in out
    assert "demo_workq_stack" in out and "CONFIG_DEMO_WORKQUEUE_STACK_SIZE (same size, defined beside it" in out
    assert "CONFIG_MAIN_STACK_SIZE (same size, defined beside it, name word" in out
    assert "CONFIG_OTHER_STACK_SIZE=512" in out
    no_kconfig = F.stacks_answer(_img(), dotconfig, None)
    assert "CONFIG_DEMO_WORKQUEUE_STACK_SIZE (same size, name word" in no_kconfig


@pytest.fixture()
def atlas_dir(tmp_path: Path) -> Path:
    a = tmp_path / "fw" / "embarch" / "atlas"
    at = a / "atlases" / "core@000000000004"
    write_json(at / "atlas.json", {"id": "core@000000000004", "commit": "0" * 40, "build": {}})
    write_text(at / "build" / ".config", "CONFIG_MAIN_STACK_SIZE=1024\n")
    elf = at / "build" / "zephyr.elf"
    elf.parent.mkdir(parents=True, exist_ok=True)
    elf.write_bytes(b"\x7fELF not really")
    key = hashlib.sha256(elf.read_bytes()).hexdigest()[:16]
    write_json(a / "cache" / "footprint" / f"{key}.json", {"elf_sha256": key, **_img()})
    write_json(at / "dt.json", DT)
    write_json(a / "current.json", {"matches": [{"atlas": "core@000000000004"}]})
    return a


def test_footprint_and_stacks_through_query(atlas_dir):
    a = query.Atlas(atlas_dir)
    out = query.footprint(a)
    assert out.startswith("src: atlas core@000000000004 zephyr.elf by=dir")
    assert "87.5% used" in out
    st = query.stacks(a)
    assert "z_main_stack" in st and "CONFIG_MAIN_STACK_SIZE" in st
    assert query.footprint(a, build="nowhere").startswith("src: none")


def test_footprint_and_stacks_page_to_the_uncut_answer(atlas_dir, monkeypatch):
    from paging_synth import assert_exact

    a = query.Atlas(atlas_dir)
    assert_exact(monkeypatch, "footprint", lambda o: query.footprint(a, offset=o))
    assert_exact(monkeypatch, "stacks", lambda o: query.stacks(a, offset=o))

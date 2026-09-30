"""mcu rm-outline / rm-sections / rm-text / rm-check / rm-freeze on a synthetic reference manual.

Everything here is invented: device DEMOM2 (a family member of DEMOM1), document demo-rm@1.
`demo_atlas()` is shared with test_vote.py.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from embarch_atlas import cli, rm, svd
from embarch_atlas.common import read_json, write_json

N = 9
HEAD_EVEN = "{title:<60}DEMO-RM"
HEAD_ODD = "DEMO-RM{title:>60}"


def _page(p: int, title: str | None, body: str) -> str:
    head = ""
    if title:
        head = (HEAD_ODD if p % 2 else HEAD_EVEN).format(title=title) + "\n\n\n"
    foot = f"\n\n\n{'':>30}DEMO-RM Rev 1{'':>20}{p}/{N}\n"
    return head + body.strip("\n") + "\n" + foot


PAGES = [
    _page(1, None, """
                     DEMO-RM Reference manual
                     Demo family of microcontrollers
"""),
    _page(2, "Documentation conventions", """
1        Documentation conventions

1.1      Availability of peripherals
         The following table shows which peripherals each device has.
                            Table 1. Peripherals versus devices
                      DEMO      DEMO
     Feature
                       M1        M2

      CRC              X         -
      AUX              -         X
  Wide name
                       X         X
  on two lines
1. Basic configuration
"""),
    _page(3, "Universal widget (UART)", """
2        Universal widget (UART)

2.1      UART introduction
         The UART moves bytes between the core and a serial line. EN must be set first.
2.2      UART functional description
         Overview of the functional description.
2.2.1    UART transmitter
         The transmitter sends data when EN = 1 and MODE = 0.
"""),
    _page(4, "Universal widget (UART)", """
2.2.2    UART receiver
         The receiver samples at 16 times the baud rate.

2.3      UART registers
2.3.1    UART control register (UART_CR)
         Address offset: 0x00
         Reset value: 0x0000 0000

  31     30     29     28     27     26     25     24     23     22     21     20     19     18     17     16
 Res.   Res.   Res.   Res.   Res.   Res.   Res.   Res.   Res.   Res.   Res.   Res.   Res.   Res.   Res.   Res.
  15     14     13     12     11     10      9      8      7      6      5      4      3      2      1      0
 Res.   Res.   Res.   Res.   Res.   Res.   Res.   Res.   Res.   Res.  CRCEN  AUXEN     MODE[2:0]         EN
                                                                       rw     rw     rw     rw     rw     rw

       Bits 31:6 Reserved, must be kept at reset value.
          Bit 5 CRCEN: CRC unit clock enable
                This bit connects the CRC unit.
          Bit 4 AUXEN: Auxiliary enable
                Note: This bit is only available in DEMOM1xx devices, reserved otherwise.
       Bits 3:1 MODE[2:0]: Mode selection
                000: normal
                001: loopback
          Bit 0 EN: UART enable
                This bit can be set only when MODE = 0.
                Note: This bit is cleared by hardware on a framing error.

2.3.2    UART status register (UART_SR)
         Address offset: 0x04
         Reset value: 0x0000 0001

  15     14     13     12     11     10      9      8      7      6      5      4      3      2      1      0
 Res.   Res.   Res.   Res.   Res.   Res.   Res.   Res.   Res.   Res.   Res.   Res.   Res.   PARF   RXNE    TXE
                                                                                              r      r      r

       Bits 31:3 Reserved, must be kept at reset value.
          Bit 2 PARF: Parity error flag
          Bit 1 RXNE: Receive data register not empty
          Bit 0 TXE: Transmit data register empty
"""),
    _page(5, "Universal widget (UART)", """
2.3.3    UART data register x (UART_DATAx)
         Address offset: 0x10 + 0x4 * x, (x = 0 to 1)
         Reset value: 0x0000 0000

  15     14     13     12     11     10      9      8      7      6      5      4      3      2      1      0
 Res.   Res.   Res.   Res.   Res.   Res.   Res.   Res.                     DATA[7:0]
                                                        rw     rw     rw     rw     rw     rw     rw     rw

       Bits 31:8 Reserved, must be kept at reset value.
       Bits 7:0 DATA[7:0]: Data
                Writing is not allowed while TXE = 0.

2.3.4    UART baud register (UART_BRR)
         Address offset: 0x20
         Reset value: 0x0000 0000
       Bits 31:16 Reserved, must be kept at reset value.
       Bits 15:0 DIV[15:0]: Divider

2.3.5    UART register map
         Offset  Register
"""),
    _page(6, "Reset and clock control (RCC)", """
3        Reset and clock control (RCC)

3.1      RCC introduction
         The RCC gates every peripheral clock. The reset value of the clock tree is 8 MHz.
"""),
    _page(7, "Timer (TMR)", """
4        Timer (TMR)

4.1      TMR introduction
         A timer that nothing here uses.
"""),
    _page(8, "Checksum unit (CRC)", """
5        Checksum unit (CRC)

5.1      CRC registers
5.1.1    CRC data register (CRC_DR)
         Address offset: 0x00
         Reset value: 0xFFFF FFFF
       Bits 31:0 DR[31:0]: Data
"""),
    _page(9, "Auxiliary block (AUX)", """
6        Auxiliary block (AUX)

6.1      AUX registers
6.1.1    AUX control register (AUX_CR)
         Address offset: 0x00
         Reset value: 0x0000 0000
       Bits 31:1 Reserved, must be kept at reset value.
          Bit 0 ON: Auxiliary on
"""),
]

TOC = [
    [1, "1 Documentation conventions", 2], [2, "1.1 Availability of peripherals", 2],
    [3, "Table 1. Peripherals versus devices", 2],
    [1, "2 Universal widget (UART)", 3], [2, "2.1 UART introduction", 3],
    [2, "2.2 UART functional description", 3], [3, "2.2.1 UART transmitter", 3],
    [3, "Figure 1. Transmitter", 3], [3, "2.2.2 UART receiver", 4],
    [2, "2.3 UART registers", 4], [3, "2.3.1 UART control register (UART_CR)", 4],
    [3, "2.3.2 UART status register (UART_SR)", 4], [3, "2.3.3 UART data register x (UART_DATAx)", 5],
    [3, "2.3.4 UART baud register (UART_BRR)", 5], [3, "2.3.5 UART register map", 5],
    [1, "3 Reset and clock control (RCC)", 6], [2, "3.1 RCC introduction", 6],
    [1, "4 Timer (TMR)", 7], [2, "4.1 TMR introduction", 7],
    [1, "5 Checksum unit (CRC)", 8], [2, "5.1 CRC registers", 8], [3, "5.1.1 CRC data register (CRC_DR)", 8],
    [1, "6 Auxiliary block (AUX)", 9], [2, "6.1 AUX registers", 9], [3, "6.1.1 AUX control register (AUX_CR)", 9],
]

SVD_XML = """<?xml version="1.0" encoding="utf-8"?>
<device schemaVersion="1.1">
  <name>DEMOM2</name><version>1.0</version><width>32</width>
  <size>0x20</size><resetValue>0x0</resetValue><resetMask>0xFFFFFFFF</resetMask><access>read-write</access>
  <peripherals>
    <peripheral>
      <name>UART1</name><groupName>UART</groupName><baseAddress>0x40001000</baseAddress>
      <addressBlock><offset>0x0</offset><size>0x400</size><usage>registers</usage></addressBlock>
      <interrupt><name>UART1</name><value>5</value></interrupt>
      <registers>
        <register><name>UART_CR</name><addressOffset>0x0</addressOffset>
          <fields>
            <field><name>EN</name><bitOffset>0</bitOffset><bitWidth>1</bitWidth></field>
            <field><name>MODE</name><bitOffset>1</bitOffset><bitWidth>3</bitWidth></field>
          </fields></register>
        <register><name>UART_SR</name><addressOffset>0x4</addressOffset><access>read-only</access>
          <resetValue>0x1</resetValue>
          <fields>
            <field><name>TXE</name><bitOffset>0</bitOffset><bitWidth>1</bitWidth></field>
            <field><name>RXNEF</name><bitOffset>1</bitOffset><bitWidth>1</bitWidth></field>
          </fields></register>
        <register><name>UART_DATA%s</name><dim>2</dim><dimIncrement>4</dimIncrement>
          <addressOffset>0x10</addressOffset>
          <fields><field><name>DATA</name><bitOffset>0</bitOffset><bitWidth>8</bitWidth></field></fields></register>
      </registers>
    </peripheral>
    <peripheral derivedFrom="UART1">
      <name>UART2</name><baseAddress>0x40001400</baseAddress>
      <interrupt><name>UART2</name><value>6</value></interrupt>
    </peripheral>
    <peripheral>
      <name>RCC</name><groupName>RCC</groupName><baseAddress>0x40002000</baseAddress>
      <registers><register><name>CR</name><addressOffset>0x0</addressOffset>
        <fields><field><name>ON</name><bitOffset>0</bitOffset><bitWidth>1</bitWidth></field></fields>
      </register></registers>
    </peripheral>
    <peripheral>
      <name>TMR</name><groupName>TMR</groupName><baseAddress>0x40003000</baseAddress>
      <registers><register><name>CNT</name><addressOffset>0x0</addressOffset><size>16</size></register></registers>
    </peripheral>
  </peripherals>
</device>
"""

HEADER = """
#define PERIPH_BASE           (0x40000000UL)
#define UART1_BASE            (PERIPH_BASE + 0x00001000UL)
#define AUX_BASE              (PERIPH_BASE + 0x00006000UL)
#define UART1               ((UART_TypeDef *) UART1_BASE)
#define UART_SR_PARF_Pos      (2U)
#define UART_SR_PARF_Msk      (0x1UL << UART_SR_PARF_Pos)
#define UART_SR_RXNE_Pos      (1U)
#define UART_SR_RXNE_Msk      (0x1UL << UART_SR_RXNE_Pos)
#define UART_BRR_DIV_Pos      (0U)
#define UART_BRR_DIV_Msk      (0xFFFFUL << UART_BRR_DIV_Pos)
typedef enum {
  UART1_IRQn                 = 5,      /*!< UART1 interrupt */
} IRQn_Type;
"""

IN_USE = {"peripherals": {
    "UART1": {"dt_okay": True, "instantiated": True, "touched_by_code": False, "compatible": "demo,uart"},
    "UART2": {"dt_okay": True, "instantiated": False, "touched_by_code": False, "compatible": "demo,uart"},
    "TMR": {"dt_okay": False, "instantiated": False, "touched_by_code": False, "compatible": ""},
    "NVIC": {"dt_okay": True, "instantiated": False, "touched_by_code": True, "compatible": "arm,nvic"},
}}


def demo_rmdoc(pages: list[str] | None = None, root: Path | None = None) -> rm.RmDoc:
    pages = pages or PAGES
    layer = rm.TextLayer(pages=pages)
    plain = [rm.tokens(" ".join(p.split())) for p in pages]
    o = rm.build_outline(TOC, plain, layer, "demo-rm@1", len(pages))
    o["page_sha"] = [rm.page_sha(layer(p)) for p in range(1, len(pages) + 1)]
    return rm.RmDoc("demo-rm@1", o, layer, root)


def demo_atlas(tmp: Path) -> Path:
    """An atlas dir with the SVD doc, the RM outline and its pages/ text layer, an in_use.json and a header."""
    root = tmp / "atlas"
    (tmp / "demo.svd").write_text(SVD_XML)
    svd.write_svd_doc(tmp / "demo.svd", "demo-svd", root)
    d = root / "docs" / "demo-rm@1"
    for p, text in enumerate(PAGES, 1):
        (d / "pages").mkdir(parents=True, exist_ok=True)
        (d / "pages" / f"p{p:03d}.txt").write_text(text)
    o = demo_rmdoc().o
    write_json(d / "outline.json", o)
    write_json(tmp / "atl" / "in_use.json", IN_USE)
    (tmp / "demo.h").write_text(HEADER)
    return root


# --------------------------------------------------------------------------- outline

def test_outline_sections_pages_and_register_flags():
    r = demo_rmdoc()
    o = r.o
    assert o["pages"] == N and o["toc_entries"] == len(TOC)
    assert [t["title"] for t in o["tables"]] == ["Table 1. Peripherals versus devices", "Figure 1. Transmitter"]
    by = r.by_id
    assert [c["id"] for c in r.chapters()] == ["1", "2", "3", "4", "5", "6"]
    assert by["2"]["abbrevs"] == ["UART"] and by["3"]["abbrevs"] == ["RCC"]
    assert (by["2"]["start"], by["2"]["end"]) == (3, 5)          # next chapter heads page 6
    assert (by["2.1"]["start"], by["2.1"]["end"]) == (3, 3)      # 2.2 starts mid-page 3
    assert (by["2.2.1"]["start"], by["2.2.1"]["end"]) == (3, 3)  # 2.2.2 heads page 4
    assert (by["2.2"]["start"], by["2.2"]["end"]) == (3, 4)
    assert by["2.3"].get("register") and by["2.3.1"].get("register") and not by["2.2"].get("register")
    assert by["2.3.1"]["regname"] == "UART_CR" and by["2.3.3"]["regname"] == "UART_DATAx"
    assert by["2.2.2"]["parent"] == "2.2" and by["2.2.2"]["chapter"] == "2"


def test_strip_running_header_and_footer():
    body = rm.strip_running(PAGES[3], "DEMO-RM", N)
    assert not any("DEMO-RM" in ln for ln in body)
    assert next(ln for ln in body if ln.strip()).strip().startswith("2.2.2")


def test_page_hash_ignores_form_feed_and_trailing_blanks():
    assert rm.page_sha("a\nb  \n\n\f") == rm.page_sha("a\nb")
    assert rm.page_sha("a\nb") != rm.page_sha("a\nc")


def test_map_chapter_by_group_derived_and_title():
    r = demo_rmdoc()
    s = svd.SvdDoc("demo-svd", {"device": "DEMOM2", "peripherals": {
        "UART1": {"group": "UART", "derived_from": None}, "UART2": {"group": None, "derived_from": "UART1"},
        "RCC": {"group": "RCC", "derived_from": None}, "TMR": {"group": "TMR", "derived_from": None}}}, [], [])
    assert rm.map_chapter("UART1", r, s)[0] == "2"
    assert rm.map_chapter("UART2", r, s)[0] == "2"
    assert rm.map_chapter("TMR", r, s)[0] == "4"
    assert rm.map_chapter("RCC", r, None)[0] == "3"
    assert rm.map_chapter("NVIC", r, s) == (None, None)


def test_section_text_is_cut_at_headings_with_page_markers():
    r = demo_rmdoc()
    txt = rm.render_lines(r.unit_lines({"id": "2.2", "pages": [3, 4]}))
    assert txt.startswith("[p.3]\n2.2")
    assert "UART transmitter" in txt and "UART receiver" in txt and "[p.4]" in txt
    assert "UART registers" not in txt and "UART introduction" not in txt
    intro = rm.render_lines(r.unit_lines({"id": "2.2", "pages": [3, 3], "intro": True}))
    assert "Overview" in intro and "transmitter" not in intro


# --------------------------------------------------------------------------- worklist

def _svd_doc(tmp: Path) -> svd.SvdDoc:
    (tmp / "demo.svd").write_text(SVD_XML)
    svd.write_svd_doc(tmp / "demo.svd", "demo-svd", tmp / "a")
    return svd.SvdDoc.load(tmp / "a", "demo-svd")


def test_worklist_in_use_plus_core_without_register_sections(tmp_path):
    r = demo_rmdoc()
    wl = rm.build_worklist(r, IN_USE, _svd_doc(tmp_path), core=["RCC"])
    chs = {c["chapter"]: c for c in wl["chapters"]}
    assert set(chs) == {"2", "3"}                     # UART1 instantiated, RCC core; TMR off, UART2 dt-only
    assert [u["id"] for u in chs["2"]["units"]] == ["2.1", "2.2"]
    assert [x["id"] for x in wl["excluded"]] == ["2.3"]
    assert wl["not_in_use"][0]["peripheral"] == "UART2" and wl["not_in_use"][0]["chapter"] == "2"
    assert wl["unmapped"] == ["NVIC"]
    assert wl["peripherals"]["TMR"]["state"] == "off"
    assert chs["2"]["prose_tokens"] == chs["2"]["tokens"] - chs["2"]["register_tokens"]
    assert all(u["status"] == "todo" for c in wl["chapters"] for u in c["units"])
    assert sum(len(i["sections"]) for i in wl["items"]) == wl["totals"]["units"] == 3


def test_large_sections_split_and_small_ones_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(rm, "SPLIT_TOKENS", 5)
    r = demo_rmdoc()
    wl = rm.build_worklist(r, IN_USE, _svd_doc(tmp_path), core=[])
    units = [(u["id"], bool(u.get("intro"))) for u in wl["chapters"][0]["units"]]
    assert units == [("2.1", False), ("2.2", True), ("2.2.1", False), ("2.2.2", False)]
    monkeypatch.setattr(rm, "MERGE_TOKENS", 10_000)
    wl = rm.build_worklist(r, IN_USE, _svd_doc(tmp_path), core=[])
    assert [i["id"] for i in wl["items"]] == ["2.1-2.2.2"]


def test_unit_keys_follow_page_content_only():
    a = demo_rmdoc()
    pages = list(PAGES)
    pages[3] = pages[3].replace("16 times", "8 times")        # page 4 only
    b = demo_rmdoc(pages)
    k = lambda r, u: rm.unit_key(r, u)
    assert k(a, {"id": "2.1", "pages": [3, 3]}) == k(b, {"id": "2.1", "pages": [3, 3]})
    assert k(a, {"id": "2.2", "pages": [3, 4]}) != k(b, {"id": "2.2", "pages": [3, 4]})
    # doc-independent: the same pages under another doc id give the same key
    c = rm.RmDoc("other-rm@2", a.o, a.layer)
    assert k(a, {"id": "2.1", "pages": [3, 3]}) == k(c, {"id": "2.1", "pages": [3, 3]})


# --------------------------------------------------------------------------- CLI: sections, text, freeze, check

def _run(*argv) -> int:
    return cli.main([str(a) for a in argv])


def test_cli_sections_freeze_reuse(tmp_path, capsys):
    root = demo_atlas(tmp_path)
    assert _run("--atlas-dir", root, "mcu", "rm-sections", tmp_path / "atl", "--core", "RCC") == 0
    wl = read_json(root / "docs" / "demo-rm@1" / "worklist.json")
    assert wl["totals"]["todo_units"] == 3 and wl["in_use"].endswith("in_use.json")
    capsys.readouterr()
    assert _run("--atlas-dir", root, "mcu", "rm-text", "demo-rm@1", "2.1") == 0
    assert "UART introduction" in capsys.readouterr().out
    (root / "docs" / "demo-rm@1" / "s").mkdir(exist_ok=True)
    (root / "docs" / "demo-rm@1" / "s" / "2.1.md").write_text("- `UART_CR.EN` must be set first [§2.1 p.3]\n")
    assert _run("--atlas-dir", root, "mcu", "rm-freeze", "demo-rm@1", "--sections", "2.1",
                "--files", "s/2.1.md", "--model", "demo-model") == 0
    entries = list((root / "cache" / "llm").glob("*.json"))
    assert len(entries) == 1 and read_json(entries[0])["sections"].keys() == {"2.1"}
    assert _run("--atlas-dir", root, "mcu", "rm-sections", tmp_path / "atl", "--core", "RCC") == 0
    wl = read_json(root / "docs" / "demo-rm@1" / "worklist.json")
    st = {u["id"]: u["status"] for c in wl["chapters"] for u in c["units"]}
    assert st == {"2.1": "frozen", "2.2": "todo", "3.1": "todo"}
    assert all("2.1" not in i["sections"] for i in wl["items"])
    # an edited frozen file is flagged, not silently reused
    (root / "docs" / "demo-rm@1" / "s" / "2.1.md").write_text("edited\n")
    _run("--atlas-dir", root, "mcu", "rm-sections", tmp_path / "atl", "--core", "RCC")
    wl = read_json(root / "docs" / "demo-rm@1" / "worklist.json")
    assert {u["id"]: u["status"] for c in wl["chapters"] for u in c["units"]}["2.1"] == "frozen-files-changed"


def test_rm_check_flags_names_pages_sections_and_numbers(tmp_path, capsys):
    root = demo_atlas(tmp_path)
    s = root / "docs" / "demo-rm@1" / "s"
    s.mkdir(exist_ok=True)
    (s / "2.1-2.2.md").write_text("\n".join([
        "- Set `UART_CR.EN` first [§2.1 p.3]",               # ok
        "- `UART_CR.BOGUS` is made up [§2.1 p.3]",          # not in the SVD
        "- `UART_SR.TXE` is described here [§2.1 p.3]",     # field not on the cited page
        "- samples at 16 times [§2.2.2 p.4]",               # number on page
        "- samples at 99 times [§2.2.2 p.4]",               # number not on page
        "- wrong section [§2.1 p.4]",                       # 2.1 is page 3 only
        "- 77 (derived) [§2.1 p.3]",                        # tagged: numbers not checked
        "- see `worklist.json` [§2.1 p.3]",                 # a file name, not a register
    ]) + "\n")
    rc = _run("--atlas-dir", root, "mcu", "rm-check", "demo-rm@1")
    out = capsys.readouterr().out
    assert rc == 1
    tot = json.loads(out.splitlines()[0].split(": ", 1)[1])
    assert tot["names_bad"] == 1 and tot["support_bad"] == 2 and tot["sec_bad"] == 1 and tot["numbers_bad"] == 1
    assert "BOGUS" in out and "99" in out and "77" not in out
    rep = read_json(root / "docs" / "demo-rm@1" / "rm_checks.json")
    assert len(rep["files"]["s/2.1-2.2.md"]["fails"]) == 5


def test_rm_check_tags_per_clause_svd_cites_and_not_claims(tmp_path, capsys):
    root = demo_atlas(tmp_path)
    s = root / "docs" / "demo-rm@1" / "s"
    s.mkdir(exist_ok=True)
    (s / "2.2.md").write_text("\n".join([
        "- margin 44 (derived); samples at 99 times [§2.2.2 p.4]",       # the tag covers 44 only: 99 fails
        "- samples at 16 times [§2.2.2 p.4]; the UART1 IRQ is 55 [SVD]",  # 55 is in the [SVD] clause
        "7. Set `UART_CR.EN` first [§2.1 p.3]",                           # a list marker is not a claim
        "- `UART_CR.MODE` = 0b001 is loopback, 0b111 is not [§2.3.1 p.4]",  # 0b001 is printed as 001:
        "- 77x oversampling, 16x in the RM [§2.2.2 p.4]",                 # digits glued to a letter count
        "- see `s/2.2-2.3.md`, 1,234 tokens, Tier 2, `UART1` [§2.2.2 p.4]",  # file name, tokens, tier label
        "- `UART_SR` is read, `UART_CR9` is made up, `LINE0_1` is an IRQ name [§2.3.2 p.4]",
    ]) + "\n")
    rc = _run("--atlas-dir", root, "mcu", "rm-check", "demo-rm@1", s / "2.2.md")
    out = capsys.readouterr().out
    assert rc == 1
    fails = [ln.strip() for ln in out.splitlines() if ln.startswith("  ")]
    assert fails == ["s/2.2.md:1 number 99 not on cited page(s) [4]",
                     "s/2.2.md:4 number 0b111 not on cited page(s) [4]",
                     "s/2.2.md:5 number 77 not on cited page(s) [4]",
                     "s/2.2.md:7 `UART_CR9` not in the SVD (no such register of UART1, UART2)"], out
    tot = json.loads(out.splitlines()[0].split(": ", 1)[1])
    assert tot["numbers_tagged"] == 2                                   # 44 and 55


def test_number_claims_follow_the_m_clause_rule():
    c = rm.number_claims
    assert c("- 38 margin (derived) [§1 p.2]; 777 uA") == [("38", "derived"), ("777", None)]
    assert c("- 24 V [SVD]; 25 V [§1 p.2]") == [("24", "svd"), ("25", None)]
    assert c("4. Write 0x1F, 10ms, 8th [§1 p.2]") == [("0x1F", None), ("10", None), ("8", None)]
    assert c("- `LINE4_15`, `s/1.2-1.3.md`, Tier 1, 2,642 tokens, 1..15") == [("1", None), ("15", None)]
    assert rm.on_pages("8", "after the 8th pulse") and not rm.on_pages("8", "the 18th, 0x08, 8.5")
    assert rm.on_pages("0b10", "10: alternate") and rm.on_pages("0x1F", "reset 0x1f")


# --------------------------------------------------------------------------- cut: never drop pages

# A chapter whose second section spans a flowchart page (one lone word per line, far indented: what
# the old layout heuristic took for a rotated table) and ends where the next section heads a page;
# its register section ends on a rotated register-map page.
FLOW = "\n".join(" " * 46 + w for w in ["Start", "Init", "No", "FLAG", "=1?", "Yes", "Read", "DATA", "Clear",
                                        "FLAG", "No", "Last?", "Yes", "Stop", "End", "Wait", "Poll", "Again",
                                        "Done", "Exit", "FIG-0001"])
CUT_PAGES = [
    _page(1, "Widget (WDG)", "1        Widget (WDG)\n\n1.1      WDG overview\n         The widget counts ticks."),
    _page(2, "Widget (WDG)", "1.2      WDG start sequence\n         Start the widget as Figure 1 shows.\n"
                             "              Figure 1. Start sequence"),
    _page(3, "Widget (WDG)", FLOW),
    _page(4, "Widget (WDG)", "         Then poll FLAG until it reads 1; the count starts 12 ticks later."),
    _page(5, "Widget (WDG)", "1.3      WDG registers\n1.3.1    WDG control register (WDG_CR)\n"
                             "         Address offset: 0x00\n         Reset value: 0x0000 0000\n"
                             "       Bits 31:1 Reserved, must be kept at reset value.\n"
                             "          Bit 0 EN: Widget enable\n"
                             "                Note: EN is cleared by hardware on a fault."),
    _page(6, "Widget (WDG)", "\n".join(" " * 50 + w for w in ["Offset", "Register", "0x00", "WDG_CR", "Res.",
                                                                 "EN", "Reset", "value", "0", "1.3.2", "WDG",
                                                                 "register", "map", "Res.", "Res.", "Res.",
                                                                 "Res.", "Res.", "Res.", "Res.", "Res."])),
    _page(7, "Other (OTH)", "2        Other (OTH)\n\n2.1      OTH overview\n         Nothing."),
]
CUT_TOC = [[1, "1 Widget (WDG)", 1], [2, "1.1 WDG overview", 1], [2, "1.2 WDG start sequence", 2],
           [2, "1.3 WDG registers", 5], [3, "1.3.1 WDG control register (WDG_CR)", 5],
           [3, "1.3.2 WDG register map", 6], [1, "2 Other (OTH)", 7], [2, "2.1 OTH overview", 7]]


def _cut_rmdoc(vertical=None) -> rm.RmDoc:
    layer = rm.TextLayer(pages=CUT_PAGES)
    plain = [rm.tokens(" ".join(p.split())) for p in CUT_PAGES]
    o = rm.build_outline(CUT_TOC, plain, layer, "demo-rm@1", len(CUT_PAGES), vertical=vertical)
    return rm.RmDoc("demo-rm@1", o, layer)


def _pages(lines) -> list[int]:
    return sorted({p for p, _ in lines})


def test_cut_keeps_the_range_when_the_stop_heading_is_past_it():
    r = _cut_rmdoc(vertical=[0.0] * 7)
    e = r.by_id["1.2"]
    assert (e["start"], e["end"]) == (2, 4)                   # 1.3 heads page 5
    u = {"id": "1.2", "pages": [2, 4]}
    lines = r.unit_lines(u)
    assert _pages(lines) == [2, 3, 4] and rm.missing_pages(u, lines) == []
    assert "12 ticks later" in rm.render_lines(lines)          # the page after the flowchart is kept
    # the register parser's cut too: a flowchart is not a rotated table
    assert _pages(r.cut(e, r.next_after(e), 2, 4)) == [2, 3, 4]


def test_cut_at_a_rotated_stop_page_by_text_direction():
    r = _cut_rmdoc(vertical=[0.0, 0.0, 0.0, 0.0, 0.0, 0.62, 0.0])   # page 6 runs vertically
    cr = r.by_id["1.3.1"]
    assert (cr["start"], cr["end"]) == (5, 6)                 # the map's heading is not at the top of page 6
    assert r.rotated(6) and not r.rotated(3)
    assert _pages(r.cut(cr, r.next_after(cr), 5, 6)) == [5]    # the register parser stops before the map
    assert _pages(r.unit_lines({"id": "1.3.1", "pages": [5, 6]})) == [5, 6]   # rm-text keeps every page
    unknown = _cut_rmdoc(vertical=None)                        # no text direction known: never guess a cut
    assert unknown.rotated(6) is False
    assert _pages(unknown.cut(unknown.by_id["1.3.1"], unknown.by_id["1.3.2"], 5, 6)) == [5, 6]


def test_rm_text_fails_loudly_when_a_page_would_be_lost(tmp_path, capsys, monkeypatch):
    root = demo_atlas(tmp_path)
    real = rm.RmDoc.unit_lines
    monkeypatch.setattr(rm.RmDoc, "unit_lines", lambda self, u: [x for x in real(self, u) if x[0] != 4])
    with pytest.raises(SystemExit) as ex:
        _run("--atlas-dir", root, "mcu", "rm-text", "demo-rm@1", "2.2")
    assert "misses p.4 of its range p.3-4" in str(ex.value)
    assert capsys.readouterr().out == ""                        # nothing half-printed


# --------------------------------------------------------------------------- frozen entries sharing a unit key

def _entry(root: Path, name: str, sections: dict, files: dict, created: str, **kw) -> Path:
    f = root / "cache" / "llm" / name
    write_json(f, {"kind": "rm-distillation", "doc": "demo-rm@1", "procedure": rm.PROCEDURE, "model": "m",
                   "sections": sections, "pages": {}, "files": files, "created": created, **kw})
    return f


def test_frozen_index_prefers_current_files_then_newest_never_superseded(tmp_path):
    from embarch_atlas.common import sha256_file

    root = demo_atlas(tmp_path)
    s = root / "docs" / "demo-rm@1" / "s"
    s.mkdir(exist_ok=True)
    (s / "2.1.md").write_text("- `UART_CR.EN` must be set first [§2.1 p.3]\n")
    (s / "2.must.md").write_text("1. Set `UART_CR.EN` [§2.1 p.3]\n")
    r = rm.RmDoc.load(root, "demo-rm@1")
    key = rm.unit_key(r, {"id": "2.1", "pages": [3, 3]})
    good = {"s/2.1.md": sha256_file(s / "2.1.md")}
    # the must file changed after its freeze; its entry sorts last by name, as the stale one did
    _entry(root, "0000.json", {"2.1": key}, good, "2026-01-01T00:00:00Z")
    _entry(root, "ffff.json", {"2.1": key}, {"s/2.must.md": "0" * 64}, "2026-01-02T00:00:00Z")
    fz = rm.frozen_index(root, "demo-rm@1")[key]
    assert fz["entry"] == "0000.json" and fz["files_ok"] and fz["stale"] == {"ffff.json": ["s/2.must.md"]}
    # two current entries: the newest wins, whatever the names sort as
    _entry(root, "0001.json", {"2.1": key}, good, "2026-01-03T00:00:00.5Z")
    assert rm.frozen_index(root, "demo-rm@1")[key]["entry"] == "0001.json"
    # a superseded entry is never used
    rm.supersede(root / "cache" / "llm" / "0001.json", "0000.json", "test")
    assert rm.frozen_index(root, "demo-rm@1")[key]["entry"] == "0000.json"
    wl = rm.build_worklist(r, IN_USE, svd.SvdDoc.load(root, "demo-svd"), core=["RCC"], frozen=rm.frozen_index(root, "demo-rm@1"))
    ch2 = next(c for c in wl["chapters"] if c["chapter"] == "2")
    assert ch2["units"][0]["status"] == "frozen" and ch2["stale_entries"] == {"ffff.json": ["s/2.must.md"]}


def test_rm_freeze_supersedes_a_replaced_entry_and_retires_on_request(tmp_path, capsys):
    root = demo_atlas(tmp_path)
    s = root / "docs" / "demo-rm@1" / "s"
    s.mkdir(exist_ok=True)
    (s / "2.card.md").write_text("# UART card\n")
    args = ["--atlas-dir", root, "mcu", "rm-freeze", "demo-rm@1", "--sections", "2.1,2.2", "--files", "s/2.card.md"]
    assert _run(*args, "--model", "model-a") == 0
    first = list((root / "cache" / "llm").glob("*.json"))
    assert len(first) == 1
    (s / "2.card.md").write_text("# UART card, updated by another model\n")
    assert _run(*args, "--model", "model-b") == 0
    entries = {f.name: read_json(f) for f in (root / "cache" / "llm").glob("*.json")}
    assert len(entries) == 2                                     # the old entry is kept ...
    old = entries[first[0].name]
    new_name = next(n for n in entries if n != first[0].name)
    assert old["superseded"]["by"] == new_name                   # ... and marked
    assert not entries[new_name].get("superseded")
    idx = rm.frozen_index(root, "demo-rm@1")
    assert {v["entry"] for v in idx.values()} == {new_name}
    # --retire marks by key prefix; the file stays
    capsys.readouterr()
    assert _run("--atlas-dir", root, "mcu", "rm-freeze", "demo-rm@1", "--retire", new_name[:12]) == 0
    assert "marked, not deleted" in capsys.readouterr().out
    assert read_json(root / "cache" / "llm" / new_name)["superseded"]["by"] is None
    assert rm.frozen_index(root, "demo-rm@1") == {}
    with pytest.raises(SystemExit):
        _run("--atlas-dir", root, "mcu", "rm-freeze", "demo-rm@1", "--sections", "2.1")   # no --model


@pytest.mark.skipif(shutil.which("pdftotext") is None, reason="poppler pdftotext not installed")
def test_rm_outline_from_a_real_pdf(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    for text in PAGES:
        page = doc.new_page(width=842, height=1190)
        y = 40
        for line in text.splitlines():
            page.insert_text((20, y), line, fontname="cour", fontsize=7)
            y += 10
    doc.set_toc([[lvl, t, p] for lvl, t, p in TOC])
    pdf = tmp_path / "demo-rm.pdf"
    doc.save(pdf)
    root = tmp_path / "atlas"
    assert _run("--atlas-dir", root, "mcu", "rm-outline", "demo-rm@1", "--pdf", pdf) == 0
    o = read_json(root / "docs" / "demo-rm@1" / "outline.json")
    assert o["pages"] == N and len(o["page_sha"]) == N and len(o["page_tokens"]) == N
    assert [e["id"] for e in o["entries"] if e["level"] == 1] == ["1", "2", "3", "4", "5", "6"]
    by = {e["id"]: e for e in o["entries"]}
    assert (by["2"]["start"], by["2"]["end"]) == (3, 5)
    assert o["source"]["sha256"]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))


def test_rm_check_field_forms_and_svd_clauses(tmp_path, capsys):
    assert rm.field_on_page("TEIF1", "TEIFx is set by hardware on a transfer error")
    assert rm.field_on_page("CC2IF", "flag CCxIF is set")
    assert not rm.field_on_page("TEIF1", "HTIFx is set at half transfer")
    root = demo_atlas(tmp_path)
    s = root / "docs" / "demo-rm@1" / "s"
    s.mkdir(exist_ok=True)
    (s / "2.1.md").write_text("\n".join([
        "- `UART_SR.TXE` flags an empty buffer [SVD]; EN must be set first [§2.1 p.3]",   # the SVD clause names it
        "- `UART_SR.TXE` is described here [§2.1 p.3]",                                  # still needs the page
    ]) + "\n")
    _run("--atlas-dir", root, "mcu", "rm-check", "demo-rm@1", s / "2.1.md")
    fails = [ln.strip() for ln in capsys.readouterr().out.splitlines() if ln.startswith("  ")]
    assert fails == ["s/2.1.md:2 field TXE not on cited page(s) [3]"]


def test_number_claims_skip_table_and_figure_references():
    c = rm.number_claims
    assert c("- Table 192 lists the clear bits, Figures 7 and 8 the flows, 24 MHz max [§1 p.2]") == [("24", None)]

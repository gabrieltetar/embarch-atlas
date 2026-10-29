"""The seven atlas lookups over a small synthetic atlas (invented parts and nets)."""
import asyncio
import json
from pathlib import Path

import pytest

from embarch_atlas import query
from embarch_atlas.common import write_json, write_jsonl, write_text


@pytest.fixture()
def atlas_dir(tmp_path: Path) -> Path:
    a = tmp_path / "atlas"
    # a datasheet doc
    d = a / "docs" / "demo-bb@A"
    write_json(d / "doc.json", {"doc_id": "demo-bb@A", "part": "DEMO-BB", "doc_no": "DEMO001", "kind": "datasheet"})
    write_text(d / "card.md", "# DEMO-BB card\n\nBuck-boost. VOUT 0.8..20 V (p.1).\n\n## Quirks\n- OCP_MASK resets to 1 (p.9)\n")
    write_json(d / "toc.json", {"doc_id": "demo-bb@A", "sections": [
        {"id": "1", "title": "Features", "level": 1, "pages": [1, 1], "tokens": 300},
        {"id": "7", "title": "Registers", "level": 1, "pages": [8, 10], "tokens": 2000},
        {"id": "7.1", "title": "MODE Register", "level": 2, "pages": [9, 9], "tokens": 400}]})
    write_text(d / "s" / "registers.md", "# Registers\n\n| Addr | Name |\n|---|---|\n| 0x06 | MODE |\n")
    write_text(d / "pages" / "p009.txt", "MODE Register (Address = 06h) [reset = 00100000b]\n")
    write_text(d / "facts.txt", "\n".join([
        "demo-bb.ec.VOVP kind=limit; group=ec; sym=VOVP; param=output OVP threshold; min=22.5; typ=23.5; max=24.5; unit=V; cite=demo-bb@A p6; prov=llm-verified",
        "demo-bb.ec.VREF kind=limit; group=ec; sym=VREF; param=reference voltage; typ=1.2; unit=V; cite=demo-bb@A p5; prov=llm-verified",
    ]))
    reg = {"id": "demo-bb.reg.MODE", "kind": "register", "doc": "demo-bb@A", "name": "MODE", "addr": "0x06",
           "width": 8, "reset": "0b00100000", "access": "rw", "p": 9, "src": "table", "prov": "llm-verified"}
    f_llm = {"id": "demo-bb.reg.MODE.OE", "kind": "field", "doc": "demo-bb@A", "reg": "demo-bb.reg.MODE", "name": "OE",
             "bits": [7, 7], "reset": "0b0", "access": "rw", "p": 9, "src": "table", "prov": "llm-verified"}
    f_det = dict(f_llm, prov="deterministic")
    rule = {"id": "demo-bb.rule.ocp", "kind": "quirk", "doc": "demo-bb@A", "text": "OCP_MASK must be 0 before OE", "p": 9,
            "src": "text", "prov": "llm-verified", "refs": ["demo-bb.reg.MODE"]}
    write_jsonl(d / "records" / "register.jsonl", [reg])
    write_jsonl(d / "records" / "field.jsonl", [f_llm])
    write_jsonl(d / "records" / "regparse.jsonl", [f_det])
    write_jsonl(d / "records" / "quirk.jsonl", [rule])
    # an MCU SVD doc, records only (no doc.json), like the SVD module writes
    s = a / "docs" / "demo-mcu-svd"
    write_jsonl(s / "records" / "register.jsonl", [{"id": "demo-mcu.reg.I2C1.I2C_TIMINGR", "kind": "register",
                "doc": "demo-mcu-svd", "name": "I2C_TIMINGR", "addr": "0x40005410", "width": 32, "reset": "0x00000000",
                "src": "svd", "p": None, "prov": "deterministic"}])
    # board + atlas
    write_json(a / "boards" / "B" / "netlist.json", {"board": "B", "components": {
        "U3": {"mpn": "DEMO-BB-QFN", "pins": {"6": {"name": "SDA", "net": "SDA_1"}, "5": {"name": "SCL", "net": "SCL_1"}}}}})
    at = a / "atlases" / "core@000000000001"
    write_json(at / "atlas.json", {"commit": "0" * 40})
    write_json(at / "pins.json", {"atlas": "core@000000000001", "part_docs": {"B:U3": ["demo-bb@A"]}, "pins": [
        {"num": 1, "port": "PB9", "net": "SDA_1", "far": [{"board": "B", "kind": "part-pin", "pin": "U3-6",
                                                          "pin_name": "SDA", "mpn": "DEMO-BB-QFN", "net": "SDA_1"}],
         "dt": [{"label": "i2c1", "prop": "pinctrl-0", "func": "i2c1_sda_pb9", "loc": "fw:board.dts:10",
                 "driver": {"files": ["drivers/i2c/demo.c"], "compiled": True}}]}]})
    write_json(at / "mismatches.json", {"summary": "2 checks, 3/3 items passed; 1 candidate", "checks": [
        {"check": "I2C device identity by address strap", "passed": 1, "ran": 1}],
        "not_run": [{"check": "DT limits vs datasheet", "why": "no limit records mapped"}],
        "candidates": [{"class": "connected-no-dt", "key": "B:PA4", "summary": "PA4 is on net BTN_1 with no DT use",
                        "evidence": ["netlist B"], "settle": "the engineer says whether firmware should use it"}]})
    write_json(a / "current.json", {"commit": "0" * 40, "matches": [{"atlas": "core@000000000001"}]})
    return a


def test_part_card_by_mpn_and_board_bindings(atlas_dir):
    out = query.part_card(query.Atlas(atlas_dir), "DEMO-BB-QFN", board="")
    assert out.startswith("src: demo-bb@A card")
    assert "OCP_MASK resets to 1" in out
    assert "B:U3 DEMO-BB-QFN: 5=SCL_1, 6=SDA_1" in out


def test_part_card_missing_says_where_to_look(atlas_dir):
    out = query.part_card(query.Atlas(atlas_dir), "NOPE123")
    assert out.startswith("src: none") and "missing.md" in out


def test_doc_toc_and_under(atlas_dir):
    a = query.Atlas(atlas_dir)
    top = query.doc_toc(a, "demo-bb", depth=1)
    assert "7 | Registers | p8-10 | 2000 tok | s/registers.md" in top
    sub = query.doc_toc(a, "demo-bb@A", under="7")
    assert "7.1 | MODE Register | p9" in sub and "Features" not in sub


def test_doc_toc_under_by_title_word(atlas_dir):
    a = query.Atlas(atlas_dir)
    sub = query.doc_toc(a, "demo-bb@A", under="register")
    assert sub.startswith("src: demo-bb@A toc under 7") and "7.1 | MODE Register" in sub
    toc = atlas_dir / "docs" / "demo-bb@A" / "toc.json"
    data = json.loads(toc.read_text())
    data["sections"].append({"id": "8", "title": "Register Maps", "level": 1, "pages": [11, 11], "tokens": 100})
    toc.write_text(json.dumps(data))
    many = query.doc_toc(a, "demo-bb@A", under="register")
    assert "3 sections match 'register'" in many and "8 | Register Maps | p11" in many


def test_doc_section_distilled_text_and_pagination(atlas_dir):
    a = query.Atlas(atlas_dir)
    assert "| 0x06 | MODE |" in query.doc_section(a, "demo-bb@A", section="7")
    assert "Address = 06h" in query.doc_section(a, "demo-bb@A", page=9, tier="text")
    big = atlas_dir / "docs" / "demo-bb@A" / "s" / "long.md"
    big.write_text("row\n" * 20000)
    first = query.doc_section(a, "demo-bb@A", section="long")
    assert "next=" in first
    nxt = int(first.rsplit("next=", 1)[1].split(")")[0])
    second = query.doc_section(a, "demo-bb@A", section="long", offset=nxt)
    assert second.startswith("src: demo-bb@A s/long.md") and second.split("\n\n", 1)[1].startswith("row\n")


def test_find_fact_ranks_by_symbol(atlas_dir):
    out = query.find_fact(query.Atlas(atlas_dir), "OVP threshold")
    lines = out.splitlines()
    assert lines[0].startswith("src: facts.txt")
    assert lines[2].startswith("demo-bb.ec.VOVP")
    assert "VREF" not in out.split("\n", 3)[2]


def _flr_doc(atlas_dir):
    """A doc where one fact matches a rare term and six match only a common one."""
    d = atlas_dir / "docs" / "flr@1"
    write_json(d / "doc.json", {"doc_id": "flr@1", "part": "FLR", "kind": "datasheet"})
    lines = ["flr.ec.VOVP kind=limit; sym=VOVP; param=OVP threshold; cite=flr@1 p1"]
    lines += [f"flr.ec.X{i} kind=limit; sym=X{i}; param=threshold {i}; cite=flr@1 p1" for i in range(6)]
    lines += [f"flr.ec.Y{i} kind=limit; sym=Y{i}; param=other {i}; cite=flr@1 p1" for i in range(3)]
    write_text(d / "facts.txt", "\n".join(lines))


def test_find_fact_floor_hides_weak_hits_and_limit_lists_them(atlas_dir):
    _flr_doc(atlas_dir)
    a = query.Atlas(atlas_dir)
    out = query.find_fact(a, "OVP threshold", part="flr")
    assert "hits=7 shown=1" in out.splitlines()[0]
    assert "flr.ec.VOVP" in out and "flr.ec.X0" not in out
    assert "6 more hits" in out
    listed = query.find_fact(a, "OVP threshold", part="flr", limit=20)
    assert "shown=7" in listed and "flr.ec.X5" in listed and "more hits" not in listed


def test_find_fact_names_unmatched_terms(atlas_dir):
    out = query.find_fact(query.Atlas(atlas_dir), "VOVP nosuchterm")
    assert "unmatched=nosuchterm" in out.splitlines()[0]
    assert "unmatched" not in query.find_fact(query.Atlas(atlas_dir), "VOVP").splitlines()[0]


def test_find_fact_narrows_to_a_part_named_in_the_query(atlas_dir):
    d = atlas_dir / "docs" / "xldo42@1"
    write_json(d / "doc.json", {"doc_id": "xldo42@1", "part": "XLDO42", "kind": "datasheet"})
    write_text(d / "facts.txt", "xldo42.ec.VUVLO kind=limit; sym=VUVLO; param=UVLO threshold; cite=xldo42@1 p4")
    a = query.Atlas(atlas_dir)
    for q in ("XLDO42 threshold", "XLDO42DBVR threshold"):
        out = query.find_fact(a, q)
        assert "part=xldo42@1 (named in the query)" in out.splitlines()[0], q
        assert "xldo42.ec.VUVLO" in out and "demo-bb.ec.VOVP" not in out, q
    both = query.find_fact(a, "threshold")
    assert "named in the query" not in both and "demo-bb.ec.VOVP" in both and "xldo42.ec.VUVLO" in both


def test_register_byte_pair_is_one_16_bit_view(atlas_dir):
    d = atlas_dir / "docs" / "demo-bb@A"
    base = {"kind": "register", "doc": "demo-bb@A", "width": 8, "access": "rw", "p": 12, "prov": "llm-verified"}
    write_jsonl(d / "records" / "pair.jsonl", [
        dict(base, id="demo-bb.reg.CHGOPT_MSB", name="CHGOPT_MSB", addr="0x35", reset="0b00000100"),
        dict(base, id="demo-bb.reg.CHGOPT_LSB", name="CHGOPT_LSB", addr="0x34", reset="0b00110100"),
        {"id": "demo-bb.reg.CHGOPT_MSB.EN_HIZ", "kind": "field", "doc": "demo-bb@A", "reg": "demo-bb.reg.CHGOPT_MSB",
         "name": "EN_HIZ", "bits": [7, 7], "reset": "0b0", "access": "rw", "p": 12, "prov": "llm-verified"},
        {"id": "demo-bb.reg.CHGOPT_LSB.BATFET", "kind": "field", "doc": "demo-bb@A", "reg": "demo-bb.reg.CHGOPT_LSB",
         "name": "BATFET", "bits": [0, 0], "reset": "0b0", "access": "rw", "p": 12, "prov": "llm-verified"},
        {"id": "demo-bb.quirk.batfet", "kind": "quirk", "doc": "demo-bb@A", "text": "BATFET is active low", "p": 12,
         "prov": "llm-verified", "refs": ["demo-bb.reg.CHGOPT_LSB"]}])
    a = query.Atlas(atlas_dir)
    out = query.register_lookup(a, "demo-bb.CHGOPT")
    assert out.startswith("src: demo-bb@A demo-bb.reg.CHGOPT_MSB + demo-bb.reg.CHGOPT_LSB")
    assert "width=16 reset=0x0434 access=rw" in out
    assert out.index("- EN_HIZ[15]") < out.index("- BATFET[0]")
    assert "BATFET is active low" in out
    assert "width=8" in query.register_lookup(a, "demo-bb.CHGOPT_LSB")


def test_find_fact_reaches_prose_but_not_rendered_tables(atlas_dir):
    d = atlas_dir / "docs" / "demo-bb@A" / "s"
    write_text(d / "7.must.md", "# Must\n\n4. Keep the bus master clocked, or a debugger read during sleep returns zero [p.9].\n")
    write_text(d / "7.fields.md", "- generated field list mentioning sleep and debugger reads [p.9]\n")
    write_text(d / "8.md", "Intro line long enough to count as prose.\n<!-- atlas:table limit -->\n"
                           "| debugger | sleep | zero |\n<!-- atlas:end -->\n")
    a = query.Atlas(atlas_dir)
    out = query.find_fact(a, "debugger read during sleep")
    assert out.splitlines()[0].startswith("src: facts.txt + prose")
    assert "demo-bb@A s/7.must.md:3 kind=prose; text=Keep the bus master clocked" in out
    assert "7.fields.md" not in out and "| debugger | sleep |" not in out
    only = query.find_fact(a, "debugger sleep", kind="prose")
    assert only.splitlines()[0].startswith("src: prose") and "demo-bb.ec" not in only
    assert "kind=prose" not in query.find_fact(a, "OVP threshold", kind="limit")


def test_register_merges_twins_and_joins_quirks(atlas_dir):
    out = query.register_lookup(query.Atlas(atlas_dir), "demo-bb.MODE")
    assert out.count("- OE[7]") == 1
    assert "prov=deterministic & llm-verified" in out
    assert "OCP_MASK must be 0 before OE" in out


def test_register_mcu_instance_prefix_stripped(atlas_dir):
    out = query.register_lookup(query.Atlas(atlas_dir), "I2C1.TIMINGR")
    assert out.startswith("src: demo-mcu-svd demo-mcu.reg.I2C1.I2C_TIMINGR")


def test_pin_trace_by_port_net_and_part_pin(atlas_dir):
    a = query.Atlas(atlas_dir)
    for q in ("PB9", "SDA_1", "B:U3-6"):
        out = query.pin_trace(a, q)
        assert "pin 1 PB9 net=SDA_1 -> B:U3-6/SDA DEMO-BB-QFN" in out, q
        assert "driver drivers/i2c/demo.c" in out


def test_mismatch_report_leads_with_passed_checks(atlas_dir):
    out = query.mismatch_report(query.Atlas(atlas_dir))
    assert out.index("I2C device identity by address strap: 1/1 passed") < out.index("[connected-no-dt]")
    assert "DT limits vs datasheet: no limit records mapped" in out
    assert "[connected-no-dt]" not in query.mismatch_report(query.Atlas(atlas_dir), cls="other")


def test_mcp_server_lists_seven_tools_and_answers(atlas_dir):
    pytest.importorskip("mcp")
    from embarch_atlas.mcp_server import build_server

    server = build_server(atlas_dir, live=False)
    tools = asyncio.run(server.list_tools())
    assert sorted(t.name for t in tools) == sorted(query.TOOLS)
    live_tools = asyncio.run(build_server(atlas_dir, live=True).list_tools())
    assert sorted(t.name for t in live_tools) == sorted([*query.TOOLS, "reg_read", "reg_read_clear", "shell", "shell_run", "live_stacks"])
    writes = {"shell_run", "reg_read_clear"}
    for t in live_tools:
        hint = bool(t.annotations and t.annotations.read_only_hint)
        assert hint == (t.name not in writes), t.name
    res = asyncio.run(server.call_tool("pin_trace", {"pin_or_net": "PB9"}))
    text = json.dumps(res.model_dump() if hasattr(res, "model_dump") else res, default=str)
    assert "SDA_1" in text


def test_mcp_reg_read_refuses_allow_clear_and_names_the_write_tool(atlas_dir):
    pytest.importorskip("mcp")
    from embarch_atlas.mcp_server import build_server

    res = asyncio.run(build_server(atlas_dir, live=True).call_tool("reg_read", {"target": "X.Y", "allow_clear": True}))
    text = json.dumps(res.model_dump() if hasattr(res, "model_dump") else res, default=str)
    assert "reg_read_clear" in text and "Refused" in text


# ---- field-session misses (atlas/012)

def test_part_card_board_all_is_every_board_and_empty_still_works(atlas_dir):
    a = query.Atlas(atlas_dir)
    assert "B:U3 DEMO-BB-QFN" in query.part_card(a, "DEMO-BB-QFN", board="all")
    assert "B:U3 DEMO-BB-QFN" in query.part_card(a, "DEMO-BB-QFN", board="")
    assert "On the boards" not in query.part_card(a, "DEMO-BB-QFN")
    assert "(no board ref" in query.part_card(a, "DEMO-BB-QFN", board="Z")


def test_no_mcp_tool_documents_an_empty_string_value(atlas_dir):
    pytest.importorskip("mcp")
    from embarch_atlas.mcp_server import build_server

    for live in (False, True):
        for t in asyncio.run(build_server(atlas_dir, live=live).list_tools()):
            assert '""' not in (t.description or ""), t.name
            assert '""' not in json.dumps(t.input_schema), t.name
    card = next(t for t in asyncio.run(build_server(atlas_dir, live=False).list_tools()) if t.name == "part_card")
    assert '"all"' in card.description


def test_register_by_cmsis_member_name(atlas_dir):
    a = atlas_dir
    hdr = a.parent / "devhdr.h"
    hdr.write_text("""
typedef struct
{
  __IO uint32_t CFG1;      /*!< config 1, Address offset: 0x00 */
  __IO uint32_t CFG2;      /*!< config 2, Address offset: 0x04 */
  uint32_t RESERVED[2];
  __IO uint32_t RX_PAYSZ;  /*!< Address offset: 0x10 */
} UCPD_TypeDef;
#define PERIPH_BASE 0x40000000UL
#define UCPD1_BASE (PERIPH_BASE + 0x00008000UL)
#define UCPD1 ((UCPD_TypeDef *) UCPD1_BASE)
""")
    write_json(a / "docs" / "demo-rm@1" / "vote.json", {"header": {"file": str(hdr)}})
    write_jsonl(a / "docs" / "demo-mcu-svd" / "records" / "register.jsonl", [
        {"id": "demo-mcu.reg.UCPD1.UCPD_CFGR1", "kind": "register", "doc": "demo-mcu-svd", "name": "UCPD_CFGR1",
         "addr": "0x40008000", "width": 32, "reset": "0x0", "src": "svd", "p": None, "prov": "deterministic"},
        {"id": "demo-mcu.reg.UCPD1.UCPD_RX_PAYSZR", "kind": "register", "doc": "demo-mcu-svd", "name": "UCPD_RX_PAYSZR",
         "addr": "0x40008010", "width": 32, "reset": "0x0", "src": "svd", "p": None, "prov": "deterministic"}])
    out = query.register_lookup(query.Atlas(a), "UCPD1.CFG1")
    assert "demo-mcu.reg.UCPD1.UCPD_CFGR1" in out and "UCPD1->CFG1" in out
    out = query.register_lookup(query.Atlas(a), "UCPD1.RX_PAYSZ")
    assert "UCPD_RX_PAYSZR" in out and "addr=0x40008010" in out
    assert query.register_lookup(query.Atlas(a), "UCPD1.RESERVED").startswith("src: none")
    assert query.register_lookup(query.Atlas(a), "UCPD1.NOPE").startswith("src: none")


def test_cmsis_struct_offsets_skip_what_they_cannot_lay_out():
    from embarch_atlas import cmsisalias
    s = cmsisalias.parse_structs("typedef struct { __IO uint16_t A; __IO uint32_t B; uint8_t C, D; } T_TypeDef;"
                                 "typedef struct { struct { int x; } n; } U_TypeDef;")
    assert s == {"T": [("A", 0), ("B", 4), ("C", 8), ("D", 9)]}


def test_doc_section_on_an_undistilled_toc_section_says_what_it_is(atlas_dir):
    a = query.Atlas(atlas_dir)
    out = query.doc_section(a, "demo-bb@A", section="1")
    assert out.startswith("src: demo-bb@A toc") and "'Features' (p1, 300 tok)" in out and "not distilled" in out
    assert "tier=text page=1" in out
    out = query.doc_section(a, "demo-bb@A", section="Mode Register")
    assert "7.1" in out and "tier=text page=9" in out
    miss = query.doc_section(a, "demo-bb@A", section="9.9 Nonesuch Registers")
    assert miss.startswith("src: none") and "Nearest titles" in miss and "7 | Registers" in miss


def test_doc_section_undistilled_with_no_section_files_at_all(atlas_dir):
    import shutil
    shutil.rmtree(atlas_dir / "docs" / "demo-bb@A" / "s")
    assert "not distilled" in query.doc_section(query.Atlas(atlas_dir), "demo-bb@A", section="7")


def test_rm_register_section_points_to_register(atlas_dir):
    d = atlas_dir / "docs" / "demo-rm@1"
    write_json(d / "doc.json", {"doc_id": "demo-rm@1", "kind": "reference-manual"})
    write_json(d / "outline.json", {"entries": [
        {"id": "5", "title": "UCPD", "level": 1, "start": 10, "end": 30, "tokens": 5000},
        {"id": "5.1", "title": "UCPD configuration register 1 (UCPD_CFGR1)", "level": 2, "start": 20, "end": 20,
         "tokens": 400, "register": True, "regname": "UCPD_CFGR1"}]})
    out = query.doc_section(query.Atlas(atlas_dir), "demo-rm@1", section="5.1")
    assert "register <PERIPH>.UCPD_CFGR1" in out and "tier=text page=20" in out


def test_doc_toc_under_with_no_match_lists_nearest_titles(atlas_dir):
    out = query.doc_toc(query.Atlas(atlas_dir), "demo-bb@A", under="Mode Config")
    assert out.startswith("src: none") and "Nearest titles" in out and "7.1 | MODE Register" in out.split("Nearest titles")[1]


def test_cards_and_sections_name_the_pdf_relative_to_the_firmware_repo(atlas_dir):
    d = atlas_dir / "docs" / "demo-bb@A" / "doc.json"
    meta = json.loads(d.read_text())
    meta["path"] = "../../hardware/demo-bb.pdf"      # as pdftext records it: relative to the atlas dir
    d.write_text(json.dumps(meta))
    a = query.Atlas(atlas_dir)
    assert query.part_card(a, "demo-bb").startswith("src: demo-bb@A card pdf=hardware/demo-bb.pdf\n")
    assert "pdf=hardware/demo-bb.pdf" in query.doc_section(a, "demo-bb@A", section="7").split("\n")[0]
    assert "pdf=hardware/demo-bb.pdf" in query.doc_section(a, "demo-bb@A", page=9, tier="text").split("\n")[0]

import pytest

import datasheet_synth as ds
from embarch_atlas import check, cli, records as R, regparse, render

SECTION = """# SYN1234 6.5: Electrical characteristics (p.2)

aka: electrical characteristics, EC

Hand-written prose stays exactly as it is, including this line (p.2).

<!-- atlas:table kind=limit group=ec -->
stale content that the renderer replaces
<!-- atlas:end -->

More prose after the table.

<!-- atlas:table kind=limit group=ec id=syn1234.ec.V* id!=syn1234.ec.VOL.* -->
<!-- atlas:end -->
"""


@pytest.fixture()
def doc(tmp_path):
    atlas, docdir = ds.make_doc(tmp_path)
    (docdir / "s").mkdir()
    (docdir / "s" / "6.5.md").write_text(SECTION)
    return atlas, docdir


def test_tables_between_markers_prose_kept(doc):
    _, docdir = doc
    st = render.render_doc(docdir)
    out = (docdir / "s" / "6.5.md").read_text()
    assert st["blocks"] == 3 and st["warnings"] == []          # + the check table in s/invariants.md
    assert "stale content" not in out
    assert "Hand-written prose stays exactly as it is, including this line (p.2)." in out
    assert "More prose after the table." in out
    rows = [ln for ln in out.splitlines() if ln.startswith("| ") and not ln.startswith("| Symbol")]
    # one record per row, symbol and unit on every row, page cite on every row
    first = out.split("<!-- atlas:end -->")[0]
    assert sum(1 for ln in first.splitlines() if ln.startswith("| ") and "Symbol" not in ln) == len(ds.EC_ROWS)
    assert "| VUVLO | Undervoltage lockout | VIN rising | 2.6 | 2.8 | 3.05 | V |  | p.2 |" in out
    assert "| tON | Turn-on delay | EN rising | 1.5 | 2.5 | 4.5 | ms | fn 1; by design, not production tested | p.2 |" in out
    # footnote text is pulled from the page it qualifies
    assert "- fn 1 (p.2): Specified by design; not production tested." in out
    second = "\n".join(ln for ln in out.split("<!-- atlas:end -->")[1].splitlines() if ln.startswith("| "))
    assert "VUVLO" in second and "VHYS" in second and "VOL" not in second and "IQ" not in second
    assert all(r.count("|") == rows[0].count("|") for r in rows if "Footnotes" not in r)


def test_render_is_deterministic_and_idempotent(doc):
    _, docdir = doc
    render.render_doc(docdir)
    a = (docdir / "s" / "6.5.md").read_bytes(), (docdir / "facts.txt").read_bytes()
    st = render.render_doc(docdir)
    assert st["files"] == 0
    assert ((docdir / "s" / "6.5.md").read_bytes(), (docdir / "facts.txt").read_bytes()) == a


def test_facts_txt_one_line_per_record(doc):
    _, docdir = doc
    render.render_doc(docdir)
    lines = (docdir / "facts.txt").read_text().splitlines()
    assert len(lines) == len(ds.records())
    assert lines == sorted(lines, key=lambda ln: ln.split(" ", 1)[0])
    vu = next(ln for ln in lines if ln.startswith("syn1234.ec.VUVLO.vin "))
    assert "min=2.6; typ=2.8; max=3.05; unit=V; cite=syn1234@2b p2; prov=" in vu


def test_prov_shows_in_tables_after_check(doc):
    _, docdir = doc
    pairs = R.load_records(docdir)
    for _, r in pairs:
        if r["id"] == "syn1234.ec.VOL.isink":
            r["max"] = 0.34
    R.save_records(docdir, pairs)
    render.render_doc(docdir)
    check.run_check(docdir)          # sets prov, then re-renders
    out = (docdir / "s" / "6.5.md").read_text()
    row = next(ln for ln in out.splitlines() if ln.startswith("| VOL "))
    assert "unverified" in row


def test_register_quirk_and_other_kinds(doc):
    _, docdir = doc
    regparse.run(docdir, "ti")
    (docdir / "s" / "7.6.md").write_text("# regs (p.5)\n\n<!-- atlas:table kind=register -->\n<!-- atlas:end -->\n\n"
                                         "<!-- atlas:table kind=conflict -->\n<!-- atlas:end -->\n")
    (docdir / "s" / "quirks.md").write_text("# quirks\n\n<!-- atlas:table quirk -->\n<!-- atlas:end -->\n\n"
                                            "<!-- atlas:table kind=nothing -->\n<!-- atlas:end -->\n")
    st = render.render_doc(docdir)
    regs = (docdir / "s" / "7.6.md").read_text()
    assert "| 0x00 | CTRL | 8 | 0b10000001 (0x81) |  |" in regs          # mixed field access: none at register level
    assert "### 0x01 DATA_LSB (p.5)" in regs and "| 7:0 | VALUE_7_0 | rw | 0b00000011 |" in regs
    assert "| 0 | STAT | rclr | 0b1 |" in regs
    assert "GHOST" in regs
    q = (docdir / "s" / "quirks.md").read_text()
    assert "- **tsd_above_operating** (absence; p.1-2): TSD is typical only" in q
    assert any("unknown kind 'nothing'" in w for w in st["warnings"])


def test_selector_parsing():
    assert render.parse_selector("limit group=ec,abs id!=a.*") == {"kind": ["limit"], "group": ["ec", "abs"],
                                                                   "!id": ["a.*"]}
    with pytest.raises(ValueError):
        render.parse_selector("group=ec")
    recs = [{"id": "a.b", "kind": "limit", "p": [5, 9]}, {"id": "a.c", "kind": "limit", "p": 12}]
    assert [r["id"] for r in render.select(recs, render.parse_selector("limit p=6-9"))] == ["a.b"]


def test_footnote_blocks_restart_per_table():
    text = "\n".join(["Row A 1 2 3", "", "(1) first table note", "", "Row B 4 5 6", "", "(1) second table note"])
    assert render.footnotes(text)["1"] == "first table note"
    assert render.footnotes(text, render._quote_line(text, "Row B 4 5 6"))["1"] == "second table note"


def test_cli_render(doc, capsys):
    atlas, _ = doc
    assert cli.main(["--atlas-dir", str(atlas), "doc", "render", ds.DOC_ID]) == 0
    assert "3 tables in s/" in capsys.readouterr().out


def _write(docdir, name, recs):
    from embarch_atlas import common
    common.write_jsonl(docdir / "records" / name, recs)


def test_field_enum_and_compact_fieldless_registers(doc):
    _, docdir = doc
    d = ds.DOC_ID
    _write(docdir, "register.jsonl", [
        {"id": "syn1234.reg.MODE", "kind": "register", "doc": d, "p": 5, "src": "table", "q": "x", "name": "MODE",
         "addr": "0x04", "width": 8, "reset": "0x00", "desc": "operating mode"},
        {"id": "syn1234.reg.TRIM", "kind": "register", "doc": d, "p": 5, "src": "table", "q": "x", "name": "TRIM",
         "addr": "0x05", "width": 16, "reset": None, "note": "0x1234 on the A variant, 0x1235 on B",
         "desc": "factory trim, do not write"},
        {"id": "syn1234.reg.SPARE", "kind": "register", "doc": d, "p": 5, "src": "table", "q": "x", "name": "SPARE",
         "addr": "0x06", "width": 8}])
    _write(docdir, "field.jsonl", [
        {"id": "syn1234.reg.MODE.SEL", "kind": "field", "doc": d, "p": 5, "src": "table", "q": "x",
         "reg": "syn1234.reg.MODE", "name": "SEL", "bits": [7, 0], "reset": "0b00000000", "access": "rw",
         "enum": {"0": "off", "1": "on"}, "desc": "mode select"}])
    (docdir / "s" / "7.6.md").write_text("# regs (p.5)\n\n<!-- atlas:table kind=register id=syn1234.reg.MODE,"
                                         "syn1234.reg.TRIM,syn1234.reg.SPARE -->\n<!-- atlas:end -->\n\n"
                                         "<!-- atlas:table kind=field -->\n<!-- atlas:end -->\n")
    render.render_doc(docdir)
    out = (docdir / "s" / "7.6.md").read_text()
    assert "| Addr | Register | Width | Reset | Access | Description | Note | p |" in out
    assert "| 0x05 | TRIM | 16 |  |  | factory trim, do not write | 0x1234 on the A variant, 0x1235 on B | p.5 |" in out
    assert "| 0x04 | MODE | 8 | 0x00 |  |  |  | p.5 |" in out          # its description is under its heading
    assert "### 0x04 MODE (p.5)" in out
    assert "### 0x05 TRIM" not in out and "### 0x06 SPARE" not in out  # field-less: the summary row only
    assert "| Bits | Field | Access | Reset | Description | Enum | Note | p |" in out
    assert "| 7:0 | SEL | rw | 0b00000000 | mode select | 0 = off; 1 = on |  | p.5 |" in out
    assert "| MODE | 7:0 | SEL | rw | 0b00000000 | mode select | 0 = off; 1 = on |  | p.5 |" in out   # kind=field


def test_selector_empty_value_selects_fixed_address_strap():
    recs = [{"id": "a.strap.fixed", "kind": "strap", "p": 3, "pin": "", "condition": "fixed", "value": "0x6B"},
            {"id": "a.strap.ADDR.gnd", "kind": "strap", "p": 3, "pin": "ADDR", "condition": "ADDR = GND",
             "value": "0x40"}]
    assert render.parse_selector("strap pin=") == {"kind": ["strap"], "pin": [""]}
    assert [r["id"] for r in render.select(recs, render.parse_selector("strap pin="))] == ["a.strap.fixed"]
    assert [r["id"] for r in render.select(recs, render.parse_selector("strap pin!="))] == ["a.strap.ADDR.gnd"]


def test_check_records_render_only_in_invariants(doc):
    _, docdir = doc
    (docdir / "s" / "quirks.md").write_text("# quirks\n\n<!-- atlas:table kind=quirk,check -->\n<!-- atlas:end -->\n")
    st = render.render_doc(docdir)
    q = (docdir / "s" / "quirks.md").read_text()
    assert "_1 invariants: see s/invariants.md._" in q and "uvlo_order" not in q
    assert any("check records render only in s/invariants.md" in w for w in st["warnings"])
    inv = (docdir / "s" / "invariants.md").read_text()
    assert inv.startswith("# syn1234@2b: invariants") and "| uvlo_order | `R['syn1234.ec.VUVLO.vin'].max < 3.1" in inv
    (docdir / "s" / "invariants.md").write_text("# my own heading (p.2)\n\nProse kept.\n\n"
                                                 "<!-- atlas:table kind=check -->\n<!-- atlas:end -->\n")
    render.render_doc(docdir)                                            # an existing file is only rendered
    inv = (docdir / "s" / "invariants.md").read_text()
    assert inv.startswith("# my own heading (p.2)\n\nProse kept.") and "uvlo_order" in inv


def test_quirk_source_tag_follows_its_text_and_requirement_columns():
    q = render.t_quirk([{"id": "a.quirk.curve", "kind": "quirk", "p": 4, "src": "figure", "class": "figure-only",
                         "note": "read: png", "text": "the curve peaks near 0.77 A", "why": "budget 0.8 A"}], {})
    assert q == ["- **curve** (figure-only; p.4; read: png): the curve peaks near 0.77 A (figure) "
                 "*Why it matters:* budget 0.8 A"]
    reqs = [{"id": "p.req.a", "kind": "requirement", "p": 2, "rid": "prd.a", "text": "charge first from USB",
             "fw_relevant": True, "modality": "shall", "order": 1},
            {"id": "p.req.b", "kind": "requirement", "p": 2, "rid": "prd.b", "text": "then from the dock",
             "fw_relevant": True, "modality": "nice-to-have", "order": 2}]
    t = render.t_requirement(reqs, {})
    assert t[0] == "| Req | Requirement | Modality | Order | Firmware | Note | p |"
    assert t[3] == "| prd.b | then from the dock | nice-to-have | 2 | yes |  | p.2 |"
    assert render.t_requirement([dict(reqs[0], modality=None, order=None)], {})[0] == \
        "| Req | Requirement | Firmware | Note | p |"


def test_field_table_takes_meaning_from_the_llm_twin():
    det = {"id": "a.reg.R.EN", "kind": "field", "p": 5, "reg": "a.reg.R", "name": "EN", "bits": [0, 0],
           "reset": "0b1", "access": "rw", "prov": "deterministic"}
    llm = dict(det, prov="llm-verified", desc="enable", enum={"0": "off", "1": "on"})
    rows = render.t_field([det], {"all": [det, llm]})
    assert rows[0] == "| Register | Bits | Field | Access | Reset | Description | Enum | Note | p |"
    assert rows[2] == "| R | 0 | EN | rw | 0b1 | enable | 0 = off; 1 = on |  | p.5 |"

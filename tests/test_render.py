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
    assert st["blocks"] == 2 and st["warnings"] == []
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
    assert "2 tables in s/" in capsys.readouterr().out

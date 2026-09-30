import copy
import io
import json

import pytest

import datasheet_synth as ds
import mutate_records
from embarch_atlas import check, cli, common, records as R


@pytest.fixture()
def doc(tmp_path):
    atlas, docdir = ds.make_doc(tmp_path)
    return atlas, docdir


def recs_by_id(docdir):
    return {r["id"]: (f, r) for f, r in R.load_records(docdir)}


def test_numbers_and_cites():
    assert check.numbers("rise 10-90% of step") == [10, 90]            # not -90
    assert check.numbers("-0.3 to 29 v") == [-0.3, 29]
    assert sorted(check.numbers("±2 kv")) == [-2, 2]
    assert check.numbers("3..23 v") == [3, 23]
    assert check.numbers("en1 and 1.0a17") == [1.0]                      # identifiers and glued footnotes
    assert check.cited_pages("see p.8, p.4-5 and pp. 9") == {4, 5, 8, 9}
    assert check.cited_pages("step 5 of the sequence, §7") == set()      # not page cites
    assert check.cited_pages("cite=demo@A p12") == {12}


def test_clean_run_verifies_and_sets_prov(doc):
    atlas, docdir = doc
    rep = check.run_check(docdir)
    s = rep["summary"]
    assert s["failed"] == 0 and s["exit"] == 0
    assert s["A"]["anchored"] == s["A"]["of"] and s["K"]["disagree"] == 0 and s["K"]["agree"] == 8
    assert s["K"]["unit_seen"] == 8 and s["X"]["as_expected"] == 1
    by = recs_by_id(docdir)
    assert by["syn1234.ec.VUVLO.vin"][1]["prov"] == "llm-verified"
    assert by["syn1234.fig.figure-7-1"][1]["prov"] == "llm-unverified"     # a figure fact needs a second look
    assert (docdir / "checks.json").exists()


def _rewrite(docdir, rid, **patch):
    pairs = R.load_records(docdir)
    for _, r in pairs:
        if r["id"] == rid:
            for k, v in patch.items():
                if v is None:
                    r.pop(k, None)
                else:
                    r[k] = v
    R.save_records(docdir, pairs)


def result(rep, rid):
    return next(x for x in rep["records"] if x["id"] == rid)


def test_failures_are_kept_and_marked_unverified(doc):
    _, docdir = doc
    _rewrite(docdir, "syn1234.ec.fSW.rfsw", typ=450, max=None)          # a column slip: 447 moved to typ
    _rewrite(docdir, "syn1234.ec.IQ.vin", q="VIN = 7 V 355 555 uA", max=555)  # hallucinated with its quote
    _rewrite(docdir, "syn1234.ec.VHYS", p=3)                                  # wrong page
    _rewrite(docdir, "syn1234.ec.VOL.isink", cond="ISINK = 12 mA")            # changed condition
    _rewrite(docdir, "syn1234.ec.tON.en", unit="us")                         # unit prefix
    rep = check.run_check(docdir)
    assert rep["summary"]["exit"] == 1
    assert isinstance(result(rep, "syn1234.ec.fSW.rfsw")["checks"]["K"], dict)
    assert result(rep, "syn1234.ec.IQ.vin")["checks"]["A"].startswith("not-found")
    assert result(rep, "syn1234.ec.VHYS")["checks"]["A"].startswith("wrong-page: quote found on p2")
    assert result(rep, "syn1234.ec.VOL.isink")["checks"]["C"].startswith("condition numbers not on page")
    assert "unit" in result(rep, "syn1234.ec.tON.en")["checks"]["K"]["disagree"]
    by = recs_by_id(docdir)
    for rid in ("syn1234.ec.fSW.rfsw", "syn1234.ec.IQ.vin", "syn1234.ec.VHYS", "syn1234.ec.VOL.isink"):
        assert by[rid][1]["prov"] == "llm-unverified"                     # kept, never deleted
    assert len(by) == len(ds.records())


def test_quote_anchoring_is_glyph_aware(tmp_path):
    docdir = tmp_path / "docs" / "demo-switch@A"
    common.write_json(docdir / "doc.json", {"doc_id": "demo-switch@A", "pages": 2, "file": "x.pdf", "path": "none"})
    (docdir / "pages").mkdir(parents=True)
    (docdir / "pages" / "p001.txt").write_text("VSYS > VOVP, CVBUS = 0F, RL = 100 90 ns\n")
    (docdir / "pages" / "p002.txt").write_text("Maximum VIN  5.5 V at 25 °C\n")
    doc = check.DocText(docdir, use_pdf=False)
    assert doc.has("VSYS > VOVP, CVBUS = 0µF, RL = 100Ω 90 ns", 1)      # typed with the real glyphs
    assert doc.has("VSYS > VOVP, CVBUS = 0F, RL = 100 90 ns", 1)         # typed from Read, glyphs invisible
    assert doc.has("Maximum VIN 5.5 V", 2) and doc.has("Maximum VIN ≤ 5.5 V", 2)
    assert doc.has("Maximum VIN ... at 25 °C", 2)                       # an ellipsis joins two verbatim parts
    assert not doc.has("Maximum VIN 6.5 V", 2)


def test_units_and_order():
    assert check.check_unit({"sym": "VOVP", "unit": "mA", "param": "threshold"}) is not None
    assert check.check_unit({"sym": "CVBUS", "unit": "V", "param": "capacitor voltage rating"}) is None  # param
    assert check.check_unit({"sym": "IQ", "unit": "mV", "param": "supply current"}) is not None
    assert check.check_unit({"sym": "V(CVBUS)", "unit": "V", "param": "capacitor rating"}) is None
    assert check.check_unit({"sym": "INL", "unit": "LSB"}) is None
    assert check.check_unit({"sym": "RθJA", "unit": "°C/W"}) is None
    rec = {"id": "a.b.c", "kind": "limit", "p": 1, "min": 5, "typ": 3, "max": 4, "group": "other", "src": "text"}
    assert "order" in check.record_checks(_Fake(), rec)["O"]


class _Fake:
    def has(self, q, p):
        return True

    def page_numbers(self, ps):
        return []

    def page_text(self, ps):
        return ""


def test_invariants_are_sandboxed():
    by = {"a.x": {"min": 1, "typ": 2, "max": 3}}
    ok = check.check_invariants(by, [{"id": "c1", "expr": "all(R[k].typ > 1 for k in ['a.x'])", "expect": "pass"},
                                     {"id": "c2", "expr": "(R['a.x'].max & 1) == 1", "expect": "pass"},
                                     {"id": "c3", "expr": "R['a.x'].typ > 5", "expect": "fail"}])
    assert all(x["ok"] for x in ok)
    bad = check.check_invariants(by, [{"id": "c4", "expr": "__import__('os').getcwd()", "expect": "pass"},
                                      {"id": "c5", "expr": "R['a.x'].__class__", "expect": "pass"},
                                      {"id": "c6", "expr": "open('x')", "expect": "pass"}])
    assert all(x["got"].startswith("error") and not x["ok"] for x in bad)


def test_registers_twin_diff_and_crosscheck(doc):
    _, docdir = doc
    from embarch_atlas import regparse
    regparse.run(docdir, "ti")
    det = [r for f, r in R.load_records(docdir) if f == "regparse.jsonl" and r["kind"] in ("register", "field")]
    llm = []
    for r in det:
        x = {k: v for k, v in r.items() if k not in ("prov", "note", "summary")}
        llm.append(x)
    # the LLM wrote reserved bits as one field where the parser has one row per bit range: still agrees
    llm = [x for x in llm if not x["id"].endswith("RESERVED_6_1")]
    llm.append({"id": "syn1234.reg.CTRL.RESERVED_6_1", "kind": "field", "doc": ds.DOC_ID, "p": 5, "src": "table",
                "reg": "syn1234.reg.CTRL", "name": "RESERVED_6_1", "bits": [6, 1], "reset": "0b000000",
                "access": "ro"})
    common.write_jsonl(docdir / "records" / "register.jsonl", [x for x in llm if x["kind"] == "register"])
    common.write_jsonl(docdir / "records" / "field.jsonl", [x for x in llm if x["kind"] == "field"])
    rep = check.run_check(docdir)
    assert rep["G"]["diff"] == [] and rep["summary"]["G"]["structure_issues"] == 0
    provs = {(f, r["id"]): r.get("prov") for f, r in R.load_records(docdir)}
    assert provs[("regparse.jsonl", "syn1234.reg.CTRL")] == "deterministic+crosschecked"
    assert provs[("register.jsonl", "syn1234.reg.CTRL")] == "llm-verified"
    # now the LLM gets a reset wrong
    pairs = R.load_records(docdir)
    for f, r in pairs:
        if f == "field.jsonl" and r["id"] == "syn1234.reg.CTRL.EN":
            r["reset"] = "0b0"
    R.save_records(docdir, pairs)
    rep = check.run_check(docdir)
    assert any("reset llm 0b0 det 0b1" in d["issue"] for d in rep["G"]["diff"])
    provs = {(f, r["id"]): r.get("prov") for f, r in R.load_records(docdir)}
    assert provs[("field.jsonl", "syn1234.reg.CTRL.EN")] == "llm-unverified"
    assert provs[("regparse.jsonl", "syn1234.reg.CTRL.EN")] == "deterministic"   # back from crosschecked


def test_register_structure_invariants():
    recs = [{"id": "d.reg.A", "kind": "register", "name": "A", "addr": "0x01", "width": 8, "reset": "0b10000000"},
            {"id": "d.reg.A.X", "kind": "field", "reg": "d.reg.A", "name": "X", "bits": [7, 4], "reset": "0b0000"},
            {"id": "d.reg.A.Y", "kind": "field", "reg": "d.reg.A", "name": "Y", "bits": [4, 1], "reset": "0b0000"},
            {"id": "d.reg.B", "kind": "register", "name": "B", "addr": "0x01", "width": 8}]
    issues = {x["issue"] for x in check.check_registers(recs)["structure"]}
    assert any("overlaps X at bit 4" in i for i in issues)
    assert any("bits [0] covered by no field" in i for i in issues)
    assert any("compose to 0x0" in i for i in issues)
    assert any("address 0x1 is also A" in i for i in issues)


def test_markdown_numbers(doc):
    _, docdir = doc
    (docdir / "s").mkdir(exist_ok=True)
    (docdir / "card.md").write_text(
        "# SYN1234 card (p.1-5)\n\n"
        "aka: SYN1234, 99 alternative names\n"
        "- UVLO 2.6 / 2.8 / 3.05 V (p.2)\n"
        "- restarts below 142 C (p.3); rise measured 10-90% (p.3)\n"
        "- turn-on is step 5 of the sequence (p.2)\n"
        "- 38 C margin between the two (derived) (p.3)\n"
        "- the typical curve peaks near 0.77 A (figure) (p.4)\n"
        "- quiescent current 777 uA (p.2)\n")
    rep = check.run_check(docdir)
    m = next(x for x in rep["M"] if x["file"] == "card.md")
    assert m["stats"]["derived"] == 1 and m["stats"]["figure"] == 1
    assert [b["number"] for b in m["unanchored"]] == [777]
    assert rep["summary"]["exit"] == 1


def test_table_structure_checks(doc):
    _, docdir = doc
    (docdir / "s").mkdir(exist_ok=True)
    (docdir / "s" / "6.5.md").write_text(
        "# EC (p.2)\n\n| Symbol | MIN TYP MAX | Min | Typ | Max | p |\n|---|---|---|---|---|---|\n"
        "| VUVLO | x | 2.6 2.8 | 3.05 | 2.8 | p.2 |\n| V 2 | x | 2.6.3 |  |  | p.2 |\n")
    rep = check.run_check(docdir)
    kinds = {x["check"] for x in rep["S"]}
    assert {"fused-header", "packed", "glued", "orphan-subscript"} <= kinds


def test_cli_exit_codes(doc, capsys):
    atlas, docdir = doc
    assert cli.main(["--atlas-dir", str(atlas), "doc", "check", ds.DOC_ID]) == 0
    out = capsys.readouterr().out
    assert "A quotes anchored" in out and "exit 0" in out
    assert cli.main(["--atlas-dir", str(atlas), "doc", "check", "nope@1"]) == 2


def test_error_injection_catch_rates(doc):
    """The spike's mutation table, on the synthetic EC table: every class stays caught at the rate the
    checker is designed for (the synthetic table has a UNIT column and one number per cell)."""
    _, docdir = doc
    d = check.DocText(docdir)
    recs = [r for _, r in R.load_records(docdir)]
    checks = [r for r in recs if r["kind"] == "check"]
    table = mutate_records.run(d, recs, checks, n=40, seed=1)
    floor = {"column_slip": 0.85, "drop_value": 0.85, "value_typo": 0.9, "sign_flip": 0.95, "value_and_quote": 1.0,
             "unit_scale": 0.9, "page_shift": 1.0, "cond_change": 0.8}
    for cls, row in table.items():
        assert row["tried"] >= 20, (cls, row)
        assert row["rate"] >= floor[cls], (cls, row)
    assert table["page_shift"]["by"].get("A") == table["page_shift"]["caught"]
    assert table["value_and_quote"]["by"].get("A") == table["value_and_quote"]["caught"]


# ---------------------------------------------------------------- K: ±, %, VALUE columns, stacked rows

def _table_doc(tmp_path, pages):
    """A synthetic PDF (Courier 8 pt) from [(x, y, text), ...] per page, prepped. Invented part SYN5678."""
    import pymupdf

    from embarch_atlas import pdftext
    doc = pymupdf.open()
    for items in pages:
        page = doc.new_page(width=612, height=792)
        for x, y, t in items:
            page.insert_text((x, y), t, fontname="cour", fontsize=8)
    pdf = tmp_path / "SYN5678-1.pdf"
    doc.save(pdf)
    return pdftext.prep(pdf, tmp_path / "atlas", doc_id="syn5678@1", log=lambda *_: None)


VALUE_PAGE = [
    (40, 60, "Absolute Maximum Ratings"),
    (40, 80, "SYMBOL"), (110, 80, "DESCRIPTION"), (420, 80, "VALUE"), (500, 80, "UNITS"),
    (40, 96, "VIN"), (110, 96, "IN to GND"), (410, 96, "-0.3 to 29"), (500, 96, "V"),
    (40, 112, "VOVLO"), (110, 112, "OVLO to GND"), (410, 112, "-0.3 to VIN"), (500, 112, "V"),
    (40, 128, "ISW"), (110, 128, "Peak switch current"), (420, 128, "18"), (500, 128, "A"),
    (40, 144, "CIN"), (110, 144, "Input capacitance"), (410, 144, "1 to 10"), (500, 144, "uF"),
    (40, 160, "VBAT"), (110, 160, "BAT to GND"), (404, 160, "-0.3V to 6V"), (500, 160, "V"),
]
EC_PAGE = [
    (40, 60, "PARAMETER"), (200, 60, "CONDITIONS"), (380, 60, "MIN"), (425, 60, "TYP"), (470, 60, "MAX"),
    (520, 60, "UNIT"),
    (40, 76, "IOS offset current"), (200, 76, "TCT > 280 us"), (420, 76, "±0.48"), (466, 76, "±1.5"), (520, 76, "mA"),
    (40, 92, "TACC accuracy"), (200, 92, "TJ = 25 C"), (420, 92, "+1.3"), (466, 92, "±2.5"), (520, 92, "C"),
    (40, 108, "VACC regulation accuracy"), (200, 108, "VBAT = 8.4 V"), (376, 108, "-2%"), (466, 108, "2%"),
    (40, 124, "VALRT alert voltage"), (376, 124, "-0.3"), (462, 124, "Vs + 0.3"), (520, 124, "V"),
    (40, 140, "VXERR voltage error"), (200, 140, "TA = 25 C"), (376, 140, "-0.2"), (466, 140, "+0.2"),
    (516, 140, "% of"), (516, 150, "Reading"),
    (40, 170, "tDEB debounce time"), (200, 170, "RL = 100, VOUT = 5 V"), (520, 170, "ms"), (422, 177, "18"),
]


def _lim(rid, q, p, **kw):
    return {"id": f"syn5678.{rid}", "kind": "limit", "doc": "syn5678@1", "p": p, "q": q, "src": "table",
            "sym": rid.split(".")[1], **kw}


def _k(doc, rec):
    return check.record_checks(doc, rec).get("K")


def test_k_value_column_ranges_expressions_and_single_values(tmp_path):
    docdir = _table_doc(tmp_path, [VALUE_PAGE])
    doc = check.DocText(docdir)
    ok = [_lim("abs.VIN", "IN to GND -0.3 to 29", 1, group="abs", min=-0.3, max=29, unit="V"),
          _lim("abs.VOVLO", "OVLO to GND -0.3 to VIN", 1, group="abs", min=-0.3, max="VIN", unit="V"),
          _lim("abs.ISW", "Peak switch current 18", 1, group="abs", max=18, unit="A"),
          _lim("recommended.CIN", "Input capacitance 1 to 10", 1, group="recommended", min=1, max=10, unit="uF"),
          _lim("abs.VBAT", "BAT to GND -0.3V to 6V", 1, group="abs", min=-0.3, max=6, unit="V")]   # glued units
    for r in ok:
        assert _k(doc, r) == "ok", (r["id"], _k(doc, r))
    # `a to b` is min/max: a wrong end, a dropped end and a number for an expression all disagree
    assert _k(doc, dict(ok[0], max=24)) == {"disagree": {"max": {"record": 24, "columns": 29.0}}}
    assert "min" in _k(doc, {k: v for k, v in ok[0].items() if k != "min"})["disagree"]
    assert _k(doc, dict(ok[1], max=5.5))["disagree"]["max"]["columns"] == "expression"
    assert "value" in _k(doc, dict(ok[2], max=17))["disagree"]          # a lone value: key free, value not


def test_k_plus_minus_percent_expression_wrapped_unit_stacked_row(tmp_path):
    docdir = _table_doc(tmp_path, [EC_PAGE])
    doc = check.DocText(docdir)
    ios = _lim("ec.IOS", "TCT > 280 us ±0.48 ±1.5 mA", 1, group="ec", min=-1.5, typ="±0.48", max=1.5, unit="mA")
    assert _k(doc, ios) == "ok"                                          # ±X in MAX is -X..X; ±X in TYP a string
    assert _k(doc, dict(ios, typ=0.48)) == "ok"                          # ... or its magnitude
    assert "typ" in _k(doc, dict(ios, typ="±0.5"))["disagree"]
    assert "min" in _k(doc, dict(ios, min=-1.6))["disagree"]
    tacc = _lim("ec.TACC", "TJ = 25 C +1.3 ±2.5 C", 1, group="ec", min=-2.5, typ=1.3, max=2.5, unit="C")
    assert _k(doc, tacc) == "ok"
    vacc = _lim("ec.VACC", "VBAT = 8.4 V -2% 2%", 1, group="ec", min=-2, max=2, unit="%")
    res = check.record_checks(doc, vacc)
    assert res["K"] == "ok" and res["K_unit_seen"]                       # 2% is 2, and % is the unit
    assert "unit" in _k(doc, dict(vacc, unit="V"))["disagree"]
    valrt = _lim("ec.VALRT", "VALRT alert voltage -0.3 Vs + 0.3 V", 1, group="ec", min=-0.3, max="Vs + 0.3", unit="V")
    assert _k(doc, valrt) == "ok"                                        # the expression is not compared
    vx = _lim("ec.VXERR", "TA = 25 C -0.2 +0.2 % of", 1, group="ec", min=-0.2, max=0.2, unit="% of Reading")
    assert _k(doc, vx) == "ok"                                           # the unit cell wraps onto the next line
    assert "unit" in _k(doc, dict(vx, unit="% of Range"))["disagree"]
    tdeb = dict(_lim("ec.tDEB", "RL = 100, VOUT = 5 V ms 18", 1, group="ec", typ=18, unit="ms"), kind="timing")
    assert _k(doc, tdeb) == "ok"                                         # the value sits a line below its row
    assert "typ" in _k(doc, dict(tdeb, typ=19, q="RL = 100, VOUT = 5 V ms 18"))["disagree"]


def test_k_row_not_found_is_a_counted_warning(doc, monkeypatch):
    _, docdir = doc
    # the row of IQ is not in the word bands (as when the two text layers split a row differently)
    real = check.DocText.find_row
    monkeypatch.setattr(check.DocText, "find_row", lambda self, bands, q: None if "355" in q else real(self, bands, q))
    rep = check.run_check(docdir)
    s = rep["summary"]
    assert s["K"]["row_not_found"] == 1 and s["K"]["not_checkable"] == 1 and s["warnings"] == 1
    assert any(w.startswith("K: 1 table rows not found") and "syn1234.ec.IQ.vin" in w for w in rep["warnings"])
    iq = result(rep, "syn1234.ec.IQ.vin")
    assert iq["checks"]["K"] == "not checkable: row-not-found"
    assert iq["warnings"] == ["K: row not found on p2: the column check did not run"]
    out = io.StringIO()
    check.print_summary(rep, out)
    assert "warning: K: 1 table rows not found" in out.getvalue()


# ---------------------------------------------------------------- A: quotes split with `...`

def test_split_quote_parts_in_order_and_near(tmp_path):
    docdir = tmp_path / "docs" / "demo-prd@1"
    common.write_json(docdir / "doc.json", {"doc_id": "demo-prd@1", "pages": 1, "file": "x.pdf", "path": "none"})
    (docdir / "pages").mkdir(parents=True)
    (docdir / "pages" / "p001.txt").write_text(
        "Contents\n"
        "Design Requirements ................................ 8\n"
        "Technical Specifications ........................... 12\n"
        "Wall Prongs ........................................ 13\n"
        "Magnets ............................................ 13\n"
        "Pricing ............................................ 14\n"
        "When the case is closed the charger enters its low power mode after a delay that the host\n"
        "can configure over the bus, and it leaves that mode as soon as the lid opens again, which\n"
        "takes at most a few milliseconds on every unit that was tested in the lab so far.\n"
        "Standby supply current\n\n\n                          42 mA\n")
    doc = check.DocText(docdir, use_pdf=False)
    assert doc.has("Technical Specifications ... 12", 1)             # the number on the title's line
    assert not doc.has("Technical Specifications ... 14", 1)         # a 14 three lines later is not its page
    assert doc.has("Wall Prongs ... 13", 1)
    assert not doc.has("Pricing ... Design Requirements", 1)          # parts out of order
    assert doc.has("When the case is closed ... it leaves that mode as soon as the lid opens again", 1)
    assert doc.has("When the case is closed the charger ... tested in the lab so far.", 1)   # long parts: nearby lines
    assert not doc.has("Contents ... tested in the lab so far.", 1)   # a short part far from the next one
    assert doc.has("Standby supply current ... 42 mA", 1)             # blank lines are not a gap
    assert check.record_checks(doc, {"id": "demo-prd.quirk.toc", "kind": "quirk", "p": 1, "src": "text",
                                     "q": "Technical Specifications ... 14", "class": "doc-artefact",
                                     "text": "x"})["A"].startswith("not-found")


# ---------------------------------------------------------------- M: tags per number, structure, record pages

def test_md_tags_cover_numbers_before_them_in_their_clause():
    c = check.md_claims
    assert c("- 38 C margin between the two (derived) (p.3)") == [(38, "derived")]
    assert c("- (derived) 38 C margin (p.3)") == [(38, None)]                     # a tag before: no cover
    assert c("- margin 38 C (derived); current 777 uA (p.3)") == [(38, "derived"), (777, None)]
    assert c("- 23.2 V typ (derived: 24 - 0.8). Why: parked at 23.2 V") == \
        [(23.2, "derived"), (24, "derived"), (0.8, "derived"), (23.2, None)]
    assert c("- fields 7 and 8 bits (127; 255 codes) (derived)") == [(7, "derived"), (8, "derived"),
                                                                    (127, "derived"), (255, "derived")]
    assert c("- read off the curve: 0.77 A (equation)") == [(0.77, "figure")]
    assert c("- see (Figure 7-1) at 5 V") == [(5, None)]                          # a reference is not a tag
    assert c("- **q** (absence; p.2; (derived)): TSD 165 C *Why it matters:* 150 C (figure)") == \
        [(165, None), (150, "figure")]


def test_md_hex_suffix_does_not_eat_decimals():
    assert check._md_numbers("updates every 102.3984h (p.3)") == [102.3984]
    assert check._md_numbers("registers 3Ch and 1Fh, 00h, mask 101b") == []


def test_md_rendered_structure_is_not_a_claim(doc):
    _, docdir = doc
    (docdir / "s").mkdir(exist_ok=True)
    f = docdir / "s" / "7.6.md"
    f.write_text("# regs (p.5)\n\n<!-- atlas:table kind=register -->\n"
                 "| Addr | Register | Width | Reset | Access | Note | p |\n|---|---|---|---|---|---|---|\n"
                 "| 0x09 | WIDE | 24 | 0x000000 | rw |  | p.5 |\n\n"
                 "| Bits | Field | Access | Reset | Description | Enum | Note | p |\n|---|---|---|---|---|---|---|---|\n"
                 "| 29:27 | MODE | rw | 0b000 | mode select | 0 = off; 31 = 777 mA limit |  | p.5 |\n"
                 "| 3 | FIG | rw | 0b0 | read from the diagram 4242 | | (figure) | p.5 |\n"
                 "<!-- atlas:end -->\n\n"
                 "| Bits | Width |\n|---|---|\n| 29:27 | 24 |\n")                       # hand-written: counted
    m = check.check_markdown_file(check.DocText(docdir), f, {})
    # width, bit range and enum keys of rendered rows are structure; the enum's meaning is a claim
    assert sorted(b["number"] for b in m["unanchored"]) == [24, 27, 29, 777]
    assert m["stats"]["figure"] == 1                                               # the row's Note tag


def test_md_record_fallback_only_on_cited_pages(doc):
    _, docdir = doc
    f = docdir / "card.md"
    f.write_text("# card\n\n- a value 4242 (p.4)\n- the same value 4242 (p.3)\n- another 5151 (p.3)\n")
    m = check.check_markdown_file(check.DocText(docdir), f, {4: [4242.0]})
    assert m["stats"]["anchored_record"] == 1 and m["stats"]["record_elsewhere"] == 1
    assert m["record_elsewhere"][0]["record_pages"] == [4] and m["record_elsewhere"][0]["line"] == 4
    assert [b["number"] for b in m["unanchored"]] == [5151]


def test_invariants_expressions_are_not_claims(doc):
    from embarch_atlas import render
    _, docdir = doc
    (docdir / "s").mkdir(exist_ok=True)
    render.render_doc(docdir)
    rep = check.run_check(docdir)
    m = next(x for x in rep["M"] if x["file"] == "s/invariants.md")
    # the expression's 3.1 is skipped; the Why cell "UVLO below 3.1 V" is a claim, on no cited page
    assert [(b["number"], b["line"]) for b in m["unanchored"]] == [(3.1, 8)]
    assert "3.1" in (docdir / "s" / "invariants.md").read_text().splitlines()[7].split("|")[2]


# ---------------------------------------------------------------- X and G

def test_invariants_read_register_reset_and_bits():
    by = {"d.reg.A": {"reset": "0x2603", "width": 16, "addr": "0x0A"},
          "d.reg.A.X": {"bits": [3, 0], "reset": "0b0011"}}
    xs = check.check_invariants(by, [
        {"id": "c1", "expr": "(R['d.reg.A'].reset & 1) == 0", "expect": "fail"},
        {"id": "c2", "expr": "R['d.reg.A.X'].bits[0] - R['d.reg.A.X'].bits[1] + 1 == 4", "expect": "pass"},
        {"id": "c3", "expr": "R['d.reg.A'].addr == 10 and R['d.reg.A'].width == 16", "expect": "pass"}])
    assert all(x["ok"] for x in xs), xs


def test_variant_reset_null_with_note_is_not_a_twin_disagreement():
    det = [{"id": "d.reg.A", "kind": "register", "name": "A", "addr": "0x01", "width": 8, "reset": "0x0A",
            "prov": "deterministic"}]
    llm = {"id": "d.reg.A", "kind": "register", "name": "A", "addr": "0x01", "width": 8, "reset": None}
    rep = check.check_registers(det + [dict(llm, note="0x0A on 1S parts, 0x0B on 2S-5S")])
    assert rep["diff"] == [] and rep["verdict"]["d.reg.A"] == "agree"
    rep = check.check_registers(det + [llm])                          # no note: an undefined reset disagrees
    assert any("reset" in d["issue"] for d in rep["diff"])


def test_md_rendered_figure_quirk_tag_covers_its_text():
    line = ("- **curve** (figure-only; p.4): peaks near 0.77 A; falls to 0.2 A at 85 C. (figure) "
            "*Why it matters:* budget 0.8 A")
    assert check.md_claims(line, None, "quirk") == [(0.77, "figure"), (0.2, "figure"), (85, "figure"), (0.8, None)]
    assert check.md_claims(line, None, None)[:2] == [(0.77, None), (0.2, None)]     # outside a rendered block


def test_cell_number_with_glued_footnote_marker():
    from embarch_atlas.check import _CELL_NUM
    m = _CELL_NUM.match("3000(4)")
    assert m and m.group(2) == "3000"
    assert _CELL_NUM.match("1.7(1)").group(2) == "1.7"
    assert _CELL_NUM.match("±2%").group(3) == "%"


# ---------------------------------------------------------------- R across documents, M on RM-style prose

def test_refs_resolve_against_every_doc_under_docs(doc):
    atlas, docdir = doc
    other = atlas / "docs" / "demo-svd" / "records"
    other.mkdir(parents=True)
    common.write_jsonl(other / "register.jsonl", [{"id": "demo.reg.OTHER.CTRL", "kind": "register", "doc": "demo-svd",
                                                  "p": None, "q": None, "src": "svd", "prov": "deterministic",
                                                  "name": "CTRL", "addr": "0x0", "width": 32}])
    common.write_jsonl(docdir / "records" / "conflict.jsonl", [{
        "id": "syn1234.conflict.reg.CTRL.name", "kind": "conflict", "doc": ds.DOC_ID, "p": 5, "q": None,
        "src": "text", "prov": "deterministic", "refs": ["demo.reg.OTHER.CTRL", "demo.reg.OTHER.NOPE"],
        "sources": ["demo-svd OTHER.CTRL: x", f"{ds.DOC_ID} p5: y"], "text": "CTRL: name differs"}])
    rep = check.run_check(docdir)
    r = [i for i in result(rep, "syn1234.conflict.reg.CTRL.name")["issues"] if i.startswith("R:")]
    assert r == ["R: ref demo.reg.OTHER.NOPE resolves to no record in docs/"]     # the sibling doc's id resolves


def test_md_rm_prose_list_markers_tiers_tokens_identifiers_and_svd_cites():
    c = check.md_claims
    assert c("3. The pending bit is set only when the mask is open (p.3)") == []
    assert c("Tier 1. Every step cites the page.") == []
    assert c("- see `s/9.4.16-9.8.md`, `LINE4_15`, §6.3-§6.5 and 9.4.16-9.8 (p.3)") == []
    assert c("- the index holds 2,048 tokens and 2 raw tokens (p.3)") == []
    # an [SVD] cite covers the numbers before it in its clause, as a tag does; not the next clause
    assert c("- lines 55 and 66 [SVD]; restarts below 142 C (p.3)") == [(55, "svd"), (66, "svd"), (142, None)]
    cols = ["section", "pages", "raw tokens", "distilled"]
    assert c("| 4.1 main features | 81 | 947 | `s/4.1-4.4.md` |", cols) == [(81, None)]


def test_md_svd_numbers_are_counted_not_flagged(doc):
    _, docdir = doc
    f = docdir / "card.md"
    f.write_text("# card (p.3)\n\n4. Restarts below 142 C (p.3).\n- the SVD puts it at IRQ line 57 [SVD]\n"
                 "- a claim with no source 5151 (p.3)\n")
    m = check.check_markdown_file(check.DocText(docdir), f, {})
    assert m["stats"]["svd"] == 1 and [b["number"] for b in m["unanchored"]] == [5151]


def _twins(det_fields, llm_fields, reg_reset="0x00"):
    reg = {"id": "d.reg.B", "kind": "register", "name": "B", "addr": "0x02", "width": 8, "reset": reg_reset}
    det = [dict(reg, prov="deterministic")] + [dict(f, kind="field", reg="d.reg.B", prov="deterministic",
                                                    id="d.reg.B." + f["name"].replace(" ", "_").upper())
                                               for f in det_fields]
    llm = [dict(reg)] + [dict(f, kind="field", reg="d.reg.B", id="d.reg.B." + f["name"].replace(" ", "_"))
                         for f in llm_fields]
    return check.check_registers(det + llm)


def test_g_field_names_compare_without_case_or_separators():
    rep = _twins([{"name": "INPUT_LEVEL", "bits": [7, 0], "reset": "0x00"}],
                 [{"name": "Input Level", "bits": [7, 0], "reset": "0x00"}])
    assert rep["diff"] == [] and rep["verdict"]["d.reg.B"] == "agree"
    rep = _twins([{"name": "INPUT_LEVEL", "bits": [7, 0], "reset": "0x00"}],
                 [{"name": "LEVEL", "bits": [7, 0], "reset": "0x00"}])     # other words: still two fields
    assert {d["issue"] for d in rep["diff"]} == {"field INPUT_LEVEL only in deterministic", "field LEVEL only in llm"}


def test_g_what_the_parser_could_not_read_is_not_compared():
    # the template has no access column and no reset in the heading
    rep = _twins([{"name": "RDY", "bits": [7, 0], "reset": None}],
                 [{"name": "RDY", "bits": [7, 0], "reset": "0x00", "access": "ro"}], reg_reset=None)
    assert rep["diff"] == [], rep["diff"]
    # an LLM `reset: null` without a note is a claim (undefined) and still disagrees with a read value
    rep = _twins([{"name": "RDY", "bits": [7, 0], "reset": "0x00"}], [{"name": "RDY", "bits": [7, 0], "reset": None}])
    assert any("reset llm None det 0x00" in d["issue"] for d in rep["diff"])


def test_g_access_refinements_agree_contradictions_do_not():
    ok = _twins([{"name": "DONE", "bits": [7, 0], "reset": "0x00", "access": "ro"}],
                [{"name": "DONE", "bits": [7, 0], "reset": "0x00", "access": "rclr"}])
    assert ok["diff"] == []
    bad = _twins([{"name": "DONE", "bits": [7, 0], "reset": "0x00", "access": "ro"}],
                 [{"name": "DONE", "bits": [7, 0], "reset": "0x00", "access": "rw"}])
    assert any("access llm rw det ro" in d["issue"] for d in bad["diff"])


def test_g_reset_composition_the_document_contradicts_is_a_warning(doc):
    docdir = doc[1]
    recs = [{"id": "demo-conv.reg.CFG", "kind": "register", "doc": "demo-conv@B", "p": 1, "q": None, "src": "table",
             "name": "CFG", "addr": "0x09", "width": 8, "reset": "0x00", "note": "heading prints 0h"},
            {"id": "demo-conv.reg.CFG.EN", "kind": "field", "doc": "demo-conv@B", "p": 1, "q": None, "src": "table",
             "reg": "demo-conv.reg.CFG", "name": "EN", "bits": [7, 0], "reset": "0x10"}]
    conflict = {"id": "demo-conv.conflict.CFG.reset", "kind": "conflict", "doc": "demo-conv@B", "p": 1, "q": None,
                "src": "table", "refs": ["demo-conv.reg.CFG"], "sources": ["heading", "field table"],
                "text": "field resets compose to 0x10, the heading says 0x00"}
    rd = docdir / "records"
    for name, rows in (("register.jsonl", recs[:1]), ("field.jsonl", recs[1:])):
        with open(rd / name, "a") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
    without = check.run_check(docdir, render=False, use_pdf=False)
    assert any("field resets compose" in i for i in result(without, "demo-conv.reg.CFG")["issues"])
    with open(rd / "conflict.jsonl", "a") as f:
        f.write(json.dumps(conflict) + "\n")
    rep = check.run_check(docdir, render=False, use_pdf=False)
    assert not any("field resets compose" in i for i in result(rep, "demo-conv.reg.CFG")["issues"])
    assert any(w.startswith("G: 1 register resets disagree") for w in rep["warnings"])


def test_md_bare_bit_strings_are_not_numbers():
    assert check._md_numbers("field resets compose to 0000000000010000, the heading says 00000000") == []
    assert check._md_numbers("the count is 10000100 cycles") == [10000100.0]       # no leading 0: a number

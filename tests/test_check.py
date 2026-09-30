import copy

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

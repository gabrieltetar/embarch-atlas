"""fw_check: firmware configuration against a standard's requirements (`fwcheck.py`, class config-vs-standard).

Everything is synthetic and invented: a "Demo PD" standard whose power rule caps a fixed supply's current at
2500 mA and at the declared port power divided by its voltage, a demo Kconfig symbol, and PDO words built from
the public PDO bit layout with invented values. No real power-rules table is encoded here.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_join import make_project, run_join  # noqa: E402

from embarch_atlas import check, fwcheck, query, records as R  # noqa: E402
from embarch_atlas.common import read_json, write_json, write_jsonl, write_text  # noqa: E402

STD = "demo-pd@r1.0"
CONN = "/connector-a"
CONN_B = "/connector-b"


# ---------------------------------------------------------------- words, by the public layout

def fixed(mv: int, ma: int, *flags: int) -> int:
    w = ((mv // 50) << 10) | (ma // 10)
    for b in flags:
        w |= 1 << b
    return w


def variable(min_mv: int, max_mv: int, ma: int) -> int:
    return (0b10 << 30) | ((max_mv // 50) << 20) | ((min_mv // 50) << 10) | (ma // 10)


def battery(min_mv: int, max_mv: int, mw: int) -> int:
    return (0b01 << 30) | ((max_mv // 50) << 20) | ((min_mv // 50) << 10) | (mw // 250)


def pps(min_mv: int, max_mv: int, ma: int, limited: bool = False) -> int:
    return (0b11 << 30) | (int(limited) << 27) | ((max_mv // 100) << 17) | ((min_mv // 100) << 8) | (ma // 50)


def epr_avs(min_mv: int, max_mv: int, pdp_w: int) -> int:
    return (0b11 << 30) | (0b01 << 28) | ((max_mv // 100) << 17) | ((min_mv // 100) << 8) | pdp_w


def spr_avs(ma_15v: int, ma_20v: int) -> int:
    return (0b11 << 30) | (0b10 << 28) | ((ma_15v // 10) << 10) | (ma_20v // 10)


def test_pdo_words_decode_by_type():
    f = fwcheck.pd_pdo(fixed(5000, 1730, 29, 26))
    assert (f.type, f.mv, f.ma, f.drp, f.usb_comm, f.epr) == ("fixed", 5000, 1730, True, True, False)
    assert str(f) == "fixed 5000 mV 1730 mA"
    v = fwcheck.pd_pdo(variable(4750, 13450, 1210))
    assert (v.type, v.min_mv, v.max_mv, v.ma) == ("variable", 4750, 13450, 1210)
    b = fwcheck.pd_pdo(battery(6000, 9050, 7750))
    assert (b.type, b.min_mv, b.max_mv, b.mw) == ("battery", 6000, 9050, 7750)
    p = fwcheck.pd_pdo(pps(3700, 7300, 1450, limited=True))
    assert (p.type, p.min_mv, p.max_mv, p.ma, p.limited) == ("pps", 3700, 7300, 1450, True)
    e = fwcheck.pd_pdo(epr_avs(14300, 23100, 77))
    assert (e.type, e.min_mv, e.max_mv, e.pdp_w) == ("epr-avs", 14300, 23100, 77)
    s = fwcheck.pd_pdo(spr_avs(1330, 990))
    assert (s.type, s.ma_15v, s.ma_20v) == ("spr-avs", 1330, 990)
    assert str(s) == "spr-avs 1330 mA at 15 V, 990 mA at 20 V"
    sink = fwcheck.pd_sink_pdo(fixed(5000, 470, 28) | (0b10 << 23))
    assert (sink.higher_cap, sink.frs, sink.usb_suspend) == (True, 2, None)
    assert fwcheck.pd_pdo((0b11 << 30) | (0b11 << 28)).type == "reserved"


def test_the_invariant_evaluator_keeps_its_surface_and_a_fw_check_widens_it():
    with pytest.raises(ValueError, match="only abs/all/any/min/max/round/sum/len may be called"):
        check.safe_eval("pd_pdo(1)", {})
    with pytest.raises(ValueError, match="attribute .mv not allowed"):
        check.safe_eval("x.mv", {"x": 1})
    with pytest.raises(ValueError, match="Slice"):
        check.safe_eval("[1, 2, 3][1:]", {})
    assert fwcheck.safe("[pd_pdo(w).mv for w in ws[1:]]", {"ws": [fixed(5000, 100), fixed(9000, 100)]}) == [9000]
    assert fwcheck.safe("{'a': 1}[k]", {"k": "a"}) == 1
    with pytest.raises(ValueError, match="not allowed"):
        fwcheck.safe("pd_pdo(1).__class__", {})


def test_schema_takes_fw_check_on_requirements_and_named_checks():
    req = {"id": "demopd.req.4.1.fixed", "kind": "requirement", "doc": STD, "p": 3, "q": "x", "src": "text",
           "rid": "demopd.4.1.fixed", "text": "x", "fw_relevant": True,
           "fw_check": {"inputs": {"src": {"dt": "usb-c-connector", "prop": "source-pdos", "decode": "pd_pdo"},
                                   "drp": {"kconfig": "CONFIG_DEMO_PD_DRP"}, "pdp": {"declared": "pdp_w"}},
                        "applies_when": "drp == 'y'", "expr": "len(src) > 0"}}
    assert R.validate(req) == []
    two = dict(req, fw_check={"inputs": {"x": {"dt": "a", "prop": "b", "kconfig": "C"}}, "expr": "x"})
    assert R.validate(two)
    assert R.validate(dict(req, fw_check={"inputs": {"CONFIG_X": {"kconfig": "X"}}, "expr": "1"}))
    assert R.validate(dict(req, fw_check={"expr": "1", "guard": "1"}))
    chk = {"id": "demopd.check.fixed_rule", "kind": "check", "doc": STD, "p": 3, "q": None, "src": "table",
           "refs": ["demopd.req.4.1.fixed"], "fw_check": {"expr": "True"}}
    assert R.validate(chk) == []
    assert R.validate({k: v for k, v in chk.items() if k != "refs"})       # a fw_check names its requirements
    assert R.validate({**chk, "expr": "1 > 0"})                            # an invariant still needs its expect
    enum = R.schema()["$defs"]["fw_input"]["properties"]["decode"]["enum"]
    assert sorted(enum) == sorted(fwcheck.HELPERS)


# ---------------------------------------------------------------- a synthetic product with a standard

FIXED_RULE = {"inputs": {"src": {"dt": "usb-c-connector", "prop": "source-pdos", "decode": "pd_pdo"},
                         "pdp_w": {"declared": "pdp_w"}},
              "expr": "all(p.ma <= min(2500, pdp_w * 1000000 // p.mv) for p in src if p.type == 'fixed')"}
DRP_RULE = {"inputs": {"src": {"dt": "usb-c-connector", "prop": "source-pdos", "decode": "pd_pdo"}},
            "applies_when": "CONFIG_DEMO_PD_DRP == 'y'", "expr": "src[0].drp"}


def std_doc(ad: Path, extra: list[dict] | None = None) -> None:
    d = ad / "docs" / STD
    write_json(d / "doc.json", {"doc_id": STD, "kind": "standard", "pages": 6, "file": "DemoPD.pdf", "rev": "r1.0"})
    req = lambda sec, p, q, text, fc, **kw: {"id": f"demopd.req.{sec}", "kind": "requirement", "doc": STD, "p": p,
                                            "q": q, "src": "text", "prov": "llm-verified", "rid": f"demopd.{sec}",
                                            "text": text, "fw_relevant": True, "modality": "shall", "fw_check": fc, **kw}
    write_jsonl(d / "records" / "requirement.jsonl", [
        req("4.1.fixed", 3, "A Source shall limit each fixed supply to 2500 mA and to its port power",
            "each fixed supply at most 2500 mA and at most the port power over its voltage", FIXED_RULE, role="Source"),
        req("5.2.drp-flag", 5, "A dual-role port shall set the Dual-Role Power bit in its first PDO",
            "a dual-role port sets the Dual-Role Power bit in its first PDO", DRP_RULE),
    ] + (extra or []))


def connector(path: str, label: str, pdos: list[int], line: int) -> dict:
    return {"labels": [label], "compats": ["usb-c-connector"], "okay": True, "ordinal": 90 + line, "regs": [],
            "parent": "/", "props": {"source-pdos": {"val": pdos, "loc": f"fw:board.dts:{line}"},
                                     "power-role": {"val": "dual", "loc": f"fw:board.dts:{line + 1}"}}}


def pd_project(tmp: Path, pdos: list[int], drp: str | None = "n", toml: str | None = None,
               more: dict | None = None) -> tuple[Path, Path]:
    ad, atlas = make_project(tmp)
    std_doc(ad)
    dt = read_json(atlas / "dt.json")
    dt["nodes"][CONN] = connector(CONN, "usbc_a", pdos, 50)
    dt["nodes"].update(more or {})
    write_json(atlas / "dt.json", dt)
    syms = {"GPIO": {"t": "bool", "v": "y"}}
    if drp is not None:
        syms["DEMO_PD_DRP"] = {"t": "bool", "v": drp, "asg": [["fw:prj.conf", 7, drp]]}
    write_json(atlas / "kconfig.json", {"symbols": syms})
    write_text(ad / "atlas.toml", toml if toml is not None else
               f'[standards."{STD}"]\npdp_w = 20\n\n[standards."{STD}".ports.A]\nnode = "usbc_a"\n')
    return ad, atlas


def _by_class(rep: dict, cls: str) -> list[dict]:
    return [c for c in rep["candidates"] if c["class"] == cls]


GOOD = [fixed(5000, 2000), fixed(9000, 2000)]                   # 20 W: 5 V capped at 2500, 9 V at 2222
BAD = [fixed(5000, 2000), fixed(12000, 2500)]                   # 12 V may carry 1666 mA at 20 W, not 2500


def test_a_pdo_set_inside_the_invented_rule_passes(tmp_path):
    ad, atlas = pd_project(tmp_path, GOOD)
    run_join(ad, atlas)
    rep = read_json(atlas / "mismatches.json")
    assert not _by_class(rep, "config-vs-standard")
    checks = {c["check"]: c for c in rep["checks"]}
    row = checks["config vs standard: demopd.4.1.fixed"]
    assert (row["ran"], row["passed"], row["candidates"]) == (1, 1, 0)
    assert "demo-pd@r1.0 p3" in row["sources"]


def test_a_pdo_set_breaking_the_invented_rule_is_a_candidate_with_both_cites(tmp_path):
    ad, atlas = pd_project(tmp_path, BAD)
    run_join(ad, atlas)
    rep = read_json(atlas / "mismatches.json")
    [c] = _by_class(rep, "config-vs-standard")
    assert c["key"] == "demopd.4.1.fixed @ usbc_a"
    assert "Fails on usbc_a (port A)" in c["summary"] and "at most 2500 mA" in c["summary"]
    ev = " | ".join(c["evidence"])
    assert "requirement demopd.4.1.fixed (shall, Source): demo-pd@r1.0 p3" in ev          # the requirement's page
    assert "fixed 12000 mV 2500 mA" in ev and "@ fw:board.dts:50" in ev                   # the line that set it
    assert f'pdp_w = 20 engineer-declared, atlas.toml [standards."{STD}"].pdp_w' in ev
    assert c["settle"] == fwcheck.SETTLE
    checks = {x["check"]: x for x in rep["checks"]}
    assert (checks["config vs standard: demopd.4.1.fixed"]["ran"],
            checks["config vs standard: demopd.4.1.fixed"]["passed"]) == (1, 0)
    md = (atlas / "checks.md").read_text()
    assert "## Firmware configuration vs standards" in md and "### config-vs-standard (1)" in md
    assert "- demopd.4.1.fixed (requirement demopd.4.1.fixed (shall, Source): demo-pd@r1.0 p3, llm-verified): fails on usbc_a" in md
    out = query.mismatch_report(query.Atlas(ad), cls="config-vs-standard")
    assert "[config-vs-standard] demopd.4.1.fixed" in out and "fw:board.dts:50" in out


def test_the_guard_skips_the_rule_when_its_kconfig_symbol_is_off(tmp_path):
    ad, atlas = pd_project(tmp_path, GOOD, drp="n")              # first PDO has no DRP bit: would fail if run
    run_join(ad, atlas)
    rep = read_json(atlas / "mismatches.json")
    assert not any(c["key"].startswith("demopd.5.2") for c in rep["candidates"])
    assert "config vs standard: demopd.5.2.drp-flag" not in {c["check"] for c in rep["checks"]}
    assert rep["folded"][fwcheck.FOLD_GUARD] == ["demopd.5.2.drp-flag on usbc_a (CONFIG_DEMO_PD_DRP = 'n' @ fw:prj.conf:7)"]
    assert "skipped on usbc_a: applies_when `CONFIG_DEMO_PD_DRP == 'y'` is false" in (atlas / "checks.md").read_text()


def test_the_guard_reads_an_absent_symbol_as_n_and_runs_the_rule_when_on(tmp_path):
    ad, atlas = pd_project(tmp_path / "absent", GOOD, drp=None)
    run_join(ad, atlas)
    folded = read_json(atlas / "mismatches.json")["folded"][fwcheck.FOLD_GUARD]
    assert folded == ["demopd.5.2.drp-flag on usbc_a (CONFIG_DEMO_PD_DRP = 'n' @ not a symbol of this build)"]
    ad, atlas = pd_project(tmp_path / "on", GOOD, drp="y")
    run_join(ad, atlas)
    [c] = [c for c in read_json(atlas / "mismatches.json")["candidates"] if c["key"].startswith("demopd.5.2")]
    assert "CONFIG_DEMO_PD_DRP = 'y' @ fw:prj.conf:7" in c["evidence"]
    ad, atlas = pd_project(tmp_path / "on-ok", [fixed(5000, 2000, 29)], drp="y")
    run_join(ad, atlas)
    assert not _by_class(read_json(atlas / "mismatches.json"), "config-vs-standard")


def test_each_declared_port_runs_with_its_own_values(tmp_path):
    toml = (f'[standards."demo-pd"]\npdp_w = 20\n\n[standards."demo-pd".ports.A]\nnode = "usbc_a"\n\n'
            f'[standards."demo-pd".ports.B]\nnode = "{CONN_B}"\npdp_w = 30\n')
    ad, atlas = pd_project(tmp_path, BAD, toml=toml, more={CONN_B: connector(CONN_B, "usbc_b", BAD, 60)})
    run_join(ad, atlas)
    rep = read_json(atlas / "mismatches.json")
    assert [c["key"] for c in _by_class(rep, "config-vs-standard")] == ["demopd.4.1.fixed @ usbc_a"]   # 30 W: 12 V 2500 mA fits
    row = {c["check"]: c for c in rep["checks"]}["config vs standard: demopd.4.1.fixed"]
    assert (row["ran"], row["passed"]) == (2, 1)
    md = (atlas / "checks.md").read_text()
    assert "fails on usbc_a; passes on usbc_b" in md


def test_declared_values_are_validated_at_join(tmp_path):
    toml = (f'[standards."{STD}"]\npdp_w = 20\npdp = 25\nnotes = [1, 2]\n\n[standards."{STD}".ports.A]\nnode = "usbc_z"\n\n'
            '[standards."other-spec@r9"]\nx = 1\n')
    ad, atlas = pd_project(tmp_path, GOOD, toml=toml)
    run_join(ad, atlas)
    rep = read_json(atlas / "mismatches.json")
    why = [n["why"] for n in rep["not_run"] if n["check"].startswith("atlas.toml [standards]")]
    assert f'[standards."{STD}"] notes: a declared value is a number, a string or a boolean' in why
    assert '[standards."other-spec@r9"] names no document in docs/ (a doc_id, or its slug before `@`)' in why
    assert f"[standards.\"{STD}\"].ports.A node 'usbc_z' is no okay devicetree node" in why
    md = (atlas / "checks.md").read_text()
    assert f'Declared in atlas.toml but read by no fw_check (a misspelt key?): [standards."{STD}"] pdp' in md


def test_a_rule_that_cannot_run_says_why_and_never_counts_as_passed(tmp_path):
    ad, atlas = pd_project(tmp_path, GOOD, toml="")             # nothing declared: pdp_w has no value
    std_doc(ad, [{"id": "demopd.req.6.1.bad-expr", "kind": "requirement", "doc": STD, "p": 6, "q": "q", "src": "text",
                  "rid": "demopd.6.1.bad-expr", "text": "t", "fw_relevant": True,
                  "fw_check": {"inputs": {"src": {"dt": "usb-c-connector", "prop": "source-pdos"}},
                               "expr": "src.nope > 1"}}])
    run_join(ad, atlas)
    rep = read_json(atlas / "mismatches.json")
    nr = {n["check"]: n["why"] for n in rep["not_run"]}
    assert "declared `pdp_w`: atlas.toml has no [standards.\"demo-pd@r1.0\"]" in nr["config vs standard: demopd.4.1.fixed on usbc_a"]
    assert nr["config vs standard: demopd.6.1.bad-expr on usbc_a"].startswith("expr: ValueError: attribute .nope not allowed")
    names = {c["check"] for c in rep["checks"]}
    assert "config vs standard: demopd.4.1.fixed" not in names and "config vs standard: demopd.6.1.bad-expr" not in names


def test_a_check_record_names_the_requirements_it_encodes_and_doc_check_leaves_it_to_join(tmp_path):
    ad, atlas = pd_project(tmp_path, BAD)
    d = ad / "docs" / STD
    write_jsonl(d / "records" / "check.jsonl", [
        {"id": "demopd.check.pdo_count", "kind": "check", "doc": STD, "p": 4, "q": None, "src": "table",
         "prov": "llm-verified", "refs": ["demopd.req.4.1.fixed"], "why": "at most one fixed supply above 9 V",
         "fw_check": {"inputs": {"src": {"dt": "usbc_a", "prop": "source-pdos", "decode": "pd_pdo"}},
                      "expr": "sum(1 for p in src if p.type == 'fixed' and p.mv > 9000) <= 1"}}])
    for p in range(1, 7):
        write_text(d / "pages" / f"p{p:03d}.txt", "page\n")
    rep = check.run_check(d, write=False, use_pdf=False)
    by = {r["id"]: r for r in rep["records"]}
    assert not any(i.startswith("X:") for i in by["demopd.check.pdo_count"]["issues"])
    run_join(ad, atlas)
    m = read_json(atlas / "mismatches.json")
    row = {c["check"]: c for c in m["checks"]}["config vs standard: demopd.check.pdo_count"]
    assert (row["ran"], row["passed"]) == (1, 1)
    assert "requirement demopd.4.1.fixed (shall, Source): demo-pd@r1.0 p3; check demopd.check.pdo_count: demo-pd@r1.0 p4" == row["sources"]


def test_mismatch_report_runs_the_rules_on_a_named_build_and_writes_nothing(tmp_path, monkeypatch):
    from embarch_atlas import buildq
    ad, atlas = pd_project(tmp_path, GOOD)
    run_join(ad, atlas)
    before = (atlas / "mismatches.json").read_bytes()
    dt = read_json(atlas / "dt.json")
    dt["nodes"][CONN]["props"]["source-pdos"] = {"val": BAD, "loc": "fw:board.dts:77"}
    src = buildq.Source("build /tmp/other (app v2)", tmp_path / "none" / ".config", [], [])
    src.dt = dt
    src.kconfig = {"symbols": {"DEMO_PD_DRP": {"t": "bool", "v": "n"}}}
    monkeypatch.setattr(buildq, "versus", lambda a, vs: src)
    out = query.mismatch_report(query.Atlas(ad), build="other")
    assert out.startswith("src: build /tmp/other (app v2) devicetree + kconfig")
    assert "[config-vs-standard] demopd.4.1.fixed" in out and "fw:board.dts:77" in out
    assert "Skipped (applies_when false):" in out
    assert (atlas / "mismatches.json").read_bytes() == before


def test_the_procedures_worked_example_is_a_valid_fw_check():
    import ast
    import json
    import re
    text = (Path(__file__).parents[1] / "skills" / "atlas-translate-docs" / "reference" / "standard-procedure.md").read_text()
    blocks = re.findall(r"```json\n(.*?)```", text, re.S)
    fcs = []
    for b in blocks:
        obj = json.loads(b if b.lstrip().startswith("{") else "{" + b + "}")
        fcs.append(obj["fw_check"])
        if obj.get("kind"):
            assert R.validate(obj) == [], obj["id"]
    assert len(fcs) == 3
    for fc in fcs:
        assert fwcheck.shape_error(fc) is None
        req = {"id": "demopd.req.x.y", "kind": "requirement", "doc": STD, "p": 1, "q": "x", "src": "text",
               "rid": "demopd.x", "text": "x", "fw_relevant": True, "fw_check": fc}
        assert R.validate(req) == []
        for e in (fc["expr"], fc.get("applies_when")):
            if e:
                ast.parse(e, mode="eval")

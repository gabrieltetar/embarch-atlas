"""limits: DT unit properties vs the part's datasheet limit/timing records.

Everything is synthetic: an invented charger part (doc `demo-chg@A`, part `DCG100`,
compatible `acme,demochg`) with abs/recommended/register/EC rows, one variant row for
another part number, a register-setting point, a typ-only row, a row stated at another
sense resistor, and a rule that states `output current limit = VSNS/RSNS`. The DT node
carries one property for each outcome: inside, over abs max, outside EC, scaled through
the sense resistor, needs a declared formula, declared (plain, scaled, none, unknown),
not comparable, and no datasheet limit.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from embarch_atlas import limits  # noqa: E402
from embarch_atlas.common import read_json, write_json, write_jsonl  # noqa: E402

DOC = "demo-chg@A"
NODE = "/soc/i2c@40005400/chg@6a"


def _lim(id_, group, sym, unit, min_=None, max_=None, typ=None, param=None, cond=None, p=5, kind="limit"):
    r = {"id": id_, "kind": kind, "doc": DOC, "p": p, "q": f"{sym} {min_} {typ} {max_}", "src": "table",
         "prov": "llm-verified", "group": group, "sym": sym, "unit": unit}
    for k, v in (("min", min_), ("max", max_), ("typ", typ), ("param", param), ("cond", cond)):
        if v is not None:
            r[k] = v
    return r


def demo_records() -> tuple[list[dict], list[dict], list[dict]]:
    lim = [
        _lim("demo-chg.abs.VIN", "abs", "VIN", "V", -0.3, 28, param="VIN to GND", p=4),
        _lim("demo-chg.abs.VIN.dcg120", "abs", "VIN", "V", -0.3, 20, param="VIN to GND", cond="DCG120 only", p=4),
        _lim("demo-chg.recommended.VIN", "recommended", "VIN", "V", 3.5, 24, param="input voltage"),
        _lim("demo-chg.abs.VOUT", "abs", "VOUT, SW", "V", -0.3, 20, param="VOUT, SW", p=4),
        _lim("demo-chg.recommended.VOUT", "recommended", "VOUT", "V", 1, 16, param="output voltage"),
        _lim("demo-chg.ec.VSNS.64h", "ec", "VSNS", "mV", 48, 52, typ=50, param="sense regulation",
             cond="ILIM_CODE = 64h", p=7),
        _lim("demo-chg.ec.IDISCHG", "ec", "IDISCHG", "mA", 40, 170, typ=100, param="output discharge current", p=7),
        _lim("demo-chg.ec.IQ", "ec", "IQ", "µA", typ=30, max_=50, param="quiescent current", p=6),
        _lim("demo-chg.ec.VSC", "ec", "VSC", "mV", 150, 250, typ=200, param="short-circuit threshold", p=8),
        _lim("demo-chg.ec.fSW", "ec", "fSW", "MHz", typ=1.2, param="switching frequency", p=6),
        _lim("demo-chg.register.ICHG.range_5mohm", "register", "ICHG", "mA", 0, 6000,
             param="charge current setting range", cond="RSNS = 5 mΩ", p=30),
        _lim("demo-chg.register.IPRE.range_10mohm", "register", "IPRE", "mA", 50, 500,
             param="precharge current setting range", cond="RSNS = 10 mΩ", p=30),
    ]
    tim = [_lim("demo-chg.timing.tSS", "ec", "tSS", "ms", 3, 5, typ=4, param="soft-start time", p=9, kind="timing")]
    rule = [{"id": "demo-chg.rule.ilim", "kind": "rule", "doc": DOC, "p": 20, "src": "text", "prov": "llm-verified",
             "q": "The step between ISP and ISN is 0.5 mV.", "cond": "RSNS between ISP and ISN",
             "text": "output current limit = VSNS/RSNS; programmable in 0.5 mV steps up to 60 mV"}]
    return lim, tim, rule


def _p(val, line):
    return {"val": val, "loc": f"fw:board.dts:{line}"}


def demo_dt() -> dict:
    props = {
        "input-voltage-max-microvolt": _p(30_000_000, 50),        # over abs max (28 V) and recommended (24 V)
        "input-voltage-min-microvolt": _p(4_000_000, 51),         # inside
        "output-voltage-max-microvolt": _p(12_000_000, 52),       # inside
        "output-current-max-microamp": _p(5_000_000, 53),         # 5 A x 10 mOhm = 50 mV <= 60 mV (rule)
        "charge-current-max-microamp": _p(3_000_000, 54),         # row at 5 mOhm only: needs a formula
        "precharge-current-microamp": _p(200_000, 55),            # declared
        "short-circuit-microamp": _p(20_000_000, 56),             # declared, times the sense resistor
        "acme,soft-start-ms": _p(2, 57),                          # outside the EC range (3..5 ms)
        "acme,switching-frequency-hz": _p(1_000_000, 58),         # typ-only row: not comparable
        "acme,ntc-hot-millicelsius": _p(60_000, 59),              # no datasheet limit
        "acme,wdt-timeout-ms": _p(1000, 60),                      # declared: none applies
        "acme,ilim-percent": _p(80, 61),                          # declared to an id no doc has
        "sense-resistor-milliohm": _p(10, 62),
        "reg": _p([0x6A], 49),
    }
    return {"nodes": {
        "/soc/i2c@40005400": {"labels": ["i2c1"], "compats": ["vendor,soc-i2c"], "okay": True, "parent": "/soc",
                              "props": {}},
        NODE: {"labels": ["chg0"], "compats": ["acme,demochg"], "okay": True, "parent": "/soc/i2c@40005400", "props": props},
        f"{NODE}/otg": {"labels": [], "compats": [], "okay": True, "parent": NODE,
                        "props": {"output-voltage-max-microvolt": _p(22_000_000, 70)}},
        "/pack": {"labels": ["pack0"], "compats": ["acme,pack"], "okay": True, "parent": "/",
                  "props": {"voltage-max-design-microvolt": _p(8_400_000, 80)}},
        "/off": {"labels": ["off0"], "compats": ["acme,demochg"], "okay": False, "parent": "/", "props": {}},
    }}


BINDINGS = {"prop_limits": {"acme,demochg": {
    "precharge-current-microamp": "demo-chg.register.IPRE.range_10mohm",
    "short-circuit-microamp": {"records": ["demo-chg.ec.VSC"], "times": "sense-resistor-milliohm"},
    "acme,wdt-timeout-ms": None,
    "acme,ilim-percent": "demo-chg.limit.nope",
}}}


def make_atlas(tmp: Path, route: str = "bindings") -> tuple[Path, Path]:
    ad = tmp / "fw" / "embarch" / "atlas"
    d = ad / "docs" / DOC
    write_json(d / "doc.json", {"doc_id": DOC, "kind": "datasheet", "part": "DCG100", "file": "demo-chg.pdf", "pages": 40})
    lim, tim, rule = demo_records()
    write_jsonl(d / "records" / "limit.jsonl", lim)
    write_jsonl(d / "records" / "timing.jsonl", tim)
    write_jsonl(d / "records" / "rule.jsonl", rule)
    b = dict(BINDINGS)
    if route == "bindings":
        b["compat_parts"] = {"acme,demochg": "DCG100"}
        pins = {}
    else:                                   # the I2C identity join: node -> board part -> its docs
        pins = {"i2c": {"identity": [{"node": NODE, "status": "identified", "part": "M:U5", "mpn": "DCG100RTE",
                                      "reg": 0x6A}]},
                "part_docs": {"M:U5": [DOC]}}
    write_json(ad / "boards" / "bindings.json", b)
    atlas = ad / "atlases" / "demo@000000000001"
    write_json(atlas / "atlas.json", {"id": "demo@000000000001", "target": "demo", "board": "demo_board",
                                      "commit": "0" * 40, "build": {}})
    write_json(atlas / "dt.json", demo_dt())
    if pins:
        write_json(atlas / "pins.json", pins)
    return ad, atlas


@pytest.fixture(params=["bindings", "identity"])
def rep(request, tmp_path):
    ad, atlas = make_atlas(tmp_path, request.param)
    return limits.run(ad, atlas, log=lambda *a: None), atlas


def _prop(rep, name, node=NODE):
    return next(x for x in rep["properties"] if x["prop"] == name and x["node"] == node)


def _find(rep, cls):
    return [f for f in rep["findings"] if f["class"] == cls]


# ---------------------------------------------------------------- units and words

def test_prop_units_to_si():
    assert limits.prop_unit("output-current-max-microamp") == ("output-current-max", "A", 1e-6)
    assert limits.prop_unit("acme,input-voltage-min-millivolt") == ("input-voltage-min", "V", 1e-3)
    assert limits.prop_unit("sense-resistor-milliohm")[1:] == ("ohm", 1e-3)
    assert limits.prop_unit("shunt-micro-ohms")[1:] == ("ohm", 1e-6)
    assert limits.prop_unit("psys-resistor-ohms")[1:] == ("ohm", 1.0)
    assert limits.prop_unit("charge-full-design-microamp-hours")[1:] == ("Ah", 1e-6)
    assert limits.prop_unit("trip-point-millicelsius")[1:] == ("degC", 1e-3)
    assert limits.prop_unit("debounce-interval-ms")[1:] == ("s", 1e-3)
    assert limits.prop_unit("startup-delay-us")[1:] == ("s", 1e-6)
    assert limits.prop_unit("clock-hz")[1:] == ("Hz", 1.0)
    assert limits.prop_unit("full-threshold-percent")[1:] == ("%", 1.0)
    assert limits.prop_unit("acme,slew-rate-microvolt-per-microsecond") == ("slew-rate", "V/s", 1.0)
    for name in ("status", "reg", "bus-width", "sense-resistor", "mode-select"):
        assert limits.prop_unit(name) is None, name


def test_record_units_and_bounds():
    assert limits.record_unit("mV") == ("V", 1e-3)
    assert limits.record_unit("kΩ") == ("ohm", 1e3)
    assert limits.record_unit("μA") == ("A", 1e-6)                 # Greek mu as well as the micro sign
    assert limits.record_unit("°C") == ("degC", 1.0)
    assert limits.record_unit("mAh") == ("Ah", 1e-3)
    assert limits.record_unit("MHz") == ("Hz", 1e6)
    assert limits.record_unit("mV/µs")[0] == "V/s" and abs(limits.record_unit("mV/µs")[1] - 1e3) < 1e-9
    for u in ("% of reading", "bits", "LSB", "writes", None, ""):
        assert limits.record_unit(u) is None, u
    assert limits.num("±51.2", "min") == -51.2 and limits.num("±51.2", "max") == 51.2
    assert limits.num("−0.3") == -0.3
    assert limits.num("VOUT+0.3") is None and limits.num(None) is None


def test_words_read_symbols_and_prose():
    assert limits.wordset("VOUT", True) == ({"output"}, None)
    assert limits.wordset("VSYS_MIN_RNG", True) == ({"system"}, "min")
    assert limits.wordset("InputVoltage", True)[0] == {"input"}
    assert limits.wordset("tSS", True)[0] == {"softstart"}
    assert limits.wordset("soft-start time")[0] == {"softstart"}
    assert limits.wordset("ILIM_AVG", True) == ({"average"}, "max")
    assert "voutmax" in limits.prop_syms(["output"], "V", "max")


def test_sense_values_points_and_formulas():
    assert limits.sense_values("5-mΩ RAC, 0°C to 85°C") == [0.005]
    assert limits.sense_values("RSNS = 10 mOhm") == [0.01]
    assert limits.sense_values("RILIM = 20k, RFSW = 100k") == []
    for cond in ("ILIM_CODE = 64h", "IOUT_LIMIT = 11100100b", "REG0x0F() = 0x4E00H", "CTRL[2:0] = 111", "CDC = 111"):
        assert limits.POINT_RE.search(cond), cond
    for cond in ("VIN = 8V, VOUT = 20V", "RFSW = 100k", "TA = 25°C", "VCC = 2.7..5.5 V"):
        assert not limits.POINT_RE.search(cond), cond
    m = limits.F_DIV.search("output current limit = VSNS/RSNS; up to 60 mV")
    assert (m["lhs"], m["v"], m["r"]) == ("output current limit", "VSNS", "RSNS")
    assert limits.is_sense_sym("RSNS") and not limits.is_sense_sym("RFB_UP")
    assert limits.stated_bounds("0.5 mV steps up to 60 mV (60 mV max)", "V") == (None, 0.06)
    assert not limits.F_DIV.search("VOUT = VREF x (1 + RFB_UP / RFB_BT)")
    assert limits.stated_bounds("IMAX 5 A", "A") == (None, None)            # a symbol is not the word "max"


def test_a_relation_stated_in_a_limit_row():
    class Docs:
        def records(self, d, kind):
            return [{"id": "x.ec.ILIM", "kind": "limit", "doc": "x@1", "p": 3,
                     "param": "current limit, ILIM = VLIM / RSENSE"}] if kind == "limit" else []
    (f,) = limits.formulas(Docs(), ["x@1"])
    assert (f.lhs, f.v, f.r, f.id) == ("ILIM", "VLIM", "RSENSE", "x.ec.ILIM")


# ---------------------------------------------------------------- the analysis

def test_counts_and_summary(rep):
    r, atlas = rep
    c = r["counts"]
    assert c["properties"] == 14 and c["nodes"] == 2             # the chg node's 13 unit props + the inherited child
    assert c["unresolved_properties"] == 1 and c["unresolved_nodes"] == 1
    assert c["declared"] == 3 and c["compared"] == 8 and c["inside"] == 5
    assert c["outside"] == 3 and c["needs_formula"] == 1 and c["not_comparable"] == 1
    assert c["declared_none"] == 1
    assert (atlas / "limits.json").is_file()
    assert read_json(atlas / "limits.json")["summary"] == r["summary"]


def test_inside_and_over_abs_max(rep):
    r, _ = rep
    x = _prop(r, "input-voltage-min-microvolt")
    assert x["status"] == "inside" and x["mapping"] == "proposed"
    assert {c["records"][0] for c in x["comparisons"]} == {"demo-chg.abs.VIN", "demo-chg.recommended.VIN"}
    assert any(s["record"] == "demo-chg.abs.VIN.dcg120" and s["why"].startswith("variant") for s in x["skipped"])
    assert _prop(r, "output-voltage-max-microvolt")["status"] == "inside"
    over = _find(r, "dt-limit-over-abs-max")
    assert [f["key"] for f in over] == ["chg0.input-voltage-max-microvolt", "otg.output-voltage-max-microvolt"]
    f = over[0]
    assert f["severity"] == "error"
    assert "input-voltage-max-microvolt = 30 V is outside demo-chg.abs.VIN [-300 mV, 28 V] (abs, proposed)" in f["summary"]
    assert "also outside 1 more row" in f["summary"]                # the recommended row, weighed lower
    assert f["evidence"][0] == "DT fw:board.dts:50: input-voltage-max-microvolt = 30000000 (30 V)"
    assert any("demo-chg.abs.VIN" in e and "demo-chg@A p4" in e for e in f["evidence"])
    assert "boards/bindings.json prop_limits" in f["settle"]         # proposed: the settle says how to confirm it
    assert "parent" in _prop(r, "output-voltage-max-microvolt", f"{NODE}/otg")["part_via"]


def test_ec_range_is_info_and_noise_is_not_mapped(rep):
    r, _ = rep
    ec = _find(r, "dt-limit-outside-ec")
    assert [f["key"] for f in ec] == ["chg0.acme,soft-start-ms"] and ec[0]["severity"] == "info"
    assert "demo-chg.timing.tSS [3 ms, 5 ms]" in ec[0]["summary"]
    x = _prop(r, "output-current-max-microamp")
    assert not any(m["record"] in ("demo-chg.ec.IDISCHG", "demo-chg.ec.IQ") for m in x["mapped"])
    nc = _prop(r, "acme,switching-frequency-hz")
    assert nc["status"] == "not-comparable" and nc["skipped"][0]["why"].startswith("no numeric bound")
    assert "chg0.acme,switching-frequency-hz (no numeric bound)" in r["folded"][limits.FOLD_NOT_COMPARABLE]


def test_sense_scaled_current_needs_a_stated_relation(rep):
    r, _ = rep
    x = _prop(r, "output-current-max-microamp")
    assert x["status"] == "inside" and x["formula"]["record"] == "demo-chg.rule.ilim"
    (c,) = x["comparisons"]
    assert c["via"] == "scaled" and c["group"] == "rule" and c["ranges"] == ["[-, 60 mV]"]
    assert abs(c["scaled"]["values_si"][0] - 0.05) < 1e-12        # 5 A x 10 mOhm
    assert any(s["record"] == "demo-chg.ec.VSNS.64h" and "register setting" in s["why"] for s in x["skipped"])
    y = _prop(r, "charge-current-max-microamp")
    assert y["status"] == "needs-formula" and not y["comparisons"]
    assert any("another sense resistor" in s["why"] for s in y["skipped"])
    (f,) = _find(r, "dt-limit-needs-formula")
    assert f["key"] == "chg0.charge-current-max-microamp" and f["check"] == limits.CHECK_SENSE
    assert "sense-resistor-milliohm = 10 mΩ" in f["summary"] and "stated at another sense resistor" in f["summary"]
    assert any(e.startswith("sense: DT fw:board.dts:62") for e in f["evidence"])
    checks = {c["check"]: c for c in r["checks"]}
    assert (checks[limits.CHECK_SENSE]["ran"], checks[limits.CHECK_SENSE]["passed"]) == (2, 1)


def test_declared_mappings_win(rep):
    r, _ = rep
    x = _prop(r, "precharge-current-microamp")
    assert x["mapping"] == "declared" and x["status"] == "inside"
    assert x["comparisons"][0]["mapping"] == ["declared"]
    y = _prop(r, "short-circuit-microamp")
    assert y["status"] == "inside" and y["comparisons"][0]["via"] == "scaled"
    assert abs(y["comparisons"][0]["scaled"]["values_si"][0] - 0.2) < 1e-12   # 20 A x 10 mOhm, declared
    assert _prop(r, "acme,wdt-timeout-ms")["status"] == "declared-none"
    assert _prop(r, "acme,ilim-percent")["status"] == "declared-unknown"
    (f,) = _find(r, "dt-limit-declared-unknown")
    assert "demo-chg.limit.nope" in f["summary"] and f["severity"] == "warn"


def test_no_limit_and_unresolved_are_counted_not_listed(rep):
    r, _ = rep
    no = r["folded"][limits.FOLD_NO_LIMIT]
    assert "chg0.acme,ntc-hot-millicelsius" in no and "chg0.sense-resistor-milliohm" in no
    assert not any(f["key"] in no for f in r["findings"])
    assert r["folded"][limits.FOLD_NO_PART] == ["pack0.voltage-max-design-microvolt"]
    assert not any(x["node"] == "/off" for x in r["properties"])


def test_no_records_is_not_run(tmp_path):
    ad, atlas = make_atlas(tmp_path)
    for f in (ad / "docs" / DOC / "records").glob("*.jsonl"):
        f.unlink()
    r = limits.run(ad, atlas, log=lambda *a: None)
    assert any(n["check"] == f"DT limits vs datasheet: {DOC}" for n in r["not_run"])
    assert r["counts"]["compared"] == 0


# ---------------------------------------------------------------- the join folds it into the mismatch report

def test_join_reports_the_classes(tmp_path):
    from test_join import make_project, run_join
    from embarch_atlas import mismatch
    ad, atlas = make_project(tmp_path)
    d = ad / "docs" / DOC
    write_json(d / "doc.json", {"doc_id": DOC, "kind": "datasheet", "part": "DCG100", "file": "demo-chg.pdf", "pages": 40})
    lim, tim, rule = demo_records()
    write_jsonl(d / "records" / "limit.jsonl", lim)
    write_jsonl(d / "records" / "timing.jsonl", tim)
    write_jsonl(d / "records" / "rule.jsonl", rule)
    write_json(ad / "boards" / "bindings.json", {**BINDINGS, "compat_parts": {"acme,demochg": "DCG100"}})
    dt = read_json(atlas / "dt.json")
    for path, n in demo_dt()["nodes"].items():
        if path != "/soc/i2c@40005400":
            dt["nodes"][path] = {**n, "ordinal": 90, "regs": []}
    write_json(atlas / "dt.json", dt)
    run_join(ad, atlas)
    mm = read_json(atlas / "mismatches.json")
    classes = {c["class"] for c in mm["candidates"]}
    assert {"dt-limit-over-abs-max", "dt-limit-outside-ec", "dt-limit-needs-formula",
            "dt-limit-declared-unknown"} <= classes
    assert all(c in mismatch.ORDER for c in limits.ORDER)
    checks = {c["check"]: c for c in mm["checks"]}
    assert (checks[limits.CHECK]["ran"], checks[limits.CHECK]["passed"]) == (8, 5)
    assert not any(n["check"] == "DT limits vs datasheet" for n in mm["not_run"])
    assert "chg0.acme,ntc-hot-millicelsius" in mm["folded"][limits.FOLD_NO_LIMIT]
    assert "mon_a.sense-resistor-milliohm" in mm["folded"][limits.FOLD_NO_LIMIT]   # I2C identity -> sns701@a, no ohm row
    text = (atlas / "checks.md").read_text()
    assert "## DT limits vs datasheet" in text and text.index("## DT limits vs datasheet") < text.index("## Candidates")
    assert "- chg0.output-current-max-microamp = 5 A: x sense-resistor-milliohm 10 mΩ = 50 mV vs demo-chg.rule.ilim" in text
    assert text.index("### dt-limit-over-abs-max") < text.index("### dt-limit-outside-ec")        # ranked
    assert "fw:board.dts:50" in text
    assert (atlas / "limits.json").is_file()

"""mismatch: checks run and passed first, candidates ranked by class, expected items folded."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_join import make_project, run_join  # noqa: E402

from embarch_atlas import mismatch  # noqa: E402
from embarch_atlas.common import read_json, write_json, write_jsonl  # noqa: E402


def _report(tmp_path, **kw):
    ad, atlas = make_project(tmp_path, **kw)
    run_join(ad, atlas)
    return ad, atlas, read_json(atlas / "mismatches.json")


def _classes(rep):
    out = {}
    for c in rep["candidates"]:
        out.setdefault(c["class"], []).append(c)
    return out


def test_candidates_by_class(tmp_path):
    _, _, rep = _report(tmp_path)
    by = _classes(rep)
    assert [c["key"] for c in by["cross-board-net-name"]] == ["M:J1-3"]
    assert "MON_ALERT" in by["cross-board-net-name"][0]["summary"]
    assert [c["key"] for c in by["dt-comment-vs-net"]] == ["PA5"]
    assert "LED_WRONG" in by["dt-comment-vs-net"][0]["summary"]
    assert [c["key"] for c in by["dt-value-vs-bom"]] == ["/soc/i2c@40005400/mon-b@45"]
    assert [c["key"] for c in by["dt-no-driver"]] == ["PA0"]
    assert [c["key"] for c in by["connected-no-dt"]] == ["PA2"]
    assert [c["key"] for c in by["pin-shared-in-dt"]] == ["PA1"]
    assert "wired-OR" in by["pin-shared-in-dt"][0]["summary"]          # ALERT is open-drain per the pin record
    assert [c["key"] for c in by["exti-line-shared"]] == ["EXTI1"]
    assert [c["key"] for c in by["dni-on-path"]] == ["PA0"]
    assert by["polarity-resolved"][0]["severity"] == "pass"
    for cls in ("device-signal-mismatch", "i2c-address-strap", "i2c-bus-membership", "pull-conflict", "open-drain-no-pull"):
        assert cls not in by, cls
    for c in rep["candidates"]:
        assert c["evidence"] and c["settle"], c


def test_checks_counts_and_folding(tmp_path):
    _, atlas, rep = _report(tmp_path)
    checks = {c["check"]: c for c in rep["checks"]}
    assert (checks["I2C device identity by address strap"]["ran"], checks["I2C device identity by address strap"]["passed"]) == (2, 2)
    assert checks["I2C device signals land on the DT pins"]["passed"] == 2
    assert checks["DT comment net names vs netlist"]["ran"] == 3
    assert checks["open-drain far end has a pull"]["passed"] >= 1
    assert checks["net name agrees across a board connector"]["ran"] == 6
    folded = rep["folded"]
    assert any("PA13" in x for x in folded["connected, no DT: debug/boot/oscillator pins (expected)"])
    assert folded["test-point-only pins"] == ["PC6 PC6"]
    text = (atlas / "checks.md").read_text()
    assert text.index("## Checks run") < text.index("## Candidates, ranked by class")
    assert text.splitlines()[4].startswith("**") and "checks" in text.splitlines()[4]
    # ranked: the cross-board name leads the candidates
    first = text.index("### ")
    assert text[first:].startswith("### cross-board-net-name")


def test_record_fed_hooks(tmp_path):
    ad, atlas = make_project(tmp_path, conflict=True)
    write_jsonl(atlas / "second_look.jsonl", [
        {"id": "sns701.ec.IQ.active", "verdict": "conflict", "value": 90, "unit": "µA", "cond": "active mode", "q": "IQ 60 90",
         "p": 6, "reader": "second-look", "doc": "sns701@a", "first": {"max": 80}},
        {"id": "sns701.ec.VS", "verdict": "confirmed", "p": 5, "q": "VS 2.7 5.5", "reader": "second-look"},
        {"id": "sns701.quirk.alert_default", "verdict": "unresolved", "reader": "second-look"}])
    run_join(ad, atlas)
    rep = read_json(atlas / "mismatches.json")
    by = _classes(rep)
    assert by["driver-vs-datasheet"][0]["key"] == "sns701.conflict.alert_latch"
    assert any("drivers/sns.c:88" in e for e in by["driver-vs-datasheet"][0]["evidence"])
    assert by["document-reading-conflict"][0]["key"] == "sns701.ec.IQ.active"
    checks = {c["check"]: c for c in rep["checks"]}
    assert (checks["second look agrees with the first pass"]["ran"], checks["second look agrees with the first pass"]["passed"]) == (2, 1)
    assert rep["folded"]["second look unresolved (record stays llm-unverified)"] == ["sns701.quirk.alert_default"]


def test_expected_pin_names():
    for n in ("SWDIO", "PA14-BOOT0", "nRST_LINE", "PC14-OSC32_IN", "SWCLK", "JTAG_TDO"):
        assert mismatch.EXPECTED_RE.search(n), n
    for n in ("SLEEP_EN", "LED_STATUS", "RESETTABLE_FUSE_EN"):
        assert not mismatch.EXPECTED_RE.search(n), n


def test_active_low_name():
    assert mismatch.active_low_name("nIRQ")
    assert mismatch.active_low_name("RESET_N")
    assert not mismatch.active_low_name("LED_STATUS")
    assert not mismatch.active_low_name("node_A")


def test_driver_source_detection():
    assert mismatch.is_driver_source("driver: fw:drivers/x.c:12")
    assert mismatch.is_driver_source("fw:drivers/x.c:12")
    assert mismatch.is_driver_source({"kind": "driver", "file": "x.c"})
    assert not mismatch.is_driver_source({"doc": "rm0@1", "q": "Bit 5 resume driver"})
    assert not mismatch.is_driver_source({"define": "X_Msk", "doc": "header:vendor.h"})
    assert not mismatch.is_driver_source("svd")


def test_rail_volts_and_pin_tolerance():
    from embarch_atlas.mismatch import pin_tolerance, rail_volts
    assert rail_volts("3V3") == 3.3 and rail_volts("1V8_IO") == 1.8 and rail_volts("+5V") == 5.0
    assert rail_volts("VBUS") is None and rail_volts("NET_12") is None and rail_volts("3V3_SW") == 3.3
    assert pin_tolerance("FT_fa") == 5.5 and pin_tolerance("TT_a") == 3.6 and pin_tolerance("RST, FT") == 3.6
    assert pin_tolerance(None) is None


def test_declared_dt_intent_folds_the_no_driver_candidate(tmp_path):
    ad, atlas = make_project(tmp_path)
    write_json(ad / "boards" / "bindings.json", {"dt_intent": {"/soc/adc@40012400": "placeholder for a driver to come"}})
    run_join(ad, atlas)
    rep = read_json(atlas / "mismatches.json")
    assert "dt-no-driver" not in _classes(rep)
    folded = rep["folded"][mismatch.FOLD_INTENT]
    assert folded and all("/soc/adc@40012400" in x and "placeholder" in x for x in folded)
    chk = {c["check"]: c for c in rep["checks"]}["DT node has a driver in the build"]
    assert chk["ran"] == chk["passed"]
    assert "DT okay, no driver in this build: ADC (on purpose)" in (atlas / "MAP.md").read_text()


def test_dt_intent_keys_and_forms():
    class J:
        bindings = {"dt_intent": {"adc1": {"why": "later"}, "USB": "bring-up", "/soc/x": ""}}
    assert mismatch.dt_intent(J, "/soc/adc@0", "adc1") == "later"
    assert mismatch.dt_intent(J, None, "USB") == "bring-up"
    assert mismatch.dt_intent(J, "/soc/x") == ""            # an empty reason declares nothing
    assert mismatch.dt_intent(object(), "USB") == ""


def test_second_look_counts_only_verdicts_of_the_current_record(tmp_path):
    from embarch_atlas import secondlook
    from embarch_atlas.common import read_jsonl
    ad, atlas = make_project(tmp_path)
    d = ad / "docs" / "sns701@a"
    rec = next(r for r in read_jsonl(d / "records" / "pin.jsonl") if r["id"] == "sns701.pin.ALERT")
    now = secondlook.content_key(rec, d)
    write_jsonl(atlas / "second_look.jsonl", [
        {"id": "sns701.pin.ALERT", "doc": "sns701@a", "key": "0" * 64, "verdict": "conflict", "why": ["name"]},
        {"id": "sns701.pin.ALERT", "doc": "sns701@a", "key": now, "verdict": "confirmed"},
        {"id": "sns701.pin.GONE", "doc": "sns701@a", "key": "1" * 64, "verdict": "conflict", "why": ["name"]}])
    run_join(ad, atlas)
    rep = read_json(atlas / "mismatches.json")
    chk = {c["check"]: c for c in rep["checks"]}["second look agrees with the first pass"]
    assert (chk["ran"], chk["passed"]) == (1, 1)
    assert "document-reading-conflict" not in _classes(rep)
    assert rep["folded"][mismatch.FOLD_OLD_VERDICT] == ["sns701.pin.ALERT (conflict)"]
    assert rep["folded"][mismatch.FOLD_GONE_VERDICT] == ["sns701.pin.GONE (conflict)"]


def test_a_verdict_under_the_checked_key_is_current(tmp_path):
    from embarch_atlas import secondlook
    from embarch_atlas.common import read_jsonl
    ad, atlas = make_project(tmp_path)
    d = ad / "docs" / "sns701@a"
    rec = next(r for r in read_jsonl(d / "records" / "pin.jsonl") if r["id"] == "sns701.pin.ALERT")
    write_jsonl(atlas / "second_look.jsonl", [
        {"id": "sns701.pin.ALERT", "doc": "sns701@a", "key": secondlook.checked_key(rec, d), "verdict": "confirmed"}])
    run_join(ad, atlas)
    rep = read_json(atlas / "mismatches.json")
    chk = {c["check"]: c for c in rep["checks"]}["second look agrees with the first pass"]
    assert (chk["ran"], chk["passed"]) == (1, 1)
    assert mismatch.FOLD_OLD_VERDICT not in rep["folded"]

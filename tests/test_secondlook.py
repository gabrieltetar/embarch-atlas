"""second-look: which used facts are queued, what the reader is asked, recording and freezing verdicts."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_join import make_project, run_join  # noqa: E402

from embarch_atlas import secondlook as sl  # noqa: E402
from embarch_atlas.common import read_jsonl, write_json, write_jsonl  # noqa: E402


def _project(tmp_path):
    ad, atlas = make_project(tmp_path)
    run_join(ad, atlas)
    return ad, atlas


def test_reasons():
    assert sl.reasons({"kind": "limit", "prov": "deterministic", "unit": "V", "q": "x"}, set()) == []
    r = sl.reasons({"kind": "limit", "prov": "llm-verified", "unit": "µA", "cond": "active", "q": "IQ 60 80"}, set())
    assert r == ["limit", "unit not in the quoted row (unit-prefix risk)", "condition-bearing"]
    assert sl.reasons({"kind": "limit", "prov": "llm-verified", "unit": "µA", "q": "IQ 60 80 uA"}, set()) == ["limit"]
    assert sl.reasons({"kind": "quirk", "src": "figure", "prov": "llm-unverified"}, set()) == ["figure-only", "llm-unverified", "quirk"]
    assert sl.reasons({"kind": "field", "prov": "llm-verified"}, set()) == ["register semantics"]
    assert sl.reasons({"kind": "strap", "prov": "llm-verified"}, {"strap"}) == ["strap fact read by the join"]
    assert sl.reasons({"kind": "requirement", "prov": "llm-verified"}, {"strap"}) == []


def test_ask_never_carries_the_first_pass_value():
    r = {"kind": "limit", "param": "Quiescent current", "sym": "IQ", "cond": "active mode", "typ": 60, "max": 80, "unit": "µA"}
    a = sl.ask(r)
    assert "IQ" in a and "active mode" in a
    assert "60" not in a and "80" not in a and "µA" not in a
    assert "0x44" not in sl.ask({"kind": "strap", "pin": "A0", "condition": "GND", "value": "0x44"})


def test_queue_contents(tmp_path):
    ad, atlas = _project(tmp_path)
    q, st = sl.build_queue(ad, atlas)
    ids = {x["id"]: x for x in q}
    assert "sns701.abs.VS" not in ids                               # deterministic: no second look
    assert set(ids) == {"sns701.ec.VS", "sns701.ec.IQ.active", "sns701.quirk.alert_default", "sns701.pin.ALERT",
                        "sns701.strap.A0.gnd", "sns701.strap.A0.vs", "sns701.strap.A0.sda", "sns701.strap.A0.scl"}
    iq = ids["sns701.ec.IQ.active"]
    assert iq["p"] == [6] and "unit not in the quoted row (unit-prefix risk)" in iq["why"] and "condition-bearing" in iq["why"]
    assert iq["pdf"].endswith("sns701.pdf")
    assert ids["sns701.quirk.alert_default"]["src"] == "figure"
    assert st["used_records"] == 9 and st["queued"] == 8 and st["frozen"] == 0


def test_mcu_doc_records_only_for_peripherals_in_use(tmp_path):
    ad, atlas = _project(tmp_path)
    d = ad / "docs" / "rm0@1"
    write_json(d / "doc.json", {"doc_id": "rm0@1", "kind": "reference-manual", "part": "MCU0"})
    write_jsonl(d / "records" / "field.jsonl", [
        {"id": "rm0.field.I2C.CR1.PE", "kind": "field", "doc": "rm0@1", "p": 900, "q": "PE", "src": "table", "prov": "llm-verified",
         "reg": "I2C_CR1", "name": "PE", "bits": [0, 0]},
        {"id": "rm0.field.SPI.CR1.SPE", "kind": "field", "doc": "rm0@1", "p": 1000, "q": "SPE", "src": "table", "prov": "llm-verified",
         "reg": "SPI_CR1", "name": "SPE", "bits": [6, 6]}])
    q, _ = sl.build_queue(ad, atlas)
    ids = {x["id"] for x in q}
    assert "rm0.field.I2C.CR1.PE" in ids and "rm0.field.SPI.CR1.SPE" not in ids


def test_record_validates_appends_and_freezes(tmp_path):
    ad, atlas = _project(tmp_path)
    q, _ = sl.build_queue(ad, atlas)
    write_jsonl(atlas / "second_look_queue.jsonl", q)
    bad = tmp_path / "bad.jsonl"
    write_jsonl(bad, [{"id": "sns701.ec.VS", "verdict": "maybe", "reader": "me"}, {"id": "nope", "verdict": "confirmed", "reader": "second-look"}])
    with pytest.raises(SystemExit) as e:
        sl.record(ad, atlas, bad)
    assert "verdict 'maybe'" in str(e.value) and "not in second_look_queue" in str(e.value) and "reader" in str(e.value)
    assert not (atlas / "second_look.jsonl").exists()
    good = tmp_path / "good.jsonl"
    write_jsonl(good, [
        {"id": "sns701.ec.VS", "verdict": "confirmed", "value": [2.7, 5.5], "unit": "V", "cond": None,
         "q": "VS Supply voltage 2.7 5.5 V", "p": 5, "reader": "second-look"},
        {"id": "sns701.ec.IQ.active", "verdict": "conflict", "value": 90, "unit": "µA", "cond": "active mode",
         "q": "IQ 60 90 uA", "p": 6, "reader": "second-look"},
        {"id": "sns701.quirk.alert_default", "verdict": "unresolved", "reader": "second-look"}])
    res = sl.record(ad, atlas, good)
    assert res["added"] == 3 and res["by_verdict"] == {"confirmed": 1, "conflict": 1, "unresolved": 1}
    assert any("not on p[6]" in w for w in res["warnings"])        # the second quote is not on the page text
    rows = {r["id"]: r for r in read_jsonl(atlas / "second_look.jsonl")}
    assert rows["sns701.ec.VS"]["q_on_page"] is True and rows["sns701.ec.IQ.active"]["first"] == {"typ": 60, "max": 80, "unit": "µA",
                                                                                                    "cond": "active mode"}
    assert len(list((ad / "cache" / "llm").glob("sl-*.json"))) == 3
    # recording twice appends nothing; the next queue reuses the frozen verdicts
    assert sl.record(ad, atlas, good)["added"] == 0
    q2, st2 = sl.build_queue(ad, atlas)
    assert st2["frozen"] == 3 and {x["id"] for x in q2}.isdisjoint(rows)
    # a changed page re-queues its facts
    (ad / "docs" / "sns701@a" / "pages" / "p005.txt").write_text("VS Supply voltage 2.7 5.0 V\n")
    q3, _ = sl.build_queue(ad, atlas)
    assert "sns701.ec.VS" in {x["id"] for x in q3}
    json.dumps(q3)


def test_cli_queue_and_record(tmp_path, capsys):
    from embarch_atlas import cli
    ad, atlas = _project(tmp_path)
    assert cli.main(["--atlas-dir", str(ad), "second-look", "queue", atlas.name]) == 0
    assert "8 queued" in capsys.readouterr().out
    v = tmp_path / "v.jsonl"
    write_jsonl(v, [{"id": "sns701.pin.ALERT", "verdict": "confirmed", "q": "C2 ALERT O open-drain", "p": 3, "reader": "second-look"}])
    assert cli.main(["--atlas-dir", str(ad), "second-look", "record", atlas.name, str(v)]) == 0
    assert "recorded 1" in capsys.readouterr().out


def test_figure_ask_uses_caption_not_description():
    a = sl.ask({"kind": "figure", "fig": "F1", "caption": "Typical application", "what": "R1 47k pull-up"})
    assert "Typical application" in a and "47k" not in a


def test_reasons_accepts_a_list_quote():
    from embarch_atlas.secondlook import reasons
    r = {"kind": "limit", "unit": "mV", "q": ["Threshold rising", "10 12 14 mV"], "src": "table"}
    why = reasons(r, set())
    assert "limit" in why and not any("unit-prefix" in w for w in why)


def test_priority_and_page_batches():
    from embarch_atlas.secondlook import batches, priority
    assert priority(["strap fact read by the join"]) > priority(["figure-only"]) > priority(["limit"])
    assert priority(["limit", "condition-bearing"]) == 20
    q = [{"id": f"x{i}", "doc": "d@1", "p": [3 if i < 10 else 4], "ask": "?", "key": str(i), "pdf": "f.pdf",
          "priority": 100 - i} for i in range(12)]
    bs = batches(q, top=12, per_batch=8)
    assert [len(b["items"]) for b in bs] == [8, 2, 2]
    assert bs[0]["pages"] == [3] and bs[0]["priority"] == 100

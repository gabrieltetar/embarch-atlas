"""The standard document kind: classification, identity, the lazy worklist (`doc sections`), sections
suggested from use, check W, and the card's list of distilled sections. A synthetic, invented standard."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_inventory import FILLER, make_pdf  # noqa: E402

from embarch_atlas import check, cli, common, freeze, inventory as inv, query, standard  # noqa: E402
from embarch_atlas.common import write_json, write_jsonl, write_text  # noqa: E402

DOC = "demobus-spec@r2.0v1.1"
SECTIONS = [  # id, title, level, pages
    ("1", "Introduction", 1, [2, 2]),
    ("4", "Power Rules", 1, [3, 5]),
    ("4.1", "Source Rules", 2, [3, 4]),
    ("4.2", "Sink Rules", 2, [5, 5]),
    ("6", "Messages", 1, [6, 8]),
    ("6.2", "Request Message", 2, [6, 7]),
    ("6.3", "Status Message", 2, [8, 8]),
]
PAGES = {
    1: "Demo Bus Power Delivery Specification\nRevision 2.0, Version 1.1\n",
    2: "1 Introduction\nThis specification defines the demo bus.\n",
    3: "4.1 Source Rules\nA Source shall not apply more than 5.5 V before the contract.\n",
    4: "A Source should offer 5 V at 3 A.\n",
    5: "4.2 Sink Rules\nA Sink may request less than offered.\n",
    6: "6.2 Request Message\nThe Sink shall send a Request within 30 ms of a Capabilities message.\n",
    7: "The Request carries the object position.\n",
    8: "6.3 Status Message\nTable 6-3 Required fields\nTemperature status\n",
}


@pytest.fixture()
def std(tmp_path: Path) -> Path:
    """An atlas dir holding one prepped standard: doc.json, pages, toc."""
    ad = tmp_path / "atlas"
    d = ad / "docs" / DOC
    write_json(d / "doc.json", {"doc_id": DOC, "kind": "standard", "pages": len(PAGES), "file": "DemoBus_Spec.pdf",
                                "part": None, "rev": "r2.0v1.1"})
    for p, text in PAGES.items():
        write_text(d / "pages" / f"p{p:03d}.txt", text)
    write_json(d / "toc.json", {"doc_id": DOC, "sections": [
        {"id": i, "title": t, "level": lv, "pages": pg, "tokens": 100 * lv} for i, t, lv, pg in SECTIONS]})
    write_text(d / "card.md", "# Demo Bus Power Delivery Specification, Revision 2.0 Version 1.1\n\n"
                              "Defines the demo bus's power contract between a Source and a Sink (p.2).\n")
    return ad


def _sections(ad: Path, *argv: str) -> int:
    return cli.main(["--atlas-dir", str(ad), "doc", "sections", DOC, *argv])


# ---------------------------------------------------------------- classification

def test_inventory_files_a_standard_and_keeps_a_datasheet_citing_one(tmp_path):
    spec = make_pdf(tmp_path / "DemoBus_Spec_R2_0_V1_1_2026_03.pdf", [
        "Demo Bus Power Delivery Specification\nRevision 2.0, Version 1.1\nMarch 2026\n",
        "Contents\n1 Introduction 2\n4 Power Rules 3\n", "1 Introduction\n" + FILLER])
    ds = make_pdf(tmp_path / "abc1234.pdf", [
        "ABC1234 Synchronous Buck Converter\nRevision 2.0\nFeatures\n- ESD protection per IEC 61000-4-2\n" + FILLER,
        "Absolute Maximum Ratings\nVIN -0.3 6 V\n", "Electrical Characteristics\n" + FILLER])
    rec = inv.inventory_pdf(spec)
    assert rec["kind"] == "standard" and rec["rev"] == "r2.0v1.1" and rec["parts"] == []
    assert inv.doc_id_for({**rec, "file": spec.name}) == "demobus-spec@r2.0v1.1"
    assert inv.inventory_pdf(ds)["kind"] == "datasheet"
    ieee = make_pdf(tmp_path / "demo-links.pdf", ["IEEE Standard for Demo Links\nIEEE Std 9999-2020\n", "Scope\n"])
    assert inv.inventory_pdf(ieee)["kind"] == "standard" and inv.inventory_pdf(ieee)["rev"] == "2020"


def test_a_standard_names_no_part_and_is_not_a_document_nothing_uses():
    from embarch_atlas import coverage, missing
    assert "standard" not in coverage.DOC_KINDS and "standard" in missing.UNUSED_KINDS


# ---------------------------------------------------------------- the worklist

def test_sections_add_list_freeze_and_a_rerun_redoes_nothing(std, capsys):
    assert _sections(std, "--add", "6.2,Sink Rules,9.9", "--why", "the firmware sends Requests") == 1   # 9.9 unknown
    wl = common.read_json(std / "docs" / DOC / "worklist.json")
    assert [s["id"] for s in wl["sections"]] == ["4.2", "6.2"] and wl["procedure"] == standard.PROCEDURE
    assert wl["sections"][1]["why"] == ["the firmware sends Requests"] and wl["sections"][1]["pages"] == [6, 7]
    out = capsys.readouterr()
    assert "not in the toc: 9.9" in out.err and "todo   6.2 Request Message (p.6-7, 200 tok)" in out.out
    assert _sections(std, "--add", "6.2", "--why", "a review asked") == 0                 # a reason, not a second entry
    assert common.read_json(std / "docs" / DOC / "worklist.json")["sections"][1]["why"] == [
        "the firmware sends Requests", "a review asked"]
    # distil 6.2 and freeze it
    d = std / "docs" / DOC
    write_text(d / "s" / "6.2.md", "# 6.2 Request Message\n\nThe Sink sends a Request within 30 ms (p.6).\n")
    write_jsonl(d / "records" / "requirement.jsonl", [
        {"id": "demobus.req.6.2.request-time", "kind": "requirement", "doc": DOC, "p": 6, "src": "text",
         "q": "The Sink shall send a Request within 30 ms", "prov": "llm-verified", "rid": "demobus.6.2.request-time",
         "text": "Sink sends Request within 30 ms", "fw_relevant": True, "modality": "shall", "role": "Sink"}])
    freeze.freeze(std, DOC, model="m")
    secs = {s["id"]: s for s in standard.listing(std, DOC)}
    assert secs["6.2"]["status"] == "frozen" and secs["4.2"]["status"] == "todo"
    key = secs["6.2"]["key"]
    assert {s["id"]: s for s in standard.listing(std, DOC)}["6.2"] == secs["6.2"]       # unchanged pages: nothing to redo
    write_text(d / "pages" / "p003.txt", PAGES[3] + "A changed sentence in another section.\n")
    assert {s["id"]: s for s in standard.listing(std, DOC)}["6.2"]["status"] == "frozen"
    write_text(d / "pages" / "p006.txt", PAGES[6] + "A new note on the Request.\n")
    now = {s["id"]: s for s in standard.listing(std, DOC)}["6.2"]
    assert now["status"] == "redo" and now["key"] != key


def test_the_card_lists_the_distilled_sections_and_names_find_text_for_the_rest(std):
    standard.add(std, DOC, ["6.2", "4.1"], "asked")
    d = std / "docs" / DOC
    write_text(d / "s" / "6.2.md", "# 6.2 Request Message\n\nWithin 30 ms (p.6).\n")
    assert standard.refresh_card(std, DOC)                                                 # 6.2 still todo here
    freeze.freeze(std, DOC, model="m")
    standard.refresh_card(std, DOC)                                                        # now frozen: the list moves
    card = (d / "card.md").read_text()
    assert card.startswith("# Demo Bus Power Delivery Specification")                     # the LLM's text is kept
    assert "`6.2` Request Message (p.6-7, `s/6.2.md`)" in card
    assert "Asked for, not distilled yet: `4.1` Source Rules (todo)" in card
    assert f"`find_text doc={DOC} pattern=<words>`" in card and card.count(standard.CARD_BLOCK) == 1
    standard.refresh_card(std, DOC)
    assert (d / "card.md").read_text() == card                                             # rewritten in place
    assert freeze.thaw_check(std, DOC)["edited"] == []                                     # the block never thaws the card
    rep = check.run_check(d, write=False, use_pdf=False)
    assert not [m for m in rep["M"] if m["file"] == "card.md" and m["stats"]["unanchored"]]


# ---------------------------------------------------------------- sections from use

def test_suggest_ranks_undistilled_sections_by_the_lookups_that_landed_in_them(std, capsys):
    standard.add(std, DOC, ["4.2"], "asked")
    rows = ([{"doc": DOC, "section": "6.2", "page": 6, "tool": "find_text"}] * 3
            + [{"doc": DOC, "section": None, "page": 8, "tool": "doc_section"}] * 2           # credited by page: 6.3
            + [{"doc": DOC, "section": "4.2", "page": 5, "tool": "find_text"}] * 5           # asked for already
            + [{"doc": "other@1", "section": "6.2", "page": 6, "tool": "find_text"}] * 9
            + [{"doc": DOC, "id": "x", "tool": "find_fact"}])
    log = std / "usage.jsonl"
    log.write_text("".join(json.dumps(r) + "\n" for r in rows) + "not json\n")
    assert [(s["id"], s["calls"]) for s in standard.suggest(std, DOC, log)] == [("6.2", 3), ("6.3", 2)]
    assert [(s["id"], s["calls"]) for s in standard.suggest(std, DOC, log, level=1)] == [("6", 5)]
    assert _sections(std, "--suggest") == 0
    out = capsys.readouterr().out
    assert "      3 calls  6.2 Request Message (p.6-7, 200 tok)" in out and "2 calls  6.3 Status Message" in out


def test_doc_section_text_tier_logs_the_page_and_its_section(std):
    a = query.Atlas(std)
    query.doc_section(a, DOC, tier="text", page=7)
    rows = [json.loads(x) for x in (std / "usage.jsonl").read_text().splitlines()]
    assert rows == [{"doc": DOC, "section": "6.2", "page": 7, "tool": "doc_section"}]


# ---------------------------------------------------------------- check W

@pytest.mark.parametrize("modality, quote, note, ok", [
    ("shall-not", "A Source shall not apply more than 5.5 V", None, True),
    ("shall-not", "A Source must not apply more than 5.5 V", None, True),
    ("shall", "A Source shall not apply more than 5.5 V", None, False),
    ("shall", "The Sink shall send a Request", None, True),
    ("should", "A Source should offer 5 V at 3 A", None, True),
    ("shall", "A Source should offer 5 V at 3 A", None, False),
    ("may", "A Sink may request less than offered", None, True),
    ("shall", "Temperature status", "Table 6-3 heading: Required fields", True),
    ("shall", "Temperature status", None, False),
])
def test_check_w_compares_the_modality_with_the_keyword_in_the_quote(modality, quote, note, ok):
    assert (check.modality_check(modality, quote, note) == "ok") is ok


def test_check_w_runs_on_a_standards_requirements_and_fails_a_wrong_modality(std):
    d = std / "docs" / DOC
    req = lambda i, p, q, m: {"id": f"demobus.req.{i}", "kind": "requirement", "doc": DOC, "p": p, "src": "text", "q": q,
                              "prov": "llm-verified", "rid": f"demobus.{i}", "text": "x", "fw_relevant": True, "modality": m}
    write_jsonl(d / "records" / "requirement.jsonl", [
        req("4.1.max", 3, "A Source shall not apply more than 5.5 V before the contract", "shall-not"),
        req("4.1.offer", 4, "A Source should offer 5 V at 3 A", "shall")])
    rep = check.run_check(d, write=False, use_pdf=False)
    by = {r["id"]: r for r in rep["records"]}
    assert by["demobus.req.4.1.max"]["checks"]["W"] == "ok"
    assert by["demobus.req.4.1.offer"]["checks"]["W"] == "modality shall: the quote says should"
    assert by["demobus.req.4.1.offer"]["status"] != "pass" and rep["summary"]["W"] == {"ok": 1, "of": 2}

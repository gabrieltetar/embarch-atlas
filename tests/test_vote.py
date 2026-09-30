"""mcu vote: SVD vs RM register sections vs CMSIS header, device filter, rule records. Synthetic data only."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from test_rm import demo_atlas, demo_rmdoc  # noqa: E402

from embarch_atlas import cli, vote  # noqa: E402
from embarch_atlas.common import read_json, read_jsonl  # noqa: E402


def _run(*argv) -> int:
    return cli.main([str(a) for a in argv])


# --------------------------------------------------------------------------- RM register sections

def test_register_section_parse_and_self_check():
    r = demo_rmdoc()
    cr, _ = vote.parse_register_section(r, r.by_id["2.3.1"])
    assert cr["offset"] == 0 and cr["reset"] == 0 and cr["width"] == 32
    assert [(f["name"], f["hi"], f["lo"]) for f in cr["fields"]] == [
        ("Reserved", 31, 6), ("CRCEN", 5, 5), ("AUXEN", 4, 4), ("MODE[2:0]", 3, 1), ("EN", 0, 0)]
    assert cr["coverage"] == 32 and cr["access_ok"] and cr["parse_ok"]
    assert cr["bitacc"][0] == "rw" and cr["bitacc"][6] is None
    sr, _ = vote.parse_register_section(r, r.by_id["2.3.2"])
    assert sr["reset"] == 1 and sr["parse_ok"] and sr["bitacc"][2] == "r"


def test_parametric_register_expands_with_offset_formula():
    r = demo_rmdoc()
    d, _ = vote.parse_register_section(r, r.by_id["2.3.3"])
    ex = vote.expand_parametric(d)
    assert [(x["name"], x["offset"]) for x in ex] == [("UART_DATA0", 0x10), ("UART_DATA1", 0x14)]
    assert d["fields"][-1]["name"] == "DATA[7:0]"


def test_parametric_field_names_per_bit():
    assert vote._param_names("IMx", 4, 0, "mask on line x (x = 36 to 32)") == ["IM32", "IM33", "IM34", "IM35", "IM36"]
    assert vote._param_names("IMx", 4, 0, "no range here") is None


# --------------------------------------------------------------------------- device filter

def test_availability_table_and_part_column():
    av = vote.availability(demo_rmdoc(), "DEMOM2")
    assert av["columns"] == ["DEMOM1", "DEMOM2"] and av["part_column"] == "DEMOM2"
    assert av["absent"] == ["CRC"] and "AUX" in av["present"]
    assert "Wide name on two lines" in av["features"]


def test_availability_notes_and_part_patterns():
    n = vote.availability_notes("Note: This bit is only available in DEMOM1xx devices, reserved otherwise.", "DEMOM2")
    assert n and n[0]["available"] is False
    n = vote.availability_notes("The IM36 and IM35 bits are only available in DEMOM2xx and DEMOM3xx.", "DEMOM2")
    assert n[0]["available"] is True and n[0]["names"] == ["IM36", "IM35"]
    n = vote.availability_notes("They are reserved in DEMOM2xx and DEMOM4xx.", "DEMOM2")
    assert n[0]["available"] is False
    assert vote.part_matches("DEMOM2xx", "DEMOM2") and vote.part_matches("DEMOM2xB/xC", "DEMOM2")
    assert not vote.part_matches("DEMOM1xx", "DEMOM2")
    assert vote.part_matches("MOM2", "DEMOM2")  # a short table column


def test_header_parse(tmp_path):
    (tmp_path / "d.h").write_text((Path(__file__).parent / "test_rm.py").read_text().split('HEADER = """')[1].split('"""')[0])
    h = vote.parse_header(tmp_path / "d.h")
    assert h["pos"]["UART_SR_PARF"][0] == 2 and h["msk"]["UART_SR_PARF"][0] == 0x4
    assert h["bases"]["AUX_BASE"][0] == 0x40006000 and h["types"]["UART1"] == "UART"
    assert h["irqs"]["UART1"] == 5


# --------------------------------------------------------------------------- the vote

def _verdict(c: dict) -> str:
    return c["note"].split(":", 1)[0]


def test_vote_end_to_end(tmp_path, capsys):
    root = demo_atlas(tmp_path)
    assert _run("--atlas-dir", root, "mcu", "vote", "--svd-doc", "demo-svd", "--rm-doc", "demo-rm@1",
                "--header", tmp_path / "demo.h") == 0
    rd = root / "docs" / "demo-rm@1"
    conf = {c["id"]: c for c in read_jsonl(rd / "records" / "conflict.jsonl")}
    quirks = list(read_jsonl(rd / "records" / "quirk.jsonl"))
    by_verdict: dict[str, list[str]] = {}
    for c in conf.values():
        by_verdict.setdefault(_verdict(c), []).append(c["text"])
    # PARF: RM defines it, the SVD omits it, the header has UART_SR_PARF at bit 2
    assert any("PARF" in t for t in by_verdict["svd-defect"])
    # RXNE vs RXNEF: the header uses the RM's name
    assert any("RXNE" in t and "RXNEF" in t for t in by_verdict["svd-naming-defect"])
    # AUX: chapter with registers, no SVD peripheral, Table 1 present, header base
    aux = conf["demom2.conflict.periph.AUX"]
    assert _verdict(aux) == "svd-defect" and aux["doc"] == "demo-rm@1"
    assert any(x.startswith("demo-svd") for x in aux["sources"]) and any(" L" in x for x in aux["sources"])
    # a whole register the RM and the header have and the SVD lacks
    brr = next(c for c in conf.values() if "UART_BRR" in c["text"])
    assert _verdict(brr) == "svd-defect" and "register missing from SVD" in brr["text"]
    # family-wide features the part lacks are omissions, not defects
    qt = " ".join(q["text"] for q in quirks)
    assert "AUXEN" in qt and "CRCEN" in qt and "CRC: in the RM" in qt
    assert not any("AUXEN" in c["text"] or "CRCEN" in c["text"] for c in conf.values())
    # every conflict carries its sources, a page cite, and SVD refs
    parf = next(c for c in conf.values() if "PARF" in c["text"])
    assert parf["refs"] == ["demom2.reg.UART1.UART_SR"] and parf["p"] == 4
    assert parf["q"] == "Bit 2 PARF: Parity error flag"
    assert [x.split(" ", 1)[0] for x in parf["sources"]] == ["demo-svd", "demo-rm@1", "demo.h"]
    # rules keyed by SVD names (in the id), page-cited, verbatim
    rules = list(read_jsonl(rd / "records" / "rule.jsonl"))
    en = [r for r in rules if vote.rule_view(r)["field"] == "EN"]
    assert {r["q"] for r in en} == {"This bit can be set only when MODE = 0.",
                                    "Note: This bit is cleared by hardware on a framing error."}
    assert all(r["p"] == 4 and r["id"].startswith("demo-rm.rule.reg.UART1.UART_CR.EN.") for r in en)
    assert all(r["note"] == "§2.3.1; SVD demom2.reg.UART1.UART_CR.EN" for r in en)
    assert any(vote.rule_view(r)["field"] == "DATA" and "not allowed" in r["q"] for r in rules)
    assert "`UART_CR.EN` [§2.3.1 p.4]" in (rd / "s" / "2.fields.md").read_text()
    # records the vote writes pass the shared record schema
    from embarch_atlas.records import validate
    for rec in list(conf.values()) + quirks + rules:
        assert validate(rec) == [], (rec["id"], validate(rec))
    summary = read_json(rd / "vote.json")
    assert summary["per_periph"]["UART1"]["matched"] == 4       # CR, SR, DATA0, DATA1
    assert summary["tally"]["correct-omission"] == len(quirks) == 3
    assert (rd / "vote.md").exists() and (rd / "availability.json").exists()


def test_vote_without_header_leaves_omissions_unresolved(tmp_path):
    root = demo_atlas(tmp_path)
    assert _run("--atlas-dir", root, "mcu", "vote", "--svd-doc", "demo-svd", "--rm-doc", "demo-rm@1") == 0
    conf = list(read_jsonl(root / "docs" / "demo-rm@1" / "records" / "conflict.jsonl"))
    parf = next(c for c in conf if "PARF" in c["text"])
    assert _verdict(parf) == "unresolved"
    aux = next(c for c in conf if c["id"].endswith("periph.AUX"))
    assert _verdict(aux) == "svd-defect-candidate"          # Table 1 only, no header base


def test_reset_and_access_differences_are_candidates():
    r = demo_rmdoc()
    sr, _ = vote.parse_register_section(r, r.by_id["2.3.2"])
    sreg = {"name": "UART_SR", "off": "0x004", "reset": "0x00000000", "access": "rw", "fields": {
        "TXE": {"name": "TXE", "bits": [0, 0], "access": "rw"},
        "RXNE": {"name": "RXNE", "bits": [1, 1], "access": "ro"},
        "PARF": {"name": "PARF", "bits": [2, 2], "access": "ro"}}}
    diffs = vote.diff_register(sr, sreg)
    kinds = {d["kind"]: d for d in diffs}
    assert set(kinds) == {"reset differs", "access differs"}
    v, _, _ = vote.adjudicate(kinds["reset differs"], sr, sreg, "UART", vote.Header(None), "DEMOM2", {})
    assert v == "svd-defect-candidate"


def test_vote_is_idempotent_and_scoped(tmp_path):
    root = demo_atlas(tmp_path)
    args = ["--atlas-dir", root, "mcu", "vote", "--svd-doc", "demo-svd", "--rm-doc", "demo-rm@1", "--header", tmp_path / "demo.h"]
    _run(*args)
    f = root / "docs" / "demo-rm@1" / "records" / "conflict.jsonl"
    first = f.read_bytes()
    _run(*args)
    assert f.read_bytes() == first
    _run(*args, "--periph", "UART1")   # re-voting one peripheral keeps the others' records
    ids = [c["id"] for c in read_jsonl(f)]
    assert any(i.endswith("periph.AUX") for i in ids) and any(".UART1." in i for i in ids)

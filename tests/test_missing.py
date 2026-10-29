"""missing: BOM part numbers vs the manifest, ranked by how directly firmware sees each part."""
from __future__ import annotations

from pathlib import Path

import pytest

from embarch_atlas import cli, common, interfaces, missing as ms


def comp(pins: dict[str, tuple[str | None, str | None]], mpn: str | None, dni: bool = False) -> dict:
    return {"mpn": mpn, "value": None, "dni": dni, "pages": [1], "parts": [], "sheets": [],
            "pins": {p: {"name": nm, "net": net} for p, (nm, net) in pins.items()}}


def board(adir: Path, label: str, comps: dict[str, dict], extra: dict[str, dict] | None = None) -> None:
    nets: dict[str, dict] = {}
    for ref, c in comps.items():
        for p, P in c["pins"].items():
            if P["net"]:
                nets.setdefault(P["net"], {"pins": [], "sheets": ["S"], "labels": 0, "ports": 0,
                                           "auto": P["net"].startswith("Net")})["pins"].append(f"{ref}-{p}")
    nl = {"board": label, "source": {"file": f"{label}.pdf", "sha256": "0" * 64}, "method": "smartpdf-outline",
          "sheets": {"S": {"page": 1}}, "components": comps, "nets": nets, "counts": {}, "issues": []}
    bom = [{"ref": r, "mpn": c["mpn"], "value": None, "dni": c["dni"], "description": f"desc of {r}",
            "datasheet_link": None, "in_bom": True, **((extra or {}).get(r) or {})} for r, c in comps.items()]
    common.write_json(adir / "boards" / label / "netlist.json", nl)
    common.write_json(adir / "boards" / label / "bom.json", bom)


def make_atlas(tmp_path: Path, errata: bool = False, declared: bool = True) -> Path:
    adir = tmp_path / "atlas"
    adir.mkdir()
    (adir / "atlas.toml").write_text(f'[project]\nfw = "{tmp_path}"\nsources = "hw"\n\n[mcu]\npart = "MCU123"\n')
    board(adir, "M", {
        "U1": comp({"1": ("PA0", "SIG1"), "2": ("VDD", "3V3"), "3": ("PA1", "SIG2"), "4": ("PA2", "LINK1"),
                    "5": ("VSS", "GND"), "6": ("PA3", "BUSY")}, "MCU123ABC"),
        "U2": comp({"1": (None, "SIG1"), "2": (None, "GND")}, "ICAA100X"),
        "Q1": comp({"1": (None, "SIG1"), "2": (None, "GND"), "3": (None, "LOADX")}, "FETZZ9"),
        "R1": comp({"1": (None, "SIG2"), "2": (None, "SIG3")}, "RES10K"),
        "U3": comp({"1": (None, "SIG3"), "2": (None, "3V3")}, "GATE77Q"),
        "U4": comp({"1": (None, "3V3"), "2": (None, "GND")}, "REGUL8"),
        "D1": comp({"1": (None, "LOADX"), "2": (None, "GND")}, "TVS01", dni=True),
        "J1": comp({"1": (None, "LINK1"), "2": (None, "GND")}, "CONN2P"),
        "TP1": comp({"1": (None, "BUSY")}, "TP-LOOP"),
        "U9": comp({"1": (None, "BUSY"), "2": (None, "GND")}, None),
    }, extra={"U4": {"datasheet_link": "https://example.com/regul8.pdf"}, "TP1": {"in_bom": False}})
    board(adir, "S", {
        "J1": comp({"1": (None, "LINKB"), "2": (None, "GND")}, "CONN2P"),
        "U1": comp({"1": (None, "LINKB"), "2": (None, "GND")}, "SENS55"),
        "Q3": comp({"1": (None, "GND"), "2": (None, "OTHER")}, "FETZZ9"),
    })
    docs = [{"file": "icaa100.pdf", "kind": "datasheet", "parts": ["ICAA100"]},
            {"file": "mcu-ds.pdf", "kind": "datasheet", "parts": ["MCU123xA/xB"]},
            {"file": "mcu-rm.pdf", "kind": "reference-manual", "parts": ["MCU123x"]},
            {"file": "old-fetzz9.pdf", "kind": "datasheet", "parts": ["FETZZ9"], "superseded_by": "x.pdf"},
            {"file": "stm-family.pdf", "kind": "datasheet", "parts": ["MCU12"]}]  # too short to cover anything
    if errata:
        docs.append({"file": "mcu-errata.pdf", "kind": "errata", "parts": ["MCU123x"]})
    common.write_json(adir / "inventory" / "manifest.json", {"documents": docs})
    if declared:
        common.write_json(adir / "boards" / "system.json", {"interfaces": [
            {"id": "M:J1~S:J1", "kind": "board-to-board", "a": "M:J1", "b": "S:J1", "rule": "identity",
             "prov": "engineer-declared"}]})
    return adir


def test_family_regex():
    assert ms.family_regex("ABC12xE/xG").match("ABC12QE7")
    assert ms.family_regex("ABC12xE/xG").match("ABC12QG") and not ms.family_regex("ABC12xE/xG").match("ABC12QC")
    assert ms.family_regex("DEF120x").match("DEF1205GT")
    assert ms.family_regex("GHI1234").match("GHI1234ARZ")
    assert ms.family_regex("ABC32") is None  # a bare family prefix never covers everything


def test_tiers_and_coverage(tmp_path):
    a = ms.analyse(make_atlas(tmp_path))
    R = {r["mpn"]: r for r in a["rows"]}
    assert a["mcu_refs"] == ["M:U1"]
    assert R["MCU123ABC"]["tier"] == "mcu" and R["MCU123ABC"]["covered_by"] == ["mcu-ds.pdf", "mcu-rm.pdf"]
    assert R["ICAA100X"]["tier"] == "direct" and R["ICAA100X"]["covered_by"] == ["icaa100.pdf"]
    assert R["FETZZ9"]["tier"] == "direct" and R["FETZZ9"]["covered_by"] == []  # superseded file does not count
    assert R["FETZZ9"]["refs"] == ["M:Q1", "S:Q3"]
    assert R["GATE77Q"]["tier"] == "via-series" and R["GATE77Q"]["why"][0]["via"] == "M:R1"
    assert R["SENS55"]["tier"] == "cross-board" and R["SENS55"]["why"][0]["mating"] == "engineer-declared"
    assert R["REGUL8"]["tier"] == "not-visible"  # on supply nets only
    assert R["TVS01"]["tier"] == "not-visible" and R["TVS01"]["dni"] == ["M:D1"]
    assert R["CONN2P"]["category"] == "other" and R["CONN2P"]["tier"] == "direct"
    assert "RES10K" not in R and "TP-LOOP" not in R  # passives and no-BOM parts are not asked about
    assert a["mcu_docs"]["errata"] == [] and a["mcu_docs"]["reference-manual"] == ["mcu-rm.pdf"]


def test_missing_md_asks_questions(tmp_path, capsys):
    adir = make_atlas(tmp_path)
    assert cli.main(["--atlas-dir", str(adir), "missing"]) == 0
    out = capsys.readouterr().out
    assert "5 without a document, 3 firmware-visible" in out and "errata MISSING" in out
    md = (adir / "inventory" / "missing.md").read_text()
    assert "**7 active part numbers (U/Q/D) on the boards; 2 have a document; 5 missing, 3 of them firmware-visible.**" in md
    assert "Q1. **MCU errata sheet**" in md
    assert "Q3. **FETZZ9** x2 (M:Q1, S:Q3): shares MCU net(s) SIG1 (M:U1)." in md
    assert "Q2. **GATE77Q** x1 (M:U3): one series part from MCU net(s) SIG2 (M:U1) via M:R1. Stops 1 net_trace walk." in md
    assert "Supply it, mark it not needed, or accept a standard-function card?" in md
    assert "Link in the schematic's parameters (not fetched): https://example.com/regul8.pdf" in md
    assert "download" not in md.lower()
    assert "Not fitted in this print: M:D1." in md
    assert "| ICAA100X | M:U2 | direct | icaa100.pdf |" in md


def test_errata_present_and_proposed_mating(tmp_path):
    adir = make_atlas(tmp_path, errata=True, declared=False)
    interfaces.run(adir)
    a = ms.run(adir)
    assert a["mcu_docs"]["errata"] == ["mcu-errata.pdf"]
    R = {r["mpn"]: r for r in a["rows"]}
    assert R["SENS55"]["tier"] == "cross-board" and R["SENS55"]["why"][0]["mating"] == "proposed"
    md = (adir / "inventory" / "missing.md").read_text()
    assert "MCU errata sheet" not in md and "- errata: mcu-errata.pdf" in md
    assert "the mating rule is a proposal, not yet engineer-declared" in md


def test_no_mcu_part(tmp_path):
    adir = make_atlas(tmp_path)
    (adir / "atlas.toml").write_text(f'[project]\nfw = "{tmp_path}"\n')
    a = ms.run(adir)
    assert a["mcu_refs"] == [] and {r["tier"] for r in a["rows"]} == {"unknown"}
    assert "Which part is the MCU?" in (adir / "inventory" / "missing.md").read_text()


def test_needs_inputs(tmp_path):
    with pytest.raises(FileNotFoundError, match="inventory"):
        ms.analyse(tmp_path)

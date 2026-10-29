"""answers.toml and the generic function cards, over a synthetic two-board system (invented parts and nets)."""
from pathlib import Path

import pytest

from embarch_atlas import answers, cli, common, missing as ms, query
from embarch_atlas.common import write_json


def _pins(**nets):
    return {num: {"name": name, "net": net} for num, (name, net) in nets.items()}


@pytest.fixture()
def atlas_dir(tmp_path: Path) -> Path:
    """Board A: input connector J1 -> PFET9 (Q1, a p-channel switch) -> 5V0_SW; its gate net SW_EN is driven by
    an inverter INVX1 (U7) whose input PWR_ON is MCU pin PA3. Board B hangs a load NOCARD1 on 5V0_SW through a header."""
    a = tmp_path / "atlas"
    comps_a = {
        "U9": {"mpn": "DEMO-MCU", "pins": _pins(**{"1": ("PA3", "PWR_ON"), "2": ("VDD", "3V3")})},
        "J1": {"mpn": "BARREL", "pins": _pins(**{"1": (None, "VIN"), "2": (None, "GND")})},
        "Q1": {"mpn": "PFET9", "pins": _pins(**{"1": ("g", "SW_EN"), "2": ("s", "VIN"), "3": ("d", "5V0_SW"),
                                                "4": ("d", "5V0_SW")})},
        "U7": {"mpn": "INVX1", "pins": _pins(**{"1": ("a", "PWR_ON"), "2": ("y", "SW_EN"), "3": ("vcc", "3V3"),
                                                "4": ("gnd", "GND")})},
        "J3": {"mpn": "HDR3", "pins": _pins(**{"1": (None, "5V0_SW"), "2": (None, "SIG1"), "3": (None, "SIG2")})},
    }
    comps_b = {
        "J2": {"mpn": "HDR3", "pins": _pins(**{"1": (None, "5V0_SW"), "2": (None, "SIG1"), "3": (None, "SIG2")})},
        "U5": {"mpn": "NOCARD1", "pins": _pins(**{"1": ("VCC", "5V0_SW")})},
    }
    for label, comps in (("A", comps_a), ("B", comps_b)):
        nets: dict = {}
        for ref, c in comps.items():
            for num, p in c["pins"].items():
                nets.setdefault(p["net"], {"pins": []})["pins"].append(f"{ref}-{num}")
        write_json(a / "boards" / label / "netlist.json", {"board": label, "components": comps, "nets": nets})
        write_json(a / "boards" / label / "bom.json", [{"ref": r, "mpn": c["mpn"], "dni": False, "in_bom": True}
                                                       for r, c in comps.items()])
    write_json(a / "inventory" / "manifest.json", {"documents": []})
    (a / "atlas.toml").write_text(f'[project]\nfw = "{tmp_path}"\nsources = "hw"\n\n[mcu]\npart = "DEMO-MCU"\n')
    at = a / "atlases" / "core@000000000002"
    write_json(at / "atlas.json", {"id": "core@000000000002", "commit": "0" * 40, "schematic_boards": ["A", "B"],
                                   "mcu": {"ref": "A:U9"}})
    write_json(at / "dt.json", {"uses": [{"pin": "PA3", "node": "/sw", "prop": "enable-gpios", "labels": ["sw"],
                                          "compat": "regulator-fixed"}],
                                "nodes": {"/sw": {"compats": ["regulator-fixed"], "labels": ["sw"]}}})
    write_json(a / "current.json", {"matches": [{"atlas": "core@000000000002"}]})
    return a


def _answer(adir, *args):
    return cli.main(["--atlas-dir", str(adir), "missing", "--answer", *args])


def test_without_an_answer_the_parts_are_unknown(atlas_dir):
    out = query.net_trace(query.Atlas(atlas_dir), "B:5V0_SW")
    assert "fed by" not in out and "- PFET9: A:Q1" in out and "- NOCARD1: B:U5" in out
    assert "generic card" not in out


def test_generic_fet_and_inverter_extend_the_walk(atlas_dir):
    assert _answer(atlas_dir, "PFET9", "standard-function", "p-mosfet", "--pins", "g=G,s=S,3=D,4=D") == 0
    assert _answer(atlas_dir, "INVX1", "standard-function", "inverter", "--pins", "a=A,y=Y,vcc=VCC,gnd=GND") == 0
    out = query.net_trace(query.Atlas(atlas_dir), "B:5V0_SW")
    up = out.split("## Upstream", 1)[1]
    # the feed is found through the FET, and the hop says where the role came from
    assert "fed by A:Q1-3,4 PFET9 D flow=bidir (generic card, engineer-declared: p-mosfet) [generic card, engineer-declared]" in up
    # the gate is an enable; an inverter drives it, and its input is the MCU port
    assert "enable G on A:SW_EN: driven by A:U7-2 INVX1 Y [generic card, engineer-declared]" in up
    assert "input A on A:PWR_ON: MCU PA3 (DT sw.enable-gpios)" in up
    # the walk continues out of the FET's other channel pin, to the board's input connector
    assert "input connector A:J1-1" in up
    # answered parts are no longer asked for; the unanswered one still is
    assert "- PFET9" not in out and "- INVX1" not in out and "- NOCARD1: B:U5" in out
    part = query.net_trace(query.Atlas(atlas_dir), "A:Q1")
    assert "pin roles from a generic card the engineer declared" in part and "g -> A:SW_EN" not in part
    assert "A:Q1-1 G -> A:SW_EN dir=in type=gate flow=sink" in part


def test_card_gives_roles_and_never_a_limit(atlas_dir):
    for fn, card in answers.CARDS.items():
        for pin, spec in card["pins"].items():
            assert set(spec) == {"dir", "type", "flow", "control", "optional"}, (fn, pin)
    rec = answers.pin_records({"function": "n-mosfet", "pins": {"1": "G", "2": "S", "3": "D"}},
                              {"pins": _pins(**{"1": (None, "x"), "2": (None, "y"), "3": (None, "z")})})
    assert rec["1"]["prov"] == "engineer-declared" and rec["3"]["flow"] == "bidir" and rec["1"]["control"] is True
    assert not ({"min", "max", "typ", "unit", "limit", "t"} & set(rec["1"]))


def test_a_card_is_used_only_for_an_answered_mpn_and_never_over_a_datasheet(atlas_dir):
    out = query.net_trace(query.Atlas(atlas_dir), "A:Q1")
    assert "generic" not in out                      # nothing answered: names like `g`, `s`, `d` mean nothing
    _answer(atlas_dir, "PFET9", "standard-function", "p-mosfet", "--pins", "g=G,s=S,3=D,4=D")
    assert "generic card" in query.net_trace(query.Atlas(atlas_dir), "A:Q1")
    d = atlas_dir / "docs" / "pfet9@1"
    write_json(d / "doc.json", {"doc_id": "pfet9@1", "part": "PFET9", "kind": "datasheet"})
    assert "generic card" not in query.net_trace(query.Atlas(atlas_dir), "A:Q1")


def test_missing_md_orders_by_walks_and_shows_answers(atlas_dir):
    a = ms.run(atlas_dir)
    R = {r["mpn"]: r for r in a["rows"]}
    assert R["PFET9"]["walks"] >= 1 and R["NOCARD1"]["walks"] >= 1
    md = (atlas_dir / "inventory" / "missing.md").read_text()
    assert "Stops " in md and "net_trace walk" in md and "## Answered" not in md
    _answer(atlas_dir, "PFET9", "standard-function", "p-mosfet", "--pins", "g=G,s=S,3=D,4=D")
    _answer(atlas_dir, "NOCARD1", "not-needed", "--why", "firmware never touches it")
    _answer(atlas_dir, "INVX1", "supplied")
    ms.run(atlas_dir)
    md = (atlas_dir / "inventory" / "missing.md").read_text()
    assert "## Answered (3)" in md
    assert "**PFET9** x1 (A:Q1): generic card p-mosfet (engineer-declared; pins g=G, s=S, 3=D, 4=D)" in md
    assert "**NOCARD1** x1 (B:U5): not needed: firmware never touches it" in md
    assert "**INVX1** x1 (A:U7): supplied; the file is not in the manifest yet" in md
    assert "Q2. **PFET9**" not in md and "Q2. **NOCARD1**" not in md   # no longer open


def test_open_questions_rank_by_walks_stopped(atlas_dir):
    a = ms.analyse(atlas_dir)
    walks = [r["walks"] for r in a["rows"]]
    assert walks == sorted(walks, reverse=True)
    R = {r["mpn"]: r for r in a["rows"]}
    assert R["PFET9"]["walks"] == 2          # the 5V0_SW walk and the VIN walk both stop at the switch
    md = ms.render_md(a)
    assert "Stops 2 net_trace walks." in md


def test_answer_validation(atlas_dir, capsys):
    assert _answer(atlas_dir, "PFET9", "standard-function", "p-mosfet") == 2           # no pin map
    assert _answer(atlas_dir, "PFET9", "standard-function", "p-mosfet", "--pins", "g=G,s=S") == 2   # D missing
    assert _answer(atlas_dir, "PFET9", "standard-function", "flux-capacitor", "--pins", "g=G") == 2
    assert _answer(atlas_dir, "PFET9", "standard-function", "p-mosfet", "--pins", "zz=G,s=S,3=D") == 2  # no such pin
    assert _answer(atlas_dir, "NOPART", "standard-function", "inverter", "--pins", "1=A,2=Y") == 2
    assert _answer(atlas_dir, "NOCARD1", "not-needed") == 2                            # why is required
    assert not answers.path_of(atlas_dir).exists()
    err = capsys.readouterr().err
    assert "no part on the boards has MPN" in err and "needs a `why`" in err and "leaves out D" in err


def test_answers_file_round_trips_and_a_bad_file_is_reported(atlas_dir):
    _answer(atlas_dir, "NOCARD1", "not-needed", "--why", 'has "quotes"')
    _answer(atlas_dir, "PFET9", "standard-function", "p-mosfet", "--pins", "g=G,s=S,3=D")
    got = answers.load(atlas_dir)
    assert got["NOCARD1"]["why"] == 'has "quotes"' and got["PFET9"]["pins"] == {"g": "G", "s": "S", "3": "D"}
    _answer(atlas_dir, "pfet9", "supplied")                                            # replaces, matched ignoring case
    assert list(answers.load(atlas_dir)) == ["NOCARD1", "pfet9"]
    answers.path_of(atlas_dir).write_text('[parts."X"]\nanswer = "maybe"\n')
    with pytest.raises(ValueError, match="answer"):
        answers.load(atlas_dir)
    assert cli.main(["--atlas-dir", str(atlas_dir), "missing"]) == 2                   # said, not a traceback
    out = query.net_trace(query.Atlas(atlas_dir), "B:5V0_SW")                          # net_trace goes on without it
    assert "inventory/answers.toml ignored" in out and "generic card" not in out

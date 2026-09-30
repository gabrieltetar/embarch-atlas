"""interfaces: mating-rule proposals, scores, rename flags, engineer entries left alone."""
from __future__ import annotations

from pathlib import Path

from embarch_atlas import cli, common, interfaces as itf


def comp(pins: dict[str, str | None], mpn: str | None = None, dni: bool = False) -> dict:
    return {"mpn": mpn, "value": None, "dni": dni, "pages": [1], "parts": [], "sheets": [],
            "pins": {p: {"name": None, "net": n} for p, n in pins.items()}}


def netlist(board: str, comps: dict[str, dict]) -> dict:
    nets: dict[str, dict] = {}
    for ref, c in comps.items():
        for p, P in c["pins"].items():
            if P["net"]:
                nets.setdefault(P["net"], {"pins": [], "sheets": ["S"], "labels": 0, "ports": 0,
                                           "auto": P["net"].startswith("Net")})["pins"].append(f"{ref}-{p}")
    return {"board": board, "source": {"file": f"{board}.pdf", "sha256": board * 64}, "method": "smartpdf-outline",
            "sheets": {"S": {"page": 1}}, "components": comps, "nets": nets, "counts": {}, "issues": []}


# Board X's J1 contact n meets board Y's J2 contact 7-n. One signal drops its active-low `n`,
# one contact is on an auto-named net on each side, and the mounting pads are GND on both.
X = netlist("X", {
    "J1": comp({"1": "PWR", "2": "SDA", "3": "SCL", "4": "ALERT", "5": "NetJ1_5", "6": "GND", "ME1": "GND"}, "FFC-6"),
    "J3": comp({"1": "LINE_X_EXT"}, "PAD-1", dni=True),
    "J4": comp({"1": "UART_X_TX_EXT"}, "PAD-1"),
    "J5": comp({"1": "GND_P"}, "PAD-1"),
    "U1": comp({"1": "SDA", "2": "SCL", "3": "LOCAL_OK"}),
})
Y = netlist("Y", {
    "J2": comp({"1": "GND", "2": "NetJ2_2", "3": "nALERT", "4": "SCL", "5": "SDA", "6": "PWR", "ME1": "GND"}, "FFC-6"),
    "J7": comp({"1": "LINE_Y_EXT"}, "PIN-1"),
    "J11": comp({"1": "UART_Y_RX_EXT"}, "PIN-1"),
    "J12": comp({"1": "GND_P"}, "PIN-1"),
    "U5": comp({"1": "SDA", "2": "LOCAL_OK"}),
})


def write_boards(adir: Path, *nls) -> None:
    for nl in nls:
        common.write_json(adir / "boards" / nl["board"] / "netlist.json", nl)


def test_rule_maps():
    assert itf.rule_map("mirror", 6) == {1: 6, 2: 5, 3: 4, 4: 3, 5: 2, 6: 1}
    assert itf.rule_map("row-swap", 6) == {1: 2, 2: 1, 3: 4, 4: 3, 5: 6, 6: 5}
    # dual-row, odd/even across rows, columns reversed and rows kept: 1 -> 5, 2 -> 6
    assert itf.rule_map("column-reverse", 6) == {1: 5, 2: 6, 3: 3, 4: 4, 5: 1, 6: 2}
    assert itf.rule_map("row-swap", 5) is None
    for n in (4, 6, 32):
        for r in itf.RULE_ORDER:
            m = itf.rule_map(r, n)
            assert m is None or sorted(m.values()) == list(range(1, n + 1))


def test_classify_rename():
    assert itf.classify_rename("ALERT", "nALERT") == "active-low-marker"
    assert itf.classify_rename("RESET_N", "RESET") == "active-low-marker"
    assert itf.classify_rename("Sda", "SDA") == "case"
    assert itf.classify_rename("I2C_SDA", "I2CSDA") == "separator"
    assert itf.classify_rename("SDA", "MISO") == "different"


def test_board_to_board_proposal(tmp_path, capsys):
    adir = tmp_path / "atlas"
    write_boards(adir, X, Y)
    assert cli.main(["--atlas-dir", str(adir), "interfaces"]) == 0
    sysj = common.read_json(adir / "boards" / "system.json")
    b2b = [p for p in sysj["proposed"] if p["kind"] == "board-to-board"]
    assert len(b2b) == 1
    p = b2b[0]
    assert (p["id"], p["rule"], p["score"], p["contacts"]) == ("X:J1~Y:J2", "mirror", "4/5", 6)
    assert p["rules"]["identity"] == "0/4" and p["status"] == "proposed" and p["prov"] == "deterministic"
    assert [(f["a"], f["b"], f["class"]) for f in p["flags"]] == [("X:J1-4", "Y:J2-3", "active-low-marker")]
    assert [r["a"] for r in p["unnamed"]] == ["X:J1-5"]  # auto-named on both sides: not scored
    assert p["pads"] == [{"a": "X:J1-ME1", "a_net": "GND", "b": "Y:J2-ME1", "b_net": "GND", "match": "="}]
    out = capsys.readouterr().out
    assert "X:J1 <-> Y:J2  rule=mirror 4/5" in out and "active-low-marker" in out
    # same-name nets the interface does not join are listed; joined ones are not
    nc = sysj["name_check"]["X~Y"]
    assert nc["same_name_not_joined"] == ["GND_P", "LOCAL_OK"]
    assert ["X:ALERT", "Y:nALERT"] in nc["joined_under_different_names"]


def test_unit_to_unit_pairs_by_role(tmp_path):
    adir = tmp_path / "atlas"
    write_boards(adir, X, Y)
    sysj = itf.run(adir)
    u = next(p for p in sysj["proposed"] if p["kind"] == "unit-to-unit")
    assert u["substitution"] == ["X", "Y"] and u["score"] == "3/3" and u["status"] == "proposed"
    rel = {(q["a"], q["b"]): q["relation"] for q in u["pairs"]}
    assert rel == {("X:J3-1", "Y:J7-1"): "renamed", ("X:J4-1", "Y:J11-1"): "crossed", ("X:J5-1", "Y:J12-1"): "same"}
    assert next(q for q in u["pairs"] if q["a"] == "X:J3-1")["a_dni"] is True
    assert "not in the schematics" in u["note"]


def test_engineer_entries_are_never_overwritten(tmp_path):
    adir = tmp_path / "atlas"
    write_boards(adir, X, Y)
    declared = {"id": "X:J1~Y:J2", "kind": "board-to-board", "a": "X:J1", "b": "Y:J2", "rule": "identity",
                "prov": "engineer-declared", "note": "per the assembly drawing"}
    confirmed = {"id": "X~Y:unit-to-unit", "kind": "unit-to-unit", "a": "X", "b": "Y", "pairs": [],
                 "prov": "engineer-confirmed"}
    common.write_json(adir / "boards" / "system.json",
                      {"units": [{"i": 0}], "interfaces": [declared], "proposed": [confirmed, {"id": "stale"}]})
    sysj = itf.run(adir)
    assert sysj["interfaces"] == [declared] and sysj["units"] == [{"i": 0}]
    assert confirmed in sysj["proposed"] and {"id": "stale"} not in sysj["proposed"]
    ids = [p["id"] for p in sysj["proposed"]]
    assert "X:J1~Y:J2" not in ids and ids.count("X~Y:unit-to-unit") == 1
    chk = next(p for p in sysj["proposed"] if p["kind"] == "board-to-board-check")
    assert chk["declared_rule"] == "identity" and chk["declared_score"] == "0/4"
    assert chk["machine_best"] == {"rule": "mirror", "score": "4/5"}
    again = itf.run(adir)
    assert again == common.read_json(adir / "boards" / "system.json") == sysj
    # the declared rule, not the proposal, is what other modules mate through
    pairs = itf.mated_pairs(adir)
    assert ("X:J1-1", "Y:J2-1", "engineer-declared") in pairs and all(s == "engineer-declared" for *_, s in pairs)


def test_mated_pairs_fall_back_to_best_proposal(tmp_path):
    adir = tmp_path / "atlas"
    write_boards(adir, X, Y)
    itf.run(adir)
    pairs = itf.mated_pairs(adir)
    assert ("X:J1-4", "Y:J2-3", "proposed") in pairs and ("X:J1-ME1", "Y:J2-ME1", "proposed") in pairs
    assert itf.mated_pairs(adir, include_proposed=False) == []


def test_needs_two_boards(tmp_path, capsys):
    adir = tmp_path / "atlas"
    write_boards(adir, X)
    assert cli.main(["--atlas-dir", str(adir), "interfaces"]) == 2
    assert "two boards" in capsys.readouterr().err

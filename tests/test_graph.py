"""graph: the whole-atlas export a human surface draws (DESIGN.md "graph.json").

Synthetic throughout: the two-board product of test_join (M main board with the MCU,
S sensor board behind a mirrored 6-pin connector), plus hand-written `nm` and
`objdump` text standing in for a build's ELF.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from embarch_atlas import graph
from embarch_atlas.common import read_json

sys.path.insert(0, str(Path(__file__).parent))
from test_join import make_project, run_join  # noqa: E402

NM = """\
08000100 00000040 T main\t{wt}/app/src/main.c:10
08000140 00000020 t helper\t{wt}/app/src/main.c:30
08000160 00000030 t helper\t{wt}/lib/led/led.c:5
08000190 00000050 T led_set\t{wt}/lib/led/led.c:20
080001e0 00000060 t sns_init\t{wt}/drivers/sns.c:40
08000240 00000040 T sns_fetch\t{wt}/drivers/sns.c:80
08000280 00000080 t i2c_xfer\t{zb}/drivers/i2c/i2c_ll_stm32.c:200
08000300 00000020 T log_msg\t{zb}/subsys/logging/log_core.c:50
08000320 00000010 T k_sleep\t{zb}/kernel/sched.c:9
08001000 00000018 R __device_dts_ord_21
08001018 00000018 R __device_dts_ord_20
"""

OBJDUMP = """\
08000100 <main>:
 8000100:\tbl\t8000190 <led_set>
 8000104:\tbl\t8000140 <helper>
 8000108:\tbl\t8000320 <k_sleep>
 800010c:\tldr\tr0, [pc, #4]\t@ (8000114 <main+0x14>)
 8000114:\t.word\t0x08001000
08000140 <helper>:
 8000140:\tb.w\t8000300 <log_msg>
08000160 <helper>:
 8000160:\tbx\tlr
08000190 <led_set>:
 8000190:\tbl\t8000160 <helper>
 8000194:\t.word\t0x08000241
080001e0 <sns_init>:
 80001e0:\tbl\t8000280 <i2c_xfer>
 80001e4:\t.word\t0x08001018
08000240 <sns_fetch>:
 8000240:\tbne.n\t8000250 <sns_fetch+0x10>
"""


def _code(ad, atlas):
    stamp = read_json(atlas / "atlas.json")
    wt = (ad / stamp["build"]["worktree"]).resolve()
    stamp["build"]["zephyr_base"] = "/zb"
    dt = read_json(atlas / "dt.json")
    return graph.code_model(ad, atlas, stamp, dt, nm_text=NM.format(wt=wt, zb="/zb"), objdump_text=OBJDUMP)


# ---------------------------------------------------------------- ELF text

def test_parse_nm_and_objdump():
    syms = graph.parse_nm(NM.format(wt="/w", zb="/z"))
    assert syms[0] == {"addr": 0x08000100, "size": 0x40, "type": "T", "name": "main", "file": "/w/app/src/main.c", "line": 10}
    assert syms[-1]["file"] is None and syms[-1]["name"] == "__device_dts_ord_20"
    fns = graph.parse_objdump(OBJDUMP)
    assert fns[0x08000100]["calls"] == {0x08000190, 0x08000140, 0x08000320}
    assert fns[0x08000100]["words"] == {0x08001000}
    # two static functions named `helper`: kept apart by address
    assert fns[0x08000140]["calls"] == {0x08000300} and fns[0x08000160]["calls"] == set()


def test_module_of_layers():
    names = {"drivers/sensor/sns7xx": "sns7xx_alert"}
    assert graph.module_of("fw", "apps/demo/src/main.c", names)["layer"] == "app"
    assert graph.module_of("fw", "apps/demo/src/util.h", names)["id"] == "code:fw:apps/demo/src/util.c"
    assert graph.module_of("fw", "lib/led/led.c", names) == {
        "id": "code:fw:lib/led", "layer": "mw", "label": "lib/led", "zephyr": False, "path": "lib/led/"}
    d = graph.module_of("fw", "drivers/sensor/sns7xx/sns.c", names)
    assert d["layer"] == "drv" and d["label"] == "sns7xx_alert"
    assert graph.module_of("fw", "drivers/include/util.h", names) is None
    assert graph.module_of("fw", "tests/x/main.c", names) is None
    z = graph.module_of("zephyr", "drivers/i2c/i2c_ll_stm32.c", names)
    assert z["zephyr"] and z["layer"] == "drv" and z["label"] == "i2c_ll_stm32"
    assert graph.module_of("zephyr", "subsys/logging/log_core.c", names)["id"] == "code:zephyr:subsys/logging"
    assert graph.module_of("zephyr", "kernel/sched.c", names) is None


def test_code_model_calls_devices_and_callbacks(tmp_path):
    ad, atlas = make_project(tmp_path)
    code = _code(ad, atlas)
    mods = code["modules"]
    assert set(mods) == {"code:fw:app", "code:fw:lib/led",
                         "code:fw:drivers", "code:zephyr:drivers/i2c/i2c_ll_stm32",
                         "code:zephyr:subsys/logging"}
    calls = code["calls"]
    assert calls[("code:fw:app", "code:fw:lib/led")] == 1
    # the static `helper` in main.c tail-calls logging; the one in led.c does nothing
    assert calls[("code:fw:app", "code:zephyr:subsys/logging")] == 1
    assert ("code:fw:lib/led", "code:zephyr:subsys/logging") not in calls
    # a function's address in a literal pool is a callback registration: a dependency
    assert calls[("code:fw:lib/led", "code:fw:drivers")] == 1
    assert calls[("code:fw:drivers", "code:zephyr:drivers/i2c/i2c_ll_stm32")] == 1
    # kernel code is not a module, so calls into it are not edges
    assert not any(b.endswith("kernel") for _a, b in calls)
    assert code["devices"] == {("code:fw:app", 21): 1, ("code:fw:drivers", 20): 1}
    assert mods["code:fw:drivers"]["label"] == "sns7xx_alert"     # named after the compatible it implements
    assert [f["name"] for f in mods["code:fw:lib/led"]["functions"]] == ["led_set", "helper"]


# ---------------------------------------------------------------- symbols

def _pins(*spec):
    return [{"num": str(n), "name": nm, "net": net, "dir": d} for n, nm, net, d in spec]


def test_symbol_kinds():
    c2 = {"pins": {"1": {}, "2": {}}}
    c3 = {"pins": {"1": {}, "2": {}, "3": {}}}
    assert graph.symbol_kind("R4", c2, None) == "resistor"
    assert graph.symbol_kind("C1", c2, None) == "capacitor"
    assert graph.symbol_kind("FB2", c2, None) == "ferrite"
    assert graph.symbol_kind("L1", c2, None) == "inductor"
    assert graph.symbol_kind("D1", c2, {"description": "LED RED 0603"}) == "led"
    assert graph.symbol_kind("D2", c2, {"description": "TVS DIODE 5V"}) == "tvs"
    assert graph.symbol_kind("D3", c2, None) == "diode"
    assert graph.symbol_kind("Q1", c3, None) == "transistor"
    assert graph.symbol_kind("U1", c3, None) == "ic"
    assert graph.symbol_kind("J2", c2, None) == "connector"
    assert graph.symbol_kind("TP7", {"pins": {"1": {}}}, None) == "testpoint"
    # a resistor with one end unconnected is still a resistor, its open end drawn without a stub
    assert graph.symbol_kind("R9", {"pins": {"1": {}}}, None) == "resistor"
    one = graph.layout_symbol("resistor", _pins((1, None, "M:X", None)), set())
    assert one["w"] <= 12 and [p["s"] for p in one["pins"]] == ["t"]


def test_two_pin_parts_stand_with_ground_down_and_anode_up():
    r = graph.layout_symbol("capacitor", _pins((1, None, "M:GND", None), (2, None, "M:3V3", None)), {"M:GND"})
    top, bot = r["pins"]
    assert (top["net"], top["s"], bot["net"], bot["s"]) == ("M:3V3", "t", "M:GND", "b")
    assert top["y"] < 0 < bot["y"] and r["w"] <= 12
    d = graph.layout_symbol("diode", _pins((1, "K", "M:A", None), (2, "A", "M:B", None)), set())
    assert d["pins"][0]["name"] == "A" and d["pins"][0]["s"] == "t"
    # no pin names: pin 1 is the cathode, so pin 2 goes on top
    d2 = graph.layout_symbol("led", _pins((1, None, "M:A", None), (2, None, "M:B", None)), set())
    assert d2["pins"][0]["num"] == "2"


def test_ic_box_supplies_top_grounds_bottom_names_inside():
    pins = _pins((1, "SDA", "M:SDA", None), (2, "SCL", "M:SCL", None), (3, "VDD", "M:3V3", None),
                 (4, "GND", "M:GND", None), (5, "GND", "M:GND", None), (6, "INT", "M:INT", "out"))
    s = graph.layout_symbol("ic", pins, {"M:GND"})
    side = {p["name"]: p["s"] for p in s["pins"]}
    assert side == {"SDA": "l", "SCL": "l", "VDD": "t", "GND": "b", "INT": "r"}
    gnd = [p for p in s["pins"] if p["name"] == "GND"]
    assert len(gnd) == 1 and gnd[0]["nums"] == ["4", "5"]          # same name, same net: one stub
    assert graph._inner(gnd[0], "ic") == "GND"
    assert graph._inner({"num": "3", "nums": ["3"], "name": None}, "ic") == "3"


def test_mcu_box_splits_ports_by_letter():
    pins = [{"num": str(i), "name": f"P{l}{n}", "net": f"M:N{i}", "dir": None}
            for i, (l, n) in enumerate([(l, n) for l in "ABC" for n in range(6)], 1)]
    s = graph.layout_symbol("ic", pins, set())
    left = {p["name"][1] for p in s["pins"] if p["s"] == "l"}
    right = {p["name"][1] for p in s["pins"] if p["s"] == "r"}
    assert left.isdisjoint(right) and left | right == set("ABC")


def test_transistor_without_pin_names_is_a_numbered_box():
    s = graph.layout_symbol("transistor", _pins((1, None, "M:G", None), (2, None, "M:S", None), (3, None, "M:D", None)), set())
    assert s["w"] >= 40 and {p["s"] for p in s["pins"]} <= {"l", "r"}
    named = graph.layout_symbol("transistor", _pins((1, "G", "M:G", None), (2, "S", "M:S", None), (3, "D", "M:D", None)), set())
    assert {p["name"]: p["s"] for p in named["pins"]} == {"G": "l", "D": "t", "S": "b"}


def test_labels_clear_of_the_symbol():
    s = graph.layout_symbol("resistor", _pins((1, None, "M:A", None), (2, None, "M:B", None)), set())
    lab, ext = graph.place_label(s, "R1\n10k", {"M:A": "A", "M:B": "B"})
    assert lab["a"] == "start" and lab["x"] > s["w"] / 2
    box = graph.layout_symbol("ic", _pins((1, "VDD", "M:3V3", None), (2, "IO", "M:IO", None)), set())
    lab2, ext2 = graph.place_label(box, "U1\nPART", {"M:3V3": "3V3", "M:IO": "IO"})
    top_tip = min(p["y"] for p in box["pins"])
    assert lab2["y"] < top_tip and ext2[1] < lab2["y"]


# ---------------------------------------------------------------- clustering, citations

def test_louvain_two_cliques_and_stable():
    adj = {}

    def link(a, b, w=1.0):
        adj.setdefault(a, {})[b] = w
        adj.setdefault(b, {})[a] = w

    for grp in ("abcd", "wxyz"):
        for i, a in enumerate(grp):
            for b in grp[i + 1:]:
                link(a, b)
    link("d", "w", 0.2)
    c = graph.louvain(adj)
    assert len({c[x] for x in "abcd"}) == 1 and len({c[x] for x in "wxyz"}) == 1 and c["a"] != c["w"]
    shuffled = {k: dict(reversed(list(v.items()))) for k, v in reversed(list(adj.items()))}
    assert graph.louvain(shuffled) == c


def test_evidence_rows_carry_structured_citations(tmp_path):
    sp = lambda b, r: {"t": "doc", "doc": f"sch:{b}", "p": 1}
    fw = graph.evidence_row("DT alert-gpios @ fw:boards/demo/board.dts:31", tmp_path, sp)
    assert fw == {"src": "Firmware", "says": "DT alert-gpios", "cite": {"t": "code", "root": "fw", "path": "boards/demo/board.dts", "line": 31}}
    ds = graph.evidence_row("acme.reg.CONF CONF @0x00 reset 0x1234 (sns701@a p21,22)", tmp_path, sp)
    assert ds["src"] == "Datasheet" and ds["cite"]["doc"] == "sns701@a" and ds["cite"]["p"] == 21
    sch = graph.evidence_row("schematic M:U1-4 M:nMON_ALERT -> ['M:U3-C2']", tmp_path, sp)
    assert sch["src"] == "Schematic" and sch["cite"]["doc"] == "sch:M"
    assert graph.evidence_row("ELF: no __device_dts_ord for the node", tmp_path, sp)["src"] == "Build"


# ---------------------------------------------------------------- the whole export

def _export(tmp_path):
    ad, atlas = make_project(tmp_path)
    run_join(ad, atlas)                          # join writes graph.json too (without a code model here)
    assert (atlas / "graph.json").is_file()
    from embarch_atlas.join import load_join
    stamp = read_json(atlas / "atlas.json")
    J, _r, _s = load_join(ad, atlas, stamp, read_json(atlas / "dt.json"), read_json(atlas / "in_use.json"), log=lambda *a: None)
    g = graph.run(ad, atlas, J, stamp, read_json(atlas / "pins.json"), read_json(atlas / "mismatches.json"),
                  log=lambda *a: None, code=_code(ad, atlas))
    return ad, atlas, g


def test_export_layers_nodes_and_symbols(tmp_path):
    _ad, atlas, g = _export(tmp_path)
    assert g["schema"] == graph.SCHEMA and [l["id"] for l in g["vocab"]["layers"]] == ["ext", "app", "mw", "drv", "soc", "hw"]
    by = {n["id"]: n for n in g["nodes"]}
    parts = [n for n in g["nodes"] if n["kind"] == "part"]
    assert len(parts) == 15 + 4                               # every part on both boards, passives and TPs included
    assert by["hw:M:U1"].get("mcu") is True
    assert by["hw:M:R1"]["sym_kind"] == "resistor" and by["hw:M:J1"]["sym_kind"] == "connector"
    u3 = by["hw:M:U3"]
    names = {p["nm"]: p for p in u3["sym"]["pins"]}
    assert names["SDA"]["net"] == "M:BUS0_SDA" and names["SDA"]["s"] in "lr"
    assert {"pin:PB6", "pin:PB7", "pin:PA1"} <= set(by)
    assert by["periph:I2C1"]["layer"] == "soc"
    for n in g["nodes"]:
        assert "x" in n and "y" in n and n["cluster"].startswith("c"), n["id"]
    assert json.loads((atlas / "graph.json").read_text())["counts"]["nodes"] == g["counts"]["nodes"]


def test_export_nets_carry_the_firmware_name(tmp_path):
    _ad, _atlas, g = _export(tmp_path)
    sda = g["nets"]["M:BUS0_SDA"]
    assert sda["fw"]["port"] == ["PB7"] and "i2c1_sda" in sda["fw"]["uses"]
    assert sda["fw"]["text"] == "PB7 · i2c1_sda"
    # across the mated connector, the sensor board's net carries the same name
    assert g["nets"]["S:BUS0_SDA"]["fw"]["port"] == ["PB7"]
    assert "S:BUS0_SDA" in sda["mates"]
    assert g["nets"]["M:GND"]["rail"] and g["nets"]["M:GND"]["ground"] and "fw" not in g["nets"]["M:GND"]


def test_export_edges_bind_gpio_pinctrl_and_code(tmp_path):
    _ad, _atlas, g = _export(tmp_path)
    E = {(e["kind"], e["a"], e["b"]): e for e in g["edges"]}
    assert ("pinctrl", "periph:I2C1", "pin:PB7") in E
    assert E[("gpio", "code:fw:drivers", "pin:PA1")]["label"].startswith("alert-gpios")
    binds = {(e["b"], e["label"]) for e in g["edges"] if e["kind"] == "bind"}
    assert ("hw:M:U3", "mon-a@44") in binds and ("hw:S:U1", "mon-b@45") in binds
    assert ("net", "pin:PB7", "hw:M:U3") in E
    assert ("call", "code:fw:app", "code:fw:lib/led") in E
    assert E[("device", "code:fw:drivers", "code:zephyr:drivers/i2c/i2c_ll_stm32")]["label"] == "i2c1"
    assert ("mate", "hw:M:J1", "hw:S:J1") in E or ("mate", "hw:S:J1", "hw:M:J1") in E


def test_export_problems_and_checks(tmp_path):
    _ad, atlas, g = _export(tmp_path)
    report = read_json(atlas / "mismatches.json")
    n_check = sum(1 for c in report["candidates"] if c["severity"] == "check")
    assert g["counts"]["mismatches"] == n_check
    for p in g["problems"]:
        assert p["id"][0] in "MU" and p["nodes"] and all(r["src"] for r in p["rows"])
    assert {c["check"] for c in g["checks"]} == {c["check"] for c in report["checks"]}
    flagged = {i for p in g["problems"] for i in p["nodes"]}
    by = {n["id"]: n for n in g["nodes"]}
    assert all(by[i]["status"] in ("mis", "unv") for i in flagged)


def test_export_clusters_cover_and_satellites_follow(tmp_path):
    _ad, _atlas, g = _export(tmp_path)
    by = {n["id"]: n for n in g["nodes"]}
    ids = {c["id"] for c in g["clusters"]}
    assert all(n["cluster"] in ids for n in g["nodes"])
    assert g["clusters"][0]["kind"] == "hub"
    # a pull-up sits with the part it serves, in that part's cluster
    for n in g["nodes"]:
        if n["kind"] == "part" and n.get("of"):
            assert n["cluster"] == by[n["of"]]["cluster"]
    # columns do not overlap
    xs = sorted((c["x"], c["x"] + c["w"]) for c in g["clusters"])
    assert all(a[1] <= b[0] for a, b in zip(xs, xs[1:]))

"""The mismatch report: checks run and passed first, then candidates ranked by class.

Every candidate is a question for the engineer, never a hardware fact. Each one
carries its evidence (DT file:line, board:ref-pin or net, document cite) and
what would settle it. Expected items (debug/boot/oscillator pins, test-point-only
nets) are folded into counts, not listed.

The classes follow the catalogue of cross-source checks for Zephyr on STM32
(pinctrl/AF vs schematic, GPIO polarity, pull/bias, I2C address strapping, bus
assignment, EXTI sharing, DT values vs BOM, DT-declared vs driver reality, hand
annotations vs truth, cross-board names), plus two record-fed hooks: a `conflict`
record whose `sources` include a driver (driver-vs-datasheet), and a second-look
verdict of `conflict` (document-reading conflict). The `reg-*` classes (register
constants in the driver sources vs the part's register/field records) come from
`codecheck.py`, which this report runs.
"""
from __future__ import annotations

import collections
import re
from typing import Any

from embarch_atlas.common import read_jsonl, write_json, write_text

# rank order: most actionable first
ORDER = [
    "cross-board-net-name", "device-signal-mismatch", "i2c-address-strap", "i2c-bus-membership",
    "dt-value-vs-bom", "dt-comment-vs-net", "driver-vs-datasheet", "reg-field-in-other-register", "reg-address-vs-name",
    "reg-mask-vs-field", "reg-reset-vs-datasheet", "reg-address-unknown", "document-reading-conflict",
    "pinctrl-func-vs-net-name", "polarity-vs-net-name", "open-drain-no-pull", "pull-conflict",
    "pull-to-other-rail", "dt-pin-not-connected", "dt-no-binding", "dt-no-driver", "symbol-vs-datasheet",
    "i2c-identity-unresolved", "reg-comment-vs-datasheet", "exti-line-shared", "pin-shared-in-dt", "connected-no-dt", "dni-on-path",
    "polarity-resolved",
]
SETTLE = {
    "cross-board-net-name": "the engineer confirms the connector mating rule and which spelling is right",
    "device-signal-mismatch": "the schematic page of the part and the DT node: which pin is the signal really on",
    "i2c-address-strap": "the strap pin's net or resistor on the schematic against the datasheet strap table",
    "i2c-bus-membership": "is the part on this bus meant to have a DT node (or is it driven another way)",
    "dt-value-vs-bom": "the fitted resistor (BOM, rework) against the DT property",
    "dt-comment-vs-net": "fix the comment or the pin: the netlist is the schematic's own compile",
    "driver-vs-datasheet": "re-read the cited datasheet page against the driver line; one of them is wrong",
    "document-reading-conflict": "a third read of the cited page, or the engineer",
    "pinctrl-func-vs-net-name": "the far-end pin's function (the net name may just be generic)",
    "polarity-vs-net-name": "the far-end pin's active level from its datasheet (an inverting stage is legitimate)",
    "open-drain-no-pull": "add a pull on the board or in DT, or confirm one exists off-board",
    "pull-conflict": "drop the DT pull or the board pull; they fight",
    "pull-to-other-rail": "the rail voltage against the pin's I/O structure (datasheet abs max)",
    "dt-pin-not-connected": "the schematic: is the DT pin really unconnected on this board revision",
    "dt-no-binding": "give the node a compatible and binding, or remove it: no driver can use it",
    "dt-no-driver": "enable the driver (Kconfig) or disable the node: its pinctrl is never applied",
    "symbol-vs-datasheet": "the MCU symbol against the datasheet pinout table",
    "i2c-identity-unresolved": "translate the part's datasheet strap table, or declare it in boards/straps.jsonl",
    "exti-line-shared": "only one port per EXTI line can interrupt (EXTICR selects one): check which one is armed at run time",
    "pin-shared-in-dt": "is the far end a wired-OR (open-drain) line? then sharing is by design",
    "connected-no-dt": "not yet in firmware, or a missing DT property",
    "dni-on-path": "the fitted BOM or rework notes: the path through a not-fitted part is open",
    "polarity-resolved": "the LED's anode/cathode orientation on the symbol",
}
NOT_RUN = [
    ("DT limits vs datasheet", "mapping `*-microamp`/`*-microvolt` properties to limit records is not deterministic yet"),
    ("enable / sequence timing", "code delays and DT `*-ms` properties vs datasheet timing records need that mapping"),
    ("power domain of the far-end part", "far-end VCC rail vs MCU I/O level needs pin records with supply pins"),
    ("boot state before firmware runs", "pulls + inverters + part reset values: done by hand so far"),
]


def active_low_name(net: str | None) -> bool:
    return bool(net) and bool(re.search(r"^n[A-Z0-9]|^~|^/|_N$", net))


def _flags(u: dict) -> str:
    return u.get("flags") or ""


def _od(rec: dict | None) -> bool:
    if not rec:
        return False
    t = " ".join(str(rec.get(k) or "") for k in ("type", "dir", "desc")).lower()
    return bool(re.search(r"\bod\b|open[- ]drain|open[- ]collector", t))


class Report:
    def __init__(self):
        self.candidates: list[dict] = []
        self.checks: dict[str, dict] = {}
        self.folded: dict[str, list[str]] = collections.defaultdict(list)
        self.not_run: list[dict] = []

    def check(self, name: str, sources: str, ran: int = 0, passed: int = 0):
        c = self.checks.setdefault(name, {"check": name, "sources": sources, "ran": 0, "passed": 0, "candidates": 0})
        c["ran"] += ran
        c["passed"] += passed
        return c

    def cand(self, cls: str, key: str, summary: str, evidence: list[str], check: str | None = None,
             severity: str = "check", settle: str | None = None):
        self.candidates.append({"class": cls, "key": key, "summary": summary, "evidence": [e for e in evidence if e],
                                "settle": settle or SETTLE.get(cls), "severity": severity})
        if check and check in self.checks:
            self.checks[check]["candidates"] += 1


def run(J, stamp: dict, pins: dict) -> dict:
    R = Report()
    rows = pins["pins"]
    mcu = J.mcu
    mb = mcu[0] if mcu else None
    have_pin_recs = any(True for _ in J.docs.all_records("pin"))

    # ---- per MCU pin
    R.check("pinctrl function vs net name", "DT pinctrl, netlist")
    R.check("GPIO polarity vs net name", "DT flags, netlist, LED structure")
    R.check("DT pull vs board pull", "DT flags/bias, netlist + BOM values")
    R.check("pull rail vs MCU I/O supply", "netlist + BOM values, MCU supply pins")
    R.check("DT pin reaches the board", "DT, netlist")
    R.check("connected pin has a DT use", "netlist, DT")
    R.check("DNI parts on MCU paths", "netlist/BOM DNI flags")
    io_rails = set()
    if mcu:
        for r in rows:
            if re.match(r"^(VDD|VCC|VDDIO)\w*$", r["name"] or "") and r["net"] and not J.is_ground(r["net"]):
                io_rails.add(r["net"])
    for r in rows:
        port, net, far = r["port"], r["net"], r["far"]
        key = port or r["key"]
        pulls = [f for f in far if f["kind"] in ("pull-up", "pull-down") and not f.get("dni")]
        for u in r["dt"]:
            if u.get("kind") == "pinctrl" and u.get("func"):
                parts = u["func"].split("_")
                tok = parts[1] if len(parts) > 2 else (parts[0] if parts else "")
                if re.fullmatch(r"(a|v)?in[pn]?\d*", tok.lower()):
                    R.folded["pinctrl function check skipped: analog input channel"].append(f"{port} {u['func']}")
                    continue
                want = {"dm": ("_N", "DM", "D-"), "dp": ("_P", "DP", "D+")}.get(tok.lower(), (tok.upper(),))
                ok = any(w in (net or "").upper() for w in want)
                R.check("pinctrl function vs net name", "", 1, int(ok))
                if not ok:
                    R.cand("pinctrl-func-vs-net-name", key, f"{u['func']} ({u['af']}) sits on net {mb}:{net}, whose name does not carry '{tok.upper()}'",
                           [f"DT {u['label']} @ {u['loc']}", f"schematic {r['key']} net {mb}:{net}"], "pinctrl function vs net name")
            elif u.get("kind") == "gpio" and u.get("flags"):
                low = "ACTIVE_LOW" in _flags(u)
                named = active_low_name(net)
                leds = [f for f in far if f["kind"] == "part-pin" and re.match(r"(D|LED)\d", f["part"]) and f.get("reaches_rail")]
                if low and not named and leds:
                    R.check("GPIO polarity vs net name", "", 1, 1)
                    R.cand("polarity-resolved", key, f"{u['label']}.{u['prop']} is ACTIVE_LOW on {mb}:{net} (no 'n'), but "
                           f"{leds[0]['board']}:{leds[0]['part']} returns to a rail ({leds[0]['reaches_rail'][0]}), so the pin sinks: consistent",
                           [f"DT @ {u['loc']}", f"schematic {leds[0]['board']}:{leds[0]['pin']} other side {leds[0].get('other_nets')}"],
                           severity="pass")
                    continue
                R.check("GPIO polarity vs net name", "", 1, int(low == named))
                if low != named:
                    fn = sorted({f.get("pin_name") or "?" for f in far if f["kind"] == "part-pin"})
                    R.cand("polarity-vs-net-name", key, f"{u['label']}.{u['prop']} is {_flags(u)} but net {mb}:{net} "
                           f"{'has' if named else 'has no'} an active-low prefix",
                           [f"DT @ {u['loc']}", f"schematic {r['key']} net {mb}:{net}; far-end pins {fn}"], "GPIO polarity vs net name")
            fl = _flags(u)
            for f in pulls:
                up_dt = "PULL_UP" in fl or "bias-pull-up" in fl
                dn_dt = "PULL_DOWN" in fl or "bias-pull-down" in fl
                if not (up_dt or dn_dt):
                    continue
                bad = (up_dt and f["kind"] == "pull-down") or (dn_dt and f["kind"] == "pull-up")
                R.check("DT pull vs board pull", "", 1, int(not bad))
                if bad:
                    R.cand("pull-conflict", key, f"DT {u['label']}.{u['prop']} asks {fl} but the board has a {f['kind']} "
                           f"{f['board']}:{f['part']} {f.get('value')} to {f['rail']}",
                           [f"DT @ {u['loc']}", f"schematic {f['board']}:{f['part']} -> {f['board']}:{f['rail']}"], "DT pull vs board pull")
        for f in pulls:
            if not io_rails:
                break
            ok = J.is_ground(f["rail"]) or f["rail"] in io_rails or f["board"] != mb and f["rail"] in io_rails
            R.check("pull rail vs MCU I/O supply", "", 1, int(ok))
            if not ok:
                R.cand("pull-to-other-rail", key, f"{key} net {mb}:{net} is pulled to {f['board']}:{f['rail']} via "
                       f"{f['board']}:{f['part']} {f.get('value')}; MCU I/O supply is {sorted(io_rails)}; pin I/O {r.get('io') or '?'}",
                       [f"schematic {f['board']}:{f['part']}", r.get("ds_cite") or ""], "pull rail vs MCU I/O supply")
        ic = [f for f in far if f["kind"] == "part-pin"]
        conn = [f for f in far if f["kind"] in ("connector", "board-to-board")]
        tps = [f for f in far if f["kind"] == "testpoint"]
        if r["dt"] and port:
            ok = bool(ic or conn or r["rail"] or pulls)
            R.check("DT pin reaches the board", "", 1, int(ok))
            if not ok:
                R.cand("dt-pin-not-connected", key, f"DT uses {port} but its net {mb}:{net} reaches no part ({len(tps)} test point(s))",
                       [f"DT {u['label']}.{u['prop']} @ {u['loc']}" for u in r["dt"]] + [f"schematic {r['key']} net {mb}:{net}"],
                       "DT pin reaches the board")
        if port and (ic or conn) and not r["rail"]:
            R.check("connected pin has a DT use", "", 1, int(bool(r["dt"])))
            if not r["dt"]:
                if EXPECTED_RE.search(r["name"] or "") or EXPECTED_RE.search(net or ""):
                    R.folded["connected, no DT: debug/boot/oscillator pins (expected)"].append(f"{port} {net}")
                    R.checks["connected pin has a DT use"]["passed"] += 1
                else:
                    far_s = sorted({f"{f['board']}:{f['pin']} {f.get('pin_name') or ''}".strip() for f in ic})[:3] or \
                        sorted({f.get("to") or f"{f['board']}:{f['pin']}" for f in conn})[:3]
                    R.cand("connected-no-dt", key, f"{port} net {mb}:{net} reaches {len(ic)} part pin(s)"
                           f"{' and a connector' if conn else ''} but no DT property references it",
                           [f"schematic {r['key']} {mb}:{net} -> {far_s}"], "connected pin has a DT use", severity="info")
        elif port and not r["dt"] and tps and not ic and not conn:
            R.folded["test-point-only pins"].append(f"{port} {net}")
        elif not port and net and not r["rail"] and (ic or conn) and EXPECTED_RE.search(r["name"] or ""):
            R.folded["connected, no DT: debug/boot/oscillator pins (expected)"].append(f"{r['name']} {net}")
        for f in far:
            if f.get("dni") and f["kind"] in ("pull-up", "pull-down", "series", "part-pin", "cap"):
                R.check("DNI parts on MCU paths", "", 1, 0)
                R.cand("dni-on-path", key, f"{key} net {mb}:{net}: {f['board']}:{f['part']} is not fitted (DNI), so the path through it is open",
                       [f"schematic/BOM {f['board']}:{f['part']} DNI"], "DNI parts on MCU paths", severity="info")
        if r.get("ds_name") and r.get("name"):
            a, b = r["name"].split("-")[0], r["ds_name"].split("-")[0]
            ok = a in r["ds_name"] or b in r["name"]
            R.check("MCU symbol pin name vs datasheet", "netlist symbol, MCU datasheet pin records", 1, int(ok))
            if not ok:
                R.cand("symbol-vs-datasheet", r["key"], f"{r['key']}: symbol says {r['name']}, datasheet says {r['ds_name']}",
                       [r.get("ds_cite") or ""], "MCU symbol pin name vs datasheet")

    # ---- open-drain far ends (needs pin records)
    if have_pin_recs:
        R.check("open-drain far end has a pull", "datasheet pin records, netlist, DT flags")
        for r in rows:
            pulls = [f for f in r["far"] if f["kind"] in ("pull-up", "pull-down") and not f.get("dni")]
            dtpull = any("PULL_UP" in _flags(u) or "bias-pull-up" in _flags(u) for u in r["dt"])
            for f in r["far"]:
                if f["kind"] != "part-pin":
                    continue
                pr = J.part_pin_record(f["board"], f["pin"])
                if not pr or not _od(pr[1]):
                    continue
                ok = bool(pulls or dtpull)
                R.check("open-drain far end has a pull", "", 1, int(ok))
                if not ok:
                    R.cand("open-drain-no-pull", r["port"] or r["key"], f"{r['port']} net {mb}:{r['net']}: {f['board']}:{f['pin']} "
                           f"{f.get('pin_name')} is open-drain and neither the board nor DT pulls the line",
                           [f"datasheet {pr[0]} {_cite(pr[1])}", "schematic: no pull on the net", f"DT flags {[u.get('flags') for u in r['dt']]}"],
                           "open-drain far end has a pull")
    else:
        R.not_run.append({"check": "open-drain far end has a pull",
                          "why": "no `pin` records for any part yet (translate the far-end parts' datasheets)"})

    # ---- DT node reality: binding, driver
    R.check("DT node has a binding", "edt raw cells")
    R.check("DT node has a driver in the build", "edt, ELF device ordinals, compile_commands")
    seen_owner = set()
    for r in rows:
        for u in r["dt"]:
            if u.get("no_binding"):
                R.check("DT node has a binding", "", 1, 0)
                R.cand("dt-no-binding", r["port"], f"{u['node']} has {u['prop']} on {r['port']} with no compatible/binding: no driver can use it",
                       [f"DT @ {u['loc']}"], "DT node has a binding")
                continue
            R.check("DT node has a binding", "", 1, 1)
            own = u.get("owner") or u["node"]
            if own in seen_owner:
                continue
            seen_owner.add(own)
            drv = u.get("driver") or {}
            ok = bool(u.get("instantiated") or (drv.get("compiled") and drv.get("consumer")))
            R.check("DT node has a driver in the build", "", 1, int(ok))
            if not ok:
                if drv.get("compiled"):
                    why = f" (a compiled TU, {drv['files'][0]}, defines its DT_DRV_COMPAT but instantiates no device for it)"
                elif drv.get("files"):
                    why = f" (driver {drv['files'][0]} exists, not built)"
                else:
                    why = " and no driver TU is compiled"
                R.cand("dt-no-driver", r["port"], f"{own} ({u.get('compat')}) claims {r['port']} but no device for it is in the ELF" + why,
                       [f"DT {u['prop']} @ {u['loc']}", "ELF: no __device_dts_ord for the node",
                        f"compile_commands.json: no TU with DT_DRV_COMPAT {re.sub(r'[^a-z0-9]', '_', (u.get('compat') or '').lower())}"],
                       "DT node has a driver in the build")
    for name, p in sorted((J.in_use.get("peripherals") or {}).items()):
        if not p.get("dt_okay") or p.get("node") in seen_owner:
            continue
        ok = bool(p.get("instantiated") or p.get("touched_by_code"))
        R.check("DT node has a driver in the build", "", 1, int(ok))
        if not ok:
            R.cand("dt-no-driver", name, f"{name} ({p['node']}, {p['compatible']}) is okay in DT but not instantiated and not touched by code",
                   [f"in_use.json {name}", "ELF: no __device_dts_ord for the node"], "DT node has a driver in the build")

    # ---- pins shared by DT nodes; EXTI lines
    R.check("one DT owner per pin", "edt")
    for r in rows:
        owners = sorted({u.get("owner") or u["node"] for u in r["dt"]})
        if not r["port"] or not r["dt"]:
            continue
        R.check("one DT owner per pin", "", 1, int(len(owners) <= 1))
        if len(owners) > 1:
            od = [f for f in r["far"] if f["kind"] == "part-pin" and _od((J.part_pin_record(f["board"], f["pin"]) or (None, None))[1])]
            R.cand("pin-shared-in-dt", r["port"], f"{r['port']} is referenced by {len(owners)} DT nodes"
                   + (" (far end open-drain per datasheet: wired-OR)" if od else ""),
                   [f"{u['node']} {u['prop']} {u.get('flags')} @ {u['loc']}" for u in r["dt"]], "one DT owner per pin", severity="info")
    exti = collections.defaultdict(list)
    for r in rows:
        for u in r["dt"]:
            if u.get("irq_armed") and u.get("exti_line") is not None:
                exti[u["exti_line"]].append((r["port"], u["label"], u["prop"], r["net"], u.get("loc")))
    R.check("one armed port per EXTI line", "edt, driver sources (regex), RM EXTICR rule")
    for line, lst in sorted(exti.items()):
        ports = sorted({x[0] for x in lst})
        R.check("one armed port per EXTI line", "", 1, int(len(ports) <= 1))
        if len(ports) > 1:
            R.cand("exti-line-shared", f"EXTI{line}", f"EXTI{line} is wanted by {len(ports)} ports: {ports}",
                   [f"{p} {lab}.{pr} net {mb}:{net} @ {loc}" for p, lab, pr, net, loc in lst] +
                   ["interrupt arming from the driver sources (regex over gpio_pin_interrupt_configure_dt)"],
                   "one armed port per EXTI line", severity="info")

    # ---- cross-board names
    if J.interfaces:
        R.check("net name agrees across a board connector", "two netlists + mating rule")
        for itf in J.interfaces:
            ba, ra = itf["a"].split(":", 1)
            bb, rb = itf["b"].split(":", 1)
            for pa, pb in sorted(itf["map"].items(), key=lambda x: int(x[0]) if x[0].isdigit() else 0):
                na = J.boards[ba].net(f"{ra}-{pa}")
                nb = J.boards[bb].net(f"{rb}-{pb}")
                if not na or not nb or (na.startswith("Net") and nb.startswith("Net")):
                    continue
                R.check("net name agrees across a board connector", "", 1, int(na == nb))
                if na != nb:
                    R.cand("cross-board-net-name", f"{ba}:{ra}-{pa}", f"{ba} net {na} meets {bb} net {nb} at {ba}:{ra}-{pa} / {bb}:{rb}-{pb}",
                           [f"mating rule {itf.get('rule') or 'map'} ({itf['status']}"
                            + (f", {itf['agree']}/{itf['named']} names agree)" if itf.get("named") else ")")],
                           "net name agrees across a board connector")

    # ---- I2C
    i2c = pins.get("i2c", {})
    R.check("I2C parts per family on the bus vs DT nodes", "netlist, compatible->MPN, DT")
    R.check("I2C strap addresses vs DT reg", "netlist, BOM values, strap records, DT reg")
    for bus in i2c.get("buses", []):
        for fam in bus.get("families", []):
            n_dt, n_sch = len(fam["dt"]), len(fam["parts"])
            R.check("I2C parts per family on the bus vs DT nodes", "", 1, int(n_dt == n_sch))
            if n_dt != n_sch:
                R.cand("i2c-bus-membership", bus["bus"], f"{bus['bus']} ({bus.get('sda_net')}): {n_dt} DT node(s) of {fam['compat']} vs {n_sch} matching part(s) on the bus",
                       [f"DT {d['node']} reg {d['reg']:#04x} @ {d.get('loc')}" for d in fam["dt"]] + [f"schematic {p['part']} {p['mpn']}" for p in fam["parts"]],
                       "I2C parts per family on the bus vs DT nodes")
                continue
            sch = sorted(a for p in fam["parts"] for a in (p["addrs"] or [])[:1])
            dts = sorted(d["reg"] for d in fam["dt"])
            if fam["parts"] and all(p["addrs"] for p in fam["parts"]) and all(len(p["addrs"]) == 1 for p in fam["parts"]):
                R.check("I2C strap addresses vs DT reg", "", 1, int(sch == dts))
                if sch != dts:
                    R.cand("i2c-address-strap", bus["bus"], f"{bus['bus']} {fam['compat']}: straps give {[hex(a) for a in sch]}, DT regs are {[hex(a) for a in dts]}",
                           [f"{p['part']}: {p['why']}" for p in fam["parts"]] + [f"DT {d['node']} @ {d.get('loc')}" for d in fam["dt"]],
                           "I2C strap addresses vs DT reg")
        for p in bus.get("unmatched_parts", []):
            R.check("I2C parts per family on the bus vs DT nodes", "", 1, 0)
            R.cand("i2c-bus-membership", bus["bus"], f"{p['part']} {p['mpn']} has its {p['pin_name']} pin on {bus.get('sda_net')} but no DT node on {bus['bus']} matches it",
                   [f"schematic {p['part']}"], "I2C parts per family on the bus vs DT nodes")
    R.check("I2C device identity by address strap", "netlist, strap records, DT reg")
    R.check("I2C device signals land on the DT pins", "netlist (connectors, series R, FET stages), DT GPIOs")
    for d in i2c.get("identity", []):
        ok = d.get("status") == "identified"
        R.check("I2C device identity by address strap", "", 1, int(ok))
        if not ok:
            R.cand("i2c-identity-unresolved", d["node"], f"{d['node']} (reg {d['reg']:#04x}): {d.get('status')}; parts on the bus: "
                   + "; ".join(f"{p} {a} ({w})" for p, a, w in d.get("parts_on_bus", [])[:4]),
                   [f"DT {d['node']} @ {d.get('loc')}"], "I2C device identity by address strap", severity="info")
            continue
        for s in d.get("signals", []):
            if s["status"] == "part pin not found":
                R.folded["I2C signal not checkable: part pin name unknown"].append(f"{d['node']} {s['prop']}")
                continue
            ok = s["status"] == "agree"
            R.check("I2C device signals land on the DT pins", "", 1, int(ok))
            if not ok:
                R.cand("device-signal-mismatch", d["node"], f"{d['node']} {s['prop']}: DT pin {s['dt_pin']}, but {s['part_pin']} reaches {s['mcu_ports']}",
                       [f"part by address: {d.get('addr_evidence')}", f"DT @ {s.get('dt_loc')}", f"net {s['part_net']}"],
                       "I2C device signals land on the DT pins")
    R.check("DT resistor values vs BOM", "DT, netlist + BOM values")
    for v in pins.get("values", []):
        ok = v["status"] == "agree"
        R.check("DT resistor values vs BOM", "", 1, int(ok))
        if not ok:
            R.cand("dt-value-vs-bom", v["node"], f"{v['node']} {v['prop']} = {v['dt_ohms']:g} ohm; resistors near {v['part']}: {v['board'] or 'none found'}",
                   [f"DT @ {v.get('loc')}", "values from the schematic BOM"], "DT resistor values vs BOM")
    R.check("DT comment net names vs netlist", "fw DTS sources, netlist")
    for c in pins.get("comments", []):
        ok = c["status"] == "agree"
        R.check("DT comment net names vs netlist", "", 1, int(ok))
        if not ok:
            R.cand("dt-comment-vs-net", c["pin"], f"{c['loc']} names {c['comment']} but {c['pin']} is on net {mb}:{c['net']}",
                   [c["loc"], f"schematic {c['pin']} net {mb}:{c['net']}"], "DT comment net names vs netlist")

    # ---- record-fed hooks
    used_docs = sorted({d for ds in J.part_docs.values() for d in ds})
    R.check("driver claims vs datasheet", "conflict records (sources include a driver)")
    for d in used_docs:
        for rec in J.docs.records(d, "conflict"):
            srcs = rec.get("sources") or []
            if not any(is_driver_source(s) for s in srcs):
                continue
            R.check("driver claims vs datasheet", "", 1, 0)
            R.cand("driver-vs-datasheet", rec.get("id") or d, rec.get("text") or "driver and datasheet disagree",
                   [f"{s}" for s in srcs] + [_cite(rec, d)], "driver claims vs datasheet")
    # ---- register constants in the driver sources vs the datasheet register map
    from embarch_atlas import codecheck
    cc = codecheck.run_join(J, stamp, pins)
    for c in cc["checks"]:
        R.check(c["check"], c["sources"], c["ran"], c["passed"])
    for f in cc["findings"]:
        R.cand(f["class"], f["key"], f["summary"], f["evidence"], f["check"], f["severity"], f["settle"])
    R.not_run += cc["not_run"]
    sl = J.atlas / "second_look.jsonl"
    if sl.is_file():
        R.check("second look agrees with the first pass", "second_look.jsonl")
        for v in read_jsonl(sl):
            ok = v.get("verdict") == "confirmed"
            if v.get("verdict") == "unresolved":
                R.folded["second look unresolved (record stays llm-unverified)"].append(str(v.get("id")))
                continue
            R.check("second look agrees with the first pass", "", 1, int(ok))
            if v.get("verdict") == "conflict":
                R.cand("document-reading-conflict", str(v.get("id")), f"second look read {v.get('value')} {v.get('unit') or ''}"
                       f"{' (' + v['cond'] + ')' if v.get('cond') else ''} where the first pass has {v.get('first')}".strip(),
                       [f"{v.get('doc') or ''} p{v.get('p')}", f"quote: {v.get('q')}"], "second look agrees with the first pass")
    else:
        R.not_run.append({"check": "second look agrees with the first pass",
                          "why": "no second_look.jsonl yet (`second-look queue`, then `second-look record`)"})
    for name, why in NOT_RUN:
        R.not_run.append({"check": name, "why": why})
    if not mcu:
        R.not_run.insert(0, {"check": "every pin check", "why": "no MCU found on the schematic boards (set [mcu] part or --mcu-ref)"})

    return finish(R, J, stamp)


EXPECTED_RE = re.compile(r"(?<![A-Za-z0-9])(SWD\w*|SWCLK|SWO|NRST|N?RESET|BOOT0|OSC\d*|XTAL\w*|JTAG\w*|TCK|TMS|TDI|TDO)(?![A-Za-z])", re.I)


def is_driver_source(s) -> bool:
    """A `conflict` record's source that is firmware driver code: `"driver"`, `"driver: <file>:<line>"`,
    a `.c` file, or an object whose kind/doc says driver. A vendor header or the SVD is not a driver."""
    if isinstance(s, dict):
        if str(s.get("kind") or s.get("source") or "").lower() == "driver":
            return True
        return str(s.get("doc") or "").lower().startswith("driver") or str(s.get("file") or "").endswith(".c")
    s = str(s).strip().lower()
    return s == "driver" or s.startswith("driver:") or s.startswith("driver ") or bool(re.search(r"\.c(:\d+)?$", s))


def _cite(rec: dict, doc: str | None = None) -> str:
    p = rec.get("p")
    pp = "p" + "-".join(str(x) for x in p) if isinstance(p, list) and p else (f"p{p}" if p else "")
    return f"{rec.get('doc') or doc or ''} {pp}".strip()


def finish(R: Report, J, stamp: dict) -> dict:
    rank = {c: i for i, c in enumerate(ORDER)}
    R.candidates.sort(key=lambda c: (rank.get(c["class"], len(ORDER)), c["key"] or "", c["summary"]))
    checks = sorted((c for c in R.checks.values() if c["ran"]), key=lambda c: (-(c["ran"] - c["passed"] == 0), c["check"]))
    idle = sorted(c["check"] for c in R.checks.values() if not c["ran"])
    by = collections.Counter(c["class"] for c in R.candidates if c["severity"] != "pass")
    n_ran = sum(c["ran"] for c in checks)
    n_pass = sum(c["passed"] for c in checks)
    folded = {k: sorted(set(v)) for k, v in sorted(R.folded.items())}
    summary = (f"{len(checks)} checks, {n_pass}/{n_ran} items passed; "
               f"{sum(by.values())} candidates in {len(by)} classes; {sum(len(v) for v in folded.values())} expected items folded")
    rep = {"atlas": stamp["id"], "summary": summary, "checks": checks, "checks_with_nothing_to_check": idle,
           "not_run": R.not_run, "folded": folded, "candidates": R.candidates, "by_class": dict(by)}
    write_json(J.atlas / "mismatches.json", rep)
    write_text(J.atlas / "checks.md", render(rep, stamp))
    return rep


def render(rep: dict, stamp: dict) -> str:
    L = [f"# Checks: {stamp['id']}", "",
         f"Target `{stamp['target']}`, board `{stamp['board']}`, commit `{stamp['commit']}`. "
         "Every candidate below is a question for the engineer, not a hardware fact; each cites its sources.", "",
         f"**{rep['summary']}.**", "", "## Checks run", "",
         "| check | ran | passed | candidates | sources |", "|---|---|---|---|---|"]
    for c in rep["checks"]:
        L.append(f"| {c['check']} | {c['ran']} | {c['passed']} | {c['candidates']} | {c['sources']} |")
    if rep["checks_with_nothing_to_check"]:
        L += ["", "Ran with nothing to check: " + ", ".join(rep["checks_with_nothing_to_check"]) + "."]
    if rep["folded"]:
        L += ["", "## Folded into counts (expected)", ""]
        for k, v in rep["folded"].items():
            ex = ", ".join(v[:8]) + (", ..." if len(v) > 8 else "")
            L.append(f"- {k}: {len(v)} ({ex})")
    if rep["not_run"]:
        L += ["", "## Not run", ""]
        for n in rep["not_run"]:
            L.append(f"- {n['check']}: {n['why']}")
    L += ["", "## Candidates, ranked by class", ""]
    by = collections.defaultdict(list)
    for c in rep["candidates"]:
        by[c["class"]].append(c)
    rank = {c: i for i, c in enumerate(ORDER)}
    for cls in sorted(by, key=lambda x: rank.get(x, len(ORDER))):
        items = by[cls]
        L.append(f"### {cls} ({len(items)})")
        L.append("")
        if items[0].get("settle"):
            L.append(f"Settled by: {items[0]['settle']}.")
            L.append("")
        for c in items:
            ev = " | ".join(c["evidence"][:4])
            L.append(f"- **{c['key']}**: {c['summary']}" + (f"  \n  evidence: {ev}" if ev else ""))
        L.append("")
    if not rep["candidates"]:
        L.append("None.")
    return "\n".join(L).rstrip() + "\n"

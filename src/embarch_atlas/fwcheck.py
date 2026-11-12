"""Firmware configuration against a standard's requirements: the `fw_check` of a `requirement` record, or of
a `check` record whose `refs` name requirements, evaluated against one build (DESIGN.md "Firmware
configuration against a standard").

    "fw_check": {
      "inputs": {"<name>": {"dt": "<path | label | alias | compatible>", "prop": "<property>", "decode": "<helper>"}
                         | {"kconfig": "<SYMBOL>"} | {"declared": "<key>"}},
      "applies_when": "<expression>",     optional: false skips the rule (folded as expected, never a failure)
      "expr": "<expression>"              true when the configuration complies
    }

Both expressions run in `check.safe_eval`, the doc check's invariant evaluator, widened with the decode helpers
(`HELPERS`), the fields they return, slices and dict literals. A name `CONFIG_<SYM>` in either one reads that
Kconfig symbol without an input: `y`, `n`, `m`, an int, or a string, with absent and unset read as `n` as
`config` reads them. A `dt` selector that matches several okay nodes runs the rule once per node (a product
with two ports runs a per-port rule twice). `decode` turns the property's value (a word, or a list of words)
into the helper's objects before the expression sees it.

`declared` values are the engineer's: `[standards."<doc_id>"]` in atlas.toml holds the values a standard's
rules depend on that the firmware does not state (a port's maximum power), and its `ports` table says which
devicetree node is which port, with per-port values that override the standard's own. A table is found by the
record's doc_id, else by its slug (the doc_id before `@`), so a new revision of the standard keeps them. The
join validates them: a table naming no document, a port whose node is not one okay node, a value that is not a
number, string or boolean; a declared key no rule reads is listed, which is how a misspelt key shows.

A failed rule is a mismatch candidate of class `config-vs-standard` citing the requirement's page and the
file:line that set each value (`dt_node` and `config` resolve the same lines). `join` runs this on the pinned
build; `mismatch_report` with `build` runs it in memory on a build dir it names, and writes nothing.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from . import check as CK

CLS = "config-vs-standard"
CHECK_PREFIX = "config vs standard: "
SECTION = "Firmware configuration vs standards"
FOLD_GUARD = "config vs standard: rule skipped, its applies_when is false"
SETTLE = ("the requirement's cited page against the value at the cited line: fix the devicetree or Kconfig value, "
          "correct the engineer-declared value in atlas.toml, or the record's fw_check if it encodes the rule wrongly")
NEEDS_BUILD = ("firmware configuration vs standard requirements (config-vs-standard)",
               "no devicetree or Kconfig: they come from a build")
EXTRA_NODES = (ast.Slice, ast.Dict)
SHOW_MAX = 300          # characters of one shown value


# ---------------------------------------------------------------- USB PD: a PDO or APDO word decoded

FIXED_MV_UNIT = 50      # a fixed, variable or battery PDO's voltage fields count 50 mV
CURRENT_MA_UNIT = 10    # a fixed or variable PDO's current, an SPR AVS APDO's currents: 10 mA
BATTERY_MW_UNIT = 250   # a battery PDO's power: 250 mW
APDO_MV_UNIT = 100      # a PPS or AVS APDO's voltages: 100 mV
PPS_MA_UNIT = 50        # a PPS APDO's current: 50 mA
WORD_MASK = 0xFFFFFFFF

PDO_FIELDS = ("word", "type", "mv", "min_mv", "max_mv", "ma", "mw", "pdp_w", "ma_15v", "ma_20v", "peak", "drp",
              "usb_suspend", "higher_cap", "unconstrained", "usb_comm", "drd", "unchunked", "epr", "frs", "limited")


class PDO:
    """One decoded USB PD power data object. A field its type does not carry is None."""
    __slots__ = PDO_FIELDS

    def __init__(self, **kw: Any):
        for f in PDO_FIELDS:
            setattr(self, f, kw.get(f))

    def __str__(self) -> str:
        t = self.type
        if t == "fixed":
            body = f" {self.mv} mV {self.ma} mA"
        elif t in ("variable", "pps"):
            body = f" {self.min_mv}-{self.max_mv} mV {self.ma} mA"
        elif t == "battery":
            body = f" {self.min_mv}-{self.max_mv} mV {self.mw} mW"
        elif t == "epr-avs":
            body = f" {self.min_mv}-{self.max_mv} mV {self.pdp_w} W"
        elif t == "spr-avs":
            body = f" {self.ma_15v} mA at 15 V, {self.ma_20v} mA at 20 V"
        else:
            body = f" {self.word:#010x}"
        return f"{t}{body}"

    __repr__ = __str__


def _bits(w: int, hi: int, lo: int) -> int:
    return (w >> lo) & ((1 << (hi - lo + 1)) - 1)


def _flag(w: int, bit: int) -> bool:
    return bool(_bits(w, bit, bit))


def _pdo(word: Any, sink: bool) -> PDO:
    """A 32-bit PDO or APDO word by the public USB PD layout: B31..30 the supply type (fixed, battery, variable,
    augmented); an APDO's B29..28 its kind (SPR PPS, EPR AVS, SPR AVS). Voltages in mV, currents in mA, a
    battery PDO's power in mW, an EPR AVS APDO's PDP in W; `peak` and `frs` are the raw 2-bit codes."""
    w = int(word) & WORD_MASK
    kind = _bits(w, 31, 30)
    if kind == 0:
        common = dict(word=w, type="fixed", mv=_bits(w, 19, 10) * FIXED_MV_UNIT, ma=_bits(w, 9, 0) * CURRENT_MA_UNIT,
                      drp=_flag(w, 29), unconstrained=_flag(w, 27), usb_comm=_flag(w, 26), drd=_flag(w, 25))
        if sink:
            return PDO(**common, higher_cap=_flag(w, 28), frs=_bits(w, 24, 23))
        return PDO(**common, usb_suspend=_flag(w, 28), unchunked=_flag(w, 24), epr=_flag(w, 23), peak=_bits(w, 21, 20))
    if kind in (1, 2):
        rng = dict(word=w, max_mv=_bits(w, 29, 20) * FIXED_MV_UNIT, min_mv=_bits(w, 19, 10) * FIXED_MV_UNIT)
        if kind == 1:
            return PDO(**rng, type="battery", mw=_bits(w, 9, 0) * BATTERY_MW_UNIT)
        return PDO(**rng, type="variable", ma=_bits(w, 9, 0) * CURRENT_MA_UNIT)
    apdo = _bits(w, 29, 28)
    if apdo == 0:
        return PDO(word=w, type="pps", max_mv=_bits(w, 24, 17) * APDO_MV_UNIT, min_mv=_bits(w, 15, 8) * APDO_MV_UNIT,
                   ma=_bits(w, 6, 0) * PPS_MA_UNIT, limited=None if sink else _flag(w, 27))
    if apdo == 1:
        return PDO(word=w, type="epr-avs", peak=None if sink else _bits(w, 27, 26),
                   max_mv=_bits(w, 25, 17) * APDO_MV_UNIT, min_mv=_bits(w, 15, 8) * APDO_MV_UNIT, pdp_w=_bits(w, 7, 0))
    if apdo == 2:
        return PDO(word=w, type="spr-avs", peak=None if sink else _bits(w, 27, 26),
                   ma_15v=_bits(w, 19, 10) * CURRENT_MA_UNIT, ma_20v=_bits(w, 9, 0) * CURRENT_MA_UNIT)
    return PDO(word=w, type="reserved")


def pd_pdo(word: Any) -> PDO:
    """A Source_Capabilities PDO or APDO word (a `usb-c-connector` node's `source-pdos` entry)."""
    return _pdo(word, sink=False)


def pd_sink_pdo(word: Any) -> PDO:
    """A Sink_Capabilities PDO or APDO word (`sink-pdos`)."""
    return _pdo(word, sink=True)


# Per standard, prefixed by it: what an expression may call and an input may `decode` with.
HELPERS = {"pd_pdo": pd_pdo, "pd_sink_pdo": pd_sink_pdo}
HELPER_FIELDS = PDO_FIELDS


def safe(expr: str, env: dict) -> Any:
    return CK.safe_eval(expr, env, funcs=HELPERS, attrs=HELPER_FIELDS, nodes=EXTRA_NODES)


# ---------------------------------------------------------------- one build, as the inputs read it

class Unresolved(Exception):
    """An input with no value to give: no node, no Kconfig, nothing declared. The rule is not run."""


class BuildView:
    """One build's devicetree and Kconfig: the pinned build at join, or a build dir `mismatch_report` names.
    `dt` None or `syms` None carry the reason in `dt_why` / `kconfig_why`."""

    def __init__(self, label: str, dt: dict | None, syms=None, dt_why: str | None = None,
                 kconfig_why: str | None = None):
        self.label, self.dt, self.syms = label, dt, syms
        self.dt_why = dt_why or ("no devicetree in this build" if dt is None else None)
        self.kconfig_why = kconfig_why or ("no kconfig.json and no .config in this build" if syms is None else None)

    @classmethod
    def of(cls, src) -> "BuildView":
        """From a `buildq.Source`."""
        from .buildq import _Syms
        if src.dt is None:
            src.load_dt()
        syms = _Syms(src)
        have = bool(syms.syms) or bool(syms.values)
        return cls(src.label, src.dt, syms if have else None, src.dt_why, None if have else src.kconfig_why)

    def nodes(self) -> dict:
        return (self.dt or {}).get("nodes") or {}

    def select(self, sel: str) -> list[str]:
        """The okay nodes a selector names, exactly: a path, a label (`&` optional), an alias or chosen name,
        a compatible. No substring guesses: a rule must not run on a node it did not name."""
        nodes, dt = self.nodes(), self.dt or {}
        s = sel.strip()
        if s in nodes:
            hits = [s]
        else:
            s = s[1:] if s.startswith("&") else s
            hits = [p for p, n in nodes.items() if s in (n.get("labels") or [])]
            alias = (dt.get("aliases") or {}).get(s) or (dt.get("chosen") or {}).get(s)
            if not hits and alias in nodes:
                hits = [alias]
            if not hits:
                hits = [p for p, n in nodes.items() if s in (n.get("compats") or [])]
        return sorted(p for p in hits if (nodes.get(p) or {}).get("okay"))

    def node_label(self, path: str | None) -> str:
        if not path:
            return ""
        labels = (self.nodes().get(path) or {}).get("labels") or []
        return labels[0] if labels else path

    def prop(self, path: str, name: str) -> tuple[Any, str] | None:
        """(value, the file:line that set it, or "binding default"), or None when the node has no such property."""
        pv = ((self.nodes().get(path) or {}).get("props") or {}).get(name)
        if pv is None:
            return None
        if isinstance(pv, dict):
            return pv.get("val"), pv.get("loc") or "binding default"
        return pv, "binding default"

    def kconfig(self, name: str) -> tuple[Any, str]:
        """(value, where it was set) for a symbol named with or without `CONFIG_`."""
        if self.syms is None:
            raise Unresolved(f"CONFIG_{name}: {self.kconfig_why}")
        n = self.syms.resolve(name) or name
        side = self.syms.side(n)
        if side is None:
            return "n", "not a symbol of this build"
        value, where = side
        return typed(value, (self.syms.syms.get(n) or {}).get("t")), where


def typed(v: Any, t: str | None) -> Any:
    """A Kconfig value as an expression compares it: int and hex symbols as integers, the rest as strings; no
    value is `n` for a bool, a tristate or a symbol of unknown type. Without kconfig.json (no types), a value
    that reads as an integer is one."""
    if v is None:
        return "n" if t in (None, "bool", "tristate") else None
    s = str(v)
    try:
        if t == "int" or (t is None and re.fullmatch(r"-?\d+", s)):
            return int(s)
        if t == "hex" or (t is None and re.fullmatch(r"0[xX][0-9a-fA-F]+", s)):
            return int(s, 16)
    except ValueError:
        return s
    return s


# ---------------------------------------------------------------- engineer-declared values (atlas.toml)

SCALAR = (bool, int, float, str)


class Declared:
    """`[standards."<doc_id or slug>"]` of atlas.toml, with the shape errors found reading it."""

    def __init__(self, tables: dict[str, dict], errors: list[str]):
        self.tables, self.errors = tables, errors

    @classmethod
    def load(cls, ad: Path) -> "Declared":
        from . import project
        try:
            cfg = project.load_config(ad)
        except FileNotFoundError:
            return cls({}, [])
        except (OSError, ValueError) as e:
            return cls({}, [f"atlas.toml unreadable: {e}"])
        return cls.parse(cfg.get("standards"))

    @classmethod
    def parse(cls, std: Any) -> "Declared":
        if std is None:
            return cls({}, [])
        if not isinstance(std, dict):
            return cls({}, ["[standards] is not a table of `[standards.\"<doc_id>\"]` tables"])
        tables, errors = {}, []
        for key, tab in std.items():
            where = f'[standards."{key}"]'
            if not isinstance(tab, dict):
                errors.append(f"{where} is not a table")
                continue
            good: dict[str, Any] = {}
            for k, v in tab.items():
                if k == "ports":
                    ports = cls._ports(where, v, errors)
                    if ports:
                        good["ports"] = ports
                elif isinstance(v, SCALAR):
                    good[k] = v
                else:
                    errors.append(f"{where} {k}: a declared value is a number, a string or a boolean")
            tables[key] = good
        return cls(tables, errors)

    @staticmethod
    def _ports(where: str, v: Any, errors: list[str]) -> dict[str, dict]:
        if not isinstance(v, dict):
            errors.append(f"{where} ports: a table of port name -> {{node = \"<DT node>\", <values>}}")
            return {}
        out = {}
        for name, port in v.items():
            pw = f"{where}.ports.{name}"
            if not isinstance(port, dict) or not isinstance(port.get("node"), str) or not port["node"].strip():
                errors.append(f"{pw}: needs node = \"<DT node path or label>\"")
                continue
            bad = [k for k, x in port.items() if k != "node" and not isinstance(x, SCALAR)]
            for k in bad:
                errors.append(f"{pw} {k}: a declared value is a number, a string or a boolean")
            out[name] = {k: x for k, x in port.items() if k not in bad}
        return out

    def key_for(self, doc: str) -> str | None:
        if doc in self.tables:
            return doc
        slug = doc.split("@", 1)[0]
        return slug if slug in self.tables else None

    def port_of(self, doc: str, path: str | None, view: BuildView) -> str | None:
        """The declared port whose node is this DT node."""
        key = self.key_for(doc)
        if not key or not path:
            return None
        for name, port in (self.tables[key].get("ports") or {}).items():
            if view.select(port["node"]) == [path]:
                return name
        return None

    def value(self, doc: str, k: str, port: str | None) -> tuple[Any, str]:
        """(value, where it is declared): the port's own value first, then the standard's."""
        key = self.key_for(doc)
        if key is None:
            raise Unresolved(f"declared `{k}`: atlas.toml has no [standards.\"{doc}\"] (or [standards.\"{doc.split('@', 1)[0]}\"])")
        tab = self.tables[key]
        if port and k in (tab.get("ports") or {}).get(port, {}):
            return tab["ports"][port][k], f'atlas.toml [standards."{key}"].ports.{port}.{k}'
        if k in tab and k != "ports":
            return tab[k], f'atlas.toml [standards."{key}"].{k}'
        hint = f" or its port {port}'s table" if port else ""
        raise Unresolved(f"declared `{k}`: not in atlas.toml [standards.\"{key}\"]{hint}")

    def validate(self, doc_ids: list[str], view: BuildView | None, reads: dict[str, set[str]]) -> tuple[list[str], list[str]]:
        """(problems, declared keys no rule reads). `reads`: doc_id -> the declared keys its rules read."""
        problems = list(self.errors)
        slugs = {d.split("@", 1)[0] for d in doc_ids}
        unused = []
        for key, tab in sorted(self.tables.items()):
            where = f'[standards."{key}"]'
            docs = [d for d in doc_ids if d == key or d.split("@", 1)[0] == key]
            if key not in doc_ids and key not in slugs:
                problems.append(f"{where} names no document in docs/ (a doc_id, or its slug before `@`)")
            if view is not None and view.dt is not None:
                for name, port in sorted((tab.get("ports") or {}).items()):
                    hits = view.select(port["node"])
                    if len(hits) != 1:
                        problems.append(f"{where}.ports.{name} node {port['node']!r} is "
                                        + ("no okay devicetree node" if not hits else f"{len(hits)} nodes: {', '.join(hits)}"))
            read = set().union(*(reads.get(d, set()) for d in docs)) if docs else set()
            keys = {k for k in tab if k != "ports"} | {k for p in (tab.get("ports") or {}).values() for k in p if k != "node"}
            unused += [f"{where} {k}" for k in sorted(keys - read)]
        return problems, unused


# ---------------------------------------------------------------- the rules

def _cite(r: dict, doc: str) -> str:
    p = r.get("p")
    pp = "p" + "-".join(str(x) for x in p) if isinstance(p, list) and p else (f"p{p}" if p else "")
    return f"{r.get('doc') or doc} {pp}".strip()


def rules(docs) -> list[dict]:
    """Every `requirement` or `check` record that carries a fw_check, with the requirements it encodes."""
    req_by_id = {r.get("id"): (d, r) for d, r in docs.all_records("requirement")}
    out = []
    for kind in ("requirement", "check"):
        for d, r in docs.all_records(kind):
            if "fw_check" not in r:
                continue
            doc = r.get("doc") or d
            if kind == "requirement":
                reqs = [(doc, r)]
                name = str(r.get("rid") or r.get("id"))
            else:
                refs = r.get("refs") if isinstance(r.get("refs"), list) else []
                reqs = [req_by_id[x] for x in refs if isinstance(x, str) and x in req_by_id]
                name = str(r.get("id"))
            cites = [f"requirement {q.get('rid') or q.get('id')} ({q.get('modality') or 'no modality'}"
                     + (f", {q['role']}" if q.get("role") else "") + f"): {_cite(q, qd)}" for qd, q in reqs]
            if kind == "check":
                cites.append(f"check {r.get('id')}: {_cite(r, doc)}")
            text = "; ".join(str(q.get("text")) for _, q in reqs if q.get("text")) or str(r.get("why") or "")
            out.append({"rec": r, "kind": kind, "doc": doc, "name": name, "reqs": reqs, "cites": cites, "text": text,
                        "missing_refs": [x for x in (r.get("refs") or []) if kind == "check" and x not in req_by_id]})
    return sorted(out, key=lambda x: (x["doc"], x["name"]))


NAME_RE = re.compile(r"^[A-Za-z_]\w*$")


def shape_error(fc: Any) -> str | None:
    """What makes a fw_check unrunnable as written, or None (the schema says the same at doc check)."""
    if not isinstance(fc, dict):
        return "fw_check is not an object"
    if not isinstance(fc.get("expr"), str) or not fc["expr"].strip():
        return "fw_check has no expr"
    if fc.get("applies_when") is not None and not isinstance(fc["applies_when"], str):
        return "fw_check applies_when is not an expression string"
    ins = fc.get("inputs", {})
    if not isinstance(ins, dict):
        return "fw_check inputs is not an object of name -> input"
    for name, spec in ins.items():
        if not NAME_RE.match(name) or name.startswith(("__", "CONFIG_")) or name in HELPERS:
            return f"input name {name!r}: an identifier, not CONFIG_* (read directly) and not a helper's name"
        if not isinstance(spec, dict):
            return f"input {name}: not an object"
        kinds = [k for k in ("dt", "kconfig", "declared") if k in spec]
        if len(kinds) != 1:
            return f"input {name}: give exactly one of dt (with prop), kconfig, declared"
        if "dt" in spec and not (isinstance(spec["dt"], str) and isinstance(spec.get("prop"), str)):
            return f"input {name}: dt and prop are strings"
        if spec.get("decode") is not None and spec["decode"] not in HELPERS:
            return f"input {name}: decode {spec['decode']!r} is not one of {', '.join(sorted(HELPERS))}"
    return None


def names_in(expr: str | None) -> set[str]:
    if not expr:
        return set()
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return set()
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}


def _show(v: Any) -> str:
    if isinstance(v, (list, tuple)):
        s = "[" + ", ".join(_show(x) for x in v) + "]"
    elif isinstance(v, str):
        s = repr(v)
    else:
        s = str(v)
    return s if len(s) <= SHOW_MAX else s[:SHOW_MAX - 3] + "..."


def _decode(v: Any, helper: str) -> Any:
    fn = HELPERS[helper]
    return [fn(x) for x in v] if isinstance(v, list) else fn(v)


def instances(fc: dict, view: BuildView) -> tuple[list[str | None], str | None]:
    """The DT nodes a rule runs on, once each: the nodes a `dt` selector matching several picks, else the one
    node every `dt` input reads, else one run that belongs to no node."""
    sets = {name: tuple(view.select(spec["dt"])) for name, spec in (fc.get("inputs") or {}).items() if "dt" in spec}
    multi = sorted({v for v in sets.values() if len(v) > 1})
    if len(multi) > 1:
        return [], "its dt inputs select different sets of nodes: " + "; ".join(", ".join(m) for m in multi)
    if multi:
        return list(multi[0]), None
    single = {v[0] for v in sets.values() if len(v) == 1}
    return ([single.pop()] if len(single) == 1 else [None]), None


def run_rule(rule: dict, view: BuildView, declared: Declared) -> list[dict]:
    """One result per instance: status pass, fail, skipped or not-run, the values shown with where each was set."""
    fc = rule["rec"].get("fw_check")
    err = shape_error(fc)
    if err:
        return [{"status": "not-run", "node": None, "why": err}]
    if view.dt is None and any("dt" in s for s in (fc.get("inputs") or {}).values()):
        return [{"status": "not-run", "node": None, "why": f"devicetree: {view.dt_why}"}]
    nodes, why = instances(fc, view)
    if why:
        return [{"status": "not-run", "node": None, "why": why}]
    out = []
    for node in nodes:
        out.append(_run_one(rule, fc, view, declared, node))
    return out


def _run_one(rule: dict, fc: dict, view: BuildView, declared: Declared, node: str | None) -> dict:
    port = declared.port_of(rule["doc"], node, view)
    env: dict[str, Any] = {}
    shown: list[str] = []          # "<what> = <value> @ <where>" per input, as evidence
    missing: dict[str, str] = {}   # name -> why it has no value (the rule is not run)
    absent: dict[str, str] = {}    # name -> the node has no such property (None, usable in applies_when)
    for name, spec in (fc.get("inputs") or {}).items():
        try:
            if "dt" in spec:
                hits = view.select(spec["dt"])
                if not hits:
                    raise Unresolved(f"no okay devicetree node is {spec['dt']!r}")
                path = node if node in hits else (hits[0] if len(hits) == 1 else None)
                if path is None:
                    raise Unresolved(f"{spec['dt']!r} is {len(hits)} nodes")
                got = view.prop(path, spec["prop"])
                if got is None:
                    env[name] = None
                    absent[name] = f"{view.node_label(path)} has no {spec['prop']}"
                    shown.append(f"{name}: {view.node_label(path)}.{spec['prop']} absent")
                    continue
                v, where = got
                if spec.get("decode"):
                    v = _decode(v, spec["decode"])
                shown.append(f"{name} = {view.node_label(path)}.{spec['prop']} = {_show(v)} @ {where}")
            elif "kconfig" in spec:
                sym = str(spec["kconfig"])
                v, where = view.kconfig(sym[7:] if sym.upper().startswith("CONFIG_") else sym)
                shown.append(f"{name} = CONFIG_{sym.removeprefix('CONFIG_')} = {_show(v)} @ {where}")
            else:
                v, where = declared.value(rule["doc"], str(spec["declared"]), port)
                shown.append(f"{name} = {_show(v)} engineer-declared, {where}")
            env[name] = v
        except Unresolved as e:
            env[name], missing[name] = None, str(e)
        except Exception as e:  # noqa: BLE001 - a decode of a value the helper cannot read is a finding about the rule
            env[name], missing[name] = None, f"input {name}: {type(e).__name__}: {e}"
    guard, expr = fc.get("applies_when"), fc["expr"]
    for cn in sorted(n for n in names_in(guard) | names_in(expr) if n.startswith("CONFIG_")):
        try:
            v, where = view.kconfig(cn[7:])
            env[cn] = v
            shown.append(f"{cn} = {_show(v)} @ {where}")
        except Unresolved as e:
            env[cn], missing[cn] = None, str(e)
    res = {"node": node, "label": view.node_label(node), "port": port, "values": shown}
    if guard:
        need = [missing[n] for n in sorted(names_in(guard)) if n in missing]
        if need:
            return {**res, "status": "not-run", "why": "applies_when cannot be decided: " + "; ".join(need)}
        try:
            applies = bool(safe(guard, env))
        except Exception as e:  # noqa: BLE001 - any failure of an LLM expression is a finding
            return {**res, "status": "not-run", "why": f"applies_when: {type(e).__name__}: {e}"}
        if not applies:
            return {**res, "status": "skipped", "why": f"applies_when `{guard}` is false"}
    need = [missing[n] for n in sorted(names_in(expr)) if n in missing]
    need += [absent[n] for n in sorted(names_in(expr)) if n in absent]
    if need:
        return {**res, "status": "not-run", "why": "; ".join(need)}
    try:
        ok = bool(safe(expr, env))
    except Exception as e:  # noqa: BLE001
        return {**res, "status": "not-run", "why": f"expr: {type(e).__name__}: {e}"}
    return {**res, "status": "pass" if ok else "fail"}


def _reads(rs: list[dict]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for r in rs:
        fc = r["rec"].get("fw_check")
        ins = fc.get("inputs") if isinstance(fc, dict) else None
        for spec in (ins.values() if isinstance(ins, dict) else []):
            if isinstance(spec, dict) and "declared" in spec:
                out.setdefault(r["doc"], set()).add(str(spec["declared"]))
    return out


def analyze(docs, view: BuildView, declared: Declared) -> dict:
    """Every fw_check against one build: the report parts `mismatch.run` merges (checks, findings, not_run,
    folded, section) and `rules`, each rule's results."""
    rs = rules(docs)
    out: dict[str, Any] = {"checks": [], "findings": [], "not_run": [], "folded": {}, "section": None, "rules": [],
                           "summary": ""}
    if not rs and not declared.tables and not declared.errors:
        return out
    problems, unused = declared.validate(sorted(docs.docs), view, _reads(rs))
    for p in problems:
        out["not_run"].append({"check": "atlas.toml [standards] (engineer-declared values)", "why": p})
    lines: list[str] = []
    n_ran = n_pass = n_skip = n_not = 0
    for rule in rs:
        res = run_rule(rule, view, declared)
        out["rules"].append({"name": rule["name"], "doc": rule["doc"], "results": res})
        name = CHECK_PREFIX + rule["name"]
        ran = [x for x in res if x["status"] in ("pass", "fail")]
        passed = [x for x in ran if x["status"] == "pass"]
        n_ran += len(ran)
        n_pass += len(passed)
        if ran:
            out["checks"].append({"check": name, "sources": "; ".join(rule["cites"]) or rule["doc"], "ran": len(ran),
                                  "passed": len(passed), "candidates": len(ran) - len(passed)})
        bits = []
        for x in res:
            on = f" on {x['label']}" if x.get("label") else ""
            if x["status"] == "fail":
                out["findings"].append(finding(rule, x))
                bits.append(f"fails{on}")
            elif x["status"] == "pass":
                bits.append(f"passes{on}")
            elif x["status"] == "skipped":
                n_skip += 1
                kv = "; ".join(v for v in x["values"] if v.startswith("CONFIG_")) or "; ".join(x["values"][:2])
                out["folded"].setdefault(FOLD_GUARD, []).append(f"{rule['name']}{on} ({kv})" if kv else f"{rule['name']}{on}")
                bits.append(f"skipped{on}: {x['why']}" + (f" ({kv})" if kv else ""))
            else:
                n_not += 1
                out["not_run"].append({"check": name + on, "why": x["why"]})
                bits.append(f"not run{on}: {x['why']}")
        if rule["missing_refs"]:
            bits.append(f"refs naming no requirement: {', '.join(map(str, rule['missing_refs']))}")
        prov = rule["rec"].get("prov")
        lines.append(f"- {rule['name']} ({'; '.join(rule['cites']) or rule['doc']}"
                     + (f", {prov}" if prov else "") + "): " + "; ".join(bits))
    out["summary"] = (f"{len(rs)} rule(s) with a fw_check against {view.label}: {n_pass}/{n_ran} run(s) passed, "
                      f"{n_skip} skipped by applies_when, {n_not} not run")
    head = [out["summary"] + ". Every value cites the line that set it; engineer-declared values come from "
            "atlas.toml [standards].", ""]
    tail = []
    if problems:
        tail += ["", "atlas.toml [standards] problems (the values they hold are not used):"] + [f"- {p}" for p in problems]
    if unused:
        tail += ["", "Declared in atlas.toml but read by no fw_check (a misspelt key?): " + ", ".join(unused) + "."]
    if not rs:
        lines = ["- no requirement or check record carries a fw_check"]
    out["section"] = {"title": SECTION, "lines": head + lines + tail}
    return out


def finding(rule: dict, x: dict) -> dict:
    fc = rule["rec"]["fw_check"]
    on = f" on {x['label']}" if x.get("label") else ""
    port = f" (port {x['port']})" if x.get("port") else ""
    text = f": {rule['text']}" if rule["text"] else ""
    return {"class": CLS, "key": rule["name"] + (f" @ {x['label']}" if x.get("label") else ""),
            "summary": f"{rule['name']}{text}. Fails{on}{port}: `{fc['expr']}` is false",
            "evidence": rule["cites"] + x["values"] + [f"fw_check of {rule['rec'].get('id')}"],
            "check": CHECK_PREFIX + rule["name"], "severity": "check", "settle": SETTLE}


# ---------------------------------------------------------------- entry points

def run_join(J, stamp: dict) -> dict:
    """At join: the pinned build, its devicetree as the join read it and its kconfig.json (else `.config`)."""
    from . import buildq
    try:
        src = buildq.pinned(SimpleNamespace(atlas=J.atlas, dir=J.ad))
    except LookupError as e:
        return {"checks": [], "findings": [], "not_run": [{"check": NEEDS_BUILD[0], "why": str(e)}], "folded": {},
                "section": None, "rules": [], "summary": ""}
    src.dt = J.dt
    return analyze(J.docs, BuildView.of(src), Declared.load(J.ad))


def run_source(ad: Path, src) -> dict:
    """In memory, writing nothing: every fw_check against the build a `buildq.Source` reads."""
    from .join import Docs
    return analyze(Docs(ad), BuildView.of(src), Declared.load(ad))

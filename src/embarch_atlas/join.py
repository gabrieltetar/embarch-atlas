"""`join <atlas>`: the pin/net map, the I2C identity join and the DT-vs-board checks.

Inputs: the atlas's build artifacts (through `dt.json`, which this verb writes
first), `boards/<label>/netlist.json` for the target's schematic boards,
`boards/system.json` (board-to-board mating rules: engineer-declared first, else
the top proposal, marked inferred), and `docs/*/records/*.jsonl` (pin, strap,
passthrough and conflict records; they may be sparse or absent).

Outputs in the atlas: `pins.json`, `pins.md`, `pin_slice.txt`, then the mismatch
report (`checks.md`, `mismatches.json`, via `mismatch.py`) and `MAP.md` (via
`mapgen.py`). Every board key is board-qualified (`B:U1-12`, net `B:3V3`).

Optional engineer-supplied tables, used only when present:
- `boards/straps.jsonl`: strap records (`{"part": "<MPN prefix>", "pin", "condition", "value"}`,
  or `{"part", "fixed": "0x6B"}`) for parts whose datasheet is not translated yet.
- `boards/bindings.json`: `{"compat_parts": {"<compatible>": "<MPN prefix>"},
  "prop_pins": {"<compatible>": {"<prop>": "<part pin name>"}}, "rails": ["<regex>"],
  "grounds": ["<regex>"], "dt_intent": {"<node path | label | peripheral>": "<why it has no driver>"}}`.
  A `dt_intent` entry folds that node's `dt-no-driver` candidate into the expected counts.
"""
from __future__ import annotations

import collections
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Iterable

from embarch_atlas import answers
from embarch_atlas.common import (read_json, read_jsonl, sha256_file, source_files, sources_fingerprint, write_json,
                                   write_text)

GROUND_RE = re.compile(r"^(A|D|P|S|C)?GND\w*$|(^|_)(A|D|P|S)?GND(\d+)?(_\w+)?$|^VSS\w*$|^0V$", re.I)
SUPPLY_RE = re.compile(r"^\+?\d+V\d*(_\w+)?$|^\+?\d+P\d+V?\w*$|^(A|D)?V(DD|CC|EE)\w*$|^V(BAT|SYS|IN|BUS|REF)$", re.I)
RAIL_PIN_COUNT = 20


def norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def pin_norm(s: str | None) -> str:
    """`norm` for pin names, keeping polarity: `IN+` and `IN-` (or `IN–`) must stay distinct."""
    s = (s or "").strip()
    s = re.sub(r"\+$", "plus", s)
    s = re.sub(r"[-\u2013\u2212]$", "minus", s)
    return norm(s)


AKA_PART_RE = re.compile(r"^[A-Za-z]{2,}[A-Za-z0-9]*\d[A-Za-z0-9+#/-]*$")


def card_aka_parts(card: Path) -> list[str]:
    """Part-number-shaped entries of a card's `aka:` line (family forms like `ABC120x` included).

    The doc's own `part` names one family member; a datasheet usually covers several, and
    the board carries an orderable MPN. The aka line is where the translation lists them."""
    try:
        text = card.read_text(encoding="utf-8")
    except OSError:
        return []
    m = re.search(r"^aka:\s*(.+)$", text, re.M)
    if not m:
        return []
    out = []
    for tok in (t.strip() for t in m.group(1).split(",")):
        if " " in tok or len(tok) < 5 or not AKA_PART_RE.match(tok):
            continue
        if re.fullmatch(r"0x[0-9a-fA-F]+", tok):
            continue
        out.append(tok)
    return out


def pin_sort_key(num: str):
    m = re.fullmatch(r"([A-Za-z]*)(\d+)", str(num))
    return (m[1], int(m[2])) if m else (str(num), 0)


# ---------------------------------------------------------------- boards

class Board:
    def __init__(self, label: str, nl: dict):
        self.label = label
        self.nl = nl
        self.comps: dict[str, dict] = nl.get("components", {})
        self.nets: dict[str, dict] = nl.get("nets", {})
        self.pin_net: dict[str, str] = {}
        self.pin_name: dict[str, str | None] = {}
        for ref, c in self.comps.items():
            for num, p in (c.get("pins") or {}).items():
                key = f"{ref}-{num}"
                self.pin_net[key] = p.get("net")
                self.pin_name[key] = p.get("name")
        for net, n in self.nets.items():         # nets are the authority for membership
            for key in n.get("pins", []):
                self.pin_net.setdefault(key, net)

    def pins(self, ref: str) -> list[str]:
        return [f"{ref}-{n}" for n in sorted((self.comps.get(ref, {}).get("pins") or {}), key=pin_sort_key)]

    def net(self, key: str) -> str | None:
        return self.pin_net.get(key)

    def net_pins(self, net: str | None) -> list[str]:
        return list(self.nets.get(net, {}).get("pins", [])) if net else []

    def mpn(self, ref: str) -> str | None:
        return self.comps.get(ref, {}).get("mpn")

    def value(self, ref: str) -> str | None:
        return self.comps.get(ref, {}).get("value")

    def dni(self, ref: str) -> bool:
        return bool(self.comps.get(ref, {}).get("dni"))

    def pages(self, ref: str) -> list[int]:
        return list(self.comps.get(ref, {}).get("pages") or [])


def split_key(key: str) -> tuple[str, str]:
    ref, _, pin = key.partition("-")
    return ref, pin


def ohms(v: str | None) -> float | None:
    """'10kΩ', '6.19k', '249 Ω', '5mΩ', '0R', '4K7', '1M' -> ohms."""
    if not v:
        return None
    s = str(v).replace(" ", "").replace("Ω", "").replace("ohm", "").replace("Ohm", "").replace("OHM", "")
    m = re.fullmatch(r"(\d+)([RrKkMm])(\d+)", s)
    if m:
        s = f"{m[1]}.{m[3]}{'' if m[2] in 'Rr' else m[2]}"
    m = re.fullmatch(r"([\d.]+)([mkKMuµ]?)[Rr]?", s)
    if not m:
        return None
    try:
        x = float(m[1])
    except ValueError:
        return None
    return x * {"m": 1e-3, "k": 1e3, "K": 1e3, "M": 1e6, "u": 1e-6, "µ": 1e-6, "": 1}[m[2]]


# ---------------------------------------------------------------- documents

def _part_pattern(part: str) -> re.Pattern:
    """A doc's part string as an MPN-prefix pattern: `ABC700`, `MCU0x1` (x = any), `ABC201-ABC215` (a range)."""
    m = re.fullmatch(r"([A-Za-z]+)(\d+)\s*[-/]\s*(?:\1)?(\d+)", part.strip())
    if m and len(m[2]) == len(m[3]) and int(m[2]) < int(m[3]):
        alts = "|".join(f"{m[1].lower()}{i:0{len(m[2])}d}" for i in range(int(m[2]), int(m[3]) + 1))
        return re.compile(f"^({alts})")
    out = ""
    for i, ch in enumerate(part):
        prev = part[i - 1] if i else ""
        nxt = part[i + 1] if i + 1 < len(part) else ""
        if ch == "x" and (prev.isdigit() or prev.isupper() or nxt.isdigit()):
            out += "[0-9a-z]"
        elif ch.isalnum():
            out += re.escape(ch.lower())
    return re.compile("^" + out)


class Docs:
    """docs/<doc_id>/{doc.json, records/*.jsonl}, indexed by part."""

    def __init__(self, ad: Path):
        self.ad = ad
        self.docs: dict[str, dict] = {}
        root = ad / "docs"
        for dj in sorted(root.glob("*/doc.json")) if root.is_dir() else []:
            try:
                doc = read_json(dj)
            except (OSError, ValueError):
                continue
            did = doc.get("doc_id") or doc.get("id") or dj.parent.name
            recs: dict[str, list[dict]] = collections.defaultdict(list)
            files = {}
            for rf in sorted((dj.parent / "records").glob("*.jsonl")):
                try:
                    rows = list(read_jsonl(rf))
                except (OSError, ValueError):
                    continue
                files[rf.name] = sha256_file(rf)
                for r in rows:
                    recs[r.get("kind") or rf.stem].append(r)
            parts = doc.get("parts") or doc.get("part") or []
            parts = [parts] if isinstance(parts, str) else list(parts)
            parts += [p for p in card_aka_parts(dj.parent / "card.md") if p not in parts]
            self.docs[did] = {"id": did, "doc": doc, "dir": dj.parent, "records": dict(recs), "files": files,
                              "patterns": [_part_pattern(p) for p in parts if p]}

    def for_mpn(self, mpn: str | None) -> list[str]:
        n = norm(mpn)
        if not n:
            return []
        return sorted(d for d, v in self.docs.items() if any(p.match(n) for p in v["patterns"]))

    def records(self, doc_id: str, kind: str) -> list[dict]:
        return self.docs.get(doc_id, {}).get("records", {}).get(kind, [])

    def all_records(self, kind: str) -> Iterable[tuple[str, dict]]:
        for d, v in self.docs.items():
            for r in v["records"].get(kind, []):
                yield d, r

    def kind(self, doc_id: str) -> str | None:
        return self.docs.get(doc_id, {}).get("doc", {}).get("kind")


def cite(r: dict, doc_id: str | None = None) -> str:
    p = r.get("p")
    pp = "p" + "-".join(str(x) for x in p) if isinstance(p, list) and p else (f"p{p}" if p else "")
    return f"{r.get('doc') or doc_id} {pp}".strip()


# ---------------------------------------------------------------- interfaces

def _mapping(chosen: dict, n: int | None) -> dict[str, str]:
    m = chosen.get("map") or chosen.get("pin_map") or chosen.get("pins")
    if isinstance(m, dict):
        return {str(k): str(v) for k, v in m.items()}
    if isinstance(m, list):
        return {str(a): str(b) for a, b in m}
    rule = (chosen.get("rule") or "").lower()
    if n and rule in ("identity", "straight", "same"):
        return {str(k): str(k) for k in range(1, n + 1)}
    if n and rule in ("mirror", "mirrored", "reverse", "reversed"):
        return {str(k): str(n + 1 - k) for k in range(1, n + 1)}
    return {}


def _end(e) -> tuple[str, str] | None:
    if isinstance(e, str) and ":" in e:
        b, r = e.split(":", 1)
        return b, r
    if isinstance(e, dict) and e.get("board") and (e.get("ref") or e.get("refdes")):
        return e["board"], e.get("ref") or e.get("refdes")
    return None


def interfaces_from_module(root_parent: Path, boards: dict[str, Board]) -> list[dict] | None:
    """Mated pins through `interfaces.mated_pairs` (the owner of system.json): engineer entries first,
    then the best proposal per connector. None when that module is not available."""
    try:
        from embarch_atlas import interfaces as itf
        pairs = itf.mated_pairs(root_parent, {lab: B.nl for lab, B in boards.items()})
        eng = set(getattr(itf, "ENGINEER_PROV", ("engineer-declared", "engineer-confirmed")))
    except (ImportError, AttributeError, KeyError, TypeError, ValueError):
        return None
    sj = read_json(root_parent / "boards" / "system.json")
    meta = {(e.get("a"), e.get("b")): e for e in (sj.get("interfaces") or []) + (sj.get("proposed") or [])
            if e.get("kind", "board-to-board") in ("board-to-board", "board-to-board-check")}
    groups: dict[tuple, dict] = collections.defaultdict(dict)
    for pa, pb, src in pairs:
        ca, na = pa.rsplit("-", 1)
        cb, nb = pb.rsplit("-", 1)
        groups[(ca, cb, src)][na] = nb
    out = []
    for (ca, cb, src), mp in sorted(groups.items()):
        e = meta.get((ca, cb), {})
        sc = str(e.get("declared_score") or e.get("score") or "")
        k, _, n = sc.partition("/")
        out.append({"a": ca, "b": cb, "rule": e.get("rule") or e.get("declared_rule"), "map": mp,
                    "status": "engineer-declared" if src in eng else "inferred",
                    "agree": int(k) if k.isdigit() else None, "named": int(n) if n.isdigit() else None,
                    "score": round(int(k) / int(n), 3) if k.isdigit() and n.isdigit() and int(n) else None,
                    "kind": "board-to-board"})
    return out


def interfaces_from_system(sj: dict, boards: dict[str, Board]) -> list[dict]:
    """Tolerant reader of system.json interface entries (fallback when the interfaces module is absent)."""
    out = []
    for itf in sj.get("interfaces", []) or []:
        ends = itf.get("ends") or itf.get("connectors") or [itf.get("a") or itf.get("from"), itf.get("b") or itf.get("to")]
        ends = [_end(e) for e in ends or []]
        if len(ends) != 2 or not all(ends):
            continue
        (ba, ra), (bb, rb) = ends
        if ba not in boards or bb not in boards:
            continue
        declared = itf.get("declared") or itf.get("confirmed")
        if not declared and str(itf.get("prov", "")).startswith("engineer"):
            declared = itf
        props = sorted(itf.get("proposals") or itf.get("candidates") or [], key=lambda p: -(p.get("score") or 0))
        chosen = declared or (props[0] if props else None)
        if not chosen:
            continue
        npins = len(boards[ba].comps.get(ra, {}).get("pins") or {}) or None
        mp = _mapping(chosen, npins)
        if not mp:
            continue
        out.append({"a": f"{ba}:{ra}", "b": f"{bb}:{rb}", "rule": chosen.get("rule"), "map": mp,
                    "status": "engineer-declared" if declared else "inferred",
                    "score": chosen.get("score"), "agree": chosen.get("agree"), "named": chosen.get("named"),
                    "kind": itf.get("kind", "board-to-board")})
    return out


def infer_interfaces(boards: dict[str, Board]) -> list[dict]:
    """Fallback without boards/system.json: connector pairs on different boards, identity vs mirror by net names."""
    conns = []
    for b in boards.values():
        for ref, c in b.comps.items():
            pins = [p for p in (c.get("pins") or {}) if str(p).isdigit()]
            if re.fullmatch(r"(J|P|CN|X|CON)\d+", ref) and len(pins) >= 3:
                conns.append((b.label, ref, sorted(int(p) for p in pins)))
    scored = []
    for i, (ba, ra, pa) in enumerate(conns):
        for bb, rb, pb in conns[i + 1:]:
            if ba == bb or pa != pb:
                continue
            n = max(pa)
            for rule, f in (("identity", lambda k: k), ("mirror", lambda k: n + 1 - k)):
                agree = named = 0
                for k in pa:
                    na = boards[ba].net(f"{ra}-{k}") or ""
                    nb = boards[bb].net(f"{rb}-{f(k)}") or ""
                    if na and nb and not (na.startswith("Net") and nb.startswith("Net")):
                        named += 1
                        agree += na == nb
                if named >= 3:
                    scored.append((agree / named, agree, named, ba, ra, bb, rb, rule, {str(k): str(f(k)) for k in pa}))
    scored.sort(key=lambda s: (-s[0], -s[1]))
    used, out = set(), []
    for score, agree, named, ba, ra, bb, rb, rule, mp in scored:
        if score < 0.8 or (ba, ra) in used or (bb, rb) in used:
            continue
        used |= {(ba, ra), (bb, rb)}
        out.append({"a": f"{ba}:{ra}", "b": f"{bb}:{rb}", "rule": rule, "map": mp, "status": "inferred (join fallback)",
                    "score": round(score, 3), "agree": agree, "named": named, "kind": "board-to-board"})
    return out


# ---------------------------------------------------------------- the join context

class Join:
    def __init__(self, ad: Path, atlas: Path, boards: dict[str, Board], dt: dict, in_use: dict, docs: Docs,
                 interfaces: list[dict], bindings: dict, straps: list[dict], mcu_part: str | None, mcu_ref: str | None):
        self.ad, self.atlas, self.boards, self.dt, self.in_use, self.docs = ad, atlas, boards, dt, in_use, docs
        self.interfaces = interfaces
        self.bindings = bindings
        self.straps = straps
        rails = [re.compile(r) for r in bindings.get("rails", [])]
        grounds = [re.compile(r) for r in bindings.get("grounds", [])]
        self._rails, self._grounds = rails, grounds
        self.mate: dict[tuple[str, str], tuple[str, str]] = {}
        for itf in interfaces:
            ba, ra = itf["a"].split(":", 1)
            bb, rb = itf["b"].split(":", 1)
            for pa, pb in itf["map"].items():
                self.mate[(ba, f"{ra}-{pa}")] = (bb, f"{rb}-{pb}")
                self.mate[(bb, f"{rb}-{pb}")] = (ba, f"{ra}-{pa}")
        self.mcu = self._find_mcu(mcu_part, mcu_ref)
        self.pin_records: dict[tuple[str, str], dict] = {}   # (board, ref) -> {pin number/ball: record}
        self.part_docs: dict[tuple[str, str], list[str]] = {}
        for b in boards.values():
            for ref in b.comps:
                ds = docs.for_mpn(b.mpn(ref))
                if ds:
                    self.part_docs[(b.label, ref)] = ds
        # generic cards: only an MPN the engineer answered `standard-function`, only where no datasheet covers it
        self.generic: dict[tuple[str, str], dict[str, dict]] = {}
        self.answers_error: str | None = None
        try:
            ans = answers.load(ad)
        except ValueError as e:
            ans, self.answers_error = {}, str(e)
        for b in boards.values():
            for ref, c in b.comps.items():
                a = answers.find(ans, b.mpn(ref))
                if a and a["answer"] == "standard-function" and (b.label, ref) not in self.part_docs:
                    recs = answers.pin_records(a, c)
                    if recs:
                        self.generic[(b.label, ref)] = recs
        self.uses_by_pin: dict[str, list[dict]] = collections.defaultdict(list)
        for u in dt.get("uses", []):
            if u.get("pin") and not u.get("soc_defined"):
                self.uses_by_pin[u["pin"]].append(u)

    # -- rails
    def is_ground(self, net: str | None) -> bool:
        return bool(net) and (bool(GROUND_RE.search(net)) or any(r.search(net) for r in self._grounds))

    def is_rail(self, board: str, net: str | None) -> bool:
        if not net:
            return False
        if self.is_ground(net) or SUPPLY_RE.search(net) or any(r.search(net) for r in self._rails):
            return True
        return len(self.boards[board].net_pins(net)) >= RAIL_PIN_COUNT

    # -- part identity
    def _find_mcu(self, part: str | None, ref: str | None) -> tuple[str, str] | None:
        if ref and ":" in ref:
            b, r = ref.split(":", 1)
            if b in self.boards and r in self.boards[b].comps:
                return b, r
        # the configured family's MPN prefix; without one, the part with the most port-named pins (PA0, P0.12)
        best = None
        pat = _part_pattern(part) if part else None
        for b in self.boards.values():
            for r, c in b.comps.items():
                pins = c.get("pins") or {}
                ports = sum(1 for p in pins.values() if re.match(r"P[A-Z]\d{1,2}(?!\d)|P\d\.\d{1,2}", p.get("name") or ""))
                if pat and not pat.match(norm(c.get("mpn"))) or not pat and ports < 8:
                    continue
                if best is None or (ports, len(pins)) > best[0]:
                    best = ((ports, len(pins)), b.label, r)
        return (best[1], best[2]) if best else None

    def part_pin_name(self, board: str, key: str) -> str | None:
        """A part pin's name: the datasheet's pin record (by number/ball) first, else the netlist symbol."""
        ref, num = split_key(key)
        for d in self.part_docs.get((board, ref), []):
            for r in self.docs.records(d, "pin"):
                pins = r.get("pin") if isinstance(r.get("pin"), list) else [r.get("pin")]
                if str(num) in [str(x) for x in pins]:
                    return r.get("name")
        return self.boards[board].pin_name.get(key)

    def part_pin_record(self, board: str, key: str) -> tuple[str, dict] | None:
        ref, num = split_key(key)
        name = self.boards[board].pin_name.get(key)
        for d in self.part_docs.get((board, ref), []):
            for r in self.docs.records(d, "pin"):
                pins = r.get("pin") if isinstance(r.get("pin"), list) else [r.get("pin")]
                if str(num) in [str(x) for x in pins] or (name and r.get("name") == name):
                    return d, r
        return None

    def generic_pin_record(self, board: str, key: str) -> dict | None:
        """The engineer-declared generic-card record for a part pin, or None (never for a documented part)."""
        ref, num = split_key(key)
        return self.generic.get((board, ref), {}).get(num)

    def pins_named(self, board: str, ref: str, name: str) -> list[str]:
        want = pin_norm(name)
        return [k for k in self.boards[board].pins(ref) if pin_norm(self.part_pin_name(board, k)) == want]

    # -- tracing
    def trace(self, board: str, net: str | None, came_from, depth: int = 0, via=None, seen=None) -> list[dict]:
        """Everything a net reaches: across mated connectors, one series R/FB/L hop, pulls, passthroughs."""
        seen = seen if seen is not None else set()
        if not net or (board, net) in seen:
            return []
        seen.add((board, net))
        B = self.boards[board]
        items = []
        for p in B.net_pins(net):
            if (board, p) == came_from:
                continue
            ref, pin = split_key(p)
            c = B.comps.get(ref, {})
            base = {"board": board, "pin": p, "net": net, "via": via}
            if (board, p) in self.mate:
                ob, op = self.mate[(board, p)]
                onet = self.boards[ob].net(op)
                items.append({**base, "kind": "board-to-board", "to": f"{ob}:{op}", "to_net": onet})
                if onet and not self.is_rail(ob, onet):
                    items += self.trace(ob, onet, (ob, op), depth, (via or []) + [f"{board}:{p}->{ob}:{op}"], seen)
                continue
            if re.fullmatch(r"TP\d+\w*", ref):
                items.append({**base, "kind": "testpoint"})
                continue
            npins = len(c.get("pins") or {})
            if re.fullmatch(r"(R|FB|L)\d+\w*", ref) and npins == 2:
                other = next(q for q in B.pins(ref) if q != p)
                onet = B.net(other)
                val, dni = B.value(ref), B.dni(ref)
                if self.is_rail(board, onet):
                    kind = "pull-down" if self.is_ground(onet) else "pull-up"
                    items.append({**base, "kind": kind, "part": ref, "value": val, "dni": dni, "rail": onet})
                else:
                    items.append({**base, "kind": "series", "part": ref, "value": val, "dni": dni, "to_net": onet})
                    if depth < 1 and not dni:
                        items += self.trace(board, onet, (board, other), depth + 1,
                                            (via or []) + [f"{board}:{ref}({val})"], seen)
                continue
            if re.fullmatch(r"C\d+\w*", ref):
                other = next((q for q in B.pins(ref) if q != p), None)
                items.append({**base, "kind": "cap", "part": ref, "value": B.value(ref), "dni": B.dni(ref),
                              "to_net": B.net(other) if other else None})
                continue
            if re.fullmatch(r"(J|P|CN|X|CON)\d+\w*", ref):
                items.append({**base, "kind": "connector", "part": ref, "mpn": B.mpn(ref)})
                continue
            it = {**base, "kind": "part-pin", "part": ref, "pin_name": self.part_pin_name(board, p), "mpn": B.mpn(ref),
                  "dni": B.dni(ref)}
            if re.fullmatch(r"(D|Q|LED)\d+\w*", ref):
                others = [q for q in B.pins(ref) if q != p]
                it["other_nets"] = {q: B.net(q) for q in others}
                for q, onet in it["other_nets"].items():
                    if onet and self.is_rail(board, onet) and not self.is_ground(onet):
                        it.setdefault("reaches_rail", []).append(f"{q} {onet}")
                    for q2 in B.net_pins(onet):
                        r2 = split_key(q2)[0]
                        if q2 != q and re.fullmatch(r"R\d+\w*", r2) and len(B.pins(r2)) == 2:
                            o2 = next(x for x in B.pins(r2) if x != q2)
                            n2 = B.net(o2)
                            if n2 and self.is_rail(board, n2) and not self.is_ground(n2):
                                it.setdefault("reaches_rail", []).append(f"{q} via {r2} {B.value(r2)} {n2}")
            items.append(it)
            # transparent parts: a passthrough record maps this pin to another pin of the same part
            if depth < 1:
                for d in self.part_docs.get((board, ref), []):
                    for r in self.docs.records(d, "passthrough"):
                        if pin_norm(r.get("from_pin")) == pin_norm(it["pin_name"]) or str(r.get("from_pin")) == pin:
                            for q in self.pins_named(board, ref, str(r.get("to_pin"))) or \
                                    ([f"{ref}-{r['to_pin']}"] if f"{ref}-{r.get('to_pin')}" in B.pin_net else []):
                                onet = B.net(q)
                                if onet and not self.is_rail(board, onet):
                                    items.append({**base, "kind": "passthrough", "part": ref, "to": q, "to_net": onet,
                                                  "when": r.get("when"), "cite": cite(r, d)})
                                    items += self.trace(board, onet, (board, q), depth + 1,
                                                        (via or []) + [f"{board}:{ref} {r.get('from_pin')}->{r.get('to_pin')}"],
                                                        seen)
        return items

    def mcu_ports_of(self, board: str, net: str | None, depth: int = 0, path=None, seen=None) -> list[tuple[str, list]]:
        """Nets -> MCU ports, crossing connectors, series R/FB and 3-pin FET stages with a grounded pin."""
        seen = seen if seen is not None else set()
        path = path or []
        if not net or (board, net) in seen or depth > 3 or not self.mcu:
            return []
        seen.add((board, net))
        B = self.boards[board]
        mb, mref = self.mcu
        out = []
        for p in B.net_pins(net):
            ref, _ = split_key(p)
            if board == mb and ref == mref:
                port = self.port_of_key(p)
                if port:
                    out.append((port, path))
            elif (board, p) in self.mate:
                ob, op = self.mate[(board, p)]
                out += self.mcu_ports_of(ob, self.boards[ob].net(op), depth, path + [f"{board}:{p}->{ob}:{op}"], seen)
            elif re.fullmatch(r"(R|FB)\d+\w*", ref) and len(B.pins(ref)) == 2 and not B.dni(ref):
                o = next(q for q in B.pins(ref) if q != p)
                onet = B.net(o)
                if not self.is_rail(board, onet):
                    out += self.mcu_ports_of(board, onet, depth + 1, path + [f"{board}:{ref}({B.value(ref)})"], seen)
            elif re.fullmatch(r"Q\d+\w*", ref) and len(B.pins(ref)) == 3:
                others = [B.net(q) for q in B.pins(ref) if q != p]
                if any(self.is_ground(o) for o in others):
                    for onet in others:
                        if onet and not self.is_rail(board, onet):
                            out += self.mcu_ports_of(board, onet, depth + 1,
                                                     path + [f"{board}:{ref}(FET stage, inverting by inference)"], seen)
        return out

    def port_of_key(self, key: str) -> str | None:
        if not self.mcu:
            return None
        name = self.boards[self.mcu[0]].pin_name.get(key) or ""
        m = re.match(r"P[A-Z]\d{1,2}(?!\d)", name)
        return m[0] if m else None

    # -- DT
    def dt_use(self, u: dict) -> dict:
        dt = self.dt
        compats = u.get("compats") or ([u["compat"]] if u.get("compat") else [])
        toks = [re.sub(r"[^a-z0-9]", "_", c.lower()) for c in compats]
        node = dt["nodes"].get(u["node"], {})
        owner = u["node"] if node.get("compats") else (node.get("parent") or u["node"])
        ordinal = dt["nodes"].get(owner, {}).get("ordinal")
        drv = None
        for t in toks:
            if dt.get("drivers", {}).get(t):
                drv = {"files": dt["drivers"][t], "compiled": True, "compat": t}
                break
        if drv is None:
            cons = []
            for l in u.get("labels", []) + dt["nodes"].get(owner, {}).get("labels", []):
                cons += dt.get("consumers", {}).get(l, [])
            for a, p in dt.get("aliases", {}).items():
                if p in (u["node"], owner):
                    cons += dt.get("consumers", {}).get("alias:" + a, [])
            if cons:
                drv = {"files": sorted(set(cons)), "compiled": True, "compat": None, "consumer": True}
        if drv is None:
            for t in toks:
                if dt.get("drivers_not_built", {}).get(t):
                    drv = {"files": dt["drivers_not_built"][t], "compiled": False, "compat": t}
                    break
        armed = any(u.get("prop") in dt.get("armed", {}).get(t, []) for t in toks)
        lab = (u.get("labels") or [u["node"].rsplit("/", 1)[-1]])[0]
        return {"node": u["node"], "label": lab, "compat": u.get("compat"), "prop": u.get("prop"), "kind": u.get("kind"),
                "index": u.get("index"), "func": u.get("func"), "af": u.get("af"), "flags": u.get("flags"),
                "loc": u.get("loc"), "no_binding": bool(u.get("no_binding")), "irq_armed": armed,
                "exti_line": u.get("exti_line"), "owner": owner,
                "instantiated": ordinal in set(dt.get("instantiated_ordinals", [])), "driver": drv}


# ---------------------------------------------------------------- the pin map

def build_pin_map(J: Join) -> list[dict]:
    if not J.mcu:
        return []
    mb, mref = J.mcu
    B = J.boards[mb]
    ds_by_name, ds_by_num = {}, {}
    for d in J.part_docs.get((mb, mref), []):
        for r in J.docs.records(d, "pin"):
            if r.get("name"):
                ds_by_name.setdefault(str(r["name"]).split("-")[0].split("/")[0], (d, r))
                for n in (r["pin"] if isinstance(r.get("pin"), list) else [r.get("pin")]):
                    ds_by_num.setdefault(str(n), (d, r))
    rows = []
    for key in B.pins(mref):
        _, num = split_key(key)
        net = B.net(key)
        name = B.pin_name.get(key)
        port = J.port_of_key(key)
        if not port and str(num) in ds_by_num:     # the symbol says NRST, the datasheet's pinout says PF2-NRST
            m = re.match(r"P[A-Z]\d{1,2}(?!\d)", str(ds_by_num[str(num)][1].get("name") or ""))
            port = m[0] if m else None
        rail = bool(net) and J.is_rail(mb, net)
        far = [] if (net is None or rail) else J.trace(mb, net, (mb, key))
        ds = ds_by_num.get(str(num)) or ds_by_name.get(port or (name or "").split("-")[0])
        uses = [J.dt_use(u) for u in J.uses_by_pin.get(port, [])] if port else []
        reach = any(f["kind"] in ("part-pin", "connector", "board-to-board") for f in far)
        rows.append({
            "num": num, "key": f"{mb}:{key}", "port": port, "name": name,
            "ds_name": ds[1].get("name") if ds else None, "io": (ds[1].get("type") or ds[1].get("io")) if ds else None,
            "ds_cite": cite(ds[1], ds[0]) if ds else None,
            "net": net, "rail": rail, "far": far, "dt": uses,
            "reach": reach, "cross": any(f["kind"] == "board-to-board" for f in far),
        })
    return rows


# ---------------------------------------------------------------- I2C identity

def _model_pattern(compat: str) -> re.Pattern | None:
    """`vendor,abc7xx-alert` -> ^abc7[0-9a-z][0-9a-z]; the model token after the vendor, x as a wildcard."""
    model = compat.split(",", 1)[-1]
    tok = re.split(r"[-_]", model)[0]
    if not re.search(r"\d", tok):
        return None
    return re.compile("^" + "".join("[0-9a-z]" if ch == "x" else re.escape(ch) for ch in tok.lower()))


def compat_matches(J: Join, compat: str | None, mpn: str | None) -> bool:
    if not compat or not mpn:
        return False
    decl = J.bindings.get("compat_parts", {}).get(compat)
    if decl:
        return norm(mpn).startswith(norm(decl))
    pat = _model_pattern(compat)
    return bool(pat and pat.match(norm(mpn)))


OVERLINE_MARK = re.compile(r"\u0305|[-_]bar$|#$|^[~/]", re.I)


def plain_name(name: str | None) -> str | None:
    """A pin name without its active-low marker (`FLT-bar`, `INT-bar` of `FB/INT-bar`, `~RST`, `CS#`):
    a DT property names the signal, and its polarity is in the GPIO flags. Two pins that differ only by
    the marker (EN, EN-bar) then match both, and prop_to_pin, which needs one hit, resolves neither."""
    if name is None:
        return None
    return "/".join(OVERLINE_MARK.sub("", part) for part in name.split("/"))


def prop_to_pin(J: Join, compat: str, prop: str, board: str, ref: str) -> tuple[str | None, str]:
    """A DT GPIO property -> the part pin it drives or reads. Engineer table first, then name heuristics."""
    decl = J.bindings.get("prop_pins", {}).get(compat, {}).get(prop)
    names = {k: plain_name(J.part_pin_name(board, k)) for k in J.boards[board].pins(ref)}
    if decl:
        hit = [k for k, n in names.items() if norm(n) == norm(decl)]
        return (hit[0] if hit else None), "declared"
    base = re.sub(r"-gpios?$", "", prop)
    want = norm(base)
    cands = [want]
    if want.endswith("n") and len(want) > 2:
        cands.append(want[:-1])
    if want.startswith("n") and len(want) > 2:
        cands.append(want[1:])

    def skeleton(s: str) -> str:
        s = re.sub(r"\d+$", "", s)
        return s[:1] + re.sub(r"[aeiou]", "", s[1:])

    for how, test in (("name", lambda n, c: norm(n) == c),
                      ("name part", lambda n, c: c in [norm(x) for x in re.split(r"[/|]", n or "")]),
                      ("name, digits dropped", lambda n, c: re.sub(r"\d+$", "", norm(n)) == c),
                      ("consonant skeleton", lambda n, c: len(c) >= 4 and skeleton(norm(n)) == skeleton(c))):
        for c in cands:
            hit = [k for k, n in names.items() if n and test(n, c)]
            if len(hit) == 1:
                return hit[0], how
    return None, "no pin name matches"


def _strap_value(v) -> int | None:
    if isinstance(v, int):
        return v
    s = str(v or "").strip()
    m = re.search(r"0x([0-9a-f]+)", s, re.I) or re.search(r"\b([0-9a-f]+)h\b", s, re.I)
    if m:
        return int(m[1], 16)
    m = re.search(r"\b([01]{7})b?\b", s)
    if m:
        return int(m[1], 2)
    m = re.search(r"\b(\d+)\b", s)
    return int(m[1]) if m else None


def strap_records(J: Join, board: str, ref: str) -> list[tuple[dict, str]]:
    """Strap records for a part: its documents' `strap` records, else the engineer's boards/straps.jsonl."""
    out = [(r, cite(r, d)) for d in J.part_docs.get((board, ref), []) for r in J.docs.records(d, "strap")]
    if out:
        return out
    n = norm(J.boards[board].mpn(ref))
    return [(r, r.get("cite") or "boards/straps.jsonl (engineer)") for r in J.straps
            if r.get("part") and n.startswith(norm(r["part"]))]


def strap_address(J: Join, board: str, ref: str) -> tuple[list[int] | None, str]:
    B = J.boards[board]
    recs = strap_records(J, board, ref)
    if not recs:
        return None, "no strap records (datasheet not translated, and none in boards/straps.jsonl)"
    fixed = [(r, c) for r, c in recs if r.get("fixed") is not None or str(r.get("condition", "")).lower() in ("fixed", "always", "")
             and not r.get("pin")]
    if fixed:
        vals = [_strap_value(x) for r, _ in fixed for x in
                (r.get("fixed", r.get("value")) if isinstance(r.get("fixed", r.get("value")), list) else [r.get("fixed", r.get("value"))])]
        return sorted({v for v in vals if v is not None}), f"fixed address; {fixed[0][1]}"
    hits = []
    for r, c in recs:
        pin = r.get("pin")
        keys = J.pins_named(board, ref, str(pin)) if pin else []
        if not keys:
            continue
        net = B.net(keys[0])
        cond = str(r.get("condition") or "")
        cn = norm(cond)
        ok, why = False, ""
        rm = re.search(r"([\d.]+)\s*([kKmM]?)\s*(Ω|ohm|R\b)?", cond) if re.search(r"\d", cond) else None
        if rm and re.search(r"k|Ω|ohm|resist|\bR\b", cond, re.I):
            want = float(rm[1]) * {"k": 1e3, "K": 1e3, "M": 1e6, "m": 1e-3, "": 1}[rm[2]]
            for q in B.net_pins(net):
                r2 = split_key(q)[0]
                if re.fullmatch(r"R\d+\w*", r2) and len(B.pins(r2)) == 2 and not B.dni(r2):
                    o = next(x for x in B.pins(r2) if x != q)
                    v = ohms(B.value(r2))
                    to_gnd = J.is_ground(B.net(o))
                    wants_gnd = bool(re.search(r"gnd|ground|vss", cond, re.I)) or not re.search(r"v[dc]d|vcc|vs\b|supply", cond, re.I)
                    if v is not None and abs(v - want) <= 0.02 * max(want, 1e-3) and to_gnd == wants_gnd:
                        ok, why = True, f"{board}:{r2} {B.value(r2)} to {B.net(o)}"
                        break
        elif cn in ("gnd", "agnd", "ground", "vss", "low", "0"):
            ok, why = J.is_ground(net), f"on {net}"
        elif cn in ("open", "float", "floating", "nc", "unconnected"):
            ok, why = len(B.net_pins(net)) <= 1, f"net {net} has {len(B.net_pins(net))} pin(s)"
        else:
            other = J.pins_named(board, ref, cond) if cond else []
            if other:
                ok, why = B.net(other[0]) == net, f"tied to {cond} ({net})"
            elif cn in ("vdd", "vcc", "vs", "high", "1", "supply"):
                ok, why = J.is_rail(board, net) and not J.is_ground(net), f"on {net}"
        if ok:
            v = _strap_value(r.get("value"))
            if v is not None:
                hits.append((v, f"{c}; {board}:{keys[0]} {pin} {why}"))
    vals = sorted({v for v, _ in hits})
    if len(vals) == 1:
        return vals, hits[0][1]
    if not vals:
        return None, "no strap condition matches the board"
    return vals, "several strap conditions match: " + "; ".join(w for _, w in hits)


def i2c_join(J: Join, rows: list[dict]) -> dict:
    by_port = {r["port"]: r for r in rows if r["port"]}
    buses = collections.defaultdict(list)
    for d in J.dt.get("devices", []):
        if "i2c" in d.get("bus_kind", []):
            buses[d["bus"]].append(d)
    tables, identity = [], []
    for bus, devs in sorted(buses.items()):
        sda = None
        for u in J.dt.get("uses", []):
            if u["node"] == bus and u.get("kind") == "pinctrl" and "sda" in (u.get("func") or "").lower():
                sda = by_port.get(u["pin"])
                break
        if not sda or not sda["net"]:
            tables.append({"bus": bus, "sda": None, "status": "no SDA pin on the schematic"})
            continue
        mb = J.mcu[0]
        parts, seen = [], set()
        for f in [{"kind": "x", "board": mb, "net": sda["net"]}] + sda["far"]:
            if f["kind"] == "part-pin" and (f["board"], f["part"]) not in seen and not f.get("dni"):
                seen.add((f["board"], f["part"]))
                parts.append((f["board"], f["part"], f.get("mpn"), f.get("pin_name")))
        rec = {"bus": bus, "bus_labels": devs[0].get("bus_labels", []), "sda_net": f"{mb}:{sda['net']}", "families": []}
        fams = collections.defaultdict(lambda: {"dt": [], "parts": []})
        for d in devs:
            fams[d["compat"]]["dt"].append(d)
        unmatched = []
        for b, r, mp, pn in parts:
            fam = next((c for c in fams if compat_matches(J, c, mp)), None)
            if fam:
                addrs, why = strap_address(J, b, r)
                fams[fam]["parts"].append({"part": f"{b}:{r}", "mpn": mp, "addrs": addrs, "why": why})
            elif re.search(r"sda|sdio", pn or "", re.I):
                unmatched.append({"part": f"{b}:{r}", "mpn": mp, "pin_name": pn})
        for compat, v in sorted(fams.items(), key=lambda x: str(x[0])):
            rec["families"].append({"compat": compat, "dt": [{"node": d["node"], "reg": d["reg"], "loc": d.get("loc")} for d in v["dt"]],
                                    "parts": v["parts"]})
        rec["unmatched_parts"] = unmatched
        tables.append(rec)
        # per device: unique part by address, then its signal pins against the DT GPIOs
        for fam in rec["families"]:
            for d in fam["dt"]:
                match = [p for p in fam["parts"] if p["addrs"] and d["reg"] in p["addrs"]]
                idrec: dict[str, Any] = {"node": d["node"], "reg": d["reg"], "compat": fam["compat"], "loc": d.get("loc")}
                if len(match) != 1:
                    idrec.update({"status": "no unique part by address", "candidates": [p["part"] for p in match],
                                  "parts_on_bus": [(p["part"], p["addrs"], p["why"]) for p in fam["parts"]]})
                    identity.append(idrec)
                    continue
                b, ref = match[0]["part"].split(":", 1)
                idrec.update({"status": "identified", "part": match[0]["part"], "mpn": match[0]["mpn"],
                              "addr_evidence": match[0]["why"], "signals": []})
                for u in J.dt.get("uses", []):
                    if u["node"] != d["node"] or u.get("kind") != "gpio":
                        continue
                    key, how = prop_to_pin(J, fam["compat"], u["prop"], b, ref)
                    if not key:
                        idrec["signals"].append({"prop": u["prop"], "dt_pin": u["pin"], "dt_loc": u.get("loc"),
                                                 "status": "part pin not found", "how": how})
                        continue
                    pnet = J.boards[b].net(key)
                    reached = J.mcu_ports_of(b, pnet)
                    ports = sorted({x for x, _ in reached})
                    idrec["signals"].append({
                        "prop": u["prop"], "dt_pin": u["pin"], "dt_loc": u.get("loc"),
                        "part_pin": f"{b}:{key} {J.part_pin_name(b, key)}", "part_net": f"{b}:{pnet}", "how": how,
                        "mcu_ports": ports, "path": next((p for x, p in reached if x == u["pin"]), reached[0][1] if reached else []),
                        "status": "agree" if u["pin"] in ports else "DISAGREE"})
                identity.append(idrec)
    return {"buses": tables, "identity": identity}


# ---------------------------------------------------------------- DT values vs the BOM

RES_PROP = re.compile(r"(resist|shunt|sense|rsense|r-sense).*-(micro-?|milli-?)?ohms?$")


def _prop_ohms(name: str, val) -> float | None:
    if not isinstance(val, (int, float)):
        return None
    if re.search(r"micro-?ohms?$", name):
        return val * 1e-6
    if re.search(r"milli-?ohms?$", name):
        return val * 1e-3
    return float(val)


def value_checks(J: Join, identity: list[dict]) -> list[dict]:
    out = []
    for r in identity:
        if r.get("status") != "identified":
            continue
        b, ref = r["part"].split(":", 1)
        B = J.boards[b]
        props = J.dt["nodes"].get(r["node"], {}).get("props", {})
        near: dict[str, str] = {}
        for k in B.pins(ref):
            n0 = B.net(k)
            if not n0:
                continue
            near.setdefault(n0, f"{k} {J.part_pin_name(b, k)}")
            for q in B.net_pins(n0):
                r2 = split_key(q)[0]
                if re.fullmatch(r"R\d+\w*", r2) and len(B.pins(r2)) == 2 and (ohms(B.value(r2)) or 1e9) < 100:
                    o = next(x for x in B.pins(r2) if x != q)
                    near.setdefault(B.net(o), f"{k} {J.part_pin_name(b, k)} via {r2} {B.value(r2)}")
        for prop, pv in sorted(props.items()):
            if not RES_PROP.search(prop):
                continue
            want = _prop_ohms(prop, pv.get("val"))
            if want is None:
                continue
            tok = prop.split("-")[0].upper()
            named = [n for n, why in near.items() if tok in (why or "").upper()]
            got = []
            for r2, c in B.comps.items():
                if not re.fullmatch(r"R\d+\w*", r2) or len(c.get("pins") or {}) != 2 or B.dni(r2):
                    continue
                v = ohms(B.value(r2))
                if v is None:
                    continue
                n1, n2 = (B.net(k) for k in B.pins(r2))
                if named and (n1 in named or n2 in named):
                    got.append((r2, v))
                elif not named and v < 0.1 and n1 in near and n2 in near:
                    got.append((r2, v))
            ok = any(abs(v - want) <= 0.011 * want for _, v in got)
            out.append({"node": r["node"], "part": r["part"], "prop": prop, "loc": pv.get("loc"), "dt_ohms": want,
                        "board": [f"{b}:{x} {B.value(x)}" for x, _ in got], "status": "agree" if ok else "check"})
    return out


# ---------------------------------------------------------------- DT comments vs nets

def _read_source(stamp: dict, ad: Path, rel: str) -> str | None:
    b = stamp.get("build", {})
    wt = ad / b["worktree"] if b.get("worktree") else None
    if wt and (wt / rel).is_file():
        return (wt / rel).read_text(encoding="utf-8", errors="replace")
    fw = b.get("fw")
    if fw:
        r = subprocess.run(["git", "-C", fw, "show", f"{stamp['commit']}:{rel}"], capture_output=True, text=True)
        if r.returncode == 0:
            return r.stdout
    return None


def comment_checks(J: Join, stamp: dict, rows: list[dict]) -> list[dict]:
    """Net names an engineer wrote next to GPIO specifiers in the fw repo's DTS files, against the netlist."""
    ports = {lab: port for path, port in J.dt.get("gpio_controllers", {}).items()
             for lab in J.dt["nodes"].get(path, {}).get("labels", [])}
    by_port = {r["port"]: r for r in rows if r["port"]}
    files = sorted(set(stamp.get("build", {}).get("from_worktree", {}).get("dts_inputs_in_worktree", [])))
    out = []
    pat = re.compile(r"<&(\w+)\s+(\d+)\s+[^>]*>\s*[,;]?\s*(?:/\*+|//)\s*([A-Za-z0-9_+~\-]+)")
    for f in files:
        text = _read_source(stamp, J.ad, f)
        if text is None:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for m in pat.finditer(line):
                if m[1] not in ports:
                    continue
                pin = f"P{ports[m[1]]}{m[2]}"
                net = by_port.get(pin, {}).get("net")
                out.append({"loc": f"fw:{f}:{i}", "pin": pin, "comment": m[3], "net": net,
                            "status": "agree" if net == m[3] else "disagree"})
    return out


# ---------------------------------------------------------------- render

def far_str(far: list[dict]) -> str:
    parts = []
    for f in far:
        k = f["kind"]
        via = f" via {'/'.join(f['via'])}" if f.get("via") else ""
        dni = " DNI" if f.get("dni") else ""
        if k == "part-pin":
            parts.append(f"{f['board']}:{f['pin']} {f.get('pin_name') or '?'} [{(f.get('mpn') or '?')}]{dni}{via}")
        elif k in ("pull-up", "pull-down"):
            parts.append(f"{k[5:]} {f['board']}:{f['part']} {f.get('value') or '?'}{dni}->{f['rail']}")
        elif k == "series":
            parts.append(f"series {f['board']}:{f['part']} {f.get('value') or '?'}{dni}->{f['to_net']}")
        elif k == "connector":
            parts.append(f"conn {f['board']}:{f['pin']}{via}")
        elif k == "cap":
            parts.append(f"cap {f['board']}:{f['part']} {f.get('value') or '?'}")
        elif k == "passthrough":
            parts.append(f"through {f['board']}:{f['part']} ->{f['to_net']} ({f.get('cite')})")
    b2b = [f"{f['board']}:{f['pin']}->{f['to']}" for f in far if f["kind"] == "board-to-board"]
    if b2b:
        parts.append("x-board " + ",".join(b2b))
    tps = sum(1 for f in far if f["kind"] == "testpoint")
    if tps:
        parts.append(f"{tps} TP")
    return "; ".join(parts)


def dt_str(uses: list[dict]) -> str:
    out = []
    for u in uses:
        fn = u["af"] if u.get("af") else u.get("flags") or ""
        prop = u["func"] if u.get("kind") == "pinctrl" and u.get("func") else u["prop"]
        idx = f"[{u['index']}]" if u.get("index") else ""
        irq = " irq" if u.get("irq_armed") else ""
        drv = ""
        if u.get("driver"):
            files = u["driver"]["files"]
            drv = f" -> {files[0] if files else '(no source)'}{'' if u['driver']['compiled'] else ' (not in build)'}"
        elif not u.get("instantiated"):
            drv = " -> (no driver in build)"
        out.append(f"{u['label']}.{prop}{idx} {fn}{irq}{drv}")
    return "; ".join(out)


def render_pins_md(stamp: dict, mcu: tuple[str, str] | None, mpn: str | None, rows: list[dict], interfaces: list[dict],
                   i2c: dict, values: list[dict]) -> str:
    L = [f"# Pin/net map: {stamp['id']}", "",
         f"Target `{stamp['target']}`, Zephyr board `{stamp['board']}`, commit `{stamp['commit']}`. "
         f"MCU {mcu[0] + ':' + mcu[1] if mcu else '(not found)'} {mpn or ''}. "
         "Sources: boards/<label>/netlist.json (schematic), this atlas's dt.json (build), docs/*/records (datasheets).", ""]
    if interfaces:
        L.append("Board-to-board mating: " + "; ".join(
            f"{i['a']}<->{i['b']} {i.get('rule') or 'map'} ({i['status']}"
            + (f", {i['agree']}/{i['named']} names agree" if i.get("named") else "") + ")" for i in interfaces) + ".")
        L.append("")
    L += ["Columns: pin | port | I/O (datasheet) | net | far end | DT use (label.prop flags/AF, irq = armed by the driver, -> driver)", "",
          "| # | port | io | net | far end | DT |", "|---|---|---|---|---|---|"]
    for r in rows:
        far = f"(rail {r['net']})" if r["rail"] else far_str(r["far"])
        L.append(f"| {r['num']} | {r['port'] or r['name'] or ''} | {r['io'] or ''} | {r['net'] or '(unconnected)'} | "
                 f"{far.replace('|', '/')} | {dt_str(r['dt']).replace('|', '/')} |")
    L += ["", "## I2C devices: part by address strap, signals by net", "",
          "| DT node | reg | part | address evidence | signals (prop: DT pin vs part pin -> MCU) |", "|---|---|---|---|---|"]
    for r in i2c["identity"]:
        sig = "; ".join(f"{s['prop']}: {s.get('dt_pin')} vs {s.get('part_pin') or '?'} -> {s.get('mcu_ports', [])} {s['status']}"
                        for s in r.get("signals", []))
        part = f"{r.get('part')} {r.get('mpn') or ''}" if r.get("part") else r["status"]
        L.append(f"| {r['node'].rsplit('/', 1)[-1]} | {hex(r['reg']) if isinstance(r['reg'], int) else r['reg']} | {part} | "
                 f"{r.get('addr_evidence') or ''} | {sig} |")
    L += ["", "## DT resistor values vs the schematic BOM", "", "| DT node | property | DT (ohm) | board | status |", "|---|---|---|---|---|"]
    for v in values:
        L.append(f"| {v['node'].rsplit('/', 1)[-1]} | {v['prop']} | {v['dt_ohms']:g} | {', '.join(v['board'])} | {v['status']} |")
    return "\n".join(L) + "\n"


def render_slice(stamp: dict, rows: list[dict]) -> str:
    L = [f"# {stamp['id']} pin slice: port net DT-owner far-parts (pins with a DT use; full map in pins.md)"]
    for r in rows:
        if not r["port"] or r["rail"]:
            continue
        own = ",".join(sorted({f"{u['label']}.{u['func'] if u.get('kind') == 'pinctrl' and u.get('func') else u['prop']}"
                               for u in r["dt"]}))
        far = sorted({f"{f['board']}:{f['part']}" for f in r["far"] if f["kind"] == "part-pin"})[:2]
        if not far:
            far = sorted({f"{f['board']}:{f['pin']}" for f in r["far"] if f["kind"] in ("connector", "board-to-board")})[:1]
        if own:
            L.append(f"{r['port']} {r['net']} {own or '-'} {' '.join(far)}".rstrip())
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- the verb

def load_boards(ad: Path, labels: list[str], boards_dir: Path | None) -> dict[str, Board]:
    root = boards_dir or ad / "boards"
    if not labels:
        labels = sorted(p.parent.name for p in root.glob("*/netlist.json"))
    out = {}
    for lab in labels:
        p = root / lab / "netlist.json"
        if p.is_file():
            out[lab] = Board(lab, read_json(p))
    return out


def missing_boards(stamp: dict, boards: dict[str, Board]) -> list[str]:
    """The stamp's declared schematic boards that no netlist was loaded for."""
    return sorted(set(stamp.get("schematic_boards") or []) - set(boards))


def run(ad: Path, atlas: Path, boards_dir: Path | None = None, svd: str | None = None,
        mcu_part: str | None = None, mcu_ref: str | None = None, log=print,
        straps_path: Path | None = None, bindings_path: Path | None = None) -> dict:
    from embarch_atlas import dt as dtmod, graph, mapgen, mismatch
    stamp = read_json(atlas / "atlas.json")
    if (atlas / "build" / "edt.pickle").is_file():
        dt, in_use = dtmod.run(atlas, ad, svd)
    elif (atlas / "dt.json").is_file():          # build artifacts pruned: the extracted DT stands in
        dt = read_json(atlas / "dt.json")
        in_use = read_json(atlas / "in_use.json") if (atlas / "in_use.json").is_file() else {"peripherals": {}}
    else:
        raise SystemExit(f"{atlas}: no build/edt.pickle and no dt.json (run `build` first)")
    J, root, sysj = load_join(ad, atlas, stamp, dt, in_use, boards_dir, mcu_part, mcu_ref, straps_path,
                              bindings_path, log)
    boards, interfaces = J.boards, J.interfaces
    rows = build_pin_map(J)
    i2c = i2c_join(J, rows) if J.mcu else {"buses": [], "identity": []}
    values = value_checks(J, i2c["identity"])
    comments = comment_checks(J, stamp, rows) if J.mcu else []
    mpn = J.boards[J.mcu[0]].mpn(J.mcu[1]) if J.mcu else None
    io = [r for r in rows if r["port"]]
    counts = {
        "package_pins": len(rows), "io_pins": len(io), "dt_pins": len({r["port"] for r in io if r["dt"]}),
        "dt_pins_total": len({u["pin"] for u in dt.get("uses", []) if u.get("pin") and not u.get("soc_defined")}),
        "reach": sum(1 for r in io if r["reach"]), "cross": sum(1 for r in io if r["cross"]),
        "pins_with_net": sum(1 for r in rows if r["net"]),
    }
    pins = {"atlas": stamp["id"], "mcu": {"key": f"{J.mcu[0]}:{J.mcu[1]}" if J.mcu else None, "mpn": mpn},
            "interfaces": [{k: v for k, v in i.items() if k != "map"} for i in interfaces],
            "counts": counts, "pins": rows, "i2c": i2c, "values": values, "comments": comments,
            "part_docs": {f"{b}:{r}": d for (b, r), d in sorted(J.part_docs.items())}}
    write_json(atlas / "pins.json", pins)
    write_text(atlas / "pins.md", render_pins_md(stamp, J.mcu, mpn, rows, interfaces, i2c, values))
    write_text(atlas / "pin_slice.txt", render_slice(stamp, rows))
    snapshot_inputs(atlas, root, boards, J.docs, sysj, ad, missing_boards(stamp, J.boards))
    report = mismatch.run(J, stamp, pins)
    try:                                          # the MAP's RM line reads the worklist of THIS atlas
        from embarch_atlas import update
        update.rm_todo(ad, atlas)
    except Exception as e:                        # noqa: BLE001  (no RM, or a half-built one: the line says "not selected")
        log(f"join: reference-manual worklist not written: {e}")
    mp = mapgen.run(ad, atlas, J, stamp, pins, report)
    g = graph.run(ad, atlas, J, stamp, pins, report, log=log)
    log(f"{stamp['id']}: {counts['package_pins']} package pins, {counts['dt_pins']} MCU pins in DT, "
        f"{counts['reach']}/{counts['io_pins']} I/O pins reach a part or connector, {counts['cross']} cross boards; "
        f"{report['summary']}; MAP {mp['tokens']} tokens; graph {g['counts']['nodes']} nodes")
    return pins


def load_join(ad: Path, atlas: Path, stamp: dict, dt: dict, in_use: dict, boards_dir: Path | None = None,
              mcu_part: str | None = None, mcu_ref: str | None = None, straps_path: Path | None = None,
              bindings_path: Path | None = None, log=print) -> tuple[Join, Path, Path]:
    """The Join over this atlas's inputs: boards, interfaces, bindings, straps, docs. Also (boards root, system.json)."""
    root = boards_dir or ad / "boards"
    boards = load_boards(ad, list(stamp.get("schematic_boards") or []), boards_dir)
    if not boards:
        log(f"join: no netlist.json under {root} for {stamp.get('schematic_boards') or 'any board'}; pin map skipped")
    elif missing_boards(stamp, boards):
        log(f"join: declared board(s) without a netlist, left out of the pin map, checks, MAP and graph: "
            f"{', '.join(missing_boards(stamp, boards))} (embarch-atlas schematic <pdf> --board <label>, then join)")
    sysj = root / "system.json"
    interfaces = []
    if sysj.is_file():
        interfaces = interfaces_from_module(root.parent, boards) if root.name == "boards" else None
        if interfaces is None:
            interfaces = interfaces_from_system(read_json(sysj), boards)
    if not sysj.is_file() and len(boards) > 1:
        interfaces = infer_interfaces(boards)
    bindings_path = bindings_path or root / "bindings.json"
    straps_path = straps_path or root / "straps.jsonl"
    bindings = read_json(bindings_path) if bindings_path.is_file() else {}
    straps = list(read_jsonl(straps_path)) if straps_path.is_file() else []
    docs = Docs(ad)
    mcu = stamp.get("mcu") or {}
    J = Join(ad, atlas, boards, dt, in_use, docs, interfaces, bindings, straps,
             mcu_part or mcu.get("part"), mcu_ref or mcu.get("ref"))
    return J, root, sysj


def snapshot_inputs(atlas: Path, root: Path, boards: dict[str, Board], docs: Docs, sysj: Path, ad: Path,
                    missing: list[str] | None = None) -> None:
    """Copy what the join read, so the atlas stays comparable after the inputs move on.

    `sources` (the manifest fingerprint), `source_files` and `schematics` (the board PDF each joined
    netlist came from) say which documents this join saw; staleness reads them over the build's
    stamp (`project.joined_stamp`), so an atlas joined again after a document change is fresh.
    `missing_boards` names the declared boards that had no netlist: the atlas covers less than its
    stamp claims, and `project.atlas_state` says so until a join finds them."""
    inp = atlas / "inputs"
    inp.mkdir(exist_ok=True)
    rec = {"netlists": {}, "system_json": sha256_file(sysj) if sysj.is_file() else None,
           "docs": {d: {"doc_sha256": (v["doc"].get("sha256")), "records": v["files"]} for d, v in sorted(docs.docs.items())}}
    man = ad / "inventory" / "manifest.json"
    rec["sources"], rec["source_files"] = sources_fingerprint(man), source_files(man)
    rec["missing_boards"] = list(missing or [])
    rec["schematics"] = {lab: (b.nl.get("source") or {}).get("sha256") for lab, b in sorted(boards.items())}
    for lab in boards:
        src = root / lab / "netlist.json"
        shutil.copyfile(src, inp / f"netlist_{lab}.json")
        rec["netlists"][lab] = sha256_file(src)
        if (root / lab / "bom.json").is_file():
            shutil.copyfile(root / lab / "bom.json", inp / f"bom_{lab}.json")
    write_json(atlas / "inputs.json", rec)


def cmd_join(args) -> int:
    from embarch_atlas.build import resolve_atlas, resolve_atlas_dir
    ad = resolve_atlas_dir(args)
    atlas = resolve_atlas(ad, args.atlas)
    run(ad, atlas, Path(args.boards_dir).resolve() if args.boards_dir else None, args.svd, args.mcu, args.mcu_ref,
        straps_path=Path(args.straps) if args.straps else None,
        bindings_path=Path(args.bindings) if args.bindings else None)
    return 0


def register(sub, groups) -> None:
    p = sub.add_parser("join", help="dt.json, in_use.json, pins, checks, mismatches, MAP.md")
    p.add_argument("atlas", help="atlas id (<target>@<commit12>), its dir, or a target name (its current atlas)")
    p.add_argument("--boards-dir", help="read netlists from here instead of <atlas_dir>/boards")
    p.add_argument("--svd", help="SVD file (default: the stamp's mcu.svd)")
    p.add_argument("--mcu", help="MCU part family (default: the stamp's mcu.part)")
    p.add_argument("--mcu-ref", help="MCU refdes, board-qualified (B:U1)")
    p.add_argument("--straps", help="engineer strap table (default: <boards>/straps.jsonl)")
    p.add_argument("--bindings", help="engineer binding table (default: <boards>/bindings.json)")
    p.add_argument("--fw", help=argparse_suppress())
    p.set_defaults(func=cmd_join)


def argparse_suppress():
    import argparse
    return argparse.SUPPRESS

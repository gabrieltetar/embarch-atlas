"""`net_trace`: any net on any board, not only the ones that reach an MCU pin.

`pin_trace` reads pins.json, which holds the nets an MCU pin reaches. A rail, a sense line or a
load-switch enable that never touches the MCU is only in boards/<label>/netlist.json; this lookup
reads the netlists through the join (connector mates, datasheet pin records) at query time.

Two parts to an answer:

- **Members**: every pin on the net, one line each, grouped by board, crossing declared or
  inferred connector mates. Capacitors and test points fold into one line per net.
- **Upstream**: what feeds the net. A part feeds a rail only when its datasheet pin record says
  so (`flow` source or bidir); the atlas never infers a role from a pin name. For each feeding
  part the walk names its enable pins and where they come from (an MCU port, a pull), then
  continues from its input pins (`flow` sink or bidir). With no documented feed it follows
  low-ohm series parts, inductors and ferrites (direction not stated) and connector mates, and
  stops at an MCU pin, an unmated connector, a part without a datasheet or the depth limit.

Parts without a datasheet are listed as questions for the engineer, never guessed.
"""
from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Any

from .join import Join, load_join, ohms, pin_norm, split_key

PASSIVE_RE = re.compile(r"(R|C|L|FB)\d+\w*$")
SERIES_RE = re.compile(r"(R|L|FB)\d+\w*$")
TP_RE = re.compile(r"TP\w*$")
CONN_RE = re.compile(r"(J|P|CN|X|CON)\d+\w*$")
ENABLE_TOKENS = re.compile(r"^n?(en\d*|enable\d*|shdn|sd)$")
SERIES_MAX_OHMS = 1.0           # a resistor at or under this is a power path, above it a pull or a divider
MAX_HOPS = 5                    # feeds and series parts walked upstream before the walk stops


def load(atlas_dir: Path, atlas: Path | None, read_json) -> Join:
    stamp = (read_json(atlas / "atlas.json") if atlas else None) or {}
    dt = (read_json(atlas / "dt.json") if atlas else None) or {"uses": [], "nodes": {}}
    in_use = (read_json(atlas / "in_use.json") if atlas else None) or {"peripherals": {}}
    J, _, _ = load_join(atlas_dir, atlas or atlas_dir, stamp, dt, in_use, log=lambda *_: None)
    return J


# ---------------------------------------------------------------- resolving the query

def _pin_by_name(J: Join, board: str, ref: str, want: str) -> list[str]:
    w = pin_norm(re.sub(r"(-bar|̅)$", "", want))
    out = []
    for key in J.boards[board].pins(ref):
        names = {J.boards[board].pin_name.get(key), J.part_pin_name(board, key)}
        if any(n and pin_norm(re.sub(r"(-bar|̅)$", "", n)) == w for n in names):
            out.append(key)
    return out


def resolve(J: Join, query: str, board: str | None) -> dict:
    """{kind: pin|part|net|none, ...}. A query is `[board:]net`, `[board:]REF-PIN` (number or
    name) or `[board:]REF`; a net is matched exactly, then with punctuation ignored."""
    q = query.strip()
    head, sep, rest = q.partition(":")
    if sep and head in J.boards:
        boards, item = [head], rest
    else:
        boards, item = ([board] if board in J.boards else sorted(J.boards)), q
    m = re.fullmatch(r"([A-Za-z]+\d+\w*)-(.+)", item)
    if m:
        for b in boards:
            if m[1] in J.boards[b].comps:
                key = f"{m[1]}-{m[2]}"
                keys = [key] if key in J.boards[b].pin_net else _pin_by_name(J, b, m[1], m[2])
                if keys:
                    return {"kind": "pin", "board": b, "keys": keys}
    for b in boards:
        if item in J.boards[b].comps:
            return {"kind": "part", "board": b, "ref": item}
    nets = [(b, item) for b in boards if item in J.boards[b].nets]
    if not nets:
        n = re.sub(r"[^a-z0-9]", "", item.lower())
        nets = [(b, x) for b in boards for x in J.boards[b].nets if re.sub(r"[^a-z0-9]", "", x.lower()) == n]
    if nets:
        return {"kind": "net", "nets": nets}
    names = sorted({x for b in boards for x in J.boards[b].nets})
    close = difflib.get_close_matches(item, names, n=6, cutoff=0.6)
    return {"kind": "none", "close": [f"{b}:{x}" for x in close for b in boards if x in J.boards[b].nets]}


# ---------------------------------------------------------------- one net's members

def _role(J: Join, board: str, key: str) -> tuple[str, dict | None, str | None]:
    """(pin name, datasheet pin record, doc id) for a part pin."""
    found = J.part_pin_record(board, key)
    name = J.part_pin_name(board, key) or J.boards[board].pin_name.get(key) or split_key(key)[1]
    return (name, found[1], found[0]) if found else (name, None, None)


def _cite(rec: dict, doc: str) -> str:
    p = rec.get("p")
    p = ",".join(map(str, p)) if isinstance(p, list) else p
    return f"{doc} p{p}" if p else doc


def _mcu_line(J: Join, board: str, key: str) -> tuple[str, bool]:
    """(text, is an I/O port) for an MCU pin."""
    port = J.port_of_key(key)
    if not port:
        return f"{J.boards[board].pin_name.get(key) or key} (MCU supply or special pin)", False
    uses = [J.dt_use(u) for u in J.uses_by_pin.get(port, [])]
    dt = "; ".join(f"{u['label']}.{u['prop']}" for u in uses) if uses else "no DT use"
    return f"{port} (MCU) DT: {dt}", True


def members(J: Join, board: str, net: str) -> list[dict]:
    """Every pin on board:net, classified. Each item: {key, kind, ...}."""
    B = J.boards[board]
    out = []
    for p in B.net_pins(net):
        ref, _ = split_key(p)
        it: dict[str, Any] = {"board": board, "key": p, "ref": ref, "mpn": B.mpn(ref), "dni": B.dni(ref)}
        if (board, p) in J.mate:
            ob, op = J.mate[(board, p)]
            it.update(kind="mate", to=(ob, op), to_net=J.boards[ob].net(op))
        elif TP_RE.fullmatch(ref):
            it["kind"] = "testpoint"
        elif J.mcu and (board, ref) == J.mcu:
            line, io = _mcu_line(J, board, p)
            it.update(kind="mcu", line=line, io=io)
        elif PASSIVE_RE.fullmatch(ref) and len(B.pins(ref)) == 2:
            other = next(q for q in B.pins(ref) if q != p)
            onet = B.net(other)
            it.update(kind="cap" if ref.startswith("C") else "series", value=B.value(ref), to_net=onet,
                      to_ground=J.is_ground(onet))
        elif CONN_RE.fullmatch(ref):
            it["kind"] = "connector"
        else:
            name, rec, doc = _role(J, board, p)
            it.update(kind="part", pin_name=name, rec=rec, doc=doc,
                      documented=bool(J.part_docs.get((board, ref))))
        out.append(it)
    return out


def _fold(items: list[dict]) -> list[dict]:
    """One item per part and pin name: a part with several pins on the net (a MOSFET's drain pins,
    a converter's two VOUT pins) is one line, its pin numbers joined."""
    out, by = [], {}
    for it in items:
        if it["kind"] != "part":
            out.append(it)
            continue
        k = (it["board"], it["ref"], it["pin_name"] if it.get("rec") else None)
        if k in by:
            by[k]["nums"].append(split_key(it["key"])[1])
            continue
        it = dict(it, nums=[split_key(it["key"])[1]])
        by[k] = it
        out.append(it)
    return out


def _part_key(it: dict) -> str:
    return f"{it['board']}:{it['ref']}-{','.join(it.get('nums') or [split_key(it['key'])[1]])}"


def _member_lines(items: list[dict]) -> list[str]:
    lines, caps, tps = [], [], []
    for it in _fold(items):
        k, key, dni = it["kind"], f"{it['board']}:{it['key']}", " DNI" if it.get("dni") else ""
        if k == "cap":
            caps.append(f"{it['ref']} {it.get('value') or '?'}" + ("" if it["to_ground"] else f" to {it['to_net']}") + dni)
        elif k == "testpoint":
            tps.append(it["ref"])
        elif k == "mate":
            ob, op = it["to"]
            lines.append(f"  {key} => {ob}:{op} {ob}:{it['to_net']} (connector mate)")
        elif k == "mcu":
            lines.append(f"  {key} {it['line']}")
        elif k == "series":
            lines.append(f"  {key} {it['ref']} {it.get('value') or '?'}{dni} -> "
                         + ("GND" if it["to_ground"] else f"{it['board']}:{it['to_net']}"))
        elif k == "connector":
            lines.append(f"  {key} connector {it.get('mpn') or ''}".rstrip() + " (no mate declared)")
        else:
            rec, doc, pk = it.get("rec"), it.get("doc"), _part_key(it)
            name = "" if str(it["pin_name"]) in (it.get("nums") or []) else f" {it['pin_name']}"
            if rec:
                role = " ".join(f"{f}={rec[f]}" for f in ("dir", "type", "flow") if rec.get(f))
                lines.append(f"  {pk}{name} {it.get('mpn') or ''}{dni} {role} ({_cite(rec, doc)})")
            elif it.get("documented"):
                lines.append(f"  {pk}{name} {it.get('mpn') or ''}{dni} (no pin record for this pin)")
            else:
                lines.append(f"  {pk}{name} {it.get('mpn') or ''}{dni} role unknown: no datasheet")
    if caps:
        lines.append(f"  caps ({len(caps)}): " + ", ".join(caps))
    if tps:
        lines.append(f"  test points: {', '.join(tps)}")
    return lines


def connected(J: Join, start: list[tuple[str, str]]) -> list[tuple[str, str, str | None]]:
    """The nets joined to `start` by connector mates: (board, net, how it was reached)."""
    out, seen, todo = [], set(), [(b, n, None) for b, n in start]
    while todo:
        b, n, how = todo.pop(0)
        if (b, n) in seen or not n:
            continue
        seen.add((b, n))
        out.append((b, n, how))
        for it in members(J, b, n):
            if it["kind"] == "mate" and it["to_net"]:
                ob, op = it["to"]
                todo.append((ob, it["to_net"], f"{b}:{it['key']} => {ob}:{op}"))
    return out


# ---------------------------------------------------------------- upstream

def _dt_of(J: Join, port: str) -> str:
    uses = [J.dt_use(u) for u in J.uses_by_pin.get(port, [])]
    return f" (DT {'; '.join(u['label'] + '.' + str(u['prop']) for u in uses)})" if uses else " (no DT use)"


def _enables(J: Join, board: str, ref: str) -> list[str]:
    """Lines for the part's enable pins: datasheet pin names, the net each is on, what drives it."""
    out = []
    for key in J.boards[board].pins(ref):
        name, rec, _ = _role(J, board, key)
        toks = [t for t in re.split(r"[^a-z0-9]+", (rec or {}).get("name", "").lower()) if t]
        if not rec or not any(ENABLE_TOKENS.match(t) for t in toks):
            continue
        net = J.boards[board].net(key)
        src = ", ".join(sorted({f"MCU {port}" + (f" via {' / '.join(path)}" if path else "") + _dt_of(J, port)
                                for port, path in J.mcu_ports_of(board, net)}))
        if not src and net:
            found = []
            for it in _fold(members(J, board, net)):
                if it["kind"] == "series" and it["to_net"] and it["to_net"] != net:
                    found.append(f"{it['ref']} {it.get('value') or '?'} to {it['to_net']}")
                elif it["kind"] == "part" and it["ref"] != ref:
                    role = (f"{it['pin_name']} " + " ".join(f"{f}={it['rec'][f]}" for f in ("dir", "type")
                                                          if it['rec'].get(f))) if it.get("rec") else "no datasheet"
                    found.append(f"{_part_key(it)} {it.get('mpn') or ''} {role}".rstrip())
                elif it["kind"] in ("mate", "connector"):
                    found.append(f"{it['board']}:{it['key']} {it['kind']}")
            src = "; ".join(found) if found else "no MCU port, part or pull on it"
        out.append(f"enable {name} on {board}:{net}: {src or 'not connected'}")
    return out


def upstream(J: Join, nets: list[tuple[str, str]], hop: int = 0, seen=None, asks=None,
             came_from: tuple[str, str] | None = None) -> list[str]:
    """What feeds `nets`, walked toward the source. `came_from` is the part whose input this net is,
    left out as a feed so a bidirectional switch is not walked back through."""
    seen = seen if seen is not None else set()
    asks = asks if asks is not None else {}
    pad = "  " * (2 * hop)
    group = [x for x in connected(J, nets) if (x[0], x[1]) not in seen]
    if not group:
        return []
    seen.update((b, n) for b, n, _ in group)
    items = _fold([it for b, n, _ in group for it in members(J, b, n)])
    label = " = ".join(f"{b}:{n}" for b, n, _ in group)
    if J.is_ground(group[0][1]):
        return [f"{pad}{label}: ground"]
    lines = [f"{pad}{label}:"]
    for it in items:
        if it["kind"] == "mcu" and it.get("io"):
            lines.append(f"{pad}  driven or read by {it['board']}:{it['key']} {it['line']}")
    feeds = [it for it in items if it["kind"] == "part" and (it.get("rec") or {}).get("flow") in ("source", "bidir")
             and (it["board"], it["ref"]) != came_from]
    for it in feeds:
        rec, b, ref = it["rec"], it["board"], it["ref"]
        lines.append(f"{pad}  fed by {_part_key(it)} {it.get('mpn') or ''} {it['pin_name']} flow={rec['flow']} "
                     f"({_cite(rec, it['doc'])})")
        for e in _enables(J, b, ref):
            lines.append(f"{pad}    {e}")
        ins: dict[tuple[str, str], str] = {}
        for key in J.boards[b].pins(ref):
            _, r2, _ = _role(J, b, key)
            n2 = J.boards[b].net(key)
            if r2 and r2.get("flow") in ("sink", "bidir") and n2 and r2.get("name") != it["pin_name"]:
                ins.setdefault((b, n2), r2["name"])
        for (b2, n2), pname in ins.items():
            if (b2, n2) in seen:
                lines.append(f"{pad}    input {pname} on {b2}:{n2} (already shown above)")
            elif hop + 1 >= MAX_HOPS:
                lines.append(f"{pad}    input {pname} on {b2}:{n2} (walk limit; trace it on its own)")
            else:
                lines.append(f"{pad}    input {pname}:")
                lines += upstream(J, [(b2, n2)], hop + 1, seen, asks, came_from=(b, ref))
    undocumented = [it for it in items if it["kind"] == "part" and not it.get("documented") and not it.get("dni")]
    for it in undocumented:
        asks.setdefault(it.get("mpn") or "?", set()).add(f"{it['board']}:{it['ref']}")
    if feeds:
        return lines
    refs = sorted({f"{it['board']}:{it['ref']} {it.get('mpn') or ''}".rstrip() for it in undocumented})
    unflowed = [it for it in items if it["kind"] == "part" and it.get("documented") and not it.get("rec")]
    back = any(it["kind"] == "part" and (it["board"], it["ref"]) == came_from
               and (it.get("rec") or {}).get("flow") in ("source", "bidir") for it in items)
    if back:
        lines.append(f"{pad}  fed only back through {came_from[0]}:{came_from[1]}, the part walked in from")
    if refs:
        lines.append(f"{pad}  no documented feed; parts here without a datasheet: " + ", ".join(refs))
    elif not back and not any(it["kind"] == "mcu" and it.get("io") for it in items):
        lines.append(f"{pad}  no part on this net has a pin record with flow source or bidir")
    if unflowed:
        lines.append(f"{pad}  documented, but no pin record for: " + ", ".join(_part_key(it) for it in unflowed))
    for it in items:
        if it["kind"] == "connector":
            lines.append(f"{pad}  input connector {it['board']}:{it['key']} {it.get('mpn') or ''}".rstrip())
    paths = [it for it in items if it["kind"] == "series" and not it["to_ground"] and not it.get("dni")
             and it.get("to_net") and (it["board"], it["to_net"]) not in seen
             and (not it["ref"].startswith("R") or (ohms(it.get("value")) or 1e9) <= SERIES_MAX_OHMS)]
    for it in paths:
        if hop + 1 >= MAX_HOPS:
            lines.append(f"{pad}  through {it['ref']} {it.get('value') or '?'} to {it['board']}:{it['to_net']} "
                         "(walk limit)")
            continue
        lines.append(f"{pad}  through {it['ref']} {it.get('value') or '?'} (series; direction not stated):")
        lines += upstream(J, [(it["board"], it["to_net"])], hop + 1, seen, asks)
    return lines


# ---------------------------------------------------------------- the answer

def answer(J: Join, query: str, board: str | None = None) -> tuple[str, str]:
    """(src header text, body) for net_trace."""
    r = resolve(J, query, board)
    src = "boards/" + ",".join(sorted(J.boards)) + " netlist.json" + (" + connector mates" if J.mate else "")
    if r["kind"] == "none":
        near = (" Nearest names: " + ", ".join(r["close"]) + ".") if r["close"] else ""
        return "", f"net, part or part pin {query!r} not found on any board.{near}"
    if r["kind"] == "part":
        b, ref = r["board"], r["ref"]
        B = J.boards[b]
        docs = J.part_docs.get((b, ref))
        lines = [f"{b}:{ref} {B.mpn(ref) or ''} {B.value(ref) or ''}".rstrip() + (" DNI" if B.dni(ref) else "")
                 + (f" datasheet {', '.join(docs)}" if docs else " no datasheet: role of every pin unknown"), ""]
        for key in B.pins(ref):
            name, rec, doc = _role(J, b, key)
            role = " ".join(f"{f}={rec[f]}" for f in ("dir", "type", "flow") if rec and rec.get(f))
            lines.append(f"  {b}:{key} {name} -> {b}:{B.net(key)}" + (f" {role} ({_cite(rec, doc)})" if rec else ""))
        lines += ["", f"Trace one with net_trace {b}:<net> or {b}:{ref}-<pin>."]
        return src, "\n".join(lines)
    if r["kind"] == "pin":
        b = r["board"]
        start = [(b, J.boards[b].net(k)) for k in r["keys"] if J.boards[b].net(k)]
        head = ", ".join(f"{b}:{k} {_role(J, b, k)[0]} is on {b}:{J.boards[b].net(k)}" for k in r["keys"])
        if not start:
            return src, f"{head}\n(no net)"
    else:
        start, head = r["nets"], ""
    lines = [head, ""] if head else []
    for b, n, how in connected(J, start):
        pins = len(J.boards[b].net_pins(n))
        lines.append(f"{b}:{n} ({pins} pins" + (", rail" if J.is_rail(b, n) else "")
                     + (f", reached through {how}" if how else "") + ")")
        lines += _member_lines(members(J, b, n))
    asks: dict[str, set[str]] = {}
    up = upstream(J, start, asks=asks)
    if up:
        lines += ["", "## Upstream (feeds come only from datasheet pin records with flow)"] + up
    if asks:
        lines += ["", "## Datasheets needed (role unknown until the engineer supplies them; "
                  "inventory/missing.md has the links)"]
        lines += [f"- {mpn}: {', '.join(sorted(refs))}" for mpn, refs in sorted(asks.items())]
    return src, "\n".join(lines)

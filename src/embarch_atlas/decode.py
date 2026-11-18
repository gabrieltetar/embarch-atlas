"""`q decode`: the reply frame of a command-then-read part, decoded against its datasheet records.

A part with no register map (firmware sends a command, waits, reads a fixed frame) has nothing for `reg_read` to
decode. Its datasheet records are one `frame` (the bytes, each status bit and the count, by position) and one or
more `transfer` records (the count's conversion to the physical value). The range and unit a board chooses come
from the part's devicetree node, as `dt:<property>` parameters of the transfer record. Nothing is sent to the
board: the engineer already read the bytes.

A count outside the transfer function's range is reported as the number the equation gives and said to be out of
range, never clamped: that is what a driver's own reading hides.
"""
from __future__ import annotations

import re
from typing import Any

PARAMS = ("out_min", "out_max", "phys_min", "phys_max")


def parse_bytes(text: str) -> list[int]:
    """`0x1A 2b`, `1a,2b,3c` or `1a2b3c` as a list of bytes; ValueError names what is wrong."""
    toks = [t for t in re.split(r"[\s,:;]+", text.strip()) if t]
    out: list[int] = []
    for t in toks:
        t = t[2:] if t.lower().startswith("0x") else t
        if not re.fullmatch(r"[0-9a-fA-F]+", t):
            raise ValueError(f"{t!r} is not hex")
        if len(t) % 2:
            if len(toks) > 1:
                t = "0" + t                      # one token per byte: `0x5` is 0x05
            else:
                raise ValueError("an odd number of hex digits")
        out += [int(t[i:i + 2], 16) for i in range(0, len(t), 2)]
    if not out:
        raise ValueError("no bytes given")
    return out


def _span(f: dict) -> tuple[int, int]:
    b = f.get("byte")
    return (b[0], b[-1]) if isinstance(b, list) else (b, b)


def field_value(frame: list[int], f: dict) -> int:
    lo_b, hi_b = _span(f)
    v = 0
    for x in frame[lo_b:hi_b + 1]:
        v = (v << 8) | x
    bits = f.get("bits")
    if bits:
        hi, lo = max(bits), min(bits)
        v = (v >> lo) & ((1 << (hi - lo + 1)) - 1)
    return v


def _num(x: Any) -> float | None:
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        try:
            return float(int(x, 0))
        except ValueError:
            try:
                return float(x)
            except ValueError:
                return None
    return None


def _prop(props: dict, name: str) -> Any:
    p = (props or {}).get(name)
    if p is None:
        return None
    v = p.get("val") if isinstance(p, dict) else p
    return v[0] if isinstance(v, list) and len(v) == 1 else v


def frame_problem(rec: dict, size: int) -> str | None:
    cnt = [f for f in rec.get("fields") or [] if f.get("role") == "count"]
    if len(cnt) != 1:
        return f"its fields name {len(cnt)} counts, not one"
    for f in rec.get("fields") or []:
        lo, hi = _span(f)
        if not (isinstance(lo, int) and isinstance(hi, int) and 0 <= lo <= hi < rec.get("size", 0)):
            return f"field {f.get('name')!r} lies outside its {rec.get('size')} bytes"
    return None


def select_transfer(frame: dict, transfers: list[dict], props: dict) -> tuple[dict | None, str]:
    """The transfer record the node selects, else why none: what the node must say, or what the records lack."""
    mine = [t for t in transfers if t.get("frame") == frame.get("name")]
    if not mine:
        return None, f"no `transfer` record for frame {frame.get('name')!r}: the datasheet's transfer function is not distilled"
    if len(mine) == 1 and not mine[0].get("when"):
        return mine[0], ""
    asks = sorted({(t.get("when") or {}).get("prop") for t in mine if t.get("when")} - {None})
    hits = []
    for t in mine:
        w = t.get("when") or {}
        if w.get("prop") is not None and _prop(props, w["prop"]) is not None \
                and str(_prop(props, w["prop"])) == str(w.get("value")):
            hits.append(t)
    if len(hits) == 1:
        return hits[0], ""
    variants = ", ".join(f"{t.get('variant') or '?'} (when {(t.get('when') or {}).get('prop')} = {(t.get('when') or {}).get('value')})"
                         for t in mine)
    if not asks:
        return None, f"the transfer records of {frame.get('name')!r} are {len(mine)} and none says which node property selects one"
    if not hits:
        return None, (f"the node sets none of the properties that select a transfer function ({', '.join(asks)}); "
                      f"the datasheet's variants are {variants}")
    return None, f"the node matches {len(hits)} transfer variants of {frame.get('name')!r}: {variants}"


def resolve_params(t: dict, props: dict) -> tuple[dict[str, Any], list[str]]:
    """The transfer record's parameters with `dt:<prop>` read from the node; the list names what is missing."""
    got: dict[str, Any] = {}
    missing: list[str] = []
    p = t.get("params") or {}
    for k in (*PARAMS, "unit"):
        v = p.get(k)
        if k == "unit" and v is None:
            got[k] = ""
            continue
        if isinstance(v, str) and v.startswith("dt:"):
            raw = _prop(props, v[3:])
            if raw is None:
                missing.append(f"{k}: devicetree property `{v[3:]}` is not set on the node")
                continue
            v = raw
        if k == "unit":
            got[k] = str(v)
            continue
        n = _num(v)
        if n is None:
            missing.append(f"{k}: the transfer record gives no number" if v is None else f"{k}: {v!r} is not a number")
        else:
            got[k] = n
    return got, missing


def _fmt(x: float) -> str:
    return str(int(x)) if x == int(x) and abs(x) < 1e15 else f"{x:.6g}"


def convert(count: int, pr: dict[str, Any]) -> tuple[float, str | None]:
    """The linear transfer function's value and, when the count is outside [out_min, out_max], which side."""
    span = pr["out_max"] - pr["out_min"]
    value = (count - pr["out_min"]) * (pr["phys_max"] - pr["phys_min"]) / span + pr["phys_min"]
    side = "below the minimum" if count < pr["out_min"] else "above the maximum" if count > pr["out_max"] else None
    return value, side

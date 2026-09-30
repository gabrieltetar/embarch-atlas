"""Error injection for `doc check` (ported from the datasheet-distillation spike's mutate.py).

Each mutation copies one limit/timing record and injects one error of a class the table-extraction
literature reports for LLMs: a column slip, a dropped value, a typo, a sign flip, a self-consistent
hallucination (value and quote rewritten together), a unit prefix, a page shift, a changed condition.
A mutation counts as caught when any record check on that record, or any invariant, changes from the
unmutated baseline.
"""
from __future__ import annotations

import copy
import random
import re

from embarch_atlas import check as ck

PREFIX = {"V": ["mV", "kV"], "mV": ["V", "µV"], "A": ["mA"], "mA": ["A", "µA"], "µA": ["mA", "nA"], "uA": ["mA", "nA"],
          "nA": ["µA"], "ms": ["µs", "s"], "µs": ["ms", "ns"], "us": ["ms", "ns"], "ns": ["µs"], "kHz": ["MHz", "Hz"],
          "mΩ": ["Ω"], "kΩ": ["Ω", "MΩ"], "MΩ": ["kΩ"], "µF": ["nF", "mF"], "pF": ["nF"], "µH": ["nH"], "W": ["mW"]}
CLASSES = ("column_slip", "drop_value", "value_typo", "sign_flip", "value_and_quote", "unit_scale", "page_shift",
           "cond_change")


def numeric_keys(r: dict) -> list[str]:
    return [k for k in ("min", "typ", "max", "nom") if ck.isnum(r.get(k))]


def mutate(r: dict, cls: str, rnd: random.Random) -> dict | None:
    r = copy.deepcopy(r)
    ks = numeric_keys(r)
    if not ks:
        return None
    if cls == "column_slip":
        k = rnd.choice(ks)
        free = [c for c in ("min", "typ", "max") if c not in r and c != k]
        if not free:
            return None
        r[rnd.choice(free)] = r.pop(k)
    elif cls == "value_typo":
        k = rnd.choice(ks)
        v = r[k]
        r[k] = rnd.choice([v * 10, v / 10, round(v + (1 if v == int(v) else 0.1), 3)])
    elif cls == "value_and_quote":
        k = rnd.choice(ks)
        v = r[k]
        nv = round(v * 1.1 + (1 if abs(v) < 1 else 0), 3)
        sv = "%g" % v
        if not isinstance(r.get("q"), str) or sv not in r["q"]:
            return None
        r["q"] = r["q"].replace(sv, "%g" % nv, 1)
        r[k] = nv
    elif cls == "unit_scale":
        u = r.get("unit")
        if u not in PREFIX:
            return None
        r["unit"] = rnd.choice(PREFIX[u])
    elif cls == "page_shift":
        if not isinstance(r["p"], int):
            return None
        r["p"] = r["p"] + rnd.choice([-1, 1])
        if r["p"] < 1:
            r["p"] = 2
    elif cls == "cond_change":
        c = r.get("cond")
        if not isinstance(c, str) or not re.search(r"\d", c):
            return None
        m = list(re.finditer(r"\d+(?:\.\d+)?", c))
        x = rnd.choice(m)
        r["cond"] = c[:x.start()] + str(int(float(x.group()) * 3 + 7)) + c[x.end():]
    elif cls == "sign_flip":
        cand = [k for k in ks if r[k] != 0]
        if not cand:
            return None
        k = rnd.choice(cand)
        r[k] = -r[k]
    elif cls == "drop_value":
        if len(ks) < 2:
            return None
        r.pop(rnd.choice(ks))
    return r


def _flags(doc: ck.DocText, rec: dict) -> set[str]:
    res = ck.record_checks(doc, rec)
    return {k for k, v in res.items() if ck._is_fail(k, v)}


def run(doc: ck.DocText, recs: list[dict], checks: list[dict], n: int = 40, seed: int = 1) -> dict[str, dict]:
    """Detection table: class -> {tried, caught, rate, by}."""
    rnd = random.Random(seed)
    pool_all = [r for r in recs if r.get("kind") in ("limit", "timing") and r.get("q") and numeric_keys(r)]
    base = {r["id"]: _flags(doc, r) for r in pool_all}
    by_id = {r["id"]: r for r in recs}
    base_x = [(x["id"], x["got"]) for x in ck.check_invariants(by_id, checks)]
    out = {}
    for cls in CLASSES:
        pool = [r for r in pool_all if cls not in ("column_slip", "drop_value")
                or (r.get("src") == "table" and r.get("group") in ck.K_GROUPS)]
        tried = caught = 0
        by: dict[str, int] = {}
        for _ in range(n * 6):
            if tried >= n or not pool:
                break
            m = mutate(rnd.choice(pool), cls, rnd)
            if m is None:
                continue
            tried += 1
            flags = _flags(doc, m) - base[m["id"]]
            xs = [(x["id"], x["got"]) for x in ck.check_invariants(dict(by_id, **{m["id"]: m}), checks)]
            if xs != base_x:
                flags |= {"X"}
            if flags:
                caught += 1
                for f in flags:
                    by[f] = by.get(f, 0) + 1
        out[cls] = {"tried": tried, "caught": caught, "rate": caught / tried if tried else None, "by": by}
    return out

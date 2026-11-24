"""The measured plane, read: a `via: live` fact shown beside the claim it bears on, and compared with it.

`measured.py` writes `<atlas dir>/measured.jsonl`; this module reads it. Two uses, one rule for both:

- the lookups that return a claim (`register`, `find_fact`, `part_card`, so the CLI and MCP alike) append the
  measurements whose `fact` is that claim's record id: value, `via`, the receipt's time and image, the flag when
  the finding was made on a different build, and the words "development evidence" (a `via: live` fact cannot be
  cited from the atlas);
- the mismatch report's check "measured values vs the datasheet" (`run_join`) compares each measurement with its
  claim and raises a `measured-vs-datasheet` finding where they disagree. A value that agrees raises none and is
  still shown beside the claim.

What is compared: a `register` or `field` record's `reset` against the value read (numerically: `0x`, `0b` and
decimal all parse); a record with `min` and/or `max` (a `limit` or `timing`) against the value, inside the range
being agreement (an open side is open). A record with only `typ` or `nom`, a claim or value that is not a number,
or two units that differ is shown and not compared, and the check says which. An `entity` measurement bears on no
record, so nothing shows it beside a claim and nothing compares it.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from . import common, measured

CLS = "measured-vs-datasheet"
CHECK = "measured values vs the datasheet"
SETTLE = ("repeat the read on the pinned build and re-read the cited datasheet page; a `via: live` value is "
          "development evidence, so the datasheet stands until the engineer settles which one is wrong")
EVIDENCE = "development evidence"
AGREES, DISAGREES, NOT_COMPARED = "agrees", "DISAGREES", "not compared"


def number(v: Any) -> float | None:
    """A number from a value or a printed one (`0x1F`, `0b101`, `3.3`); None when it is no number."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v if v is not None else "").strip().lower().replace("_", "")
    try:
        if s.startswith(("0x", "-0x")):
            return float(int(s, 16))
        if s.startswith(("0b", "-0b")):
            return float(int(s, 2))
        return float(s)
    except ValueError:
        return None


def load(ad: Path) -> dict[str, list[dict]]:
    """The measurements that bear on a fact, by fact id, in the order written."""
    out: dict[str, list[dict]] = {}
    try:
        recs = measured.load(ad)
    except (OSError, ValueError):
        return out
    for r in recs:
        if isinstance(r, dict) and r.get("kind") == "measurement" and r.get("fact"):
            out.setdefault(str(r["fact"]), []).append(r)
    return out


def compare(claim: dict, m: dict) -> tuple[str, str]:
    """`(state, claim as printed)` for one measurement against its claim record."""
    val = number(m.get("value"))
    cu, mu = claim.get("unit"), m.get("unit")
    if cu and mu and str(cu).strip() != str(mu).strip():
        return NOT_COMPARED, f"claim is in {cu}, measured in {mu}"
    if claim.get("kind") in ("register", "field") and claim.get("reset") not in (None, ""):
        want = number(claim["reset"])
        said = f"reset {claim['reset']}"
        if want is None or val is None:
            return NOT_COMPARED, said
        return (AGREES if want == val else DISAGREES), said
    lo, hi = number(claim.get("min")), number(claim.get("max"))
    if lo is not None or hi is not None:
        said = f"{claim.get('min', '')}..{claim.get('max', '')}".strip() + (f" {cu}" if cu else "")
        if val is None:
            return NOT_COMPARED, said
        ok = (lo is None or val >= lo) and (hi is None or val <= hi)
        return (AGREES if ok else DISAGREES), said
    return NOT_COMPARED, "the claim states no reset, min or max to compare"


def _receipt(m: dict) -> str:
    r = m.get("receipt") or {}
    img = r.get("image") or {}
    seqs = ", ".join(f"#{c.get('seq')}" for c in r.get("calls") or [])
    return (f"receipt {r.get('time') or 'no time'}, image {img.get('source') or 'unknown'} ({img.get('sha256') or 'unknown'})"
            + (f", Core entries {seqs}" if seqs else ""))


def line(m: dict, claim: dict | None = None) -> str:
    """One measurement as a line: value, via, receipt, flag, the words development evidence, and, given the
    claim record, whether the value agrees with it."""
    val = f"{m.get('value')}" + (f" {m['unit']}" if m.get("unit") else "")
    out = f"- measured {val} via {m.get('via')} ({EVIDENCE}, not citable); {_receipt(m)}"
    if m.get("flag"):
        out += f"; FLAG: {m['flag']}"
    if claim is not None:
        state, said = compare(claim, m)
        out += f"; against the datasheet ({said}): {state}"
    if m.get("note"):
        out += f"; note: {m['note']}"
    return out + f" [{m.get('id')}]"


def lines_for(by_fact: dict[str, list[dict]], fact_id: str | None, claim: dict | None) -> list[str]:
    return [line(m, claim) for m in by_fact.get(str(fact_id), [])] if fact_id else []


def section(by_fact: dict[str, list[dict]], claims: Iterable[dict], title: str = "Measured") -> list[str]:
    """A `## <title> (development evidence)` block for the claims that have measurements, [] when none."""
    body = []
    for c in claims:
        for ln in lines_for(by_fact, c.get("id"), c):
            body.append(f"{c.get('id')}\n  {ln}")
    return ["", f"## {title} ({EVIDENCE})"] + body if body else []


def run_join(J) -> dict:
    """The check `measured values vs the datasheet` over `<atlas dir>/measured.jsonl`: `ran`/`passed` counts, the
    findings for a disagreement, and what was shown and not compared (`folded`)."""
    by = load(J.ad)
    out: dict = {"check": CHECK, "ran": 0, "passed": 0, "findings": [], "folded": {}}
    if not by:
        return out
    claims = {r.get("id"): (dname, r) for dname, d in J.docs.docs.items()
              for rs in (d.get("records") or {}).values() for r in rs if r.get("id") in by}
    for fid, ms in by.items():
        if fid not in claims:
            out["folded"].setdefault("measured fact whose record no document holds now", []).append(fid)
            continue
        dname, claim = claims[fid]
        cite = f"{claim.get('doc') or dname} p{claim.get('p')}" if claim.get("p") else str(claim.get("doc") or dname)
        for m in ms:
            state, said = compare(claim, m)
            if state == NOT_COMPARED:
                out["folded"].setdefault("measured value shown beside its claim, not compared", []).append(f"{m['id']}: {said}")
                continue
            out["ran"] += 1
            if state == AGREES:
                out["passed"] += 1
                continue
            val = f"{m.get('value')}" + (f" {m['unit']}" if m.get("unit") else "")
            other = f"; {m['flag']}" if m.get("flag") else ""
            out["findings"].append({
                "class": CLS, "key": fid, "check": CHECK, "severity": "check", "settle": SETTLE,
                "summary": f"{fid}: measured {val} via {m.get('via')} ({EVIDENCE}) where the datasheet has {said}{other}",
                "evidence": [f"measured {m['id']} in {measured.FILE}: {_receipt(m)}", f"datasheet {cite}"]})
    return out

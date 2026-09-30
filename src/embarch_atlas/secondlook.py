"""`second-look queue <atlas>` / `second-look record <atlas> <verdicts.jsonl>`.

The second, independent look happens when an atlas is built, on the facts that
atlas uses (owner decision). A fact is *used* when it is a record of a document
whose part is on the target's boards (by MPN), or of an MCU document (reference
manual, errata, the MCU datasheet) for a peripheral in use. It *needs* a second
look when an LLM wrote it and it is flagged or high-stakes: figure-only,
`llm-unverified`, a unit that is not in its quoted row (units printed once per
row group), a condition-bearing limit, any limit or timing, a quirk, register
semantics, or a strap, passthrough or pin fact the join reads.

The queue carries what to extract and the pages to view, never the first-pass
value. A verdict, once recorded, is frozen in `cache/llm/` under a key over the
record's content and its pages' text, so the next atlas reuses it and only new
or changed facts are queued.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
import sys
from pathlib import Path

from embarch_atlas.common import read_json, read_jsonl, write_json, write_jsonl

VERSION = "second-look/1"
VERDICTS = ("confirmed", "conflict", "unresolved")
SKIP_PROV = ("deterministic", "deterministic+crosschecked", "engineer-declared", "engineer-confirmed", "measured")
CORE = ("RCC", "PWR", "EXTI", "GPIO", "DMA", "DMAMUX", "NVIC", "SYSCFG", "FLASH")
UNIT_ALIASES = {"µ": ("µ", "u", "μ"), "Ω": ("Ω", "ohm", "Ohm")}


def _pages(r: dict) -> list[int]:
    p = r.get("p")
    if isinstance(p, list):
        return [int(x) for x in p if isinstance(x, (int, str)) and str(x).isdigit()]
    return [int(p)] if isinstance(p, (int, str)) and str(p).isdigit() else []


def _unit_in(unit: str, q: str) -> bool:
    variants = {unit}
    for k, alts in UNIT_ALIASES.items():
        if k in unit:
            variants |= {unit.replace(k, a) for a in alts}
    qn = q.replace(" ", "")
    return any(v.replace(" ", "") in qn for v in variants)


def reasons(r: dict, join_kinds: set[str]) -> list[str]:
    """Why this record needs a second look ([] when it does not)."""
    if r.get("prov") in SKIP_PROV:
        return []
    why = []
    kind = r.get("kind")
    if r.get("src") == "figure":
        why.append("figure-only")
    if r.get("prov") == "llm-unverified":
        why.append("llm-unverified")
    if kind in ("limit", "timing"):
        why.append(kind)
        unit, q = r.get("unit"), r.get("q") or ""
        if isinstance(q, list):  # a quote split over several lines (DESIGN.md: q may be a list)
            q = " ".join(str(x) for x in q)
        if unit and q and not _unit_in(str(unit), q):
            why.append("unit not in the quoted row (unit-prefix risk)")
        if r.get("cond"):
            why.append("condition-bearing")
    if kind == "quirk":
        why.append("quirk")
    if kind in ("register", "field"):
        why.append("register semantics")
    if kind in join_kinds:
        why.append(f"{kind} fact read by the join")
    return why


def ask(r: dict) -> str:
    """What the fresh reader extracts: the question, never the first-pass answer."""
    k = r.get("kind")
    if k in ("limit", "timing"):
        what = " ".join(x for x in (r.get("param"), f"({r['sym']})" if r.get("sym") else None) if x) or r.get("id")
        cond = f" under the condition '{r['cond']}'" if r.get("cond") else ""
        return f"{what}{cond}: min / typ / max with the unit as printed, and the verbatim row"
    if k == "field":
        return f"register {r.get('reg')} field {r.get('name')}: bit positions, reset value, access, and what each value means"
    if k == "register":
        return f"register {r.get('name')}: address/offset, width, reset value, access"
    if k == "quirk":
        return f"check this claim against the page, quote the text that decides it: {r.get('text')}"
    if k == "strap":
        return f"the device address (or mode) when pin {r.get('pin')} is {r.get('condition')}"
    if k == "passthrough":
        return f"is {r.get('from_pin')} connected through to {r.get('to_pin')}" + (f" when {r['when']}" if r.get("when") else "")
    if k == "figure":   # the caption only: `what` is the first reader's description, values included
        return f"figure {r.get('fig') or ''} ({r.get('caption') or 'no caption'}): what it shows, every value and unit on it".replace("  ", " ")
    if k == "pin":
        io = ", I/O structure as printed (FT, FT_fa, TT_a, RST...)" if r.get("io") else ""
        return f"pin {r.get('pin')}: name, direction, type (open-drain / push-pull / analog / supply), active level{io}"
    if k == "rule":
        return f"the behaviour rule on this page about: {r.get('cond') or (r.get('text') or '')[:80]}"
    return f"re-read record {r.get('id')}"


def _page_text(doc_dir: Path, p: int) -> str | None:
    f = doc_dir / "pages" / f"p{p:03d}.txt"
    if not f.is_file():
        f = doc_dir / "pages" / f"p{p}.txt"
    return f.read_text(encoding="utf-8", errors="replace") if f.is_file() else None


def content_key(r: dict, doc_dir: Path) -> str:
    """sha256 over the record (minus provenance and notes) and the text of its pages."""
    body = {k: v for k, v in r.items() if k not in ("prov", "note", "verdict", "second_look")}
    h = hashlib.sha256()
    h.update(VERSION.encode())
    h.update(json.dumps(body, sort_keys=True, ensure_ascii=False).encode())
    for p in _pages(r):
        t = _page_text(doc_dir, p)
        h.update(f"|p{p}:".encode())
        h.update(hashlib.sha256((t or "").encode()).digest())
    return h.hexdigest()


def cache_path(ad: Path, key: str) -> Path:
    return ad / "cache" / "llm" / f"sl-{key}.json"


def _pdf(ad: Path, doc: dict) -> str | None:
    """The source PDF for `Read(pages=)`: doc.json `path` (relative to the atlas dir), else `file` in sources."""
    if doc.get("path"):
        p = Path(doc["path"])
        p = p if p.is_absolute() else (ad / p).resolve()
        if p.exists():
            return str(p)
    f = doc.get("file")
    if not f:
        return None
    p = Path(f)
    if p.is_absolute():
        return str(p)
    try:
        from embarch_atlas.project import load_config, sources_dir
        src = sources_dir(ad, load_config(ad))
        if (src / p).exists():
            return str(src / p)
    except Exception:  # noqa: BLE001  (no atlas.toml: fall back to the manifest-relative guess)
        pass
    return str(ad.parent / "hardware" / p) if (ad.parent / "hardware" / p).exists() else f


def _family(name: str) -> str:
    f = re.sub(r"\d+$", "", name)
    m = re.fullmatch(r"(GPIO)([A-Z])", f)
    return m[1] if m else f


def mcu_record_used(r: dict, used_periph: set[str], chapter_pages: list[tuple[int, int]]) -> bool:
    ps = _pages(r)
    if chapter_pages and ps and any(lo <= p <= hi for p in ps for lo, hi in chapter_pages):
        return True
    toks = set(re.findall(r"[A-Za-z][A-Za-z0-9]*", " ".join(str(r.get(k) or "") for k in ("id", "name", "reg", "sym"))))
    toks = {t.upper() for t in toks}
    return bool(toks & used_periph) or bool({_family(t) for t in toks} & {_family(p) for p in used_periph})


# How much a wrong reading would hurt, per reason: facts the join reads feed every pin and
# identity check; figure and unverified facts have no deterministic check at all.
WEIGHTS = [("strap fact read by the join", 100), ("pin fact read by the join", 80), ("figure-only", 60),
           ("llm-unverified", 50), ("unit not in the quoted row", 40), ("quirk", 30), ("register semantics", 20),
           ("condition-bearing", 15), ("limit", 10), ("timing", 10)]


def priority(why: list[str]) -> int:
    """Rank a queue item: the heaviest reason, plus 5 per extra reason."""
    ws = [w for reason in why for key, w in WEIGHTS if reason.startswith(key)]
    return (max(ws) if ws else 1) + 5 * max(0, len(why) - 1)


def batches(queue: list[dict], top: int, per_batch: int = 8) -> list[dict]:
    """The `top` highest-priority items, grouped by (document, pages) so one fresh subagent reads
    each page once. Order: the batch holding the highest priority first."""
    groups: dict[tuple, list[dict]] = {}
    for it in queue[:top]:
        groups.setdefault((it["doc"], tuple(it["p"] or [])), []).append(it)
    out = []
    for (doc, pages), items in groups.items():
        for i in range(0, len(items), per_batch):
            chunk = items[i:i + per_batch]
            out.append({"doc": doc, "pages": list(pages), "pdf": chunk[0].get("pdf"),
                        "priority": max(x["priority"] for x in chunk),
                        "items": [{"id": x["id"], "ask": x["ask"], "key": x["key"]} for x in chunk]})
    out.sort(key=lambda b: (-b["priority"], b["doc"], b["pages"]))
    return out


def select(queue: list[dict], why: str | None = None, doc: str | None = None) -> list[dict]:
    """The queue items a round targets, rank kept: `why` is comma-separated reason substrings (any
    matches, `llm-unverified,unit`), `doc` comma-separated doc ids."""
    ws = [w.strip() for w in (why or "").split(",") if w.strip()]
    ds = {d.strip() for d in (doc or "").split(",") if d.strip()}
    return [q for q in queue if (not ws or any(w in r for r in q["why"] for w in ws))
            and (not ds or q["doc"] in ds)]


def build_queue(ad: Path, atlas: Path) -> tuple[list[dict], dict]:
    from embarch_atlas.join import Docs
    stamp = read_json(atlas / "atlas.json")
    pins = read_json(atlas / "pins.json") if (atlas / "pins.json").is_file() else {}
    in_use = read_json(atlas / "in_use.json") if (atlas / "in_use.json").is_file() else {}
    docs = Docs(ad)
    part_docs = {d for ds in (pins.get("part_docs") or {}).values() for d in ds}
    mcu_key = (pins.get("mcu") or {}).get("key")
    mcu_docs = set((pins.get("part_docs") or {}).get(mcu_key, [])) if mcu_key else set()
    for d, v in docs.docs.items():
        if (v["doc"].get("kind") or "") in ("reference-manual", "errata"):
            mcu_docs.add(d)
    used_periph = {n.upper() for n, p in (in_use.get("peripherals") or {}).items()
                   if p.get("instantiated") or p.get("touched_by_code")} | set(CORE)
    queue, stats = [], {"used_records": 0, "needs_look": 0, "frozen": 0, "by_reason": {}, "docs": {}}
    for d, v in sorted(docs.docs.items()):
        is_mcu = d in mcu_docs
        if d not in part_docs and not is_mcu:
            continue
        chapter_pages = []
        wl = v["dir"] / "worklist.json"
        if is_mcu and wl.is_file():
            try:
                chapter_pages = [tuple(c["pages"]) for c in read_json(wl).get("chapters", [])]
            except (OSError, ValueError, KeyError, TypeError):
                chapter_pages = []
        pdf = _pdf(ad, v["doc"])
        for kind, recs in sorted(v["records"].items()):
            for r in recs:
                if is_mcu and d not in part_docs and not mcu_record_used(r, used_periph, chapter_pages):
                    continue
                stats["used_records"] += 1
                why = reasons(r, {"strap", "passthrough", "pin"} if d in part_docs else set())
                if not why:
                    continue
                stats["needs_look"] += 1
                key = content_key(r, v["dir"])
                if cache_path(ad, key).is_file():
                    stats["frozen"] += 1
                    continue
                for w in why:
                    stats["by_reason"][w] = stats["by_reason"].get(w, 0) + 1
                stats["docs"][d] = stats["docs"].get(d, 0) + 1
                queue.append({"id": r.get("id"), "doc": r.get("doc") or d, "kind": r.get("kind") or kind,
                              "p": _pages(r), "pdf": pdf, "src": r.get("src"), "why": why, "ask": ask(r), "key": key})
    for q in queue:
        q["priority"] = priority(q["why"])
    queue.sort(key=lambda q: (-q["priority"], q["doc"], (q["p"] or [0])[0], str(q["id"])))
    stats["queued"] = len(queue)
    stats["atlas"] = stamp["id"]
    return queue, stats


def cmd_queue(args) -> int:
    from embarch_atlas.build import resolve_atlas, resolve_atlas_dir
    ad = resolve_atlas_dir(args)
    atlas = resolve_atlas(ad, args.atlas)
    q, st = build_queue(ad, atlas)
    write_jsonl(atlas / "second_look_queue.jsonl", q)
    if getattr(args, "top", None):
        bs = batches(select(q, getattr(args, "why", None), getattr(args, "doc", None)), args.top)
        write_json(atlas / "second_look_batches.json", bs)
        print(f"top {min(args.top, len(q))} items in {len(bs)} page batches -> {atlas / 'second_look_batches.json'}")
    pages = {}
    for it in q:
        pages.setdefault(it["doc"], set()).update(it["p"])
    print(f"{st['atlas']}: {st['used_records']} records used, {st['needs_look']} need a second look, "
          f"{st['frozen']} reuse a frozen verdict, {st['queued']} queued -> {atlas / 'second_look_queue.jsonl'}")
    for w, n in sorted(st["by_reason"].items(), key=lambda x: -x[1]):
        print(f"  {n:4} {w}")
    for d, ps in sorted(pages.items()):
        print(f"  {d}: pages {','.join(str(p) for p in sorted(ps))}")
    return 0


# ---------------------------------------------------------------- record

def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def validate(v: dict, queued: dict[str, dict]) -> list[str]:
    errs = []
    if not v.get("id"):
        errs.append("missing id")
    elif str(v["id"]) not in queued:
        errs.append(f"id {v['id']!r} is not in second_look_queue.jsonl")
    if v.get("verdict") not in VERDICTS:
        errs.append(f"verdict {v.get('verdict')!r} not one of {VERDICTS}")
    if v.get("reader") != "second-look":
        errs.append("reader must be 'second-look'")
    p = v.get("p")
    if p is not None and not (isinstance(p, int) or (isinstance(p, list) and all(isinstance(x, int) for x in p))):
        errs.append("p must be an int or a list of ints")
    it = queued.get(str(v.get("id")))
    if v.get("verdict") in ("confirmed", "conflict") and it and it.get("src") != "figure" and not v.get("q"):
        errs.append("q (a verbatim quote) is required unless the fact is figure-only or unresolved")
    return errs


def record(ad: Path, atlas: Path, verdicts_path: Path) -> dict:
    from embarch_atlas.join import Docs
    qf = atlas / "second_look_queue.jsonl"
    if not qf.is_file():
        raise SystemExit(f"{qf} missing: run `second-look queue` first")
    queued = {str(q["id"]): q for q in read_jsonl(qf)}
    rows = list(read_jsonl(verdicts_path))
    errors = []
    for i, v in enumerate(rows, 1):
        errors += [f"{verdicts_path}:{i}: {e}" for e in validate(v, queued)]
    if errors:
        raise SystemExit("verdicts rejected, nothing recorded:\n  " + "\n  ".join(errors))
    docs = Docs(ad)
    out_f = atlas / "second_look.jsonl"
    have = {(e.get("id"), e.get("key")) for e in read_jsonl(out_f)} if out_f.is_file() else set()
    added, warnings = [], []
    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for v in rows:
        it = queued[str(v["id"])]
        if (it["id"], it["key"]) in have:
            continue
        ddir = docs.docs.get(it["doc"], {}).get("dir")
        pages = v["p"] if isinstance(v.get("p"), list) else ([v["p"]] if v.get("p") else it["p"])
        q_on_page = None
        if v.get("q") and ddir:
            texts = [t for t in (_page_text(ddir, p) for p in pages) if t]
            if texts:
                q_on_page = _norm_ws(v["q"]) in _norm_ws(" ".join(texts))
                if not q_on_page and it.get("src") != "figure":
                    warnings.append(f"{it['id']}: the second-look quote is not on p{pages} (paraphrase?)")
        first = next((r for r in docs.docs.get(it["doc"], {}).get("records", {}).get(it["kind"], []) if r.get("id") == it["id"]), {})
        entry = {**{k: v.get(k) for k in ("id", "verdict", "value", "unit", "cond", "q", "p", "reader", "why")},
                 "doc": it["doc"], "kind": it["kind"], "key": it["key"], "q_on_page": q_on_page, "recorded": now,
                 "first": {k: first.get(k) for k in ("min", "typ", "max", "nom", "unit", "cond", "reset", "value", "text")
                           if first.get(k) is not None}}
        added.append(entry)
        write_json(cache_path(ad, it["key"]), {"kind": "second-look", "version": VERSION, **entry})
    if added:
        with open(out_f, "a", encoding="utf-8") as f:
            for e in added:
                f.write(json.dumps(e, sort_keys=True, ensure_ascii=False) + "\n")
    return {"added": len(added), "skipped": len(rows) - len(added), "warnings": warnings,
            "by_verdict": {k: sum(1 for e in added if e["verdict"] == k) for k in VERDICTS}}


# ---------------------------------------------------------------- blind readers

READER_RULES = """You are a second, independent reader of a hardware datasheet. You have NOT seen anyone
else's reading and must not guess what it was. For each item, open the page(s) with the Read
tool (`pages=`), find the fact, and report what the page itself shows. Copy numbers and units
exactly as printed. If the page does not show the fact, say so (found: false): never infer,
never compute, never use outside knowledge. Quote the text you read verbatim (the line with the
values for a table row).

Return JSON Lines, one object per item:
{"id": "<item id>", "reader": "second-look", "found": true|false, "p": <page>,
 "q": "<verbatim text>", "values": {...}, "claim": "supports"|"contradicts"|"not-found"}
values by kind: limit/timing {"min","typ","max","nom","unit","cond"}; strap {"value","condition"};
pin {"name","dir","type","active","io"} (dir: in/out/io/supply/ground/nc; type: od, pp, analog, supply,
or "unstated"; an overlined name gets a -bar suffix); field {"bits":[hi,lo],"reset","access"};
register {"addr","width","reset","access"}; figure {"numbers":[every number on it with its unit]}.
"claim" is only for quirk and rule items: does the page support or contradict the statement?"""


def reader_prompt(batch: dict) -> str:
    lines = [READER_RULES, "", f"Document: {batch.get('pdf')}", f"Pages: {batch.get('pages')}", "", "Items:"]
    for it in batch["items"]:
        lines.append(f"- id {it['id']}: {it['ask']}")
    return "\n".join(lines) + "\n"


def cmd_prompt(args) -> int:
    from embarch_atlas.build import resolve_atlas, resolve_atlas_dir
    ad = resolve_atlas_dir(args)
    atlas = resolve_atlas(ad, args.atlas)
    bs = read_json(atlas / "second_look_batches.json")
    if not 0 <= args.batch < len(bs):
        raise SystemExit(f"batch {args.batch} out of range 0..{len(bs) - 1}")
    print(reader_prompt(bs[args.batch]), end="")
    return 0


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("−", "-").replace(",", "")
    m = re.fullmatch(r"[+]?(-?\d+(?:\.\d+)?)(?:e(-?\d+))?", s, re.I)
    if m:
        return float(m[1]) * (10 ** int(m[2]) if m[2] else 1)
    m = re.fullmatch(r"0x([0-9a-f]+)|([0-9a-f]+)h|0b([01]+)|([01]+)b", s, re.I)
    if m:
        return float(int(m[1] or m[2], 16) if (m[1] or m[2]) else int(m[3] or m[4], 2))
    return None


def _unit(u) -> str:
    u = (u or "").strip()
    for k, alts in UNIT_ALIASES.items():
        for a in alts:
            u = u.replace(a, k)
    return u.replace(" ", "")


def _addr(v):
    s = str(v or "").strip()
    m = re.search(r"0b([01]+)|\b([01]{7,8})b?\b", s)
    if m:  # binary as printed in address tables (7 or 8 bits)
        return int(m[1] or m[2], 2)
    m = re.search(r"0x([0-9a-f]+)|\b([0-9a-f]+)h\b", s, re.I)
    return int(m[1] or m[2], 16) if m else _num(v)


def _same_address(a: int, b: int) -> bool:
    """Equal, or one is the 8-bit (shifted) form of the other's 7-bit address."""
    return a == b or a == b << 1 or b == a << 1 or (a | 1) == (b << 1 | 1) or (b | 1) == (a << 1 | 1)


STOP = {"the", "and", "for", "with", "when", "to", "of", "a", "an", "is", "in", "on", "or", "by", "as"}


UNITS = ("V", "mV", "µV", "uV", "kV", "A", "mA", "µA", "uA", "nA", "W", "mW", "µW", "Ω", "kΩ", "MΩ", "mΩ",
         "ohm", "ohms", "kohm", "mohm", "Hz", "kHz", "MHz", "GHz", "s", "ms", "µs", "us", "ns", "°C", "C", "%",
         "F", "mF", "µF", "uF", "nF", "pF", "H", "mH", "µH", "uH", "nH", "dB", "ppm", "oz", "bit", "bits", "B",
         "V/µs", "V/us", "A/µs", "mV/°C", "°C/W")
_UNIT_RE = "|".join(sorted((re.escape(u) for u in UNITS), key=len, reverse=True))
_QTY = re.compile(r"(?<![A-Za-z0-9.])(-?\d+(?:\.\d+)?)\s*(" + _UNIT_RE + r")(?![A-Za-z0-9])")


def _quantities(s: str) -> set[tuple[float, str]]:
    """Number + unit pairs (a known unit, standing alone): `200 Ω`, `35ms`, `-40 °C`."""
    s = str(s or "").replace("μ", "µ").replace("Ohm", "Ω").replace("ohm", "Ω")
    return {(float(m[1]), _unit(m[2]).lower()) for m in _QTY.finditer(s)}


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{3,}", re.sub(r"[-_]", " ", str(s or "").lower())) if w not in STOP}


def text_value_verdict(first: str, second: str) -> tuple[str, str]:
    """A printed mode or setting read twice. Quantities (number + unit) decide when both have
    them; otherwise the second reader's words must mostly appear in the first reading.
    Ambiguous overlap is `unresolved`, never `conflict`: two readers answering different
    aspects of one row is not a disagreement."""
    if not second:
        return "unresolved", "no second value"
    qf, qs = _quantities(first), _quantities(second)
    if qf and qs:
        if qs <= qf or qf <= qs:
            return "confirmed", "same quantities"
        return "conflict", "quantities differ"
    ws, wf = _words(second), _words(first)
    if not ws:
        a, b = re.sub(r"\W", "", str(first).lower()), re.sub(r"\W", "", str(second).lower())
        if b and (a == b or a.startswith(b)):
            return "confirmed", "same text"
        ta, tb = re.findall(r"[a-z0-9]+", str(first).lower()), re.findall(r"[a-z0-9]+", str(second).lower())
        if ta and tb and ta[0] != tb[0] and re.search(r"\d", ta[0]) and re.search(r"\d", tb[0]):
            return "conflict", "different codes"  # "2S" vs "3S"
        return "unresolved", "nothing comparable"
    r = len(ws & wf) / len(ws)
    if r >= 2 / 3:
        return "confirmed", f"{r:.0%} word overlap"
    if r <= 1 / 3 and not (qf & qs):
        return "conflict", f"{r:.0%} word overlap"
    return "unresolved", f"{r:.0%} word overlap"


def pin_name_norm(s) -> str:
    """Pin names with the overline spelled one way: combining U+0305, `-bar`, `_N`/`#` suffixes."""
    from embarch_atlas.join import pin_norm
    s = str(s or "")
    bar = "\u0305" in s or re.search(r"(-|_)?bar$|#$", s, re.I) is not None
    s = re.sub(r"\u0305", "", s)
    s = re.sub(r"(-|_)?bar$|#$", "", s, flags=re.I)
    return (pin_norm(s) + ("~" if bar else "")) if s else ""


def dir_class(s):
    s = str(s or "").strip().lower()
    if not s:
        return None
    if s in ("power", "supply", "ground", "gnd", "pwr", "p", "g"):
        return "power"
    if s in ("in", "i", "input"):
        return "in"
    if s in ("out", "o", "output"):
        return "out"
    if s in ("io", "i/o", "inout", "bidir", "bidirectional"):
        return "io"
    if s in ("nc", "n/c", "none"):
        return "nc"
    return s


def is_od(s):
    s = str(s or "").strip().lower()
    if not s or s in ("digital", "logic", "unknown", "unstated"):
        return None  # the reader's "not stated"
    return s in ("od", "open-drain", "open drain", "opendrain")


PM = re.compile(r"^\s*[±\u00b1]\s*([0-9]+(?:\.[0-9]+)?)\s*$")


def expand_pm(vals: dict, first: dict) -> dict:
    """A reader's `±X` in a single Value column (put under nom, typ, min or max) is `min: -X, max: X`,
    the procedure's form, unless the first pass kept that column's `±X` as printed text (a TYP ±X)."""
    out = dict(vals)
    for f in ("nom", "typ", "min", "max"):
        m = PM.match(str(vals.get(f) or ""))
        if not m or (isinstance(first.get(f), str) and PM.match(first[f])):
            continue
        x = float(m.group(1))
        out[f] = None
        if out.get("min") in (None, "") or out.get("min") == vals.get(f):
            out["min"] = -x
        if out.get("max") in (None, "") or out.get("max") == vals.get(f):
            out["max"] = x
    return out


def compare_one(first: dict, reading: dict) -> tuple[str, list[str]]:
    """confirmed / conflict / unresolved, and why. Deterministic: no judgment here."""
    if not reading.get("found", True):
        return "unresolved", ["the second reader did not find it on the page"]
    k = first.get("kind")
    vals = reading.get("values") or {}
    diffs, soft = [], []
    if k in ("quirk", "rule"):
        c = reading.get("claim")
        if c == "supports":
            return "confirmed", []
        if c == "contradicts":
            return "conflict", ["the page contradicts the statement"]
        return "unresolved", ["no supports/contradicts claim"]
    if k in ("limit", "timing"):
        vals = expand_pm(vals, first)
        for f in ("min", "nom", "typ", "max"):
            a, b = _num(first.get(f)), _num(vals.get(f))
            if a is None and b is None:
                continue
            if isinstance(first.get(f), str) and a is None:
                continue  # an expression kept as text: not compared
            if a is None or b is None or abs(a - b) > 1e-9 * max(1.0, abs(a)):
                diffs.append(f"{f}: first {first.get(f)!r} vs second {vals.get(f)!r}")
        if first.get("unit") and vals.get("unit") and _unit(first["unit"]) != _unit(vals["unit"]):
            diffs.append(f"unit: first {first['unit']!r} vs second {vals['unit']!r}")
    elif k == "strap":
        fv, sv = first.get("value"), vals.get("value")
        a, b = _addr(fv), _addr(sv)
        if a is not None and b is not None:
            if not _same_address(a, b):
                diffs.append(f"value: first {fv!r} vs second {sv!r}")
        else:  # a mode strap ("2S", "2.2 µH ..."): compare meaning, not wording
            v, why = text_value_verdict(fv, sv)
            if v != "confirmed":
                return v, [f"value: first {fv!r} vs second {sv!r} ({why})"]
    elif k == "pin":
        # Compare what the join reads: the name (overline included), the direction class, whether
        # the output is open-drain, and the active level. Free-form type words (analog, logic,
        # supply) are vocabulary, not facts the join depends on.
        a, b = pin_name_norm(first.get("name")), pin_name_norm(vals.get("name"))
        if a and b and a != b:
            diffs.append(f"name: first {first.get('name')!r} vs second {vals.get('name')!r}")
        a, b = dir_class(first.get("dir")), dir_class(vals.get("dir"))
        if a and b and a != b:
            msg = f"dir: first {first.get('dir')!r} vs second {vals.get('dir')!r}"
            if "power" in (a, b) and {a, b} - {"power"} <= {"in", "out", "io"}:
                soft.append(msg + " (a power-path pin: the direction class is ambiguous)")
            else:
                diffs.append(msg)
        a, b = is_od(first.get("type")), is_od(vals.get("type"))
        if a is not None and b is not None and a != b:
            diffs.append(f"open-drain: first {first.get('type')!r} vs second {vals.get('type')!r}")
        a, b = str(first.get("active") or "").lower(), str(vals.get("active") or "").lower()
        if a and b and a != b:
            diffs.append(f"active: first {first.get('active')!r} vs second {vals.get('active')!r}")
        squash = lambda x: re.sub(r"[\s,]+", "", str(x or "")).upper()
        if first.get("io") and vals.get("io") and squash(first["io"]) != squash(vals["io"]):
            diffs.append(f"io: first {first['io']!r} vs second {vals['io']!r}")
    elif k in ("field", "register"):
        if k == "field" and first.get("bits") and vals.get("bits") and sorted(first["bits"]) != sorted(vals["bits"]):
            diffs.append(f"bits: first {first['bits']} vs second {vals['bits']}")
        if k == "register" and first.get("addr") is not None and vals.get("addr") is not None \
                and _addr(first["addr"]) != _addr(vals["addr"]):
            diffs.append(f"addr: first {first['addr']!r} vs second {vals['addr']!r}")
        a, b = _num(first.get("reset")), _num(vals.get("reset"))
        if a is not None and b is not None and a != b:
            diffs.append(f"reset: first {first.get('reset')!r} vs second {vals.get('reset')!r}")
        acc = lambda x: re.sub(r"[^a-z0-9]", "", str(x or "").lower()).replace("rw", "rw")
        if first.get("access") and vals.get("access") and acc(first["access"]) != acc(vals["access"]):
            diffs.append(f"access: first {first['access']!r} vs second {vals['access']!r}")
    elif k == "figure":
        # Only quantities with a unit are figure facts; bare numbers in a description are mostly
        # figure, table, pin or part numbers. Axis ticks and labels are read exactly, so overlap
        # decides: most of the first reading's quantities seen again -> confirmed, few -> conflict.
        firsts = {q for q in _quantities(first.get("what") or "") if q[1]}
        seconds = set()
        for x in vals.get("numbers") or []:
            seconds |= {q for q in _quantities(str(x)) if q[1]}
        if not firsts:
            return "unresolved", ["the first reading states no quantity with a unit to compare"]
        seen = len(firsts & seconds) / len(firsts)
        missing = sorted(f"{v:g} {u}" for v, u in firsts - seconds)
        if seen >= 2 / 3:
            return "confirmed", []
        if seen == 0:
            diffs.append(f"{seen:.0%} of the first reading's quantities seen again; missing {missing[:10]}")
        else:
            soft.append(f"{seen:.0%} of the first reading's quantities seen again; missing {missing[:10]}")
    else:
        return "unresolved", [f"kind {k} has no deterministic comparison"]
    if diffs:
        return "conflict", diffs + soft
    return ("unresolved", soft) if soft else ("confirmed", [])


def compare(ad: Path, atlas: Path, readings_path: Path) -> list[dict]:
    from embarch_atlas.join import Docs
    queued = {str(q["id"]): q for q in read_jsonl(atlas / "second_look_queue.jsonl")}
    docs = Docs(ad)
    out = []
    for rd in read_jsonl(readings_path):
        it = queued.get(str(rd.get("id")))
        if not it:
            continue
        first = next((r for r in docs.docs.get(it["doc"], {}).get("records", {}).get(it["kind"], [])
                      if r.get("id") == it["id"]), {"kind": it["kind"]})
        verdict, why = compare_one(first, rd)
        vals = rd.get("values") or {}
        value = next((vals.get(f) for f in ("value", "typ", "max", "min", "reset") if vals.get(f) is not None), None)
        out.append({"id": it["id"], "verdict": verdict, "reader": "second-look", "p": rd.get("p"), "q": rd.get("q"),
                    "value": value, "unit": vals.get("unit"), "cond": vals.get("cond"), "why": why})
    return out


def cmd_compare(args) -> int:
    from embarch_atlas.build import resolve_atlas, resolve_atlas_dir
    ad = resolve_atlas_dir(args)
    atlas = resolve_atlas(ad, args.atlas)
    rows = compare(ad, atlas, Path(args.readings))
    write_jsonl(Path(args.out), rows)
    n = {v: sum(1 for r in rows if r["verdict"] == v) for v in VERDICTS}
    print(f"{len(rows)} readings compared: {n} -> {args.out}")
    for r in rows:
        if r["verdict"] == "conflict":
            print(f"  conflict {r['id']}: {'; '.join(r['why'])}")
    return 0


def cmd_record(args) -> int:
    from embarch_atlas.build import resolve_atlas, resolve_atlas_dir
    ad = resolve_atlas_dir(args)
    atlas = resolve_atlas(ad, args.atlas)
    res = record(ad, atlas, Path(args.verdicts))
    print(f"recorded {res['added']} verdicts ({res['by_verdict']}), {res['skipped']} already recorded -> {atlas / 'second_look.jsonl'}")
    for w in res["warnings"]:
        print(f"  warning: {w}", file=sys.stderr)
    return 0


def register(sub, groups) -> None:
    from embarch_atlas.common import verb_group
    g = verb_group(sub, groups, "second-look", "facts this atlas uses that need a second look; record verdicts")
    p = g.add_parser("queue", help="write second_look_queue.jsonl, ranked (skips frozen verdicts)")
    p.add_argument("atlas")
    p.add_argument("--top", type=int, help="also group the N highest-priority items into page batches "
                                           "(second_look_batches.json), one fresh subagent per batch")
    p.add_argument("--why", help="with --top: only items whose reasons contain one of these (comma-separated, "
                                 "e.g. llm-unverified,unit)")
    p.add_argument("--doc", help="with --top: only these doc ids (comma-separated)")
    p.set_defaults(func=cmd_queue)
    p = g.add_parser("prompt", help="print the blind reader's instructions for one page batch")
    p.add_argument("atlas")
    p.add_argument("batch", type=int)
    p.set_defaults(func=cmd_prompt)
    p = g.add_parser("compare", help="readings.jsonl from blind readers -> verdicts.jsonl (deterministic)")
    p.add_argument("atlas")
    p.add_argument("readings")
    p.add_argument("-o", "--out", required=True)
    p.set_defaults(func=cmd_compare)
    p = g.add_parser("record", help="validate verdicts.jsonl, append to second_look.jsonl and freeze in cache/llm/")
    p.add_argument("atlas")
    p.add_argument("verdicts")
    p.set_defaults(func=cmd_record)

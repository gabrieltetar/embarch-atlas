"""`doc render <doc_id>`: tier-2 tables and facts.txt, rendered from records so they cannot drift.

The LLM writes `s/<secid>.md`: prose, headings, and marker pairs where a table goes:

    <!-- atlas:table kind=limit group=ec -->
    <!-- atlas:end -->

Everything between a pair is replaced by the table of the records the selector matches; everything
outside is kept byte for byte. Selector keys: `kind` (required, or the first bare word), any record
field (`group=abs,esd`, `id=demo-switch.ec.V*`, globs allowed; `key!=a,b` excludes), and `p=5-9,12`
(any cited page in range).
An optional `<!-- atlas:covers 6.2 6.3 -->` line says which outline sections the file also covers
(toc.md's tier-2 column).

Row rules (DESIGN.md): one condition per row, no merged cells, symbol and unit on every row,
footnotes on the rows they qualify (Note column) and listed under the table with their text from the
page, native names verbatim. Output is deterministic: same records, same bytes.
"""
from __future__ import annotations

import fnmatch
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Callable

from . import common, records as R
from .pdftext import map_pua, page_file, write_toc

START = re.compile(r"^<!--\s*atlas:table\s+(.*?)\s*-->\s*$")
END = re.compile(r"^<!--\s*atlas:end\s*-->\s*$")
FOOTNOTE = re.compile(r"^\s{0,6}(?:\((\d{1,3})\)|(\d{1,3})\.)\s+(\S.*)$")


# ---------------------------------------------------------------- formatting

def fmt(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return "%g" % v
    if isinstance(v, list):
        return ", ".join(fmt(x) for x in v)
    return str(v).replace("|", "\\|").replace("\n", " ")


def cite_cell(rec: dict) -> str:
    ps = sorted(set(R.pages(rec)))
    runs, i = [], 0
    while i < len(ps):
        j = i
        while j + 1 < len(ps) and ps[j + 1] == ps[j] + 1:
            j += 1
        runs.append(f"p.{ps[i]}" if i == j else f"p.{ps[i]}-{ps[j]}")
        i = j + 1
    return ", ".join(runs)


def note_cell(rec: dict) -> str:
    bits = []
    if rec.get("fn"):
        bits.append("fn " + ", ".join(str(f) for f in rec["fn"]))
    if rec.get("guar") == "design":
        bits.append("by design, not production tested")
    if rec.get("src") in ("figure", "equation"):
        bits.append(f"({rec['src']})")
    if rec.get("note"):
        bits.append(fmt(rec["note"]))
    if rec.get("prov") == "llm-unverified":
        bits.append("unverified")
    return "; ".join(bits)


def _clean(s: str) -> str:
    s = map_pua(s)
    s = "".join(c for c in unicodedata.normalize("NFC", s) if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).strip()


def footnotes(text: str, after: int = 0) -> dict[str, str]:
    """Footnote id -> text, from a page's text layer, numbered `24. text` or parenthesized `(1) text`.
    TI restarts footnote numbers per table, so the first occurrence after line `after` (the row the
    footnote qualifies) wins; with after=0 that is the first block on the page."""
    lines = text.splitlines()
    out: dict[str, str] = {}
    for i, ln in enumerate(lines):
        if i < after:
            continue
        m = FOOTNOTE.match(ln)
        if not m:
            continue
        body = m.group(3)
        j = i + 1
        while j < len(lines) and lines[j].strip() and re.match(r"^\s{3,}\S", lines[j]) and not FOOTNOTE.match(lines[j]):
            body += " " + lines[j].strip()
            j += 1
        out.setdefault(m.group(1) or m.group(2), _clean(body))
    return out


def _quote_line(text: str, q: str | None) -> int:
    """Index of the line holding the tail of a quote (the row), 0 when it is not found."""
    if not q:
        return 0
    from .pdftext import normalize
    tail = " ".join(normalize(q).split()[-3:])
    for i, ln in enumerate(text.splitlines()):
        if tail and tail in normalize(ln):
            return i
    return 0


def _shorten(s: str, n: int = 300) -> str:
    if len(s) <= n:
        return s
    cut = s[:n].rsplit(" ", 1)[0]
    return cut + " …"


# ---------------------------------------------------------------- tables per kind

def _table(head: list[str], rows: list[list[str]]) -> list[str]:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return out


def t_limit(recs: list[dict], ctx: dict) -> list[str]:
    timing = any(r.get("kind") == "timing" for r in recs)
    nom = any(r.get("nom") is not None for r in recs)
    head = ["Symbol", "Parameter", "Conditions", "Min"] + (["Nom"] if nom else []) + ["Typ", "Max", "Unit"] \
        + (["Edge"] if timing else []) + ["Note", "p"]
    rows = []
    for r in recs:
        row = [fmt(r.get("sym")), fmt(r.get("param")), fmt(r.get("cond")), fmt(r.get("min"))] \
            + ([fmt(r.get("nom"))] if nom else []) + [fmt(r.get("typ")), fmt(r.get("max")), fmt(r.get("unit"))] \
            + ([fmt(r.get("edge"))] if timing else []) + [note_cell(r), cite_cell(r)]
        rows.append(row)
    out = _table(head, rows)
    notes: dict[str, dict[str, int]] = {}   # fn -> {text: page}
    missing: dict[str, int] = {}
    for r in recs:
        for f in (str(x) for x in r.get("fn") or []):
            found = None
            ps = R.pages(r)
            for p in ps[:1]:
                fp = page_file(ctx["docdir"], p)
                text = fp.read_text(encoding="utf-8") if fp.exists() else ""
                t = footnotes(text, _quote_line(text, (R.quotes(r) or [None])[0])).get(f)
                if t:
                    found = (t, p)
                for pp in (p + 1, p + 2):   # a table continued onto the next page
                    if found:
                        break
                    fp = page_file(ctx["docdir"], pp)
                    if fp.exists():
                        t = footnotes(fp.read_text(encoding="utf-8")).get(f)
                        if t:
                            found = (t, pp)
            if found:
                notes.setdefault(f, {}).setdefault(found[0], found[1])
            elif ps:
                missing.setdefault(f, min(ps))
    if notes or missing:
        out += ["", "Footnotes:"]
        for f in sorted(set(notes) | set(missing), key=lambda x: (len(x), x)):
            for text, where in sorted(notes.get(f, {}).items(), key=lambda kv: kv[1]):
                out.append(f"- fn {f} (p.{where}): {fmt(_shorten(text))}")
            if f not in notes:
                out.append(f"- fn {f}: see p.{missing[f]}")
    return out


def t_pin(recs, ctx):
    head = ["Pin", "Name", "Dir", "Type", "Active", "Pull", "Description", "Note", "p"]
    return _table(head, [[fmt(r.get("pin")), fmt(r.get("name")), fmt(r.get("dir")), fmt(r.get("type")),
                          fmt(r.get("active")), fmt(r.get("pull")), fmt(r.get("desc")), note_cell(r), cite_cell(r)]
                         for r in recs])


def t_rule(recs, ctx):
    rows = []
    for r in recs:
        then = fmt(r.get("text"))
        if r.get("steps"):
            then += " Steps: " + " ".join(f"({i}) {fmt(s)}" for i, s in enumerate(r["steps"], 1))
        rows.append([R.short_id(r["id"]), fmt(r.get("cond")), then, fmt(r.get("recover")), note_cell(r), cite_cell(r)])
    return _table(["Rule", "When", "Then", "Recovery", "Note", "p"], rows)


def _addr(v: Any) -> str:
    a = R.to_int(v)
    return "" if a is None else f"0x{a:02X}"


def _reset(v: Any, width: int | None = None) -> str:
    if v is None:
        return ""
    s = str(v)
    iv = R.to_int(v)
    if s.startswith("0b") and width and width >= 4:
        return f"{s} (0x{iv:0{max(1, (width + 3) // 4)}X})"
    return s


def register_view(recs: list[dict], all_recs: list[dict]) -> list[tuple[dict, list[dict]]]:
    """Registers with their fields: structure from the deterministic record when there is one, the
    description from the LLM twin when there is one."""
    llm = {r["id"]: r for r in all_recs if r.get("prov") in R.LLM and r.get("kind") in ("register", "field")}
    det = {r["id"]: r for r in all_recs if r.get("prov") in R.DETERMINISTIC and r.get("kind") in ("register", "field")}
    out, seen = [], set()
    for r in recs:
        if r.get("kind") != "register" or r["id"] in seen:
            continue
        seen.add(r["id"])
        base = det.get(r["id"]) or r
        desc = (llm.get(r["id"]) or {}).get("desc") or base.get("desc")
        src = det if r["id"] in det else llm
        fs = [f for f in src.values() if f.get("kind") == "field" and f.get("reg") == r["id"]]
        fs.sort(key=lambda f: -f["bits"][0])
        fields = []
        for f in fs:
            d = (llm.get(f["id"]) or {}).get("desc") or f.get("desc")
            fields.append(dict(f, desc=d))
        out.append((dict(base, desc=desc), fields))
    return out


def t_register(recs, ctx):
    view = register_view(recs, ctx["all"])
    out = _table(["Addr", "Register", "Width", "Reset", "Access", "Note", "p"],
                 [[_addr(r.get("addr")), fmt(r["name"]), fmt(r.get("width")), _reset(r.get("reset"), r.get("width")),
                   fmt(r.get("access")), note_cell(r), cite_cell(r)] for r, _ in view])
    for r, fields in view:
        out += ["", f"### {_addr(r.get('addr'))} {r['name']} ({cite_cell(r)})", ""]
        if r.get("desc"):
            out += [fmt(r["desc"]), ""]
        rows = []
        for f in fields:
            hi, lo = f["bits"]
            rows.append([f"{hi}" if hi == lo else f"{hi}:{lo}", fmt(f["name"]), fmt(f.get("access")),
                         _reset(f.get("reset")), fmt(_shorten(f.get("desc") or "", 400)), note_cell(f), cite_cell(f)])
        out += _table(["Bits", "Field", "Access", "Reset", "Description", "Note", "p"], rows)
    return out


def t_field(recs, ctx):
    rows = []
    for f in recs:
        hi, lo = f["bits"]
        rows.append([R.short_id(f["reg"]), f"{hi}" if hi == lo else f"{hi}:{lo}", fmt(f["name"]), fmt(f.get("access")),
                     _reset(f.get("reset")), fmt(f.get("desc")), note_cell(f), cite_cell(f)])
    return _table(["Register", "Bits", "Field", "Access", "Reset", "Description", "Note", "p"], rows)


def t_quirk(recs, ctx):
    out = []
    for r in recs:
        tail = f" *Why it matters:* {fmt(r['why'])}" if r.get("why") else ""
        meta = "; ".join(x for x in (fmt(r.get("class")), cite_cell(r), fmt(r.get("note")),
                                     "(figure)" if r.get("src") == "figure" else "",
                                     "unverified" if r.get("prov") == "llm-unverified" else "") if x)
        out.append(f"- **{R.short_id(r['id'])}** ({meta}): {fmt(r.get('text'))}{tail}")
    return out


def t_figure(recs, ctx):
    return _table(["Figure", "Caption", "What it shows", "Note", "p"],
                  [[fmt(r.get("fig")), fmt(r.get("caption")), fmt(r.get("what")), note_cell(r), cite_cell(r)] for r in recs])


def t_strap(recs, ctx):
    return _table(["Pin", "Condition", "Value", "Note", "p"],
                  [[fmt(r.get("pin")), fmt(r.get("condition")), fmt(r.get("value")), note_cell(r), cite_cell(r)] for r in recs])


def t_passthrough(recs, ctx):
    return _table(["From", "To", "When", "Note", "p"],
                  [[fmt(r.get("from_pin")), fmt(r.get("to_pin")), fmt(r.get("when")), note_cell(r), cite_cell(r)] for r in recs])


def t_requirement(recs, ctx):
    return _table(["Req", "Requirement", "Firmware", "Note", "p"],
                  [[fmt(r.get("rid")), fmt(r.get("text")), fmt(r.get("fw_relevant")), note_cell(r), cite_cell(r)] for r in recs])


def t_conflict(recs, ctx):
    return _table(["Conflict", "What disagrees", "Sources", "Note", "p"],
                  [[R.short_id(r["id"]), fmt(r.get("text")), fmt(r.get("sources")), note_cell(r), cite_cell(r)] for r in recs])


def t_check(recs, ctx):
    return _table(["Invariant", "Expression", "Expect", "Why", "p"],
                  [[R.short_id(r["id"]), f"`{r.get('expr', '')}`".replace("|", "\\|"), fmt(r.get("expect")),
                    fmt(r.get("why")), cite_cell(r)] for r in recs])


RENDERERS: dict[str, Callable[[list[dict], dict], list[str]]] = {
    "limit": t_limit, "timing": t_limit, "pin": t_pin, "rule": t_rule, "register": t_register, "field": t_field,
    "quirk": t_quirk, "figure": t_figure, "strap": t_strap, "passthrough": t_passthrough,
    "requirement": t_requirement, "conflict": t_conflict, "check": t_check,
}


# ---------------------------------------------------------------- selection

def parse_selector(s: str) -> dict[str, list[str]]:
    """`kind=limit group=ec id!=x.*` -> {"kind": [...], "group": [...], "!id": [...]}."""
    sel: dict[str, list[str]] = {}
    for i, tok in enumerate(s.split()):
        if "=" not in tok:
            if i == 0:
                sel["kind"] = tok.split(",")
                continue
            raise ValueError(f"selector token {tok!r} is not key=value")
        k, v = tok.split("=", 1)
        if k.endswith("!"):
            k = "!" + k[:-1]
        sel[k] = [x for x in v.split(",") if x]
    if "kind" not in sel:
        raise ValueError("selector has no kind")
    for k in sel["kind"]:
        if k not in RENDERERS:
            raise ValueError(f"unknown kind {k!r}")
    return sel


def _page_ok(rec: dict, spec: list[str]) -> bool:
    ps = R.pages(rec)
    for part in spec:
        a, _, b = part.partition("-")
        lo, hi = int(a), int(b or a)
        if any(lo <= p <= hi for p in ps):
            return True
    return False


def select(recs: list[dict], sel: dict[str, list[str]]) -> list[dict]:
    out = []
    for r in recs:
        ok = True
        for k, vs in sel.items():
            if k == "kind":
                ok = r.get("kind") in vs
            elif k == "p":
                ok = _page_ok(r, vs)
            elif k.startswith("!"):
                v = r.get(k[1:])
                ok = v is None or not any(fnmatch.fnmatchcase(fmt(v), pat) for pat in vs)
            else:
                v = r.get(k)
                ok = v is not None and any(fnmatch.fnmatchcase(fmt(v), pat) for pat in vs)
            if not ok:
                break
        if ok:
            out.append(r)
    return out


def _dedupe(recs: list[dict]) -> list[dict]:
    """One row per id: of a deterministic/LLM twin pair, the deterministic record."""
    by: dict[str, dict] = {}
    order: list[str] = []
    for r in recs:
        i = r.get("id")
        if i not in by:
            order.append(i)
            by[i] = r
        elif r.get("prov") in R.DETERMINISTIC and by[i].get("prov") not in R.DETERMINISTIC:
            by[i] = r
    return [by[i] for i in order]


# ---------------------------------------------------------------- the run

def render_section(text: str, all_recs: list[dict], docdir: Path) -> tuple[str, list[str], int]:
    lines = text.split("\n")
    out: list[str] = []
    warnings: list[str] = []
    blocks = 0
    i = 0
    ctx = {"docdir": docdir, "all": all_recs}
    recs = _dedupe(all_recs)
    while i < len(lines):
        m = START.match(lines[i])
        if not m:
            out.append(lines[i])
            i += 1
            continue
        j = i + 1
        while j < len(lines) and not END.match(lines[j]) and not START.match(lines[j]):
            j += 1
        if j >= len(lines) or START.match(lines[j]):
            warnings.append(f"line {i + 1}: atlas:table without atlas:end; left as is")
            out.append(lines[i])
            i += 1
            continue
        try:
            sel = parse_selector(m.group(1))
        except ValueError as e:
            warnings.append(f"line {i + 1}: {e}; block left as is")
            out.extend(lines[i:j + 1])
            i = j + 1
            continue
        chosen = select(recs, sel)
        body: list[str] = []
        if not chosen:
            warnings.append(f"line {i + 1}: selector {m.group(1)!r} matches no record")
            body = ["_No matching records._"]
        else:
            by_kind: dict[str, list[dict]] = {}
            for r in chosen:
                k = "limit" if r["kind"] == "timing" else r["kind"]
                by_kind.setdefault(k, []).append(r)
            for k, rs in by_kind.items():
                if body:
                    body.append("")
                body += RENDERERS[k](rs, ctx)
        out.append(lines[i])
        out.extend(body)
        out.append(lines[j])
        blocks += 1
        i = j + 1
    return "\n".join(out), warnings, blocks


def render_doc(docdir: Path, log=None) -> dict:
    docdir = Path(docdir)
    pairs = R.load_records(docdir)
    all_recs = [r for _, r in pairs]
    stats = {"files": 0, "blocks": 0, "warnings": [], "facts": 0}
    for f in sorted((docdir / "s").glob("*.md")) if (docdir / "s").is_dir() else []:
        text = f.read_text(encoding="utf-8")
        new, warns, blocks = render_section(text, all_recs, docdir)
        stats["warnings"] += [f"{f.name}: {w}" for w in warns]
        stats["blocks"] += blocks
        if new != text:
            f.write_text(new, encoding="utf-8")
            stats["files"] += 1
    prov_rank = {p: i for i, p in enumerate(R.PROV)}
    lines = sorted((R.facts_line(r) for r in all_recs if r.get("id")),
                   key=lambda ln: (ln.split(" ", 1)[0], prov_rank.get(ln.rsplit("prov=", 1)[-1], 99), ln))
    if lines:
        common.write_text(docdir / "facts.txt", "\n".join(lines))
    elif (docdir / "facts.txt").exists():
        (docdir / "facts.txt").unlink()
    stats["facts"] = len(lines)
    if (docdir / "toc.json").exists() and (docdir / "pages" / "pages.json").exists():
        write_toc(docdir)
    if log:
        log(f"{docdir.name}: {stats['blocks']} tables in s/ ({stats['files']} files changed), "
            f"{stats['facts']} lines in facts.txt")
        for w in stats["warnings"]:
            log(f"   warning: {w}")
    return stats


def cmd_render(args) -> int:
    atlas = common.atlas_dir(args.atlas_dir)
    docdir = atlas / "docs" / args.doc_id
    if not docdir.is_dir():
        print(f"doc render: no {docdir}", file=sys.stderr)
        return 2
    stats = render_doc(docdir, log=print)
    return 1 if stats["warnings"] else 0


def register(sub, groups) -> None:
    g = common.verb_group(sub, groups, "doc", "per-document steps: prep, check, render, regparse")
    p = g.add_parser("render", help="tables in s/*.md and facts.txt, rendered from records")
    p.add_argument("doc_id")
    p.set_defaults(func=cmd_render)

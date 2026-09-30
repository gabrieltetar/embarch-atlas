"""The record model: schema, validation, JSON Lines per kind, and the facts.txt line.

Records are the source of truth (DESIGN.md, Records). The LLM writes them into
`docs/<doc_id>/records/<kind>.jsonl`; deterministic producers write their own file
(`records/regparse.jsonl`), so a re-run replaces only what that producer wrote.
The loader reads every `*.jsonl` and trusts each record's `kind`, not the file name.

The validator covers the JSON Schema subset `schemas/record.schema.json` uses
(type, enum, const, required, properties, propertyNames, items, min/maxItems,
minimum, minLength, pattern, oneOf, allOf, if/then, $ref), so no `jsonschema`
dependency is needed.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from . import common

KINDS = ("limit", "timing", "pin", "rule", "register", "field", "quirk", "figure", "strap",
         "passthrough", "requirement", "conflict", "check")
PROV = ("deterministic", "deterministic+crosschecked", "llm-verified", "llm-unverified",
        "engineer-declared", "engineer-confirmed", "measured")
DETERMINISTIC = ("deterministic", "deterministic+crosschecked")
LLM = (None, "llm-verified", "llm-unverified")
COMMON = ("id", "kind", "doc", "p", "q", "src", "prov", "note")

# facts.txt key order per kind; any other key follows, sorted.
FACT_KEYS = {
    "limit": ("group", "sym", "param", "cond", "min", "nom", "typ", "max", "unit", "guar"),
    "timing": ("group", "sym", "param", "cond", "min", "nom", "typ", "max", "unit", "edge", "guar"),
    "pin": ("pin", "name", "dir", "type", "active", "pull", "desc"),
    "rule": ("cond", "text", "recover", "steps"),
    "register": ("name", "addr", "width", "reset", "access", "summary", "desc"),
    "field": ("reg", "name", "bits", "reset", "access", "enum", "desc"),
    "quirk": ("class", "text", "why", "refs"),
    "figure": ("fig", "caption", "what"),
    "strap": ("pin", "condition", "value"),
    "passthrough": ("from_pin", "to_pin", "when"),
    "requirement": ("rid", "modality", "priority", "order", "text", "fw_relevant"),
    "conflict": ("refs", "sources", "text"),
    "check": ("expr", "expect", "why"),
}
_SKIP_IN_FACTS = {"id", "kind", "doc", "p", "q", "prov", "fn", "note", "src"}


def schema_path() -> Path:
    """`schemas/record.schema.json` at the repo root (the package is installed editable)."""
    return Path(__file__).resolve().parents[2] / "schemas" / "record.schema.json"


@lru_cache(maxsize=1)
def schema() -> dict:
    return json.loads(schema_path().read_text(encoding="utf-8"))


# ---------------------------------------------------------------- validation

_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def _resolve(root: dict, ref: str) -> dict:
    if not ref.startswith("#/"):
        raise ValueError(f"only local $ref supported: {ref}")
    node: Any = root
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def _errors(inst: Any, sch: dict, root: dict, path: str) -> list[str]:
    errs: list[str] = []
    if "$ref" in sch:
        errs += _errors(inst, _resolve(root, sch["$ref"]), root, path)
    if "type" in sch:
        types = sch["type"] if isinstance(sch["type"], list) else [sch["type"]]
        if not any(_TYPES[t](inst) for t in types):
            return errs + [f"{path}: {inst!r} is not of type {'/'.join(types)}"]
    if "const" in sch and inst != sch["const"]:
        errs.append(f"{path}: {inst!r} is not {sch['const']!r}")
    if "enum" in sch and not any(inst == e and type(inst) is type(e) or (inst is None and e is None)
                                 for e in sch["enum"]):
        errs.append(f"{path}: {inst!r} not one of {sch['enum']}")
    if isinstance(inst, str):
        if len(inst) < sch.get("minLength", 0):
            errs.append(f"{path}: empty string")
        if "pattern" in sch and not re.search(sch["pattern"], inst):
            errs.append(f"{path}: {inst!r} does not match {sch['pattern']}")
    if _TYPES["number"](inst) and "minimum" in sch and inst < sch["minimum"]:
        errs.append(f"{path}: {inst} < {sch['minimum']}")
    if isinstance(inst, list):
        if len(inst) < sch.get("minItems", 0):
            errs.append(f"{path}: fewer than {sch['minItems']} items")
        if "maxItems" in sch and len(inst) > sch["maxItems"]:
            errs.append(f"{path}: more than {sch['maxItems']} items")
        if "items" in sch:
            for i, v in enumerate(inst):
                errs += _errors(v, sch["items"], root, f"{path}[{i}]")
    if isinstance(inst, dict):
        for k in sch.get("required", []):
            if k not in inst:
                errs.append(f"{path}: missing required field {k!r}")
        for k, sub in sch.get("properties", {}).items():
            if k in inst:
                errs += _errors(inst[k], sub, root, f"{path}.{k}")
        if "propertyNames" in sch:
            for k in inst:
                if _errors(k, sch["propertyNames"], root, path):
                    errs.append(f"{path}: unknown field {k!r}")
        ap = sch.get("additionalProperties")
        if isinstance(ap, dict):
            for k, v in inst.items():
                if k not in sch.get("properties", {}):
                    errs += _errors(v, ap, root, f"{path}.{k}")
    for sub in sch.get("allOf", []):
        errs += _errors(inst, sub, root, path)
    if "oneOf" in sch:
        ok = [s for s in sch["oneOf"] if not _errors(inst, s, root, path)]
        if len(ok) != 1:
            errs.append(f"{path}: {inst!r} matches {len(ok)} of the oneOf alternatives, not 1")
    if "anyOf" in sch and not any(not _errors(inst, s, root, path) for s in sch["anyOf"]):
        errs.append(f"{path}: {inst!r} matches none of the anyOf alternatives")
    if "if" in sch and not _errors(inst, sch["if"], root, path):
        if "then" in sch:
            errs += _errors(inst, sch["then"], root, path)
    elif "if" in sch and "else" in sch:
        errs += _errors(inst, sch["else"], root, path)
    return errs


def validate(record: dict) -> list[str]:
    """Schema errors for one record; [] when it is valid."""
    root = schema()
    return _errors(record, root, root, record.get("id", "<record>") if isinstance(record, dict) else "<record>")


# ---------------------------------------------------------------- files

def records_dir(docdir: Path) -> Path:
    return Path(docdir) / "records"


def load_records(docdir: Path) -> list[tuple[str, dict]]:
    """(file name, record) for every record, files sorted by name, lines in order."""
    out: list[tuple[str, dict]] = []
    rdir = records_dir(docdir)
    if not rdir.is_dir():
        return out
    for path in sorted(rdir.glob("*.jsonl")):
        for rec in common.read_jsonl(path):
            out.append((path.name, rec))
    return out


def save_file(docdir: Path, name: str, recs: Iterable[dict]) -> None:
    common.write_jsonl(records_dir(docdir) / name, recs)


def save_records(docdir: Path, pairs: list[tuple[str, dict]]) -> list[str]:
    """Write records back to their files, keeping file membership and order. Returns files written."""
    by_file: dict[str, list[dict]] = {}
    for name, rec in pairs:
        by_file.setdefault(name, []).append(rec)
    written = []
    for name, recs in by_file.items():
        path = records_dir(docdir) / name
        new = "".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in recs)
        if not path.exists() or path.read_text(encoding="utf-8") != new:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(new, encoding="utf-8")
            written.append(name)
    return written


def kind_file(kind: str) -> str:
    return f"{kind}.jsonl"


# ---------------------------------------------------------------- helpers

def pages(rec: dict) -> list[int]:
    p = rec.get("p")
    if p is None:
        return []
    return [p] if isinstance(p, int) else list(p)


def quotes(rec: dict) -> list[str]:
    q = rec.get("q")
    if q is None:
        return []
    return [q] if isinstance(q, str) else list(q)


def to_int(v: Any) -> int | None:
    """Register addresses and resets: int, '0x..', '0b..'; None when undefined."""
    if v is None:
        return None
    if isinstance(v, int):
        return v
    return int(str(v), 0)


def short_id(rec_id: str) -> str:
    """Drop `<part>.<group>.` so a table cell stays short and still greps into facts.txt."""
    parts = rec_id.split(".")
    return ".".join(parts[2:]) if len(parts) > 2 else rec_id


def slug(s: str, maxlen: int = 48) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    if len(s) > maxlen:
        s = s[:maxlen].rsplit("-", 1)[0] or s[:maxlen]
    return s or "x"


# ---------------------------------------------------------------- facts.txt

def _fmt(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return "%g" % v
    if isinstance(v, list):
        return ", ".join(_fmt(x) for x in v)
    if isinstance(v, dict):
        return ", ".join(f"{k}: {_fmt(x)}" for k, x in v.items())
    s = str(v).replace("\n", " ").strip()
    if any(c in s for c in ';"') or s != str(v):
        s = json.dumps(s, ensure_ascii=False)
    return s


def cite(rec: dict) -> str:
    """`<doc_id> p<N>[,<M>][ fn<k>[,<j>]]`, re-openable with Read(pages=N)."""
    ps = pages(rec)
    s = f"{rec.get('doc', '?')} p" + ",".join(str(p) for p in ps) if ps else f"{rec.get('doc', '?')}"
    fn = rec.get("fn")
    if fn:
        s += " fn" + ",".join(str(f) for f in fn)
    return s


def facts_line(rec: dict) -> str:
    """`<id> <key>=<value>; ...; cite=<doc_id> p<N>[ fn<k>]; prov=<class>` (DESIGN.md)."""
    kind = rec.get("kind", "")
    order = ("kind",) + FACT_KEYS.get(kind, ())
    rest = sorted(k for k in rec if k not in _SKIP_IN_FACTS and k not in order)
    parts = []
    for k in order + tuple(rest):
        v = rec.get(k)
        if v is None or v == [] or v == "" or v == {}:
            continue
        if k == "kind":
            parts.append(f"kind={kind}")
            continue
        if k == "bits" and isinstance(v, list) and len(v) == 2:
            parts.append(f"bits={v[0]}:{v[1]}")
            continue
        parts.append(f"{k}={_fmt(v)}")
    if rec.get("src") in ("figure", "equation"):
        parts.append(f"src={rec['src']}")
    if rec.get("note"):
        parts.append(f"note={_fmt(rec['note'])}")
    parts.append(f"cite={cite(rec)}")
    parts.append(f"prov={rec.get('prov') or 'llm-unverified'}")
    return f"{rec['id']} " + "; ".join(parts)

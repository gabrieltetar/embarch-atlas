"""Which supplied document covers which part number: the one source of truth.

`missing`, the join and what the join feeds (`net_trace`, `pins.json`, `MAP.md`, the graph) all read
coverage here, over `inventory/manifest.json` and `inventory/answers.toml`.

A part is **covered** by, in this order:

- a document whose part tokens match its MPN (`family_regex`: two digits on purpose, so a bare vendor
  family never covers every part);
- a document the engineer confirmed (`missing --confirm`), while the file keeps that sha256;
- when neither holds, a document a rung below says covers it.

The rungs start from what the BOM knows, the MPN, and look for it in the supplied documents, so a part
number no token pattern can read (`QRS7TUV`, `88LOG2G11`, a mixed-case family) is still found. Each gives
`{mpn, file, rung, evidence}`; strongest first:

- `exact`: the normalised MPN in the document's text. On pages 1-3 or an ordering-information page it
  **covers**. Elsewhere it is a proposal: a datasheet can name another part in a typical-application note.
- `link`: the schematic's datasheet link names this file (URL basename, ignoring case and query) or is
  the file's Zone.Identifier HostUrl. **Covers.**
- `stem`: a progressive stem of the MPN, as a whole token or token prefix, in the file name, the PDF
  title or pages 1-2. A proposal.
- `series`: the title or first page says `<prefix> Series` or `Series <prefix>`, and the MPN starts with
  that prefix. A proposal.

A rejection (`missing --reject`) takes a document off a part, whichever way it got there, until the file's
sha256 changes. An `appnote` never covers a part: a rung or token that finds one makes it supporting
material. A confirmed document whose file changed goes back to proposed.

The join is stricter than `missing` about what it reads **pin records** from, because a wrong document
there is a wrong fact and not just a question left unasked: a distillation's own identity (`Docs.for_mpn`:
its `part` and card `aka`), a confirmed document, and, only when neither gives one, a covering `exact` or
`link`. A part token in the manifest is enough to stop asking for a file; it is not enough to take a pin
map from it.

Page text comes from `inventory`'s cache. The raw rung results are kept in `inventory/matches.json`, keyed
by the documents and the BOM, so they cost nothing per lookup; answers are applied after, so a confirmation
never rescans.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from . import answers as answersmod
from . import common, inventory
from .update import distillation_of

ACTIVE = {"U", "Q", "D", "IC", "VR", "LED", "ZD", "TVS", "DS"}
PASSIVE = {"R", "C", "L", "FB", "RN", "RA", "CR", "TP", "NT", "MH", "FID", "H", "LOGO", "MP", "E", "TH"}
DOC_KINDS = ("datasheet", "reference-manual", "errata", "other")    # what a part token may cover a part with
MATCH_KINDS = ("datasheet", "reference-manual", "other")            # what a rung or a confirmation may
MATCHES_FILE = "matches.json"
MATCH_VERSION = 1       # bump when a rung changes: inventory/matches.json is keyed on it
MIN_STEM = 5
FRONT_PAGES = 3         # `exact` on these covers
STEM_PAGES = 2
MCU_RM_MIN_PAGES = 300  # a document this long that names the MCU may be its only register documentation
ORDERING = re.compile(r"\border(?:ing)?\s+(?:information|info|guide|codes?|numbers?|table)\b|\bpart\s+numbering\b", re.I)
_SERIES_NAME = r"[A-Z0-9][A-Za-z0-9-]{1,15}"
SERIES = (re.compile(rf"(?<![A-Za-z0-9-])({_SERIES_NAME})\s+(?i:series)\b"),
          re.compile(rf"\b(?i:series)\s+({_SERIES_NAME})(?![A-Za-z0-9-])"))
RUNG_ORDER = ("exact", "link", "stem", "series")


def norm(s: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def ref_prefix(ref: str) -> str:
    m = re.match(r"[A-Za-z]+", ref)
    return m.group(0).upper() if m else ""


def category(ref: str) -> str:
    p = ref_prefix(ref)
    return "active" if p in ACTIVE else ("passive" if p in PASSIVE else "other")


# ---------------------------------------------------------------- part tokens

def family_regex(token: str) -> re.Pattern | None:
    """A part token from the manifest -> a regex over normalised MPNs.

    `ABC32F4xE/xG` (ST ordering family: base, one package letter, one of the
    listed flash-size letters) -> `^ABC32F4[A-Z0-9](E|G)`; a lower-case `x` is a
    wildcard (`ABC120x` -> `^ABC120[A-Z0-9]`); anything else is a prefix of at
    least 6 characters, so a bare vendor family (`ABC32`) never covers every part.
    """
    if "/x" in token and "x" in token.split("/")[0]:
        base, *alts = token.split("/")
        i = base.index("x")
        sizes = [base[i + 1:]] + [a.lstrip("x") for a in alts]
        sizes = [norm(s) for s in sizes if norm(s)]
        if not sizes:
            return None
        return re.compile("^" + re.escape(norm(base[:i])) + "[A-Z0-9](" + "|".join(map(re.escape, sizes)) + ")")
    head = token.split("/")[0]
    if "x" in head:
        rx = "".join("[A-Z0-9]" if c == "x" else re.escape(c.upper()) for c in head if c.isalnum())
        return re.compile("^" + rx) if len(rx) >= 5 else None
    n = norm(head)
    return re.compile("^" + re.escape(n)) if len(n) >= 6 and sum(c.isdigit() for c in n) >= 2 else None


def doc_patterns(doc: dict[str, Any]) -> list[re.Pattern]:
    toks = list(doc.get("parts") or [])
    stem = re.sub(r"^(DS|ds)_", "", Path(doc["file"]).stem)
    toks += [t for t in re.split(r"[-_ .]+", stem) if re.fullmatch(r"[A-Za-z]{2,}\d{2,}[A-Za-z0-9]*", t)]
    pats = []
    for t in toks:
        rx = family_regex(t)
        if rx is not None and rx.pattern not in {p.pattern for p in pats}:
            pats.append(rx)
    return pats


def coverage_index(manifest: dict[str, Any]) -> list[tuple[dict[str, Any], list[re.Pattern]]]:
    docs = [d for d in manifest.get("documents") or []
            if d.get("kind") in DOC_KINDS and not d.get("superseded_by") and not d.get("duplicate_of")]
    return [(d, doc_patterns(d)) for d in docs]


def covering_docs(mpn: str, index, kinds: tuple[str, ...] = DOC_KINDS) -> list[dict[str, Any]]:
    n = norm(mpn)
    return [d for d, pats in index if d["kind"] in kinds and any(p.match(n) for p in pats)]


# ---------------------------------------------------------------- the rungs

def part_stems(mpn: str) -> list[str]:
    """The normalised MPN with its packaging cut after `,` `+` `/` `#`, then shorter and shorter, longest
    first, down to 5 characters; each holds at least one letter and one digit."""
    n = norm(re.split(r"[,+/#]", mpn, maxsplit=1)[0])
    out = []
    while len(n) >= MIN_STEM:
        if _letter_and_digit(n):
            out.append(n)
        n = n[:-1]
    return out


def _letter_and_digit(n: str) -> bool:
    return any(c.isalpha() for c in n) and any(c.isdigit() for c in n)


def token_rx(n: str) -> re.Pattern:
    """A normalised name as a whole token or token prefix, ignoring case and the punctuation inside it."""
    return re.compile(r"(?<![A-Za-z0-9])" + r"[-._/]{0,2}".join(map(re.escape, n)), re.I)


def _exact_rx(n: str) -> re.Pattern:
    """A normalised MPN as a whole token: its punctuation and a line break may fall anywhere in it."""
    return re.compile(r"(?<![A-Za-z0-9])" + r"[^A-Za-z0-9]{0,3}".join(map(re.escape, n)) + r"(?![A-Za-z0-9])", re.I)


class _Text:
    """One document's searchable text, normalised once."""

    def __init__(self, doc: dict[str, Any], pages: list[str] | None):
        self.doc = doc
        self.pages = pages or []
        self.norm = [norm(p) for p in self.pages]
        self.whole = "".join(self.norm)
        self.title = (doc.get("meta") or {}).get("Title") or ""
        self.path = Path(doc["file"])


def _exact(mpn: str, n: str, t: _Text) -> tuple[bool, str] | None:
    if n not in t.whole:
        return None
    rx = _exact_rx(n)
    later = None
    for i, page in enumerate(t.pages, 1):
        if n not in t.norm[i - 1] or not rx.search(page):
            continue
        if i <= FRONT_PAGES:
            return True, f"{mpn} on p{i}"
        if ORDERING.search(page):
            return True, f"{mpn} on p{i}, an ordering-information page"
        later = later or i
    return (False, f"{mpn} on p{later}, not a front or ordering page") if later else None


def _url_name(url: str) -> str:
    return unquote(urlsplit(url.strip()).path.rsplit("/", 1)[-1]).lower()


def _link(links: list[str], t: _Text) -> str | None:
    host = ((t.doc.get("origin") or {}).get("host_url") or "").strip()
    for url in links:
        if _url_name(url) == t.path.name.lower():
            return f"the schematic's link {url} names this file"
        if host and host == url.strip():
            return f"the file's Zone.Identifier HostUrl is the schematic's link {url}"
    return None


def _stem(mpn: str, t: _Text) -> tuple[str, int] | None:
    for s in part_stems(mpn):
        rx = token_rx(s)
        if rx.search(t.path.stem):
            return f"stem {s} in the file name", len(s)
        if rx.search(t.title):
            return f"stem {s} in the PDF title", len(s)
        for i, page in enumerate(t.pages[:STEM_PAGES], 1):
            if rx.search(page):
                return f"stem {s} on p{i}", len(s)
    return None


def _series(n: str, t: _Text) -> tuple[str, int] | None:
    for where, text in (("in the PDF title", t.title), ("on p1", t.pages[0] if t.pages else "")):
        for rx in SERIES:
            for m in rx.finditer(text):
                prefix = norm(m.group(1))
                if len(prefix) >= 2 and n.startswith(prefix):
                    return f"{' '.join(m.group(0).split())!r} {where}", len(prefix)
    return None


def _match(mpn: str, links: list[str], t: _Text) -> dict[str, Any] | None:
    n = norm(mpn)
    named = len(n) >= MIN_STEM and _letter_and_digit(n)    # `BARREL` or `HEADER` on a page says nothing
    exact = _exact(mpn, n, t) if named else None
    rung, covered, evidence, strength = None, False, None, 0
    if exact and exact[0]:
        rung, covered, evidence = "exact", True, exact[1]
    elif (link := _link(links, t)):
        rung, covered, evidence = "link", True, link
    elif exact:
        rung, evidence = "exact", exact[1]
    elif (stem := _stem(mpn, t)):
        rung, (evidence, strength) = "stem", stem
    elif named and (series := _series(n, t)):
        rung, (evidence, strength) = "series", series
    if rung is None:
        return None
    return {"mpn": mpn, "file": t.doc["file"], "sha256": t.doc.get("sha256"), "kind": t.doc.get("kind"), "rung": rung,
            "covered": covered, "evidence": evidence, "strength": strength}


def match_document(mpn: str, links: list[str], doc: dict[str, Any], pages: list[str] | None) -> dict[str, Any] | None:
    """The strongest rung that ties this document to this MPN, or None."""
    return _match(mpn, links, _Text(doc, pages))


def _rung_key(m: dict[str, Any]) -> tuple:
    return (RUNG_ORDER.index(m["rung"]), -m["strength"], m["file"])


def bom_parts(atlas_dir: Path) -> dict[str, list[str]]:
    """MPN -> the schematic's datasheet links, for what `missing` asks about: no passive, nothing out of the BOM."""
    out: dict[str, set[str]] = {}
    for f in sorted((Path(atlas_dir) / "boards").glob("*/bom.json")):
        try:
            rows = common.read_json(f)
        except (OSError, ValueError):
            continue
        for e in rows if isinstance(rows, list) else []:
            mpn = e.get("mpn")
            if not mpn or category(e.get("ref") or "") == "passive" or e.get("in_bom") is False:
                continue
            links = out.setdefault(mpn, set())
            if e.get("datasheet_link"):
                links.add(e["datasheet_link"])
    return {m: sorted(v) for m, v in out.items()}


def _scannable(d: dict[str, Any]) -> bool:
    return (d.get("kind") in (*MATCH_KINDS, "appnote") and not d.get("superseded_by") and not d.get("duplicate_of")
            and not d.get("needs_export"))


def _raw_matches(atlas_dir: Path, manifest: dict[str, Any], docs: list[dict[str, Any]],
                 parts: dict[str, list[str]]) -> dict[str, list[dict[str, Any]]]:
    """MPN -> every rung result, before any answer. Scanned once per set of documents and BOM."""
    todo = [d for d in docs if _scannable(d)]
    key = common.sha256_text(json.dumps([MATCH_VERSION, sorted((d["file"], d.get("sha256"), d.get("kind")) for d in todo),
                                         sorted(parts.items())]))
    path = atlas_dir / "inventory" / MATCHES_FILE
    try:
        cached = common.read_json(path)
        if cached.get("key") == key:
            return cached["matches"]
    except (OSError, ValueError, AttributeError, KeyError):
        pass
    out: dict[str, list[dict[str, Any]]] = {m: [] for m in parts}
    for d in todo if parts else []:
        t = _Text(d, inventory.document_pages(atlas_dir, d, manifest))
        for mpn, links in parts.items():
            m = _match(mpn, links, t)
            if m:
                out[mpn].append(m)
    if parts:
        try:
            common.write_json(path, {"key": key, "matches": out})
        except OSError:
            pass        # a read-only atlas dir costs a rescan next time, nothing more
    return out


# ---------------------------------------------------------------- the one reader

def _key(d: dict[str, Any]) -> str:
    return d.get("sha256") or d["file"]


class Coverage:
    def __init__(self, atlas_dir: Path, manifest: dict[str, Any], given: dict[str, dict[str, Any]],
                 documents: dict[str, dict[str, Any]]):
        self.atlas_dir, self.manifest, self.given, self.overrides = Path(atlas_dir), manifest, given, documents
        self.docs = [self._with_override(d) for d in manifest.get("documents") or []]
        view = {"documents": self.docs}
        self.index = coverage_index(view)
        self.appnotes = [(d, doc_patterns(d)) for d in self.docs if d.get("kind") == "appnote"
                         and not d.get("superseded_by") and not d.get("duplicate_of")]
        self.by_key: dict[str, dict[str, Any]] = {}
        for d in self.docs:
            self.by_key.setdefault(_key(d), d)
        self.raw = _raw_matches(self.atlas_dir, manifest, self.docs, bom_parts(self.atlas_dir))
        self._status: dict[str, dict[str, Any]] = {}

    def _with_override(self, d: dict[str, Any]) -> dict[str, Any]:
        """The kind the engineer said, even before the next `inventory` writes it into the manifest."""
        kind = (self.overrides.get((d.get("sha256") or "")[:12]) or {}).get("kind")
        return {**d, "kind": kind} if kind else d

    def doc_named(self, name: str) -> dict[str, Any] | None:
        """A manifest document by its path in the sources folder, else by a file name only one document has."""
        hit = next((d for d in self.docs if d["file"] == name), None)
        if hit:
            return hit
        low = Path(name).name.lower()
        same = [d for d in self.docs if Path(d["file"]).name.lower() == low]
        return same[0] if len(same) == 1 else None

    def rejected(self, mpn: str | None) -> set[str]:
        a = answersmod.table(self.given, mpn) or {}
        return {r["sha256"] for r in a.get("rejected") or []}

    def status(self, mpn: str | None) -> dict[str, Any]:
        """{covered: [{doc, how, evidence}], proposals: [match], supporting: [file], stale: the confirmation
        whose file no longer exists}. `how` is family, confirmed or a covering rung."""
        n = norm(mpn)
        if n in self._status:
            return self._status[n]
        rej = self.rejected(mpn)
        family = covering_docs(mpn, self.index) if n else []
        covered = [{"doc": d, "how": "family", "evidence": ""} for d in family if _key(d) not in rej]
        a = answersmod.find(self.given, mpn)
        stale = None
        if a and a["answer"] == "document":
            doc = self.by_key.get(a["sha256"])
            if doc and doc.get("kind") != "appnote":    # an app note never covers: its kind changed since
                covered = [c for c in covered if c["doc"] is not doc]
                covered.append({"doc": doc, "how": "confirmed", "evidence": ""})
            elif not doc:
                stale = a
        raw = [m for m in self.raw.get(self._raw_name(mpn), []) if (m.get("sha256") or m["file"]) not in rej]
        usable = [m for m in raw if m["kind"] != "appnote"]
        if not covered:
            covered = [{"doc": self.by_key[m.get("sha256") or m["file"]], "how": m["rung"], "evidence": m["evidence"]}
                       for m in sorted(usable, key=_rung_key) if m["covered"]]
        proposals = [] if covered else sorted((m for m in usable if not m["covered"]), key=_rung_key)
        if stale and not covered:
            now = next((d for d in self.docs if d["file"] == stale["file"] and not d.get("duplicate_of")), None)
            if now and _key(now) not in rej and now.get("kind") in MATCH_KINDS:
                proposals = [{"mpn": mpn, "file": now["file"], "sha256": now.get("sha256"), "kind": now.get("kind"),
                              "rung": stale.get("rung") or "confirmed", "covered": False, "strength": 0,
                              "evidence": f"confirmed earlier; the file changed since (sha256 was {stale['sha256'][:12]})"},
                             *[p for p in proposals if p["file"] != now["file"]]]
                stale = None
        supporting = sorted({m["file"] for m in raw if m["kind"] == "appnote"}
                            | {d["file"] for d, pats in self.appnotes if n and _key(d) not in rej and any(p.match(n) for p in pats)})
        st = {"covered": covered, "proposals": proposals, "supporting": supporting, "stale": stale}
        self._status[n] = st
        return st

    def _raw_name(self, mpn: str | None) -> str | None:
        """The BOM's own spelling of `mpn`."""
        if mpn in self.raw:
            return mpn
        n = norm(mpn)
        return next((k for k in self.raw if norm(k) == n), None)

    def documents(self, mpn: str | None, kinds: tuple[str, ...] = DOC_KINDS) -> list[dict[str, Any]]:
        return [c["doc"] for c in self.status(mpn)["covered"] if c["doc"].get("kind") in kinds]

    def part_docs(self, mpn: str | None, docs) -> list[str]:
        """The distillations whose pin records and limits speak for this part (see the module docstring)."""
        dists = {d: v["doc"] for d, v in docs.docs.items()}
        st = self.status(mpn)
        ids = set(docs.for_mpn(mpn))
        for c in st["covered"]:
            if c["how"] == "confirmed" or (not ids and c["how"] in ("exact", "link")):
                ids.add(distillation_of(c["doc"], dists))
        for key in self.rejected(mpn):
            if key in self.by_key:
                ids.discard(distillation_of(self.by_key[key], dists))
        ids.discard(None)
        return sorted(ids)

    def supplied(self, mpn: str | None) -> list[str]:
        """The files covering this part, translated or not."""
        return [c["doc"]["file"] for c in self.status(mpn)["covered"]]

    def support_docs(self, mpn: str | None, docs) -> list[str]:
        """The distilled app notes that support this part."""
        dists = {d: v["doc"] for d, v in docs.docs.items()}
        files = self.status(mpn)["supporting"]
        ids = set(docs.support_for_mpn(mpn))
        ids |= {distillation_of(d, dists) for d in self.docs if d["file"] in files}
        ids.discard(None)
        return sorted(ids)

    def names_mcu(self, part: str, doc: dict[str, Any]) -> str | None:
        """Where a document names the MCU family, ignoring case: its file name, title or pages 1-2."""
        n = norm(part)
        if not n:
            return None
        rx = token_rx(n)
        if rx.search(Path(doc["file"]).stem):
            return "the file name"
        if rx.search((doc.get("meta") or {}).get("Title") or ""):
            return "the PDF title"
        for i, page in enumerate((inventory.document_pages(self.atlas_dir, doc, self.manifest) or [])[:STEM_PAGES], 1):
            if rx.search(page):
                return f"p{i}"
        return None


def load(atlas_dir: Path, given: dict[str, dict[str, Any]] | None = None,
         documents: dict[str, dict[str, Any]] | None = None) -> Coverage:
    """The coverage of this atlas dir. No manifest is no documents; a malformed answers.toml raises ValueError
    unless the caller already holds the answers it means to use."""
    atlas_dir = Path(atlas_dir)
    given = answersmod.load(atlas_dir) if given is None else given
    documents = answersmod.load_documents(atlas_dir) if documents is None else documents
    try:
        manifest = common.read_json(atlas_dir / "inventory" / "manifest.json")
    except (OSError, ValueError):
        manifest = {}
    return Coverage(atlas_dir, manifest if isinstance(manifest, dict) else {}, given, documents)


def answers_fingerprint(atlas_dir: Path) -> str | None:
    """What a join reads from answers.toml besides cards: confirmed and rejected documents. None when there
    are none, so an atlas joined before they existed stays fresh."""
    try:
        given = answersmod.load(atlas_dir)
    except ValueError:
        return None
    items = sorted((norm(m), a.get("sha256") if a.get("answer") == "document" else "",
                    sorted(r["sha256"] for r in a.get("rejected") or []))
                   for m, a in given.items() if a.get("answer") == "document" or a.get("rejected"))
    return common.sha256_text(json.dumps(items)) if items else None

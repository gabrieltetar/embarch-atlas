"""`inventory`: every engineer-supplied file -> `inventory/manifest.{json,md}`.

Per file: kind, part number(s), document number, revision and date (from the
vendors' own document-control conventions), pages, text-layer coverage per page
(`pdftext.text_layer`, the same classification `doc prep` writes to doc.json, so the two agree
on which pages are textless), sha256 of the file and of each page's text, the doc_id, and a diff block against
the previous manifest (added / removed / changed / unchanged, and which pages
changed). No network: the Windows Mark-of-the-Web sidecar (`<file>:Zone.Identifier`)
is read when present, because it records where a file came from for free.

The manifest is byte-identical across runs: every collection is sorted, nothing
records the time of the run, and when the documents have not changed the
previous diff block is carried over unchanged (so the last real change stays
visible until the next one). Page text is not in the manifest: it goes to
`inventory/text/<sha256[:16]>.txt.gz`, a cache keyed by content that `coverage`
reads, so `missing` does not run poppler over every document again.

The engineer's kind override (`inventory/answers.toml` `[documents."<sha256[:12]>"]`)
is applied here and reported in `manifest.md`. It never changes the doc_id: a
document keeps the id its classified kind gives, so a translation stays where it is.

Detectors, first match wins per field:

- TI: pdfinfo Author `... [SLVS123,A` and the p1 banner `SLVS123A - <MONTH YYYY> - REVISED <MONTH YYYY>`
- ST: `DS12345 Rev 6` (DS/RM/ES/AN/UM/PM/TN) on the last page or p1; the date is the
  revision-history table's last `dd-Mon-yyyy`
- Maxim/ADI: `19-1234; Rev 2; 3/19` on p1; ADI's `Rev. C` footer via the generic rule
- Kinetic: footer `<Month YYYY> - Revision 03a`
- Microchip: `DS20005678B` (number + revision letter)
- Altium title block: `Revision: B`, `Date: 1/2/2026`, `Sheet 3 of 7`, `<name>.SchDoc`
- PRD: `Version: 2.1` header and its version-history table
- generic fallback: `Rev(ision) X` and a `<Month YYYY>` / ISO date near it
"""
from __future__ import annotations

import datetime as _dt
import gzip
import hashlib
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from . import __version__, answers, common, project
from .pdftext import text_layer, text_layer_of_pdf

TOOL = f"embarch-atlas/{__version__} inventory"
PART_LIMIT = 6          # part-number tokens kept per document

KINDS = ("schematic", "datasheet", "reference-manual", "errata", "appnote", "prd", "spreadsheet", "notes", "other")
WORD_EXTS = (".docx", ".doc")
TEXT_DIR = "text"       # under inventory/: page text per document, keyed by content

# ---------------------------------------------------------------- detector patterns (public vendor conventions)
TI_AUTHOR = re.compile(r"\[(S[A-Z]{3}[A-Z0-9]+),([A-Z]?)")
TI_LIT = re.compile(r"\b(S[A-Z]{3}[A-Z0-9]{2,4}?)([A-Z]?)\s+[–—-]\s+([A-Z]+ \d{4})(?:\s+[–—-]\s+REVISED\s+([A-Z]+ \d{4}))?")
ST_DOC = re.compile(r"\b((?:DS|RM|ES|AN|UM|PM|TN)\d{4,5}) Rev (\d+)\b")
ST_HIST_DATE = re.compile(r"^\s*(\d{1,2}-[A-Z][a-z]{2}-\d{4})\s*$", re.M)
MAXIM_DOC = re.compile(r"\b(19-\d{4,5}); Rev (\d+); (\d{1,2}/\d{2,4})\b")
KINETIC_REV = re.compile(r"\b([A-Z][a-z]+ \d{4})\s+[–—-]\s+Revision (\w+)\b")
MICROCHIP_DOC = re.compile(r"\b(DS\d{8})([A-Z])\b")
ALTIUM_REV = re.compile(r"Revision:?\s*([A-Z0-9][A-Z0-9.]{0,5})\b")
ALTIUM_DATE = re.compile(r"Date:?\s*(\d{1,2}/\d{1,2}/\d{2,4})")
ALTIUM_SHEET = re.compile(r"Sheet\s+(\d+)\s+of\s+(\d+)")
SCHDOC = re.compile(r"([A-Za-z0-9_\-]+\.SchDoc)", re.I)
PRD_VERSION = re.compile(r"Version:?\s*([0-9]+(?:\.[0-9]+)*)")
PRD_HISTORY = re.compile(r"^\s*(\d+\.\d+)\s+(\d{1,2}/\d{1,2}/\d{2,4})", re.M)
GENERIC_REV = re.compile(r"\bRev(?:ision)?\.?\s*:?\s*([0-9]{1,3}(?:\.[0-9]{1,2})?|[A-Z]{1,2}[0-9]?)\b")
MONTH_DATE = re.compile(r"\b((?:January|February|March|April|May|June|July|August|September|October|November|December|"
                        r"JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)"
                        r"(?: \d{1,2},)? \d{4})\b")
ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
NOTES_DATE = re.compile(r"(?:Compiled|Updated|Date|Revised)[^0-9\n]{0,20}(\d{4}-\d{2}-\d{2})")
# a part-number-looking token: letters then digits, optional suffix; ST families keep their `xB/xC` tail
PART_TOKEN = re.compile(r"\b([A-Z]{2,6}\d{2,6}[A-Z0-9]*(?:x[A-Z0-9])?(?:/x[A-Z0-9])*)")
NOT_PART = re.compile(r"^(?:DS|RM|ES|AN|UM|PM|TN|SL[A-Z]{2}|SB[A-Z]{2}|SC[A-Z]{2}|IEC|ISO|EN|JESD|JEDEC|USB|UL|ARM|IPC|"
                      r"AEC|MIL|RFC|IEEE|ANSI|CISPR|ETSI|FCC)\d")
# A package name is a family and a pin count. The second group holds to at most four digits and nothing after
# them, because a short prefix like `CSP` or `TO` can also start a real part number.
PACKAGE = re.compile(r"^(?:LQFP|TQFP|UFBGA|WLCSP|UFQFPN|TSSOP|SSOP|MSOP|QFN|WQFN|VQFN|DSBGA|WCSP|SOT|SOIC|TDFN|LGA|BGA|DFN|"
                     r"TSOT|SON|WSON|UQFN|HVQFN|XQFN|PDIP|DIP|SC)\d"
                     r"|^(?:CSP|FCCSP|LFCSP|WLP|FBGA|LFBGA|TFBGA|VFBGA|WFBGA|FCBGA|PBGA|LBGA|QFP|PQFP|HQFP|VQFP|HTQFP|"
                     r"TQFN|HQFN|UDFN|VDFN|WDFN|XDFN|SOP|QSOP|TSOP|HSOP|VSSOP|HTSSOP|VSON|USON|XSON|PLCC|MLF|MLP|LLP|"
                     r"SOD|TO)\d{1,4}$")
# A vendor's internal or application-note number: a short prefix and five or more digits starting at zero.
VENDOR_NUMBER = re.compile(r"^[A-Z]{2,3}0\d{4,}$")
APPNOTE_WORDS = re.compile(r"\bapp(?:lication)?\.?\s+notes?\b|\buser'?s?\s+guide\b|\btech(?:nical)?\.?\s+notes?\b", re.I)
APPNOTE_NUMBER = re.compile(r"^\s*(?:an|um|tn)[-_ ]?\d{4,5}(?![a-z0-9])", re.I)
APPNOTE_HEAD = 400      # a datasheet's later text may cite an application note; a note says so at the top
WORD_PRD = re.compile(r"(^|[^a-z])(prd|requirements?)([^a-z]|$)")


# ---------------------------------------------------------------- small helpers

def _run(cmd: list[str], timeout: int = 600) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def page_hash(text: str) -> str:
    """16 hex of sha256 over whitespace-normalised page text: the per-page change key."""
    return hashlib.sha256(norm_text(text).encode("utf-8")).hexdigest()[:16]


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "doc"


def natural_key(s: str) -> list:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


# ---------------------------------------------------------------- PDF readers (poppler CLI)

def pdfinfo(path: Path) -> dict[str, str]:
    info = {}
    for line in _run(["pdfinfo", "-isodates", str(path)]).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            info[k.strip()] = v.strip()
    return info


def pdf_pages_text(path: Path, npages: int) -> list[str]:
    """Text per physical page (poppler reading order); a form feed ends each page."""
    pages = _run(["pdftotext", "-enc", "UTF-8", str(path), "-"]).split("\f")
    pages = pages[:npages]
    while len(pages) < npages:
        pages.append("")
    return pages


def pdf_text_layer(path: Path, pages: list[str]) -> dict[str, Any]:
    """`pdftext.text_layer`, the one text-layer classification `doc prep` also uses (running
    header and footer lines and logos ignored). A PDF PyMuPDF cannot open is classified from the
    poppler text alone: words only, no positions and no images."""
    try:
        return text_layer_of_pdf(path)
    except Exception:  # noqa: BLE001 - fall back to the text poppler could read
        return text_layer([{"w": 1.0, "h": 1.0, "images": [], "paths": [],
                            "words": [(0.5, 0.5, 0.5, 0.5, t, 0, k) for k, t in enumerate(p.split())]}
                           for p in pages])


# ---------------------------------------------------------------- page text cache

def text_cache_path(atlas_dir: Path, sha256: str) -> Path:
    return Path(atlas_dir) / "inventory" / TEXT_DIR / f"{sha256[:16]}.txt.gz"


def write_text_cache(path: Path, pages: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress("\f".join(pages).encode("utf-8"), mtime=0))


def read_text_cache(path: Path) -> list[str] | None:
    try:
        return gzip.decompress(path.read_bytes()).decode("utf-8").split("\f")
    except (OSError, EOFError, ValueError):
        return None


def sources_of(atlas_dir: Path, manifest: dict[str, Any] | None = None) -> Path:
    """The folder the manifest was made from (`inventory --sources` included), else atlas.toml's."""
    atlas_dir = Path(atlas_dir)
    cfg = project.load_config(atlas_dir) if (atlas_dir / project.CONFIG_NAME).is_file() else {}
    label = (manifest or {}).get("sources")
    return project.fw_dir(atlas_dir, cfg) / label if label else project.sources_dir(atlas_dir, cfg)


def document_pages(atlas_dir: Path, doc: dict[str, Any], manifest: dict[str, Any] | None = None) -> list[str] | None:
    """Text per page of a manifest document: the cache, else the file itself when it still has that
    content (a manifest from before the cache), else None."""
    sha = doc.get("sha256")
    if not sha or Path(doc.get("file") or "").suffix.lower() != ".pdf":
        return None
    cache = text_cache_path(atlas_dir, sha)
    pages = read_text_cache(cache)
    if pages is not None:
        return pages
    try:
        path = sources_of(atlas_dir, manifest) / doc["file"]
        if not path.is_file() or common.sha256_file(path) != sha:
            return None
    except (OSError, IndexError):
        return None
    pages = pdf_pages_text(path, int(doc.get("pages") or 0))
    try:
        write_text_cache(cache, pages)
    except OSError:
        pass        # a read-only atlas dir costs a re-read next time, nothing more
    return pages


# ---------------------------------------------------------------- classification + identity

def is_appnote(path: Path, title: str, head: str) -> bool:
    """An application note, user guide or technical note: its title, its file name, the top of its first
    page, or an ST `AN`/`UM`/`TN` number as the first ST document number on its first pages."""
    name = re.sub(r"[-_.]+", " ", path.stem)
    if APPNOTE_WORDS.search(title) or APPNOTE_WORDS.search(name) or APPNOTE_WORDS.search(head[:APPNOTE_HEAD]):
        return True
    if APPNOTE_NUMBER.match(path.stem) or APPNOTE_NUMBER.match(head):
        return True
    m = ST_DOC.search(head)
    return bool(m) and m.group(1)[:2] in ("AN", "UM", "TN")


def classify(path: Path, info: dict[str, str], head: str) -> str:
    ext = path.suffix.lower()
    if ext in (".xlsx", ".xlsm", ".xls", ".csv", ".ods"):
        return "spreadsheet"
    if ext in (".md", ".txt", ".rst"):
        return "notes"
    if ext != ".pdf":
        return "other"
    name = path.stem.lower()
    title = (info.get("Title") or "").lower()
    low = head.lower()
    if project.schematic_evidence(path, " ".join(info.get(k, "") for k in ("Creator", "Producer")), head):
        return "schematic"
    if "reference manual" in title:
        return "reference-manual"
    if "errata" not in title and is_appnote(path, title, head):     # may cite a reference manual on its first page
        return "appnote"
    if "reference manual" in low[:2000]:
        return "reference-manual"
    if "errata" in title or "errata sheet" in low[:1500] or "device errata" in low[:1500] or "silicon errata" in low[:1500]:
        return "errata"
    if "requirements document" in title or "requirements document" in low[:1500] or re.search(r"(^|[^a-z])prd([^a-z]|$)", name):
        return "prd"
    if ("datasheet" in title or "data sheet" in low[:4000] or "datasheet" in low[:4000]
            or "general description" in low[:4000] or re.search(r"\bfeatures\b", low[:3000])):
        return "datasheet"
    return "other"


def detect_ids(kind: str, info: dict[str, str], pages: list[str]) -> dict[str, Any]:
    """doc_no, rev, date, parts, evidence (which convention matched where)."""
    d: dict[str, Any] = {"doc_no": None, "rev": None, "date": None, "parts": [], "evidence": []}
    first = norm_text("\n".join(pages[:2]))
    last = norm_text(pages[-1]) if pages else ""
    author = info.get("Author", "")

    def found(**kw):
        for k, v in kw.items():
            if k != "why" and v and not d[k]:
                d[k] = v
        d["evidence"].append(kw["why"])

    m = TI_AUTHOR.search(author)
    if m:
        found(doc_no=m.group(1), rev=m.group(2) or "-", why=f"pdfinfo Author '{m.group(0)}'")
    m = TI_LIT.search(first)
    if m:
        found(doc_no=m.group(1), rev=m.group(2) or "-", date=m.group(4) or m.group(3), why=f"p1 banner '{m.group(0)}'")
    for txt, where in ((last, "last page"), (first, "p1-2")):
        m = ST_DOC.search(txt)
        if m and not d["doc_no"]:
            found(doc_no=m.group(1), rev=m.group(2), why=f"{where} '{m.group(0)}'")
    m = MAXIM_DOC.search(first)
    if m and not d["doc_no"]:
        found(doc_no=m.group(1), rev=m.group(2), date=m.group(3), why=f"p1 '{m.group(0)}'")
    m = KINETIC_REV.search(last) or KINETIC_REV.search(first)
    if m and not d["rev"]:
        found(rev=m.group(2), date=m.group(1), why=f"footer '{m.group(0)}'")
    m = MICROCHIP_DOC.search(first) or MICROCHIP_DOC.search(last)
    if m and not d["doc_no"]:
        found(doc_no=m.group(1), rev=m.group(2), why=f"'{m.group(0)}'")
    if d["doc_no"] and re.match(r"(DS|RM|ES|AN|UM|PM|TN)\d{4,5}$", d["doc_no"]) and not d["date"]:
        dates = ST_HIST_DATE.findall("\n".join(pages[-8:]))
        if dates:
            found(date=dates[-1], why=f"revision-history table, last date '{dates[-1]}'")
    if kind == "schematic":
        revs = sorted({r for p in pages for r in ALTIUM_REV.findall(p)})
        dates = sorted({x for p in pages for x in ALTIUM_DATE.findall(p)})
        if revs or dates:
            d["rev"] = ",".join(revs) or None
            d["date"] = ",".join(dates) or None
            d["evidence"].append(f"title blocks: Revision {revs}, Date {dates}")
        sheets = []
        for i, p in enumerate(pages, 1):
            sd, sh = SCHDOC.search(p), ALTIUM_SHEET.search(p)
            sheets.append({"page": i, "schdoc": sd.group(1) if sd else None,
                           "sheet": f"{sh.group(1)}/{sh.group(2)}" if sh else None})
        d["sheets"] = sheets
    if kind == "prd":
        m = PRD_VERSION.search(first)
        if m:
            d["rev"] = m.group(1)
            d["evidence"].append(f"p1 header 'Version: {m.group(1)}'")
        hist = PRD_HISTORY.findall(pages[0] if pages else "")
        if hist:
            d["date"] = hist[-1][1]
            d["evidence"].append(f"p1 version history, last row '{hist[-1][0]} {hist[-1][1]}'")
    if not d["rev"] and kind not in ("schematic", "prd"):
        m = GENERIC_REV.search(first) or GENERIC_REV.search(last)
        if m:
            found(rev=m.group(1), why=f"generic '{m.group(0)}'")
    if not d["date"] and kind not in ("schematic",):
        m = MONTH_DATE.search(first) or MONTH_DATE.search(last) or ISO_DATE.search(first)
        if m:
            found(date=m.group(1), why=f"generic date '{m.group(1)}'")
    if kind in ("schematic", "prd"):
        return d  # a schematic's parts come from its BOM (`schematic`), a PRD has none
    blob = " ".join([info.get("Title", ""), info.get("Keywords", ""), info.get("Subject", "")] + pages[:3])
    counts: dict[str, int] = {}
    for t in PART_TOKEN.findall(blob):
        if NOT_PART.match(t) or PACKAGE.match(t) or VENDOR_NUMBER.match(t) or (d["doc_no"] and t.startswith(d["doc_no"])):
            continue
        counts[t] = counts.get(t, 0) + 1
    in_title = set(PART_TOKEN.findall(info.get("Title", "")))
    ranked = sorted(counts.items(), key=lambda kv: (-(kv[0] in in_title), -kv[1], kv[0]))
    d["parts"] = [k for k, _ in ranked[:PART_LIMIT]]
    return d


def doc_id_for(rec: dict[str, Any]) -> str:
    """DESIGN.md "Identifiers": `<part or document slug>@<revision>`.

    Datasheets use the part when it is a clean part number (`abc1234@B`) and the
    document number otherwise (an ST family like `ABC32F4xE/xG` gives `ds12345@3`);
    reference manuals and errata use the document number (`rm0123@6`); an app note
    uses its document number or file stem, never a part it names; everything else
    uses the file stem. No detected revision gives `@norev`.
    """
    kind, part, docno = rec.get("kind"), rec.get("part"), rec.get("doc_no")
    clean_part = part if part and re.fullmatch(r"[A-Z0-9]+", part) else None
    if kind == "datasheet":
        base = clean_part or docno
    elif kind in ("reference-manual", "errata"):
        base = docno or clean_part
    elif kind == "appnote":
        base = docno
    else:
        base = None
    base = base or Path(rec["file"]).stem
    rev = re.sub(r"\s+", "", str(rec.get("rev") or "")) or "norev"
    return f"{slug(base)}@{rev}"


def zone_identifier(path: Path) -> dict[str, str] | None:
    """Windows Mark-of-the-Web sidecar: where the engineer downloaded the file from."""
    z = path.with_name(path.name + ":Zone.Identifier")
    if not z.is_file():
        return None
    kv = {}
    for line in z.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            kv[k.strip()] = v.strip()
    host = kv.get("HostUrl")
    return {"host_url": host} if host else None


# ---------------------------------------------------------------- per-format inventory

def inventory_pdf(path: Path, override: str | None = None, text_cache: Path | None = None) -> dict[str, Any]:
    info = pdfinfo(path)
    npages = int(info.get("Pages", "0") or 0)
    pages = pdf_pages_text(path, npages)
    if text_cache is not None:
        write_text_cache(text_cache, pages)
    layer = pdf_text_layer(path, pages)
    found = classify(path, info, "\n".join(pages[:3]))
    kind = override or found
    rec: dict[str, Any] = {
        "kind": kind,
        "pages": npages,
        "page_words": [p["words"] for p in layer["pages"]],
        "page_hashes": [page_hash(p) for p in pages],
        "pages_textless": layer["textless"],
        "pages_near_empty": layer["near_empty"],
        "pages_with_images": len(layer["with_images"]),
        "text_layer": layer["text_layer"],
        "meta": {k: info[k] for k in ("Title", "Author", "Creator", "Producer") if info.get(k)},
    }
    if kind != found:
        rec["kind_classified"] = found
    ids = detect_ids(kind, info, pages)
    rec.update(ids)
    if kind == "schematic":
        rec["net_info"] = project.pdf_has_net_info(path)
    return rec


def _docx_title(path: Path) -> str | None:
    """A Word file's title from `docProps/core.xml`; a `.doc` has no such part."""
    try:
        with zipfile.ZipFile(path) as z:
            root = ET.fromstring(z.read("docProps/core.xml"))
    except (zipfile.BadZipFile, ET.ParseError, KeyError, OSError):
        return None
    for el in root:
        if el.tag.rsplit("}", 1)[-1] == "title" and (el.text or "").strip():
            return el.text.strip()
    return None


def inventory_word(path: Path) -> dict[str, Any]:
    """A Word file gets no pages: its name or title says whether it is the PRD, which is then asked for as a PDF."""
    title = _docx_title(path) if path.suffix.lower() == ".docx" else None
    says_prd = WORD_PRD.search(path.stem.lower()) or (title and WORD_PRD.search(title.lower()))
    rec: dict[str, Any] = {"kind": "prd" if says_prd else "other"}
    if title:
        rec["meta"] = {"Title": title}
    return rec


def _xlsx_sheets(path: Path) -> tuple[list[dict[str, Any]], list[str], str | None]:
    """Sheet names + dims, per-sheet content hashes (cell values, shared strings resolved), modified date."""
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
          "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
          "rel": "http://schemas.openxmlformats.org/package/2006/relationships"}
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall("m:si", ns):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{ns['m']}}}t")))
        rels = {}
        if "xl/_rels/workbook.xml.rels" in names:
            for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels")).findall("rel:Relationship", ns):
                rels[r.get("Id")] = r.get("Target", "")
        sheets, hashes = [], []
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        for s in wb.findall("m:sheets/m:sheet", ns):
            target = rels.get(s.get(f"{{{ns['r']}}}id"), "")
            member = target.lstrip("/") if target.startswith("/") else "xl/" + target
            cells, rows, cols = [], 0, 0
            if member in names:
                for c in ET.fromstring(z.read(member)).iter(f"{{{ns['m']}}}c"):
                    v = c.find("m:v", ns)
                    val = "".join(t.text or "" for t in c.iter(f"{{{ns['m']}}}t")) if c.get("t") == "inlineStr" else (v.text if v is not None else "")
                    if c.get("t") == "s" and val and val.isdigit() and int(val) < len(shared):
                        val = shared[int(val)]
                    if val:
                        ref = c.get("r", "")
                        cells.append(f"{ref}={val}")
                        m = re.match(r"([A-Z]+)(\d+)", ref)
                        if m:
                            rows = max(rows, int(m.group(2)))
                            col = 0
                            for ch in m.group(1):
                                col = col * 26 + ord(ch) - 64
                            cols = max(cols, col)
            sheets.append({"name": s.get("name"), "rows": rows, "cols": cols})
            hashes.append(page_hash("\n".join(cells)))
        modified = None
        if "docProps/core.xml" in names:
            m = re.search(rb"<dcterms:modified[^>]*>([^<]+)<", z.read("docProps/core.xml"))
            modified = m.group(1).decode()[:10] if m else None
    return sheets, hashes, modified


def inventory_spreadsheet(path: Path) -> dict[str, Any]:
    ext = path.suffix.lower()
    if ext in (".xlsx", ".xlsm"):
        try:
            sheets, hashes, modified = _xlsx_sheets(path)
        except (zipfile.BadZipFile, ET.ParseError, KeyError) as e:
            return {"kind": "spreadsheet", "error": f"unreadable workbook: {e}"}
        return {"kind": "spreadsheet", "sheets": sheets, "page_hashes": hashes, "date": modified,
                "evidence": ["docProps/core.xml dcterms:modified"] if modified else []}
    if ext == ".csv":
        text = path.read_text(encoding="utf-8", errors="replace")
        return {"kind": "spreadsheet", "sheets": [{"name": path.name, "rows": text.count("\n")}],
                "page_hashes": [page_hash(text)]}
    return {"kind": "spreadsheet"}


def inventory_notes(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    m = NOTES_DATE.search(text)
    return {"kind": "notes", "lines": len(text.splitlines()), "page_hashes": [page_hash(text)],
            "date": m.group(1) if m else None}


def inventory_file(path: Path, rel: str, overrides: dict[str, dict[str, Any]] | None = None,
                   atlas_dir: Path | None = None) -> dict[str, Any]:
    st = path.stat()
    rec: dict[str, Any] = {"file": rel, "bytes": st.st_size, "sha256": common.sha256_file(path)}
    origin = zone_identifier(path)
    if origin:
        rec["origin"] = origin
    override = ((overrides or {}).get(rec["sha256"][:12]) or {}).get("kind")
    ext = path.suffix.lower()
    if ext == ".pdf":
        cache = text_cache_path(atlas_dir, rec["sha256"]) if atlas_dir else None
        rec.update(inventory_pdf(path, override, cache))
    elif ext in (".xlsx", ".xlsm", ".xls", ".csv", ".ods"):
        rec.update(inventory_spreadsheet(path))
    elif ext in (".md", ".txt", ".rst"):
        rec.update(inventory_notes(path))
    elif ext in WORD_EXTS:
        rec.update(inventory_word(path))
    else:
        rec["kind"] = "other"
    found = rec.get("kind_classified") or rec["kind"]      # an override never moves the doc_id
    rec["kind"] = override or rec["kind"]
    if rec["kind"] != found:
        rec["kind_classified"] = found
    if ext in WORD_EXTS and rec["kind"] == "prd":
        rec["needs_export"] = True
    for k, v in (("doc_no", None), ("rev", None), ("date", None), ("parts", []), ("evidence", [])):
        rec.setdefault(k, v)
    rec["part"] = rec["parts"][0] if rec["parts"] else None
    rec["doc_id"] = doc_id_for({**rec, "kind": found})
    return rec


# ---------------------------------------------------------------- the manifest

def list_sources(sources: Path, exclude: list[Path] = ()) -> list[tuple[Path, str]]:
    """Every regular file under `sources`, sorted by relative path; hidden files, MOTW sidecars and `exclude` dirs skipped."""
    excl = [e.resolve() for e in exclude]
    out = []
    for p in sources.rglob("*"):
        rel = p.relative_to(sources)
        if not p.is_file() or any(part.startswith(".") for part in rel.parts) or p.name.endswith(":Zone.Identifier"):
            continue
        rp = p.resolve()
        if any(rp == e or e in rp.parents for e in excl):
            continue
        out.append((p, rel.as_posix()))
    return sorted(out, key=lambda t: t[1])


def _word_export(word: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The PDF exported from a Word file: the same stem, ignoring case, in the same folder if there is one there."""
    path = Path(word["file"])
    pdfs = [r for r in records if Path(r["file"]).suffix.lower() == ".pdf" and "duplicate_of" not in r
            and Path(r["file"]).stem.lower() == path.stem.lower()]
    return next((r for r in pdfs if Path(r["file"]).parent == path.parent), pdfs[0] if pdfs else None)


def mark_supersession(records: list[dict[str, Any]]) -> None:
    """Same content twice -> `duplicate_of`; a Word file and the PDF exported from it -> the Word file
    is `superseded_by` the PDF and no longer needs exporting; same document base, older revision ->
    `superseded_by`."""
    by_sha: dict[str, list[dict]] = {}
    for r in records:
        by_sha.setdefault(r["sha256"], []).append(r)
    for rs in by_sha.values():
        for r in rs[1:]:
            r["duplicate_of"] = rs[0]["file"]
    for r in records:
        if Path(r["file"]).suffix.lower() in WORD_EXTS and "duplicate_of" not in r:
            pdf = _word_export(r, records)
            if pdf:
                r["superseded_by"] = pdf["file"]
                r.pop("needs_export", None)
    by_base: dict[tuple, list[dict]] = {}
    for r in records:
        if "duplicate_of" in r or "superseded_by" in r or r["kind"] in ("notes", "other"):
            continue
        by_base.setdefault((r["kind"], r["doc_id"].split("@")[0]), []).append(r)
    for rs in by_base.values():
        if len(rs) < 2:
            continue
        rs = sorted(rs, key=lambda r: (natural_key(str(r.get("rev") or "")), r["file"]))
        for older in rs[:-1]:
            if older.get("rev") != rs[-1].get("rev"):
                older["superseded_by"] = rs[-1]["file"]


def diff_manifests(prev_docs: list[dict[str, Any]], docs: list[dict[str, Any]]) -> dict[str, Any]:
    prev = {r["file"]: r for r in prev_docs}
    cur = {r["file"]: r for r in docs}
    both = sorted(set(cur) & set(prev))
    changed = [f for f in both if cur[f]["sha256"] != prev[f]["sha256"]]
    pages = {}
    for f in changed:
        a, b = prev[f].get("page_hashes") or [], cur[f].get("page_hashes") or []
        diffp = [i for i in range(1, max(len(a), len(b)) + 1)
                 if i > len(a) or i > len(b) or a[i - 1] != b[i - 1]]
        pages[f] = diffp
    return {
        "added": sorted(set(cur) - set(prev)),
        "removed": sorted(set(prev) - set(cur)),
        "changed": changed,
        "unchanged": [f for f in both if cur[f]["sha256"] == prev[f]["sha256"]],
        "changed_pages": pages,
    }


def build_manifest(sources: Path, sources_label: str, previous: dict[str, Any] | None,
                   exclude: list[Path] = (), previous_sha256: str | None = None,
                   overrides: dict[str, dict[str, Any]] | None = None, atlas_dir: Path | None = None) -> dict[str, Any]:
    docs = [inventory_file(p, rel, overrides, atlas_dir) for p, rel in list_sources(sources, exclude)]
    mark_supersession(docs)
    if previous and previous.get("documents") == docs and previous.get("diff") is not None:
        diff = previous["diff"]  # nothing changed: keep the last real change visible, and stay byte-identical
    else:
        diff = diff_manifests((previous or {}).get("documents") or [], docs)
        diff["previous"] = (previous_sha256 or "unknown") if previous else None
    return {"tool": TOOL, "sources": sources_label, "documents": docs, "diff": diff}


def _cell(s: Any) -> str:
    return str(s if s is not None else "").replace("|", "\\|")


def render_md(man: dict[str, Any]) -> str:
    docs = man["documents"]
    L = ["# Document manifest", "",
         f"`{man['tool']}` over `{man['sources']}`: {len(docs)} files. Rendered from `manifest.json`; do not edit.", "",
         "| File | Kind | doc_id | Part(s) | Doc no. | Rev | Date | Pages | Text layer | Textless pages | Origin | sha256[:12] |",
         "|---|---|---|---|---|---|---|---:|---|---|---|---|"]
    for r in docs:
        tl = r.get("pages_textless") or []
        tls = ",".join(map(str, tl[:8])) + ("..." if len(tl) > 8 else "")
        origin = (r.get("origin") or {}).get("host_url") or ""
        origin = re.sub(r"^(https?://[^/]+).*$", r"\1", origin)
        extra = []
        if r.get("superseded_by"):
            extra.append(f"superseded by {r['superseded_by']}")
        if r.get("duplicate_of"):
            extra.append(f"duplicate of {r['duplicate_of']}")
        if r["kind"] == "schematic" and "net_info" in r:
            extra.append("net info" if r["net_info"] else "NO net info")
        if r.get("kind_classified"):
            extra.append(f"engineer override, classified {r['kind_classified']}")
        kind = r["kind"] + (f" ({'; '.join(extra)})" if extra else "")
        L.append("| " + " | ".join(_cell(x) for x in (
            r["file"], kind, r["doc_id"], ", ".join(r.get("parts") or []), r.get("doc_no"), r.get("rev"),
            r.get("date"), r.get("pages", ""), r.get("text_layer", ""), tls, origin, r["sha256"][:12])) + " |")
    d = man.get("diff") or {}
    L += ["", "## Change vs previous manifest", ""]
    if d.get("previous") is None:
        L.append("First inventory: every file is new.")
    else:
        for k in ("added", "removed", "changed", "unchanged"):
            L.append(f"- **{k}** ({len(d.get(k) or [])}): {', '.join(d.get(k) or []) or '-'}")
        for f, pgs in sorted((d.get("changed_pages") or {}).items()):
            L.append(f"- pages changed in {f}: {', '.join(map(str, pgs)) or 'none (metadata only)'}")
    notes = []
    for r in docs:
        if r.get("pages_textless"):
            notes.append(f"- {r['file']}: {len(r['pages_textless'])} page(s) are an image with no text layer; "
                         "extract the embedded image at native size before reading them")
        if r["kind"] == "schematic" and r.get("net_info") is False:
            notes.append(f"- {r['file']}: schematic PDF without net bookmarks; ask the EE to re-export with "
                         "\"net information\" and \"component parameters\" on")
        if r.get("needs_export"):
            notes.append(f"- {r['file']}: a PRD in Word; export it to PDF and put the PDF in the sources folder "
                         "(the tool does not convert it)")
    if notes:
        L += ["", "## Notes", ""] + notes
    return "\n".join(L) + "\n"


def _prune_text_cache(atlas_dir: Path, docs: list[dict[str, Any]]) -> None:
    """Drop the page text of documents no longer in the manifest."""
    keep = {text_cache_path(atlas_dir, d["sha256"]).name for d in docs}
    folder = Path(atlas_dir) / "inventory" / TEXT_DIR
    for f in folder.glob("*.txt.gz") if folder.is_dir() else []:
        if f.name not in keep:
            f.unlink()


def run(atlas_dir: Path, sources: Path | None = None) -> dict[str, Any]:
    atlas_dir = Path(atlas_dir)
    cfg = project.load_config(atlas_dir) if (atlas_dir / project.CONFIG_NAME).is_file() else {}
    src = Path(sources) if sources else project.sources_dir(atlas_dir, cfg)
    if not src.is_dir():
        raise FileNotFoundError(f"sources dir {src} does not exist")
    fw = project.fw_dir(atlas_dir, cfg)
    try:
        label = src.resolve().relative_to(fw.resolve()).as_posix()
    except ValueError:
        label = str(src)
    out = atlas_dir / "inventory"
    prev_path = out / "manifest.json"
    previous = common.read_json(prev_path) if prev_path.is_file() else None
    prev_sha = common.sha256_file(prev_path) if previous else None
    man = build_manifest(src, label, previous, exclude=[atlas_dir], previous_sha256=prev_sha,
                         overrides=answers.load_documents(atlas_dir), atlas_dir=atlas_dir)
    common.write_json(prev_path, man)
    _prune_text_cache(atlas_dir, man["documents"])
    common.write_text(out / "manifest.md", render_md(man))
    return man


def _cmd(args) -> int:
    adir = common.atlas_dir(args.atlas_dir)
    t0 = _dt.datetime.now()
    try:
        man = run(adir, Path(args.sources) if args.sources else None)
    except (FileNotFoundError, ValueError) as e:
        print(f"inventory: {e}", file=sys.stderr)
        return 2
    d = man["diff"]
    kinds: dict[str, int] = {}
    for r in man["documents"]:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    pages = sum(r.get("pages") or 0 for r in man["documents"])
    print(f"{len(man['documents'])} files ({', '.join(f'{v} {k}' for k, v in sorted(kinds.items()))}), "
          f"{pages} PDF pages -> {adir / 'inventory' / 'manifest.json'} "
          f"in {(_dt.datetime.now() - t0).total_seconds():.1f} s")
    if d.get("previous") is not None:
        print(f"vs previous: {len(d['added'])} added, {len(d['removed'])} removed, {len(d['changed'])} changed, "
              f"{len(d['unchanged'])} unchanged")
    return 0


def register(sub, groups) -> None:
    p = sub.add_parser("inventory", help="manifest of the supplied documents + per-page hashes + diff vs last")
    p.add_argument("--sources", help="documents dir (default: [project] sources in atlas.toml)")
    p.set_defaults(func=_cmd)

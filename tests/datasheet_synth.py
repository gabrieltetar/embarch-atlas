"""A synthetic five-page datasheet (PyMuPDF, Courier) and LLM-style records for it.

Nothing here comes from a real document: part SYN1234, made-up values. Page 2 is an EC table in the
usual PARAMETER / TEST CONDITIONS / MIN TYP MAX UNIT layout with a footnote; page 5 is a register map
in TI's layout (heading with a binary reset printed with "h", bit diagram tokens, field table,
summary table).
"""
from __future__ import annotations

from pathlib import Path

import pymupdf

DOC_ID = "syn1234@2b"
FONT = dict(fontname="cour", fontsize=8)

EC_ROWS = [
    # sym, param, cond, min, typ, max, unit
    ("VUVLO", "Undervoltage lockout", "VIN rising", "2.6", "2.8", "3.05", "V"),
    ("VHYS", "UVLO hysteresis", "", "", "210", "", "mV"),
    ("IQ", "Quiescent current", "VIN = 7 V", "", "355", "505", "uA"),
    ("tON", "Turn-on delay (1)", "EN rising", "1.5", "2.5", "4.5", "ms"),
    ("TSD", "Thermal shutdown", "TJ rising", "", "165", "", "C"),
    ("fSW", "Switching frequency", "RFSW = 91 k", "362", "405", "447", "kHz"),
    ("VOL", "Output low voltage", "ISINK = 3 mA", "", "0.12", "0.33", "V"),
    ("ILK", "Input leakage", "VI = 5.5 V", "-1.2", "", "1.3", "uA"),
]
X = {"sym": 30, "param": 80, "cond": 250, "min": 380, "typ": 425, "max": 470, "unit": 520}


def _t(page, x, y, s, **kw):
    page.insert_text((x, y), s, **{**FONT, **kw})


def build_pdf(path: Path) -> Path:
    doc = pymupdf.open()
    # p1: front page
    p = doc.new_page(width=612, height=792)
    _t(p, 30, 60, "SYN1234 Synthetic Load Switch", fontsize=14)
    _t(p, 30, 90, "1 Features")
    _t(p, 30, 110, "Input range 2.9 V to 18 V, 4 A continuous.")
    _t(p, 30, 125, "Thermal shutdown at 165 C typical.")
    _t(p, 30, 760, "SYN1234 Revision 2b")
    # p2: EC table
    p = doc.new_page(width=612, height=792)
    _t(p, 30, 50, "6 Specifications")
    _t(p, 30, 70, "6.5 Electrical Characteristics")
    y = 100
    for k, name in (("sym", "SYMBOL"), ("param", "PARAMETER"), ("cond", "TEST CONDITIONS"),
                    ("min", "MIN"), ("typ", "TYP"), ("max", "MAX"), ("unit", "UNIT")):
        _t(p, X[k], y, name)
    for row in EC_ROWS:
        y += 16
        for k, v in zip(("sym", "param", "cond", "min", "typ", "max", "unit"), row):
            if v:
                _t(p, X[k], y, v)
    _t(p, 30, 700, "(1) Specified by design; not production tested.")
    # p3: prose
    p = doc.new_page(width=612, height=792)
    _t(p, 30, 50, "7 Detailed Description")
    _t(p, 30, 70, "7.1 Overview")
    _t(p, 30, 90, "When TJ exceeds 165 C the switch turns off. It restarts once TJ falls below 142 C.")
    _t(p, 30, 105, "The OUT pin must not be driven above 6.5 V while EN is low.")
    _t(p, 30, 120, "The rise time is measured from 10-90% of the output step.")
    # p4: a raster figure, almost no text
    p = doc.new_page(width=612, height=792)
    _t(p, 30, 50, "7.2 Timing")
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 40), 0)
    pix.clear_with(200)
    p.insert_image(pymupdf.Rect(40, 80, 560, 560), pixmap=pix)
    _t(p, 200, 580, "Figure 7-1. Turn-on timing")
    # p5: TI-style register map
    p = doc.new_page(width=612, height=792)
    lines = [
        "7.6 Register Maps",
        "7.6.1 CTRL Register (Address = 0h) [reset = 10000001h]",
        "                        Figure 7-2. CTRL Register",
        "     7           6        5        4        3        2         1          0",
        "     EN                            RESERVED                               STAT",
        "     R/W-1b                        R/W-000000b                            R-1b",
        "                  Table 7-3. CTRL Register Field Descriptions",
        " Bit      Field        Type     Reset       Description",
        " 7        EN           R/W      1b          Enable",
        "                                            1b = enabled (default)",
        " 6-1      RESERVED     R        000000b     Reserved",
        " 0        STAT         R        1b          Status. Does not reset until it is read.",
        "",
        "7.6.2 DATA Register (Address = 1h, 2h) [reset = 00000011h, 00000000h]",
        "                        Figure 7-3. DATA Register",
        "     R/W-000000b                           R/W-0000000011b",
        "                  Table 7-4. DATA Register Field Descriptions",
        " Bit      Field        Type     Reset       Description",
        " 15-10    RESERVED     R/W      000000b     Reserved",
        " 9-0      VALUE        R/W      0000000011b Output setting, 10 bits",
        "",
        "7.6.3 Register Summary",
        "                  Table 7-5. Default Settings of Registers",
        " Register Address    Register Name    R/W      Default Values",
        " 00h                 CTRL             R/W      10000001",
        " 01h                 DATA_LSB         R/W      00000011",
        " 02h                 DATA_MSB         R/W      00000000",
        " 03h                 GHOST            R/W      00000000",
    ]
    for i, ln in enumerate(lines):
        _t(p, 20, 50 + 13 * i, ln)
    doc.set_toc([[1, "1 Features", 1], [1, "6 Specifications", 2], [2, "6.5 Electrical Characteristics", 2],
                 [2, "Table 6-1. Electrical Characteristics", 2], [1, "7 Detailed Description", 3],
                 [2, "7.1 Overview", 3], [2, "7.2 Timing", 4], [2, "7.6 Register Maps", 5]])
    doc.set_metadata({"title": "SYN1234 Synthetic Load Switch", "keywords": "SYN1234"})
    doc.save(path)
    return path


def ec_quote(row) -> str:
    """The numbers line of a row, as the text layer holds it."""
    return " ".join(v for v in row[2:] if v)


def _num(s: str):
    if not s:
        return None
    f = float(s)
    return int(f) if f.is_integer() and "." not in s else f


def records(doc_id: str = DOC_ID) -> list[dict]:
    part = doc_id.split("@")[0]
    unit = {"uA": "uA", "C": "C"}
    recs = []
    for row in EC_ROWS:
        sym, param, cond, mn, ty, mx, u = row
        rid = f"{part}.ec.{sym}" + (f".{cond.split()[0].lower()}" if cond else "")
        r = {"id": rid, "kind": "limit", "doc": doc_id, "p": 2, "q": ec_quote(row), "src": "table", "group": "ec",
             "sym": sym, "param": param.replace(" (1)", ""), "cond": cond or None, "min": _num(mn), "typ": _num(ty),
             "max": _num(mx), "unit": unit.get(u, u)}
        if "(1)" in param:
            r["fn"] = [1]
            r["guar"] = "design"
        recs.append({k: v for k, v in r.items() if v is not None})
    recs += [
        {"id": f"{part}.rule.tsd", "kind": "rule", "doc": doc_id, "p": 3, "src": "text",
         "q": "When TJ exceeds 165 C the switch turns off.", "cond": "TJ > 165 C", "text": "switch turns off",
         "recover": "TJ < 142 C"},
        {"id": f"{part}.rule.out_max", "kind": "rule", "doc": doc_id, "p": 3, "src": "text",
         "q": "The OUT pin must not be driven above 6.5 V while EN is low.", "text": "OUT <= 6.5 V while EN low"},
        {"id": f"{part}.fig.figure-7-1", "kind": "figure", "doc": doc_id, "p": 4, "q": None, "src": "figure",
         "fig": "Figure 7-1", "caption": "Turn-on timing", "what": "timing; EN to OUT"},
        {"id": f"{part}.quirk.tsd_above_operating", "kind": "quirk", "doc": doc_id, "p": [1, 2], "src": "text",
         "q": ["Thermal shutdown at 165 C typical."], "class": "absence", "text": "TSD is typical only (165 C); no min.",
         "refs": [f"{part}.ec.TSD.tj"]},
        {"id": f"{part}.check.uvlo_order", "kind": "check", "doc": doc_id, "p": [2], "src": "text",
         "expr": f"R['{part}.ec.VUVLO.vin'].max < 3.1 and all(R[k].typ > 0 for k in ['{part}.ec.IQ.vin'])",
         "expect": "pass", "why": "UVLO below 3.1 V"},
    ]
    return recs


def make_doc(tmp: Path, bbox: bool = False) -> tuple[Path, Path]:
    """Build the PDF, run doc prep into <tmp>/atlas, write the records. Returns (atlas, docdir)."""
    from embarch_atlas import common, pdftext

    tmp = Path(tmp)
    pdf = build_pdf(tmp / "SYN1234-2b.pdf")
    atlas = tmp / "atlas"
    docdir = pdftext.prep(pdf, atlas, bbox=bbox, log=lambda *_: None)
    by_kind: dict[str, list[dict]] = {}
    for r in records(docdir.name):
        by_kind.setdefault(r["kind"], []).append(r)
    for k, rs in by_kind.items():
        common.write_jsonl(docdir / "records" / f"{k}.jsonl", rs)
    return atlas, docdir

"""inventory: vendor conventions, per-page hashes, the diff block, byte-identical re-runs.

Every document here is synthetic (fictional part and document numbers), built
with PyMuPDF in the test; only the vendors' public numbering conventions are real.
"""
from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import pymupdf
import pytest

from embarch_atlas import cli, common, inventory as inv

pytestmark = pytest.mark.skipif(not all(shutil.which(t) for t in ("pdfinfo", "pdftotext", "pdfimages")),
                                reason="poppler CLI tools not installed")

FILLER = ("This device is intended for use in general purpose applications and offers a wide operating range "
          "with low quiescent current and robust protection features for every typical use case.")


def make_pdf(path: Path, pages: list[str], meta: dict[str, str] | None = None, image_on: set[int] = frozenset()) -> Path:
    doc = pymupdf.open()
    for i, text in enumerate(pages, 1):
        doc.new_page(width=595, height=842)
    for i, text in enumerate(pages, 1):
        page = doc[i - 1]
        if text:
            page.insert_textbox(pymupdf.Rect(50, 50, 545, 800), text, fontsize=10, fontname="helv")
        if i in image_on:
            pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 120, 90), False)
            pix.clear_with(128)
            page.insert_image(pymupdf.Rect(100, 100, 400, 325), pixmap=pix)
    if meta:
        doc.set_metadata(meta)
    doc.save(path)
    doc.close()
    return path


def make_xlsx(path: Path) -> Path:
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook xmlns="{main}" xmlns:r="{rel}"><sheets>'
                                      '<sheet name="Truth" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   f'<Relationship Id="rId1" Type="{rel}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr("xl/sharedStrings.xml", f'<sst xmlns="{main}"><si><t>MODE</t></si><si><t>ON</t></si></sst>')
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{main}"><sheetData>'
                                               '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><v>1</v></c></row>'
                                               '<row r="2"><c r="A2" t="s"><v>1</v></c><c r="C2"><v>0</v></c></row>'
                                               '</sheetData></worksheet>')
        z.writestr("docProps/core.xml", '<cp:coreProperties xmlns:cp="x" xmlns:dcterms="y">'
                                        '<dcterms:modified>2026-01-15T10:00:00Z</dcterms:modified></cp:coreProperties>')
    return path


def make_sources(root: Path) -> Path:
    src = root / "hardware"
    src.mkdir(parents=True)
    make_pdf(src / "zx1234.pdf", [
        f"ZX1234\nSLVS999B - MARCH 2020 - REVISED JUNE 2021\nZX1234 Buck Converter\nFeatures\n{FILLER}",
        f"Pin configuration\n{FILLER}"],
        {"title": "ZX1234 Buck Converter datasheet (Rev. B)", "author": "Texas Instruments, Incorporated [SLVS999,B"})
    (src / "zx1234.pdf:Zone.Identifier").write_text("[ZoneTransfer]\nZoneId=3\nHostUrl=https://vendor.example/zx1234.pdf\n")
    make_pdf(src / "rm-qq32.pdf", [
        f"RM9999\nReference manual\nQQ32F4x1 advanced 32-bit MCUs\n{FILLER}",
        f"Chapter 2 Memory\n{FILLER}",
        f"Revision history\n05-Jan-2024\n{FILLER}\nRM9999 Rev 3"])
    make_pdf(src / "es-qq32.pdf", [f"QQ32F4x1 device errata\nErrata sheet\n{FILLER}", f"Summary\n{FILLER}\nES9999 Rev 2"])
    make_pdf(src / "mx9999.pdf", [
        f"MX9999/MX9998\nDual Fuel Gauge\nGeneral Description\n{FILLER}\n19-1234; Rev 2; 3/19",
        f"Absolute maximum ratings\n{FILLER}"])
    make_pdf(src / "KX1234-03a.pdf", [f"KX1234\nLoad Switch\nFeatures\n{FILLER}", f"Ordering\n{FILLER}\nMarch 2026 - Revision 03a"])
    make_pdf(src / "board.pdf", [f"Main.SchDoc Revision: B Date: 1/2/2026 Sheet 1 of 2 {FILLER}",
                                 f"Power.SchDoc Revision: B Date: 1/2/2026 Sheet 2 of 2 {FILLER}"],
             {"creator": "Altium Designer"})
    make_pdf(src / "qq5.pdf", [f"QQ5566 Sensor\nDatasheet\nFeatures\n{FILLER}", ""], image_on={2})
    (src / "notes.md").write_text("# Bench notes\nUpdated 2026-01-02\n\nNothing to see.\n")
    make_xlsx(src / "truth.xlsx")
    (src / ".hidden.pdf").write_bytes(b"not a pdf")
    return src


@pytest.fixture()
def world(tmp_path):
    src = make_sources(tmp_path)
    return tmp_path / "atlas", src


def docs_by_file(man):
    return {d["file"]: d for d in man["documents"]}


def test_manifest_fields(world):
    adir, src = world
    man = inv.run(adir, src)
    D = docs_by_file(man)
    assert sorted(D) == ["KX1234-03a.pdf", "board.pdf", "es-qq32.pdf", "mx9999.pdf", "notes.md", "qq5.pdf",
                         "rm-qq32.pdf", "truth.xlsx", "zx1234.pdf"]
    ti = D["zx1234.pdf"]
    assert (ti["kind"], ti["doc_no"], ti["rev"], ti["date"], ti["part"]) == ("datasheet", "SLVS999", "B", "JUNE 2021", "ZX1234")
    assert ti["doc_id"] == "zx1234@B" and ti["origin"] == {"host_url": "https://vendor.example/zx1234.pdf"}
    assert ti["pages"] == 2 and len(ti["page_hashes"]) == 2 and all(len(h) == 16 for h in ti["page_hashes"])
    assert ti["text_layer"] == "full" and len(ti["sha256"]) == 64
    rm = D["rm-qq32.pdf"]
    assert (rm["kind"], rm["doc_no"], rm["rev"], rm["date"], rm["doc_id"]) == ("reference-manual", "RM9999", "3",
                                                                                "05-Jan-2024", "rm9999@3")
    es = D["es-qq32.pdf"]
    assert (es["kind"], es["doc_no"], es["rev"], es["doc_id"]) == ("errata", "ES9999", "2", "es9999@2")
    mx = D["mx9999.pdf"]
    assert (mx["doc_no"], mx["rev"], mx["date"]) == ("19-1234", "2", "3/19") and {"MX9999", "MX9998"} <= set(mx["parts"])
    kx = D["KX1234-03a.pdf"]
    assert (kx["rev"], kx["date"], kx["doc_id"]) == ("03a", "March 2026", "kx1234@03a")
    sch = D["board.pdf"]
    assert (sch["kind"], sch["rev"], sch["date"], sch["net_info"]) == ("schematic", "B", "1/2/2026", False)
    assert sch["parts"] == [] and sch["sheets"][1] == {"page": 2, "schdoc": "Power.SchDoc", "sheet": "2/2"}
    img = D["qq5.pdf"]
    assert img["pages_textless"] == [2] and img["text_layer"] == "partial" and img["page_words"][1] == 0
    assert D["notes.md"]["kind"] == "notes" and D["notes.md"]["date"] == "2026-01-02"
    xl = D["truth.xlsx"]
    assert xl["kind"] == "spreadsheet" and xl["sheets"] == [{"name": "Truth", "rows": 2, "cols": 3}]
    assert xl["date"] == "2026-01-15" and len(xl["page_hashes"]) == 1
    assert man["diff"]["previous"] is None and sorted(man["diff"]["added"]) == sorted(D)
    md = (adir / "inventory" / "manifest.md").read_text()
    assert "| zx1234.pdf | datasheet | zx1234@B |" in md and "First inventory" in md
    assert "NO net info" in md and "re-export" in md


def test_byte_identical_and_diff(world):
    adir, src = world
    inv.run(adir, src)
    mj, mm = adir / "inventory" / "manifest.json", adir / "inventory" / "manifest.md"
    first = (mj.read_bytes(), mm.read_bytes())
    inv.run(adir, src)
    assert (mj.read_bytes(), mm.read_bytes()) == first  # nothing changed: byte-identical
    # change page 2 of one file, add one, remove one
    make_pdf(src / "mx9999.pdf", [
        f"MX9999/MX9998\nDual Fuel Gauge\nGeneral Description\n{FILLER}\n19-1234; Rev 2; 3/19",
        f"Absolute maximum ratings, corrected\n{FILLER}"])
    make_pdf(src / "new.pdf", [f"NW7788\nFeatures\n{FILLER}"])
    (src / "notes.md").unlink()
    prev_sha = common.sha256_file(mj)
    man = inv.run(adir, src)
    d = man["diff"]
    assert d["previous"] == prev_sha
    assert (d["added"], d["removed"], d["changed"]) == (["new.pdf"], ["notes.md"], ["mx9999.pdf"])
    assert d["changed_pages"] == {"mx9999.pdf": [2]} and "zx1234.pdf" in d["unchanged"]
    second = (mj.read_bytes(), mm.read_bytes())
    inv.run(adir, src)  # the last real change stays visible, and the run is again byte-identical
    assert (mj.read_bytes(), mm.read_bytes()) == second
    assert "pages changed in mx9999.pdf: 2" in mm.read_text()


def test_supersession_and_duplicates(world):
    adir, src = world
    make_pdf(src / "zx1234-old.pdf", [f"ZX1234\nSLVS999A - MARCH 2020 - REVISED MAY 2020\nFeatures\n{FILLER}"],
             {"title": "ZX1234 Buck Converter datasheet (Rev. A)", "author": "Texas Instruments, Incorporated [SLVS999,A"})
    shutil.copy(src / "KX1234-03a.pdf", src / "copy-of-kx.pdf")
    D = docs_by_file(inv.run(adir, src))
    assert D["zx1234-old.pdf"]["superseded_by"] == "zx1234.pdf" and "superseded_by" not in D["zx1234.pdf"]
    assert D["copy-of-kx.pdf"]["duplicate_of"] == "KX1234-03a.pdf"


def test_cli_uses_atlas_toml(world, capsys):
    adir, src = world
    adir.mkdir(parents=True)
    (adir / "atlas.toml").write_text(f'[project]\nfw = "{src.parent}"\nsources = "hardware"\n')
    assert cli.main(["--atlas-dir", str(adir), "inventory"]) == 0
    assert "9 files" in capsys.readouterr().out
    assert common.read_json(adir / "inventory" / "manifest.json")["sources"] == "hardware"


def test_detectors_are_pure():
    d = inv.detect_ids("datasheet", {"Title": "AB12345 thing"}, ["AB12345 Features Rev. C January 2025", "x"])
    assert (d["rev"], d["date"], d["parts"][0]) == ("C", "January 2025", "AB12345")
    assert inv.doc_id_for({"kind": "datasheet", "part": "QQ32F4xE/xG", "doc_no": "DS1111", "rev": "4", "file": "a.pdf"}) \
        == "ds1111@4"
    assert inv.doc_id_for({"kind": "other", "part": None, "doc_no": None, "rev": None, "file": "My Notes.pdf"}) \
        == "my-notes@norev"

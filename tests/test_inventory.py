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

from embarch_atlas import answers, cli, common, inventory as inv, project, update

pytestmark = pytest.mark.skipif(not all(shutil.which(t) for t in ("pdfinfo", "pdftotext")),
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


def test_inventory_and_doc_prep_agree_on_textless_pages(tmp_path):
    """One classification (pdftext.text_layer) for both: a logo and a header line on every page
    make no page "has an image" or "text", and the lists of textless pages are the same."""
    from embarch_atlas import pdftext

    src = tmp_path / "hardware"
    src.mkdir()
    doc = pymupdf.open()
    logo = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 160, 80), False)
    logo.clear_with(90)
    pic = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 300, 300), False)
    pic.clear_with(200)
    bodies = [f"QQ7 Product Requirements Document\nVersion: 2.0\n{FILLER}\n{FILLER}", FILLER * 2, None,
              "Packing notes: see above.", FILLER]
    for i, body in enumerate(bodies, 1):
        p = doc.new_page(width=612, height=792)
        p.insert_text((40, 30), "QQ7 Product Requirements Document Version 2.0", fontname="helv", fontsize=9)
        p.insert_image(pymupdf.Rect(480, 8, 580, 58), pixmap=logo)        # the same logo on every page
        if body:
            p.insert_textbox(pymupdf.Rect(40, 90, 570, 740), body, fontsize=10, fontname="helv")
        else:
            p.insert_image(pymupdf.Rect(60, 100, 550, 600), pixmap=pic)   # a picture page, header only
    doc.save(src / "QQ7 PRD.pdf")
    man = inv.run(tmp_path / "atlas", src)
    rec = man["documents"][0]
    assert rec["pages_textless"] == [3] and rec["text_layer"] == "partial"
    assert rec["pages_with_images"] == 1                      # the logo is not an image of any page
    assert 4 not in rec["pages_textless"] and rec["pages_near_empty"] == [4]
    d = pdftext.prep(src / "QQ7 PRD.pdf", tmp_path / "atlas", log=lambda *_: None)
    meta = common.read_json(d / "doc.json")
    assert (meta["text_layer"], meta["pages_textless"]) == (rec["text_layer"], rec["pages_textless"])
    assert d.name == rec["doc_id"]


# ---------------------------------------------------------------- what makes a schematic

def make_docx(path: Path, title: str | None = None) -> Path:
    core = ('<cp:coreProperties xmlns:cp="x" xmlns:dc="http://purl.org/dc/elements/1.1/">'
            + (f"<dc:title>{title}</dc:title>" if title else "") + "</cp:coreProperties>")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("docProps/core.xml", core)
        z.writestr("word/document.xml", "<document/>")
    return path


@pytest.mark.parametrize("creator,is_schematic", [
    ("Altium Designer", True),
    ("KiCad Schematic Editor", True),
    ("OrCAD Capture CIS", True),
    ("Capture 17.2", True),
    ("Allegro Design Entry", True),
    ("PADS Logic", True),
    ("Acrobat Web Capture 10.0", False),
    ("Screen Capture Tool", False),
    ("iPads Exporter", False),
    ("Allegro MicroSystems", False),
    ("Prince 12", False),
])
def test_schematic_creator_is_a_word_not_a_substring(tmp_path, creator, is_schematic):
    pdf = make_pdf(tmp_path / "doc.pdf", [f"A short note about a part.\n{FILLER}"], {"producer": creator})
    assert (project.schematic_evidence(pdf) is not None) is is_schematic
    assert (inv.inventory_file(pdf, "doc.pdf")["kind"] == "schematic") is is_schematic


def test_schematic_evidence_from_text_and_outline(tmp_path):
    a = make_pdf(tmp_path / "a.pdf", [f"Main.SchDoc Sheet 1 of 1 {FILLER}"], {"producer": "Acrobat Web Capture 10.0"})
    assert project.schematic_evidence(a) == ".SchDoc in the text"
    b = tmp_path / "b.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((50, 50), "Sheet one")
    doc.set_toc([[1, "Nets", 1]])
    doc.save(b)
    doc.close()
    assert project.schematic_evidence(b) == "net information in the outline"
    assert inv.inventory_file(b, "b.pdf")["net_info"] is True


def test_init_board_hint_uses_the_same_evidence(tmp_path):
    make_pdf(tmp_path / "note.pdf", [f"A note.\n{FILLER}"], {"producer": "Acrobat Web Capture 10.0"})
    make_pdf(tmp_path / "main-board.pdf", [f"A board.\n{FILLER}"], {"creator": "Altium Designer"})
    assert project._schematic_candidates(tmp_path) == ["main-board.pdf"]


# ---------------------------------------------------------------- application notes

@pytest.mark.parametrize("name,pages,meta", [
    ("note.pdf", ["Features of the bus\n" + FILLER], {"title": "Application Note: bus layout for ZX1234"}),
    ("note.pdf", ["QQ5566 App Note\nFeatures of the bus\n" + FILLER], None),
    ("ZX1234 user guide.pdf", ["Features of the kit\n" + FILLER], None),
    ("note.pdf", ["ZX1234 User's Guide\nFeatures\n" + FILLER], None),
    ("note.pdf", ["Technical Note\nFeatures\n" + FILLER], None),
    ("note.pdf", ["Tech note 12\nFeatures\n" + FILLER], None),
    ("note.pdf", ["Features\n" + FILLER, FILLER + "\nAN9988 Rev 3"], None),
    ("note.pdf", ["Features\n" + FILLER, FILLER + "\nUM9988 Rev 3"], None),
    ("note.pdf", ["Features\n" + FILLER, FILLER + "\nTN9988 Rev 3"], None),
    ("an9988-bus-layout.pdf", ["Features\n" + FILLER], None),
    ("note.pdf", ["UM9988\nUser manual\nFeatures\n" + FILLER], None),
    ("report.pdf", ["Application Report\nFeatures\n" + FILLER], None),
    ("report.pdf", ["Features\n" + FILLER], {"title": "ZX1234 Application Report: bus layout"}),
    ("zx1234-application-report.pdf", ["Features\n" + FILLER], None),
    ("guide.pdf", ["ZX1234 User's Guide\nSee the ZX1234 Reference Manual for the registers.\n" + FILLER], None),
    ("note.pdf", ["Application note\nSee the reference manual for the registers.\n" + FILLER], None),
])
def test_appnote_is_checked_before_the_datasheet_rule(tmp_path, name, pages, meta):
    rec = inv.inventory_file(make_pdf(tmp_path / name, pages, meta), name)
    assert rec["kind"] == "appnote"


@pytest.mark.parametrize("name,pages,meta", [
    ("zx1234-user-guide.pdf", ["ZX1234 Reference Manual\nRegister map\n" + FILLER], None),
    ("guide.pdf", ["ZX1234 User's Guide\nZX1234 Reference Manual\n" + FILLER], None),
    ("guide.pdf", ["ZX1234 Series\nUser Guide and Reference Manual\n" + FILLER], {"title": "ZX1234 User's Guide"}),
])
def test_a_users_guide_that_is_a_reference_manual_is_one(tmp_path, name, pages, meta):
    assert inv.inventory_file(make_pdf(tmp_path / name, pages, meta), name)["kind"] == "reference-manual"


def test_an_application_note_naming_a_manual_is_not_one(tmp_path):
    pdf = make_pdf(tmp_path / "guide.pdf", ["User's Guide\nApplication Note\nZX1234 Reference Manual\n" + FILLER])
    assert inv.inventory_file(pdf, "guide.pdf")["kind"] == "appnote"


def test_a_datasheet_citing_an_appnote_stays_a_datasheet(tmp_path):
    long_top = "ZX1234 Buck Converter\nFeatures\n" + FILLER + "\n" + FILLER + "\n" + FILLER
    pdf = make_pdf(tmp_path / "zx1234.pdf", [long_top + "\nSee application note AN9988 for the layout.",
                                             "DS1111 Rev 4\n" + FILLER + "\nAN9988 Rev 3"])
    assert inv.inventory_file(pdf, "zx1234.pdf")["kind"] == "datasheet"


@pytest.mark.parametrize("name,pages,meta", [
    ("ds_zx1234.pdf", ["ZX1234\nFeatures\nAN9988 (device application note)\n" + FILLER],
     {"title": "Datasheet - ZX1234 - buck converter"}),
    ("zx1234-datasheet.pdf", ["ZX1234\nFeatures\nAN9988 (device application note)\n" + FILLER], None),
    ("ds_zx1234.pdf", ["Datasheet\nZX1234\nFeatures\nAN9988 (device application note)\n" + FILLER], None),
])
def test_what_a_datasheet_calls_itself_beats_a_note_its_first_page_cites(tmp_path, name, pages, meta):
    assert inv.inventory_file(make_pdf(tmp_path / name, pages, meta), name)["kind"] == "datasheet"


def test_appnote_doc_id_is_its_number_or_stem_never_a_part(tmp_path):
    a = inv.inventory_file(make_pdf(tmp_path / "a.pdf", [f"AN9988\nApplication note\nUsing the ZX1234\n{FILLER}\nAN9988 Rev 3"]), "a.pdf")
    assert (a["kind"], a["doc_no"], a["doc_id"]) == ("appnote", "AN9988", "an9988@3") and "ZX1234" in a["parts"]
    b = inv.inventory_file(make_pdf(tmp_path / "Bus Layout.pdf", [f"Application note\nUsing the ZX1234\n{FILLER}"]), "Bus Layout.pdf")
    assert b["doc_id"] == "bus-layout@norev" and b["parts"] == ["ZX1234"]


# ---------------------------------------------------------------- part tokens and doc_id stability

def test_a_package_name_does_not_become_a_part():
    d = inv.detect_ids("datasheet", {"Title": "ZX7700 wireless MCU"}, ["CSP47 CSP47 CSP47 WLP36 FBGA484 TO220 ZX7700"])
    assert d["parts"] == ["ZX7700"]
    alone = inv.detect_ids("datasheet", {}, ["CSP47 package with 47 balls, CSP47 CSP47"])
    assert alone["parts"] == [] and inv.doc_id_for({"kind": "datasheet", "part": None, "doc_no": None, "rev": "2",
                                                    "file": "zx7700.pdf"}) == "zx7700@2"


def test_a_part_with_a_package_like_prefix_stays_a_part():
    d = inv.detect_ids("datasheet", {}, ["CSP12345 Sensor, TO22A7 switch"])
    assert set(d["parts"]) == {"CSP12345", "TO22A7"}


def test_a_vendor_internal_number_does_not_become_a_part():
    d = inv.detect_ids("datasheet", {}, ["AP02001 AP02001 AP02001 Logic gate ZY1234"])
    assert d["parts"] == ["ZY1234"]
    assert inv.detect_ids("datasheet", {}, ["QL072 op-amp"])["parts"] == ["QL072"]    # three digits: a part number


def test_a_normal_datasheet_keeps_its_doc_id(world):
    """Frozen distillations are keyed by doc_id: these must not move."""
    adir, src = world
    D = docs_by_file(inv.run(adir, src))
    got = {f: D[f]["doc_id"] for f in ("zx1234.pdf", "KX1234-03a.pdf", "rm-qq32.pdf", "es-qq32.pdf", "mx9999.pdf", "qq5.pdf")}
    assert got == {"zx1234.pdf": "zx1234@B", "KX1234-03a.pdf": "kx1234@03a", "rm-qq32.pdf": "rm9999@3",
                   "es-qq32.pdf": "es9999@2", "mx9999.pdf": "mx9998@2", "qq5.pdf": "qq5566@norev"}


# ---------------------------------------------------------------- Word files

@pytest.mark.parametrize("name,title,kind", [
    ("QQ7 PRD.docx", None, "prd"),
    ("qq7_requirements.docx", None, "prd"),
    ("spec.docx", "QQ7 Product Requirements", "prd"),
    ("QQ7-PRD.doc", None, "prd"),
    ("spec.docx", "Bench plan", "other"),
    ("minutes.doc", None, "other"),
])
def test_word_prd_needs_export(tmp_path, name, title, kind):
    (make_docx(tmp_path / name, title) if name.endswith("x") else tmp_path / name).touch()
    rec = inv.inventory_file(tmp_path / name, name)
    assert rec["kind"] == kind and rec.get("needs_export", False) is (kind == "prd")
    assert rec["doc_id"].split("@")[1] == "norev" and "pages" not in rec


def test_word_prd_is_superseded_by_its_exported_pdf(tmp_path):
    src = tmp_path / "hw"
    src.mkdir()
    make_docx(src / "QQ7 PRD.docx")
    adir = tmp_path / "atlas"
    D = docs_by_file(inv.run(adir, src))
    assert D["QQ7 PRD.docx"]["needs_export"] is True and "superseded_by" not in D["QQ7 PRD.docx"]
    assert "export it to PDF" in (adir / "inventory" / "manifest.md").read_text()
    make_pdf(src / "qq7 prd.pdf", [f"QQ7 Product Requirements Document\n{FILLER}"])
    D = docs_by_file(inv.run(adir, src))
    word, pdf = D["QQ7 PRD.docx"], D["qq7 prd.pdf"]
    assert word["superseded_by"] == "qq7 prd.pdf" and "needs_export" not in word and "superseded_by" not in pdf
    assert word["doc_id"] == pdf["doc_id"] == "qq7-prd@norev"
    assert "export it to PDF" not in (adir / "inventory" / "manifest.md").read_text()


def test_a_word_file_with_a_revisioned_pdf_never_supersedes_the_pdf(tmp_path):
    src = tmp_path / "hw"
    src.mkdir()
    make_docx(src / "prd.docx")
    make_pdf(src / "prd.pdf", [f"Requirements document\nVersion: 2.1\n{FILLER}"])
    D = docs_by_file(inv.run(tmp_path / "atlas", src))
    assert D["prd.docx"]["superseded_by"] == "prd.pdf" and "superseded_by" not in D["prd.pdf"]


def test_the_word_original_does_not_stop_update(tmp_path):
    """Both files have one doc_id, and only the PDF is prepped: the Word file is not behind anything."""
    from embarch_atlas import pdftext
    src = tmp_path / "hw"
    src.mkdir()
    adir = tmp_path / "atlas"
    make_docx(src / "prd.docx")
    make_pdf(src / "prd.pdf", [f"Requirements document\n{FILLER}"])
    inv.run(adir, src)
    pdftext.prep(src / "prd.pdf", adir, log=lambda *_: None)
    assert update.stale_translations(adir) == []


# ---------------------------------------------------------------- the engineer's kind override

def _override(adir: Path, doc: dict, kind: str) -> None:
    answers.record_document(adir, doc["sha256"], doc["file"], kind=kind)


def test_kind_override_is_applied_reported_and_byte_stable(world):
    adir, src = world
    doc = docs_by_file(inv.run(adir, src))["zx1234.pdf"]
    _override(adir, doc, "reference-manual")
    D = docs_by_file(inv.run(adir, src))
    got = D["zx1234.pdf"]
    assert (got["kind"], got["kind_classified"]) == ("reference-manual", "datasheet")
    assert got["doc_id"] == "zx1234@B"                 # never moved by an override
    mj, mm = adir / "inventory" / "manifest.json", adir / "inventory" / "manifest.md"
    first = (mj.read_bytes(), mm.read_bytes())
    assert "engineer override, classified datasheet" in mm.read_text()
    inv.run(adir, src)
    assert (mj.read_bytes(), mm.read_bytes()) == first


def test_kind_override_ends_when_the_file_changes(world):
    adir, src = world
    doc = docs_by_file(inv.run(adir, src))["KX1234-03a.pdf"]
    _override(adir, doc, "other")
    assert docs_by_file(inv.run(adir, src))["KX1234-03a.pdf"]["kind"] == "other"
    make_pdf(src / "KX1234-03a.pdf", [f"KX1234\nLoad Switch, new\nFeatures\n{FILLER}", f"Ordering\n{FILLER}\nMarch 2026 - Revision 03a"])
    got = docs_by_file(inv.run(adir, src))["KX1234-03a.pdf"]
    assert got["kind"] == "datasheet" and "kind_classified" not in got


def test_override_makes_a_word_file_a_prd_and_a_schematic_a_datasheet(tmp_path):
    src = tmp_path / "hw"
    src.mkdir()
    adir = tmp_path / "atlas"
    make_docx(src / "spec.docx")
    make_pdf(src / "zz4455.pdf", [f"ZZ4455 Features\n{FILLER}"], {"creator": "Altium Designer"})
    D = docs_by_file(inv.run(adir, src))
    assert D["zz4455.pdf"]["kind"] == "schematic" and D["zz4455.pdf"]["parts"] == []
    _override(adir, D["spec.docx"], "prd")
    _override(adir, D["zz4455.pdf"], "datasheet")
    D = docs_by_file(inv.run(adir, src))
    assert D["spec.docx"]["needs_export"] is True
    assert D["zz4455.pdf"]["kind"] == "datasheet" and D["zz4455.pdf"]["parts"] == ["ZZ4455"]
    assert D["zz4455.pdf"]["doc_id"] == "zz4455@norev"


def test_a_malformed_override_is_said_not_a_traceback(world, capsys):
    adir, src = world
    adir.mkdir(parents=True)
    (adir / "atlas.toml").write_text(f'[project]\nfw = "{src.parent}"\nsources = "hardware"\n')
    answers.path_of(adir).parent.mkdir(parents=True)
    answers.path_of(adir).write_text('[documents."0123456789ab"]\nkind = "memo"\n')
    assert cli.main(["--atlas-dir", str(adir), "inventory"]) == 2
    assert "`kind` must be one of" in capsys.readouterr().err


# ---------------------------------------------------------------- the page text cache

def test_page_text_is_cached_by_content_outside_the_manifest(world):
    adir, src = world
    D = docs_by_file(inv.run(adir, src))
    doc = D["mx9999.pdf"]
    cache = inv.text_cache_path(adir, doc["sha256"])
    assert cache.is_file() and "Absolute maximum ratings" in inv.read_text_cache(cache)[1]
    assert "Absolute maximum ratings" not in (adir / "inventory" / "manifest.json").read_text()
    assert inv.document_pages(adir, doc) == inv.read_text_cache(cache)
    make_pdf(src / "mx9999.pdf", [f"MX9999\nGeneral Description\n{FILLER}\n19-1234; Rev 3; 3/20"])
    D2 = docs_by_file(inv.run(adir, src))
    assert not cache.exists() and inv.text_cache_path(adir, D2["mx9999.pdf"]["sha256"]).is_file()


def test_document_pages_reads_the_file_when_the_cache_is_gone(world, monkeypatch):
    adir, src = world
    (adir).mkdir(parents=True)
    (adir / "atlas.toml").write_text(f'[project]\nfw = "{src.parent}"\nsources = "hardware"\n')
    man = inv.run(adir)
    doc = docs_by_file(man)["mx9999.pdf"]
    cache = inv.text_cache_path(adir, doc["sha256"])
    cache.unlink()
    assert "General Description" in inv.document_pages(adir, doc, man)[0]
    assert cache.is_file()
    calls = []
    monkeypatch.setattr(inv, "_run", lambda *a, **k: calls.append(a) or "")
    assert "General Description" in inv.document_pages(adir, doc, man)[0] and not calls   # no poppler the second time
    cache.unlink()
    (src / "mx9999.pdf").write_bytes(b"other content")
    assert inv.document_pages(adir, doc, man) is None      # the file moved on: nothing to read

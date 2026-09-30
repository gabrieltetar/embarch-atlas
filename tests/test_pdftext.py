import json
from pathlib import Path

import pytest

import datasheet_synth as ds
from embarch_atlas import cli, common, pdftext


def test_normalize_maps_or_strips_symbol_font_glyphs():
    raw = "RL = 100, VIN  5V, 10A"
    assert pdftext.normalize(raw, "map") == "rl = 100ω, vin ≤ 5v, 10µa"
    # the Read tool shows the glyphs as nothing, so a typed quote matches the stripped form
    assert pdftext.normalize(raw, "strip") == "rl = 100, vin 5v, 10a"
    assert pdftext.normalize("RL = 100 , VIN  5V", "map") == "rl = 100, vin 5v"


def test_normalize_dashes_overlines_quotes_hyphenation():
    assert pdftext.normalize("–40 to 125 °C") == "-40 to 125 °c"
    assert pdftext.normalize("FLT̅̅ flag") == "flt flag"
    assert pdftext.normalize("“auto-\n   retry”") == '"auto-retry"'
    assert pdftext.normalize("1 µF") == pdftext.normalize("1 μF")


def test_derive_identity_vendor_conventions():
    ident = pdftext.derive_identity(Path("ABC1234-04b.pdf"), {}, "front page", "Page 1 of 9")
    assert ident["part"] == "ABC1234" and ident["rev"] == "04b"
    ident = pdftext.derive_identity(Path("xyz98765.pdf"), {"title": "XYZ98765 Converter datasheet (Rev. C)",
                                                           "keywords": "SLXX123C"}, "", "")
    assert ident["rev"] == "C" and ident["doc_no"] == "SLXX123C"
    ident = pdftext.derive_identity(Path("notes.pdf"), {}, "nothing here", "")
    assert ident["rev"] == "norev"


@pytest.fixture(scope="module")
def prepped(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("prep")
    pdf = ds.build_pdf(tmp / "SYN1234-2b.pdf")
    docdir = pdftext.prep(pdf, tmp / "atlas", bbox=True, log=lambda *_: None)
    return tmp, pdf, docdir


def test_prep_writes_identity_pages_and_toc(prepped):
    tmp, pdf, docdir = prepped
    assert docdir.name == ds.DOC_ID
    meta = common.read_json(docdir / "doc.json")
    assert meta["pages"] == 5 and meta["sha256"] == common.sha256_file(pdf) and meta["rev"] == "2b"
    assert pdftext.resolve_pdf_path(docdir, meta) == pdf.resolve()
    # pages are 1-based, raw pdftotext -layout
    assert "Electrical Characteristics" in (docdir / "pages" / "p002.txt").read_text()
    bbox = common.read_json(docdir / "pages" / "p002.bbox.json")
    assert bbox["page"] == 2 and any(w[4] == "MIN" for w in bbox["words"])
    toc = common.read_json(docdir / "toc.json")["sections"]
    ids = [s["id"] for s in toc]
    assert ids == ["1", "6", "6.5", "7", "7.1", "7.2", "7.6"]          # the Table entry is dropped
    by = {s["id"]: s for s in toc}
    assert by["6"]["pages"] == [2, 2]      # section 7 starts at the top of p3, so 6 ends on p2
    assert by["7"]["pages"] == [3, 5] and by["6.5"]["parent"] == "6"
    assert by["6.5"]["title"] == "Electrical Characteristics"
    md = (docdir / "toc.md").read_text()
    assert "| 6.5 | · Electrical Characteristics | 2 | - |" in md


def test_page_classes(prepped):
    _, _, docdir = prepped
    pages = {p["page"]: p for p in common.read_json(docdir / "pages" / "pages.json")}
    assert "table-heavy" in pages[2]["classes"]
    assert "register" in pages[5]["classes"]
    assert "figure" not in pages[5]["classes"]       # bit diagrams are captioned figures but are text
    assert {"image-heavy", "figure"} <= set(pages[4]["classes"])
    assert "## Pages that need an image Read" in (docdir / "toc.md").read_text()
    assert "| 4 | image-heavy, figure |" in (docdir / "toc.md").read_text()


def test_doc_id_from_manifest_then_argument(tmp_path):
    pdf = ds.build_pdf(tmp_path / "SYN1234-2b.pdf")
    atlas = tmp_path / "atlas"
    common.write_json(atlas / "inventory" / "manifest.json", {"documents": [
        {"file": "SYN1234-2b.pdf", "sha256": common.sha256_file(pdf), "doc_id": "demo-switch@A", "kind": "datasheet",
         "part": "DEMO", "rev": "A"}]})
    d = pdftext.prep(pdf, atlas, log=lambda *_: None)
    assert d.name == "demo-switch@A"
    assert common.read_json(d / "doc.json")["identity_from"] == "manifest"
    d2 = pdftext.prep(pdf, atlas, doc_id="other-part@7", log=lambda *_: None)
    assert d2.name == "other-part@7" and common.read_json(d2 / "doc.json")["rev"] == "7"
    with pytest.raises(ValueError):
        pdftext.prep(pdf, atlas, doc_id="Bad Id", log=lambda *_: None)


def test_toc_tier2_column_follows_s_files(prepped):
    _, _, docdir = prepped
    (docdir / "s").mkdir(exist_ok=True)
    (docdir / "s" / "6.md").write_text("# spec\n")
    (docdir / "s" / "7.1.md").write_text("# overview\n<!-- atlas:covers 7.2 -->\n")
    pdftext.write_toc(docdir)
    md = (docdir / "toc.md").read_text()
    assert "| 6.5 | · Electrical Characteristics | 2 | s/6.md |" in md   # a descendant of 6
    assert "| 7.2 | · Timing | 4 | s/7.1.md |" in md                   # covered explicitly
    assert "| 7.6 | · Register Maps | 5 | - |" in md


def test_cli_doc_prep(tmp_path, capsys):
    pdf = ds.build_pdf(tmp_path / "SYN1234-2b.pdf")
    rc = cli.main(["--atlas-dir", str(tmp_path / "atlas"), "doc", "prep", str(pdf)])
    assert rc == 0
    assert "syn1234@2b: 5 pages" in capsys.readouterr().out
    assert json.loads((tmp_path / "atlas" / "docs" / "syn1234@2b" / "doc.json").read_text())["kind"] == "datasheet"

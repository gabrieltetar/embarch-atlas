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


# ---------------------------------------------------------------- regressions from the first translate batch
# Every document below is invented: part DEMO-7, made-up headings and numbers.

import pymupdf  # noqa: E402

FILLER = ("The unit samples each input in turn and reports the state of every channel to the host over "
          "the serial link, so the host can plan the next measurement window. ")
HEADER = "DEMO-7 Design Notes Version 1.2"


def _pix(w=120, h=60, gray=150):
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, w, h), False)
    pix.clear_with(gray)
    return pix


def _page(doc, n, logo=True):
    """A page with the running header (inside the top 12% but below the top 8%), a running logo and
    a running footer whose page number changes."""
    p = doc.new_page(width=612, height=792)
    p.insert_text((40, 75), HEADER, fontname="helv", fontsize=9)
    if logo:
        p.insert_image(pymupdf.Rect(500, 12, 580, 52), pixmap=_pix())
    p.insert_text((280, 775), f"Page {n}", fontname="helv", fontsize=9)
    return p


def _body(p, y, text, width=530, fontsize=10):
    rect = pymupdf.Rect(40, y, 40 + width, 740)
    p.insert_textbox(rect, text, fontname="helv", fontsize=fontsize)


def test_normalize_soft_hyphen_and_line_break_hyphen():
    # PyMuPDF gives a word processor's line-break hyphen as U+00AD: it goes, with the break
    assert pdftext.normalize("sensor cali\u00ad\n   bration table") == "sensor calibration table"
    assert pdftext.normalize("sam\u00adpling") == "sampling"                   # invisible inside a line
    # a hard hyphen at a line end keeps the hyphen, whichever source the break came from
    assert pdftext.normalize("auto-  \n     retry") == pdftext.normalize("auto-retry") == "auto-retry"
    # so a quote typed across the break matches the page text
    page = pdftext.normalize("the host reads the tempera\u00ad\nture offset and the gain")
    assert pdftext.normalize("reads the temperature offset and the gain") in page


def _tier2(tmp_path, files: dict[str, str], tree: dict[str, str | None]):
    (tmp_path / "s").mkdir()
    for name, body in files.items():
        (tmp_path / "s" / name).write_text(body)
    return pdftext.tier2_map(tmp_path, [{"id": k, "parent": v} for k, v in tree.items()])


def test_tier2_map_explicit_covers_beat_a_parent_named_file(tmp_path):
    tree = {"5": None, "5.6": "5", "5.7": "5", "5.8": "5",
            "6": None, "6.3": "6", "6.3.5": "6.3", "6.3.6": "6.3", "6.3.7": "6.3",
            "7": None, "7.1": "7", "7.2": "7", "7.3": "7", "7.4": "7", "7.5": "7", "7.5.1": "7.5",
            "8": None, "8.6": "8", "8.6.1": "8.6", "9": None}
    t2 = _tier2(tmp_path, {
        "5.md": "# 5\n",                                             # named after a parent section
        "5.6.md": "# 5.6\n<!-- atlas:covers 5.7 -->\n",
        "6.md": "# 6\n",
        "6.3.5.md": "# 6.3.5\n<!-- atlas:covers 6.3.6 -->\n",
        "7.1.md": "# 7.1\n<!-- atlas:covers 7 -->\n",                 # an umbrella cover of the parent
        "7.2.md": "# 7.2\n<!-- atlas:covers 7.3 7.4 -->\n",
        "8.6.md": "# 8.6\n",
        "a-quirks.md": "<!-- atlas:covers 8 9, 5.6 -->\n",            # sorts first; 5.6 has its own file
    }, tree)
    assert t2["5"] == "s/5.md" and t2["5.8"] == "s/5.md"              # descent where nothing else claims
    assert t2["5.7"] == "s/5.6.md"                                    # explicit beats the parent's file
    assert t2["5.6"] == "s/5.6.md"                                    # its own file beats any cover
    assert t2["6.3.6"] == "s/6.3.5.md" and t2["6.3.7"] == "s/6.md"
    assert t2["7.3"] == t2["7.4"] == "s/7.2.md"                       # a cover of 7.3 beats a cover of 7
    assert t2["7"] == "s/7.1.md" and t2["7.5"] == t2["7.5.1"] == "s/7.1.md"
    assert t2["8.6.1"] == "s/8.6.md"                                  # the nearest claim wins
    assert t2["8"] == t2["9"] == "s/a-quirks.md"


def test_tier2_map_unnumbered_prd_sections(tmp_path):
    tree = {"hardware-goals": None, "mounting-clips": "hardware-goals",
            "thermal-limits": "hardware-goals", "labelling": "hardware-goals"}
    t2 = _tier2(tmp_path, {"hardware-goals.md": "# h\n", "mounting-clips.md": "# m\n",
                           "limits.md": "<!-- atlas:covers thermal-limits -->\n"}, tree)
    assert t2 == {"hardware-goals": "s/hardware-goals.md", "mounting-clips": "s/mounting-clips.md",
                  "thermal-limits": "s/limits.md", "labelling": "s/hardware-goals.md"}


def test_text_layer_ignores_running_header_and_logo(tmp_path):
    doc = pymupdf.open()
    _body(_page(doc, 1), 110, FILLER * 6)                                      # text
    p = _page(doc, 2)
    _body(p, 110, FILLER * 5)
    p.insert_image(pymupdf.Rect(60, 400, 400, 640), pixmap=_pix(200, 140))   # uncaptioned, ~17% of the page
    p = _page(doc, 3)
    p.insert_image(pymupdf.Rect(60, 110, 550, 600), pixmap=_pix(300, 300))   # a picture, no text layer
    _body(_page(doc, 4), 110, "See the sketch below.")                       # a short page, no image
    pdf = tmp_path / "demo7.pdf"
    doc.save(pdf)
    tl = pdftext.text_layer_of_pdf(pdf)
    assert tl["running"] == ["bottom: Page #", "top: DEMO-# Design Notes Version #.#"] and tl["running_images"] == 1
    assert tl["textless"] == [3] and tl["near_empty"] == [4]      # the header and the logo count for nothing
    assert tl["with_images"] == [2, 3] and tl["text_layer"] == "partial"
    assert [p["words"] for p in tl["pages"]][2:] == [0, 4]
    d = pdftext.prep(pdf, tmp_path / "atlas", doc_id="demo7@1", log=lambda *_: None)
    meta = common.read_json(d / "doc.json")
    assert meta["text_layer"] == "partial" and meta["pages_textless"] == [3]
    pages = {p["page"]: p for p in common.read_json(d / "pages" / "pages.json")}
    assert pages[1]["classes"] == [] and pages[1]["image_frac"] == 0
    assert pages[2]["classes"] == ["figure"] and pages[2]["figure_by"] == ["image"]   # no "Figure N" caption
    assert {"image-heavy", "figure"} <= set(pages[3]["classes"])
    assert "image-heavy" not in pages[4]["classes"]
    md = (d / "toc.md").read_text()
    assert "| 2 | figure | " in md and "images cover 17% of the page, no caption" in md


def _grid(p, x0, y0, labels_by_row, cw=30, rh=14):
    """A ruled bit diagram: one rectangle per cell, a label centred in each."""
    for r, labels in enumerate(labels_by_row):
        for c, lab in enumerate(labels):
            rect = pymupdf.Rect(x0 + c * cw, y0 + r * rh, x0 + (c + 1) * cw, y0 + (r + 1) * rh)
            p.draw_rect(rect, color=(0, 0, 0), width=0.5)
            p.insert_text((rect.x0 + 3, rect.y1 - 4), lab, fontname="helv", fontsize=6)


def test_vector_bit_diagram_is_text_a_plot_is_a_figure(tmp_path):
    doc = pymupdf.open()
    p = _page(doc, 1)
    _body(p, 110, FILLER)
    p.insert_text((40, 240), "Figure 3. MODE (01Ah) Format", fontname="helv", fontsize=9)
    _grid(p, 40, 250, [[f"D{15 - i}" for i in range(16)],
                       ["EN", "X", "X", "OE", "RATE2", "RATE1", "RATE0", "X", "X", "PH", "PL", "X", "TH", "TL", "S1", "S0"]])
    p = _page(doc, 2)
    _body(p, 110, FILLER)
    p.insert_text((40, 460), "Figure 4. Output ripple vs load current", fontname="helv", fontsize=9)
    p.draw_line((60, 440), (60, 250))
    p.draw_line((60, 440), (500, 440))
    for i in range(30):                                   # a plotted curve: slanted strokes
        p.draw_line((60 + 14 * i, 430 - 5 * i - (i % 3)), (74 + 14 * i, 425 - 5 * i - ((i + 1) % 3)))
    p = _page(doc, 3)
    _body(p, 110, FILLER * 3)
    pdf = tmp_path / "demo7.pdf"
    doc.save(pdf)
    d = pdftext.prep(pdf, tmp_path / "atlas", doc_id="demo7@1", log=lambda *_: None)
    pages = {p["page"]: p for p in common.read_json(d / "pages" / "pages.json")}
    assert pages[1]["figure_captions"] == 1 and pages[1]["paths"] >= 20
    assert "figure" not in pages[1]["classes"] and pages[1]["vector_grids"] == 1   # the text layer covers it
    assert pages[2]["classes"] == ["figure"] and pages[2]["figure_by"] == ["vector"]
    assert pages[2]["vector_graphics"] >= 1
    md = (d / "toc.md").read_text()
    assert "| 2 | figure |" in md and "| 1 |" not in md.split("## Pages that need an image Read")[1].split("##")[0]
    assert "- vector drawings the text layer covers (ruled tables, bit diagrams): p.1" in md


def test_overline_class_and_names():
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    text = "Pull-down resistance (EN\u0305\u0305)\nHigh-level input (EN, EN\u0305\u0305)\nOutput low (RDY\u0305\u0305\u0305)\n"
    words = [(40, 100 + 12 * i, 80, 110 + 12 * i, t, 0, i, 0) for i, t in enumerate(
        ["Pull-down", "resistance", "(EN\u0305\u0305)", "High-level", "input", "(EN,", "EN\u0305\u0305)",
         "Output", "low", "(RDY\u0305\u0305\u0305)"])]
    layer = {"words": 60, "running_words": 0, "image_frac": 0.0, "body": words, "paths": []}
    cls, ev = pdftext.page_classes(page, text, words, layer)
    assert "overline" in cls and ev["overlined"] == ["EN", "RDY"] and ev["overlined_also_plain"] == ["EN"]
    p = dict(page=5, classes=cls + ["table-heavy"], **ev)
    assert "the text layer cannot tell which row has the bar" in pdftext.image_read_reasons(p)[0]
    assert pdftext.image_read_reasons(dict(p, classes=cls)) == []      # prose: listed, not an image Read
    # normalize() drops the bar: this is why the class exists
    assert pdftext.normalize("(EN\u0305\u0305)") == pdftext.normalize("(EN)")


def _ttf() -> Path | None:
    for c in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans.ttf",
              "/usr/share/fonts/TTF/DejaVuSans.ttf", "/Library/Fonts/Arial Unicode.ttf", "C:/Windows/Fonts/arial.ttf"):
        if Path(c).is_file():
            return Path(c)
    return None


@pytest.mark.skipif(_ttf() is None, reason="no TrueType font with a combining overline on this machine")
def test_overline_pages_listed_in_toc(tmp_path):
    doc = pymupdf.open()
    for i in (1, 2, 3):
        p = _page(doc, i)
        p.insert_font(fontname="uni", fontfile=str(_ttf()))
        _body(p, 110, FILLER)
        if i == 2:
            p.insert_text((40, 300), "Pull-down resistance (EN\u0305\u0305) 150 kOhm; EN has none.", fontname="uni",
                          fontsize=10)
    pdf = tmp_path / "demo7.pdf"
    doc.save(pdf)
    d = pdftext.prep(pdf, tmp_path / "atlas", doc_id="demo7@1", log=lambda *_: None)
    pages = {p["page"]: p for p in common.read_json(d / "pages" / "pages.json")}
    assert "overline" in pages[2]["classes"] and "overline" not in pages[1]["classes"]
    assert pages[2]["overlined"] == ["EN"] and pages[2]["overlined_also_plain"] == ["EN"]
    md = (d / "toc.md").read_text()
    assert "## Overlined names" in md and "| 2 | EN | EN |" in md and "- overline: p.2" in md


def _toc_doc(path: Path) -> Path:
    doc = pymupdf.open()
    p = _page(doc, 1)
    p.insert_text((40, 110), "Overview", fontname="helv", fontsize=14)
    _body(p, 125, FILLER * 3)
    for n, title in ((2, "Table of Contents"), (3, "List of Figures")):         # front matter the outline skips
        p = _page(doc, n)
        p.insert_text((250, 110), title, fontname="helv", fontsize=12)
        for k in range(6):
            p.insert_text((40, 140 + 14 * k), f"Item {k} " + "." * 60 + f" {k + 4}", fontname="helv", fontsize=9)
    p = _page(doc, 4)
    p.insert_text((40, 110), "Product Goals", fontname="helv", fontsize=14)
    p.insert_text((40, 140), "Users", fontname="helv", fontsize=12)
    _body(p, 150, FILLER * 3)
    p.insert_text((40, 400), "Regions", fontname="helv", fontsize=12)
    _body(p, 410, FILLER)
    p.insert_text((40, 640), "Distribution", fontname="helv", fontsize=12)
    _body(p, 650, "Direct orders. Resellers.")
    p = _page(doc, 5)
    _body(p, 100, "Service partners.")                                   # Distribution spills over
    p.insert_text((40, 160), "Operating Modes", fontname="helv", fontsize=14)
    _body(p, 175, FILLER * 2)
    p = _page(doc, 6)
    p.insert_text((40, 110), "Enclosure", fontname="helv", fontsize=14)  # at the top, below the header
    _body(p, 125, FILLER)
    doc.set_toc([[1, "Overview", 1], [1, "Product Goals", 4], [2, "Users", 4], [2, "Regions", 4],
                 [2, "Distribution", 4], [1, "Operating Modes", 5], [1, "Enclosure", 6]])
    doc.save(path)
    return path


def test_toc_spill_front_matter_and_shared_page_tokens(tmp_path):
    pdf = _toc_doc(tmp_path / "demo7.pdf")
    d = pdftext.prep(pdf, tmp_path / "atlas", doc_id="demo7@1", log=lambda *_: None)
    secs = common.read_json(d / "toc.json")["sections"]
    assert [s["id"] for s in secs] == ["overview", "front-matter", "product-goals", "users", "regions",
                                       "distribution", "operating-modes", "enclosure"]
    by = {s["id"]: s for s in secs}
    assert by["overview"]["pages"] == [1, 1]                  # the contents pages do not inflate it
    assert by["front-matter"]["pages"] == [2, 3] and by["front-matter"]["synthetic"]
    assert by["distribution"]["pages"] == [4, 5]            # its last line is on p5, above the next heading
    assert by["product-goals"]["pages"] == [4, 5]
    assert by["users"]["pages"] == by["regions"]["pages"] == [4, 4]
    assert by["operating-modes"]["pages"] == [5, 5]  # Enclosure starts at the top of p6, under the header
    pages = {p["page"]: p for p in common.read_json(d / "pages" / "pages.json")}
    tok = {k: by[k]["tokens"] for k in ("users", "regions", "distribution")}
    assert tok["users"] > tok["regions"] > tok["distribution"] > 0        # not the whole page each
    assert tok["users"] < pages[4]["tokens"]
    kids = sum(tok.values())
    assert abs(by["product-goals"]["tokens"] - kids) <= 0.15 * by["product-goals"]["tokens"]
    md = (d / "toc.md").read_text()
    assert "| front-matter | Front matter" in md and "`front-matter` is not in the PDF outline" in md


def test_cut_reads_two_columns():
    # left column x 40-290, right column x 320-570; a heading at the top of the right column
    left = [(40, 100 + 12 * i, 280, 110 + 12 * i, f"l{i}") for i in range(30)]
    right = [(320, 100 + 12 * i, 560, 110 + 12 * i, f"r{i}") for i in range(30)]
    words = left + right
    g = pdftext._gutter(words, 612)
    assert g is not None and 290 <= g <= 320
    assert pdftext._cut(words, 320, 100, g)[0] == 30           # after the whole left column
    assert pdftext._cut(words, 40, 220, g)[0] == 9             # l0-l8; the right column comes later
    assert pdftext._gutter(left, 612) is None

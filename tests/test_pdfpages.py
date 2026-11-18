"""`pdf pages`: generated PDFs only (PyMuPDF), one with a text layer and one scan-like page."""
import shutil

import pymupdf
import pytest

from embarch_atlas import cli, pdfpages

pytestmark = pytest.mark.skipif(not shutil.which("pdftoppm"), reason="poppler not installed")


@pytest.fixture(scope="module")
def pdf(tmp_path_factory):
    path = tmp_path_factory.mktemp("pp") / "mixed.pdf"
    doc = pymupdf.open()
    p = doc.new_page(width=612, height=792)
    p.insert_text((50, 100), "Synthetic page with a real text layer. " * 3, fontsize=10)
    # p2: a scan, a raster of ink with no text layer
    p = doc.new_page(width=400, height=300)
    pix = pymupdf.Pixmap(pymupdf.csGRAY, pymupdf.IRect(0, 0, 200, 150), False)
    pix.set_rect(pix.irect, (180,))
    p.insert_image(p.rect, pixmap=pix)
    # p3: landscape by rotation
    p = doc.new_page(width=300, height=200)
    p.set_rotation(90)
    p.insert_text((20, 50), "Rotated page text layer here, long enough to count as usable.", fontsize=8)
    doc.save(path)
    return path


def run(capsys, *argv):
    rc = cli.main(["pdf", "pages", *argv])
    return rc, capsys.readouterr()


def test_reports_text_layer_per_page_before_files(pdf, tmp_path, capsys):
    rc, cap = run(capsys, str(pdf), "--pages", "1-2", "--out", str(tmp_path))
    assert rc == 0
    lines = cap.out.splitlines()
    p1 = next(i for i, l in enumerate(lines) if l.startswith("p1: text layer yes"))
    f1 = next(i for i, l in enumerate(lines) if l.startswith("  p1 tile"))
    assert p1 < f1
    assert any(l.startswith("p2: text layer no") for l in lines)
    assert (tmp_path / "p001.png").is_file() and (tmp_path / "p002.png").is_file()
    assert "445x334 px" in cap.out  # 400x300 pt at 80 dpi, poppler rounds up


def test_tiles_split_the_page(pdf, tmp_path, capsys):
    rc, cap = run(capsys, str(pdf), "--pages", "2", "--tiles", "2x3", "--out", str(tmp_path))
    assert rc == 0
    pngs = sorted(p.name for p in tmp_path.glob("*.png"))
    assert pngs == [f"p002_r{r}c{c}.png" for r in (1, 2, 3) for c in (1, 2)]
    sizes = [pdfpages.png_size(tmp_path / n) for n in pngs]
    assert sum(w for w, _ in sizes[:2]) == 445 and sum(h for (_, h) in sizes[::2]) == 334


def test_crop_in_points_then_tiles(pdf, tmp_path, capsys):
    rc, cap = run(capsys, str(pdf), "--pages", "1", "--crop", "36,72,144,72", "--dpi", "72",
                  "--out", str(tmp_path))
    assert rc == 0
    assert pdfpages.png_size(tmp_path / "p001_r1c1.png") == (144, 72)
    rc, _ = run(capsys, str(pdf), "--pages", "1", "--crop", "36,72,144,72", "--dpi", "72",
                "--tiles", "2x1", "--out", str(tmp_path / "t"))
    assert rc == 0
    assert [pdfpages.png_size(p) for p in sorted((tmp_path / "t").glob("*.png"))] == [(72, 72), (72, 72)]


def test_rotated_page_uses_displayed_size(pdf, tmp_path, capsys):
    rc, cap = run(capsys, str(pdf), "--pages", "3", "--tiles", "2x1", "--dpi", "72", "--out", str(tmp_path))
    assert rc == 0
    assert sum(pdfpages.png_size(p)[0] for p in tmp_path.glob("*.png")) == 200
    assert pdfpages.png_size(tmp_path / "p003_r1c1.png")[1] == 300


def test_default_out_is_a_new_temp_dir(pdf, capsys):
    rc, cap = run(capsys, str(pdf), "--pages", "1", "--dpi", "20")
    assert rc == 0
    assert "atlas-pdf-pages-" in cap.out


@pytest.mark.parametrize("argv", [["--pages", "9"], ["--pages", "x"], ["--tiles", "0x2"], ["--crop", "1,2,3"],
                                  ["--dpi", "5"], ["--crop", "900,0,10,10"], ["--pages", "1", "--tiles", "400x1", "--dpi", "10"]])
def test_bad_input_exits_2_without_traceback(pdf, tmp_path, capsys, argv):
    rc, cap = run(capsys, str(pdf), "--out", str(tmp_path), *argv)
    assert rc == 2 and "pdf pages:" in cap.err


def test_missing_file(tmp_path, capsys):
    rc, cap = run(capsys, str(tmp_path / "nope.pdf"))
    assert rc == 2 and "no such file" in cap.err


def test_parse_pages_open_range():
    assert pdfpages.parse_pages("2-", 4) == [2, 3, 4]
    assert pdfpages.parse_pages("1,3-4,3", 5) == [1, 3, 4]

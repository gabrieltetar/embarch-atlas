"""columns: a table set sideways on a portrait page read back as rows and cells."""
import pymupdf

from embarch_atlas import columns

HEAD = ["Pkg-A", "Pkg-B", "Pin name", "Pin type"]
BODY = [["1", "-", "PX1", "I/O"], ["2", "B7", "PX2", "S"], ["-", "C3", "VSUP", "S"]]


def sideways_table(path, rules=True):
    """A portrait page whose 4x4 table reads correctly only with the page turned 90° clockwise
    (body text runs upward); header cells are two lines ("Pin" / "name")."""
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    col_w, row_h, X0, Y0 = 120, 40, 60, 60             # reading frame: X right, Y down

    def to_page(X, Y):                                  # reading (X, Y) -> page (x, y)
        return pymupdf.Point(Y, 842 - X)

    for r, row in enumerate([HEAD] + BODY):
        for c, cell in enumerate(row):
            lines = cell.split(" ") if r == 0 and " " in cell else [cell]
            for k, ln in enumerate(lines):
                page.insert_text(to_page(X0 + c * col_w + 6, Y0 + r * row_h + 14 + k * 11), ln, fontsize=9, rotate=90)
    if rules:
        for c in range(len(HEAD) + 1):
            page.draw_line(to_page(X0 + c * col_w, Y0), to_page(X0 + c * col_w, Y0 + 4 * row_h))
        for r in range(5):
            page.draw_line(to_page(X0, Y0 + r * row_h), to_page(X0 + 4 * col_w, Y0 + r * row_h))
    doc.save(path)
    return pymupdf.open(path)[0]


def test_sideways_table_by_rules(tmp_path):
    g = columns.grid(sideways_table(tmp_path / "t.pdf"))
    assert g["frame"] == (0.0, -1.0) and g["method"] == {"columns": "rules", "rows": "rules"}
    assert g["rows"] == [HEAD] + BODY                    # two-line header cells joined in reading order
    text = columns.render_grid(g, 1)
    assert "turned 90° (text ran upward)" in text and "r2: 1 | - | PX1 | I/O" in text


def test_sideways_table_without_rules_falls_back_to_gaps(tmp_path):
    g = columns.grid(sideways_table(tmp_path / "t.pdf", rules=False))
    assert g["method"] == {"columns": "gaps", "rows": "lines"}
    assert g["rows"][-3:] == BODY
    # without rules a wrapped header cell is two rows: the documented limit of the fallback
    assert g["rows"][:2] == [["Pkg-A", "Pkg-B", "Pin", "Pin"], ["", "", "name", "type"]]

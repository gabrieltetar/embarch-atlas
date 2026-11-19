"""A single-root outline and the product-specification register page. Synthetic: invented names (WIDGET, ACME-PS)."""
from __future__ import annotations

from embarch_atlas import rm, vote


def _row(label: str, cells: dict[int, str]) -> str:
    """`label` then 32 columns, five characters each, bit 31 first; `cells` maps a bit to its text."""
    return f"{label:<18}" + "".join(f"{cells.get(b, ''):<5}" for b in range(31, -1, -1)).rstrip()


def _bits() -> str:
    return _row("Bit number", {b: str(b) for b in range(32)})


REG_PAGE = "\n".join([
    "5.1.1.2  CTRL",
    "Address offset: 0x30",
    "Control of the invented widget",
    _bits(),
    _row("ID", {0: "A", 4: "B", 5: "B", 6: "B", 9: "C"}),
    _row("Reset 0x00000010", {b: "1" if b == 4 else "0" for b in range(32)}),
    "ID  R/W  Field   Value ID   Value   Description",
    "A   RW   ENABLE                      Enable the widget",
    "              Disabled     0         Off",
    "              Enabled      1         On",
    "B   RW   LEVEL                       Trigger level",
    "C   R    BUSY                        Set while busy",
])

TOC = [[1, "acme-ps.pdf", 1], [2, "Contents", 1], [2, "Widget", 2],
       [3, "5.1 WIDGET - Invented widget", 2], [4, "5.1.1 Registers", 2],
       [5, "5.1.1.1 TASKS_GO", 2], [5, "5.1.1.2 CTRL", 3], [2, "Glossary", 4]]
PAGES = ["ACME-PS Contents", "Widget\n5.1 WIDGET\n5.1.1 Registers\n5.1.1.1 TASKS_GO\nAddress offset: 0x0\n"
         "Reset 0x00000000", "5.1.1.2 CTRL\n" + REG_PAGE.split("\n", 1)[1], "Glossary"]


def _doc(toc=TOC, pages=PAGES) -> rm.RmDoc:
    layer = rm.TextLayer(pages=pages)
    o = rm.build_outline(toc, [10] * len(pages), layer, "acme-ps@1", len(pages))
    return rm.RmDoc("acme-ps@1", o, layer)


def test_single_root_outline_yields_level_two_entries_as_chapters():
    d = _doc()
    assert [c["title"] for c in d.chapters()] == ["Contents", "Widget", "Glossary"]
    assert d.o["root"] == "acme-ps.pdf" and d.o["toc_entries"] == len(TOC)
    assert d.by_id["5.1.1.2"]["chapter"] == "widget"


def test_register_sections_keyed_by_bare_names_under_a_registers_container():
    d = _doc()
    assert d.by_id["5.1.1"].get("register")
    assert [e["regname"] for e in vote.register_sections(d, "widget")] == ["TASKS_GO", "CTRL"]


def test_nordic_register_page_parsed_to_offset_reset_and_fields():
    d = _doc()
    r, _ = vote.parse_register_section(d, d.by_id["5.1.1.2"])
    assert r["offset"] == 0x30 and r["reset"] == 0x10 and r["width"] == 32
    f = {x["name"]: (x["hi"], x["lo"], x.get("access")) for x in r["fields"]}
    assert f["ENABLE"] == (0, 0, "rw") and f["LEVEL"] == (6, 4, "rw") and f["BUSY"] == (9, 9, "r")
    assert sorted((x["hi"], x["lo"]) for x in r["fields"] if x["name"] == "Reserved") == [(3, 1), (8, 7), (31, 10)]
    assert r["coverage"] == 32 and r["access_ok"] and r["parse_ok"]
    assert r["bitacc"][5] == "rw" and r["bitacc"][9] == "r" and r["bitacc"][7] is None


def test_numbered_single_chapter_outline_is_left_alone():
    toc = [[1, "1 Port block", 1], [2, "1.1 Registers", 1], [2, "1.2 Notes", 1]]
    d = _doc(toc, ["x"])
    assert [c["id"] for c in d.chapters()] == ["1"] and "root" not in d.o


def test_two_level_one_entries_are_left_alone():
    toc = [[1, "One", 1], [2, "A", 1], [2, "B", 1], [1, "Two", 1]]
    d = _doc(toc, ["x"])
    assert [c["title"] for c in d.chapters()] == ["One", "Two"]

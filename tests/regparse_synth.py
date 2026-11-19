"""Synthetic register-map datasheets for `doc regparse` (PyMuPDF). Nothing here comes from a real document:
parts SYN700, SYN257 and MXSYN are invented, and so is every register, field and value.

- SYN700 (TI layout, 16-bit registers): `Long Name (ACRONYM) Register (Address = 0h) [reset = 0h]`
  headings, an `Address Acronym Name Size Go` summary table, a 40-bit register, and a reserved bit that
  resets 1 under a heading that says 0.
- SYN257 (TI layout, 8-bit register pairs): `(I2C address = 01/00h)` headings, a `(MSB/LSB)` command
  summary with `Name()` rows, one field table per bus address restarting at bit 7 (and one numbering its
  bits 15-8), names with spaces, commas and a wrap, per-bit value rows, a register with no reset bracket
  and unnamed fields, variant resets, a figure caption that disagrees with its heading, and a
  `Long Name (Other)` heading the summary table names by its long name.
- SYN880 (TI layout, 8-bit registers, `Offset = 0x..` headings with a capital-R `[Reset = ..]`): resets
  printed as width-prefixed binary (`2b10`), one register consistent, one whose heading disagrees with its
  fields, a status register with `[Reset = X]`, and a field whose digit count disagrees with its width.
- MXSYN (Maxim layout): a two-column register-description page with bold headings, Register Type /
  Nonvolatile Backup / Restore / Initial Value lines and drawn D15..D0 bit diagrams (plus one with no
  drawn grid); a grid memory map (row Nh x column 0Mxh); a factory-default table.
"""
from __future__ import annotations

from pathlib import Path

import pymupdf

MONO = dict(fontname="cour", fontsize=8)


def _lines(page, lines: list[str], y0: float = 50, step: float = 13) -> None:
    for i, ln in enumerate(lines):
        page.insert_text((20, y0 + step * i), ln, **MONO)


COLS = (5, 13, 38, 46, 58)   # BIT FIELD TYPE RESET DESCRIPTION


def row(bits: str = "", name: str = "", typ: str = "", reset: str = "", desc: str = "") -> str:
    """A field-table row in monospace columns; a name longer than its column pushes TYPE right, as
    pdftotext prints it, while RESET and DESCRIPTION stay in their columns."""
    s = " " * COLS[0] + bits
    s = s.ljust(COLS[1]) + name
    if typ:
        s = (s + " " if len(s) >= COLS[2] else s.ljust(COLS[2])) + typ
    if reset:
        s = s.ljust(COLS[3]) + reset
    if desc:
        s = (s + " " if len(s) >= COLS[4] else s.ljust(COLS[4])) + desc
    return s.rstrip()


HDR = row("BIT", "FIELD", "TYPE", "RESET", "DESCRIPTION")


def build_syn700(path: Path) -> Path:
    doc = pymupdf.open()
    p = doc.new_page(width=612, height=792)
    _lines(p, [
        "7.5 Register Maps",
        "7.5.1 SYN700 Registers",
        "                         Table 7-2. SYN700 Registers",
        "  Address Acronym      Register Name              Register Size (bits)   Section",
        "     0h      CFG        Configuration              16                     Go",
        "     1h      ACC        Accumulator Result         40                     Go",
        "     Bh      ALERT      Alert Flags                16                     Go",
        "    3Eh      MFR_ID     Manufacturer ID            16                     Go",
        "",
        "7.5.1.1 Configuration (CFG) Register (Address = 0h) [reset = 0h]",
        "Return to the Summary Table.",
        "                 Table 7-3. CFG Register Field Descriptions",
        "     Bit     Field                   Type    Reset     Description",
        "     15      RST                     R/W     0h        Reset bit. Setting this bit resets",
        "                                                       every register.",
        "     14-6    INIT_DLY                R/W     0h        Initial conversion delay, 2 ms steps.",
        "     5       RESERVED                R       0h        Reserved. Always reads 0.",
        "     4       RESERVED                R       1h        Reserved. Always reads 1.",
        "     3-0     RESERVED                R       0h        Reserved. Always reads 0.",
        "7.5.1.2 Accumulator Result (ACC) Register (Address = 1h) [reset = 0h]",
        "                 Table 7-4. ACC Register Field Descriptions",
        "     Bit     Field                   Type    Reset     Description",
        "     39-0    ACC                     R       0h        Accumulated energy.",
        "7.5.1.3 Manufacturer ID (MFR_ID) Register (Address = 3Eh) [reset = 4E53h]",
        "                 Table 7-5. MFR_ID Register Field Descriptions",
        "     Bit     Field                   Type    Reset     Description",
        "     15-0    MFRID                   R       4E53h     Reads back the vendor code.",
        "7.5.1.4 Alert Flags (ALERT) Register (Address = Bh) [reset = 0001h]",
        "                 Table 7-6. ALERT Register Field Descriptions",
        "     Bit     Field                   Type    Reset     Description",
        "     15      LATCH                   R/W     0h        Alert latch enable.",
        "     14-2    RESERVED                R       0h        Reserved.",
        "     1       CNVRDY                  R       0h        Conversion ready. When LATCH =1 this bit is",
        "                                                       cleared by reading this register.",
        "     0       MEMOK                   R       1h        Memory checksum good.",
    ])
    doc.set_toc([[1, "7 Detailed Description", 1], [2, "7.5 Register Maps", 1]])
    doc.set_metadata({"title": "SYN700 Synthetic Power Monitor", "keywords": "SYN700"})
    doc.save(path)
    return path


def build_syn257(path: Path) -> Path:
    doc = pymupdf.open()
    p = doc.new_page(width=612, height=792)
    _lines(p, [
        "5.4 Register Map",
        "                         Table 5-1. Command Summary",
        "      I2C ADDR",
        "                   REGISTER NAME          TYPE        DESCRIPTION                LINKS",
        "     (MSB/LSB)",
        " 01/00h            ChgOpt0()              R/W         Charge Option 0            Go",
        " 03/02h            LoadCur()              R/W         7-bit load current         Go",
        " 05/04h            ADCVIN/VOUT()          R           8-bit ADC readings         Go",
        " 07/06h            BoostV()               R/W         14-bit boost voltage       Go",
        " 09/08h            Floor Guard()          R/W         Floor guard thresholds     Go",
        " 2Fh               ChipID()               R           Chip ID                    Go",
    ])
    p = doc.new_page(width=612, height=792)
    _lines(p, [
        "5.4.1 ChgOpt0 Register (I2C address = 01/00h) [reset = 8203h]",
        "            Figure 5-2. ChgOpt0 Register (I2C address = 01/00h) [reset = 8203h]",
        "      7          6          5          4          3          2          1          0",
        "     R/W        R/W        R/W        R/W        R/W        R/W        R/W        R",
        "            Table 5-2. ChgOpt0 Register (I2C address = 01h) Field Descriptions",
        HDR,
        row("7", "EN_LOWPWR", "R/W", "1b", "Low power mode enable."),
        row("6-2", "Reserved", "R/W", "00000b", "Not used."),
        row("1", "AUTO_LIM_DISAB", "R/W", "1b", "Input limit auto disable."),
        row("", "LE", "", "", "Second line of the description."),
        row("0", "Fault OVP_IN", "R", "0b", "The status is latched if triggered until a read from host."),
        "            Table 5-3. ChgOpt0 Register (I2C address = 00h) Field Descriptions",
        HDR,
        row("7-2", "Reserved", "R/W", "000000b", "Not used."),
        row("1-0", "WDOG_ADJ", "R/W", "11b", "Watchdog timer adjust."),
        "",
        "5.4.2 LoadCur Register (I2C address = 03/02h) [reset = 0000h]",
        "            Table 5-4. LoadCur Register (I2C address = 03h) Field Descriptions",
        HDR,
        row("7-5", "Reserved", "R/W", "000b", "Not used."),
        row("4", "Load Current, bit 6", "R/W", "0b", "Adds 8192 mA."),
        row("3", "Load Current, bit 5", "R/W", "0b", "Adds 4096 mA."),
        row("2", "Load Current, bit 4", "R/W", "0b", "Adds 2048 mA."),
        row("1", "Load Current, bit 3", "R/W", "0b", "Adds 1024 mA."),
        row("0", "Load Current, bit 2", "R/W", "0b", "Adds 512 mA."),
        "            Table 5-5. LoadCur Register (I2C address = 02h) Field Descriptions",
        HDR,
        row("7", "Load Current, bit 1", "R/W", "0b", "Adds 256 mA."),
        row("6", "Load Current, bit 0", "R/W", "0b", "Adds 128 mA."),
        row("5-0", "Reserved", "R/W", "000000b", "Not used."),
    ])
    p = doc.new_page(width=612, height=792)
    _lines(p, [
        "5.4.3 ADCVIN/VOUT Register (I2C address = 05/04h)",
        "            Table 5-6. ADCVIN Register (I2C address = 05h) Field Descriptions",
        HDR,
        row("7-0", "", "R", "", "8-bit digital output of the input voltage."),
        "            Table 5-7. ADCVOUT Register (I2C address = 04h) Field Descriptions",
        HDR,
        row("7-0", "", "R", "", "8-bit digital output of the output voltage."),
        "",
        "5.4.4 BoostV Register (I2C address = 07/06h) [reset = 0A55h]",
        "            Table 5-8. BoostV Register (I2C address = 07h) Field Descriptions",
        HDR,
        row("15-14", "Reserved", "R/W", "00b", "Not used."),
        row("13-8", "BOOST_V_HI", "R/W", "001010b", "Upper bits."),
        "            Table 5-9. BoostV Register (I2C address = 06h) Field Descriptions",
        HDR,
        row("7-0", "BOOST_V_LO", "R/W", "01010101b", "Lower bits."),
        "",
        "5.4.5 Floor Guard Register (I2C address = 09/08h) [reset = 0058h(variant A)/0000h(variant B)]",
        "            Figure 5-6. Floor Guard Register (I2C address = 09/08h) [reset = 0060h/0000h]",
        "            Table 5-10. Floor Guard Register (I2C address = 09h) Field Descriptions",
        HDR,
        row("7-0", "Reserved", "R/W", "00000000b", "Not used."),
        "            Table 5-11. Floor Guard Register (I2C address = 08h) Field Descriptions",
        HDR,
        row("7-2", "FLOOR_THR", "R/W", "010110b(", "Threshold."),
        row("", "", "", "variant A)", ""),
        row("", "", "", "000000b(", ""),
        row("", "", "", "variant B)", ""),
        row("1-0", "Reserved", "R/W", "00b", "Not used."),
        "",
        "5.4.6 Chip ID (ChipAddress) Register (I2C address = 2Fh) [reset = 7Ah]",
        "            Table 5-12. Chip ID (ChipAddress) Register Field Descriptions",
        HDR,
        row("7-0", "CHIP_ID", "R", "7Ah", "Chip identifier."),
    ])
    doc.set_toc([[1, "5 Detailed Description", 1], [2, "5.4 Register Map", 1]])
    doc.set_metadata({"title": "SYN257 Synthetic Charger", "keywords": "SYN257"})
    doc.save(path)
    return path


def _wp_reg(sec: str, name: str, off: str, head_reset: str, tokens: str, rows: list[tuple]) -> list[str]:
    """One register in the width-prefixed layout: `Offset = 0x..` heading with a capital-R `[Reset = ..]`,
    a bit diagram of `<access>-<width>b<bits>` tokens, and a `Bit Field Type Reset Description` table."""
    return [f"{sec} {name} Register (Offset = {off}) [Reset = {head_reset}]",
            f"            Figure 7-{sec.rsplit('.', 1)[1]}. {name} Register",
            "      7      6      5      4      3      2      1      0",
            "   " + "  ".join(n for _, n, *_ in rows),
            "   " + tokens,
            f"            Table 7-{sec.rsplit('.', 1)[1]}. {name} Register Field Descriptions",
            HDR] + [row(*r) for r in rows] + [""]


def build_syn880(path: Path) -> Path:
    """SYN880 (TI layout, width-prefixed binary resets): MODE_CFG printed consistently, CHG_LIM whose
    heading says 0x21 while its fields compose to 0x20, STAT with `[Reset = X]` and `R X`, and BAD_W whose
    field prints `2b0` (a width of 2, one digit)."""
    doc = pymupdf.open()
    p = doc.new_page(width=612, height=792)
    lines = ["7.5 Register Maps"]
    lines += _wp_reg("7.5.1.1", "MODE_CFG", "0x1", "0x11", "R/W-1b0  R/W-2b00  R/W-1b1  R-3b000  R/W-1b1", [
        ("7", "REG_RST", "R/W", "1b0", "Software Reset"),
        ("6-5", "EN_MODE_1:0", "R/W", "2b00", "Mode select."),
        ("4", "FAST", "R/W", "1b1", "Fast mode."),
        ("3-1", "RESERVED", "R", "3b000", "Reserved."),
        ("0", "EN", "R/W", "1b1", "Enable.")])
    lines += _wp_reg("7.5.1.2", "CHG_LIM", "0x2", "0x21", "R/W-2b00  R/W-2b10  R/W-4b0000", [
        ("7-6", "RESERVED", "R/W", "2b00", "Reserved."),
        ("5-4", "LIM_SEL_1:0", "R/W", "2b10", "Limit select."),
        ("3-0", "LIM_TRIM_3:0", "R/W", "4b0000", "Trim.")])
    _lines(p, lines, y0=50, step=11)
    p = doc.new_page(width=612, height=792)
    lines = _wp_reg("7.5.1.3", "STAT", "0x3", "X", "R-X  R-X", [
        ("7-4", "CHG_STAT_3:0", "R", "X", "Charge state."),
        ("3-0", "FAULT_3:0", "R", "X", "Fault flags.")])
    lines += _wp_reg("7.5.1.4", "BAD_W", "0x4", "0x00", "R/W-2b0  R/W-6b000000", [
        ("7-6", "TRIM_1:0", "R/W", "2b0", "Trim."),
        ("5-0", "SPARE_5:0", "R/W", "6b000000", "Spare.")])
    _lines(p, lines, y0=50, step=11)
    doc.set_metadata({"title": "SYN880 Synthetic Charger", "keywords": "SYN880"})
    doc.save(path)
    return path


# ---------------------------------------------------------------- Maxim layout

BODY = dict(fontname="helv", fontsize=9.5)
BOLD = dict(fontname="hebo", fontsize=10)
CELL = dict(fontname="helv", fontsize=8)
X0, PITCH = 62.0, 30.5            # D15 cell starts at X0; D0 cell ends at X0 + 16 * PITCH


def _cell_x(bit: int) -> tuple[float, float]:
    i = 15 - bit
    return X0 + PITCH * i, X0 + PITCH * (i + 1)


def _center(page, text: str, cx: float, y: float, **font) -> None:
    w = pymupdf.get_text_length(text, fontname=font["fontname"], fontsize=font["fontsize"])
    page.insert_text((cx - w / 2, y), text, **font)


def _diagram(page, top: float, cells: list[tuple[int, int, str]], caption: str, grid: bool = True) -> None:
    """A D15..D0 diagram: a header row with a cell per bit, a content row with a cell per field."""
    hdr_bot, bot = top + 14, top + 40
    for b in range(16):
        x0, x1 = _cell_x(b)
        _center(page, f"D{b}", (x0 + x1) / 2, top + 11, fontname="hebo", fontsize=8)
    if grid:
        page.draw_rect(pymupdf.Rect(54, top - 10, 558, bot + 30), width=0.5)      # the figure frame
        right = X0 + 16 * PITCH
        page.draw_line((X0, top), (right, top), width=0.5)
        for b in range(16):                                                        # rules drawn cell by cell
            x0, x1 = _cell_x(b)
            page.draw_line((x0, hdr_bot), (x1, hdr_bot), width=0.5)
            page.draw_line((x0, bot), (x1, bot), width=0.5)
            page.draw_line((x0, top), (x0, hdr_bot), width=0.5)
        page.draw_line((right, top), (right, hdr_bot), width=0.5)
        for hi, lo, _ in cells:
            page.draw_line((_cell_x(hi)[0], hdr_bot), (_cell_x(hi)[0], bot), width=0.5)
        page.draw_line((right, hdr_bot), (right, bot), width=0.5)
    for hi, lo, text in cells:
        cx = (_cell_x(hi)[0] + _cell_x(lo)[1]) / 2
        parts = text.split("\n")
        for k, part in enumerate(parts):
            _center(page, part, cx, top + 30 + (k - (len(parts) - 1) / 2) * 9, **CELL)
    page.insert_text((54, bot + 22), caption, **BODY)


def build_mxsyn(path: Path) -> Path:
    doc = pymupdf.open()
    p = doc.new_page(width=612, height=792)
    left = [("EvtFlags Register (000h)", BOLD), ("Register Type: Special", BODY), ("Nonvolatile Backup: None", BODY),
            ("Initial Value: 0x0802", BODY), ("The EvtFlags register holds alert flags.", BODY), ("", BODY),
            ("ThrCfg Register (028h)", BOLD), ("Register Type: Special", BODY),
            ("Nonvolatile Backup: nThrCfg (19Fh)", BODY), ("The ThrCfg register sets thresholds.", BODY), ("", BODY),
            ("AtLoad Register (0C4h)", BOLD), ("Register Type: Current", BODY), ("Nonvolatile Backup: None", BODY)]
    right = [("nThrCfg Register (19Fh)", BOLD), ("Register Type: Special", BODY),
             ("Nonvolatile Restore: ThrCfg (028h) if nNVCfg0.enTC", BODY), ("is set.", BODY),
             ("Alternate Initial Value: 0xA4C3", BODY), ("The factory default of nThrCfg differs.", BODY)]
    for col, x in ((left, 54), (right, 317)):
        y = 100
        for text, font in col:
            if text:
                p.insert_text((x, y), text, **font)
            y += 14
    _diagram(p, 420, [(15, 15, "Rm"), (14, 14, "X"), (13, 13, "X"), (12, 12, "X"), (11, 8, "THR"),
                      (7, 7, "X"), (6, 6, "X"), (5, 5, "X"), (4, 4, "X"), (3, 3, "X"), (2, 2, "X"), (1, 1, "RST"),
                      (0, 0, "X")], "Figure 5. EvtFlags (000h) Format")
    _diagram(p, 520, [(15, 15, "1"), (14, 14, "0"), (13, 13, "1"), (12, 9, "MODE"), (8, 8, "Slow\nFilter"),
                      (7, 7, "X"), (6, 4, "STG"), (3, 3, "0"), (2, 2, "1"), (1, 1, "1"), (0, 0, "0")],
             "Figure 6. ThrCfg (028h)/nThrCfg (19Fh) Format")
    _diagram(p, 660, [(15, 0, "TICKS")], "Figure 7. GaugeTmr (03Eh) Format", grid=False)
    # p2: the memory map
    p = doc.new_page(width=612, height=792)
    p.insert_text((54, 360), "Table 3. Register Memory Map", **BODY)
    cols = {"00xh": 150, "01xh": 213, "02xh": 276, "0Dxh": 339}
    p.insert_text((72, 380), "WORD", **CELL)
    for lab, cx in cols.items():
        _center(p, lab, cx, 380, **CELL)
    cells = {(0x0, "00xh"): "EvtFlags", (0xD, "00xh"): "MixLvl", (0x2, "01xh"): "CurveT00",
             (0x8, "02xh"): "ThrCfg", (0x4, "0Dxh"): "AtLoad", (0x3, "02xh"): "sLowCap-\nAlarm"}
    for r in range(16):
        y = 400 + 16 * r
        p.insert_text((80, y), f"{r:X}h", **CELL)
        for (rr, col), text in cells.items():
            if rr == r:
                parts = text.split("\n")
                for k, part in enumerate(parts):
                    _center(p, part, cols[col], y + (k - (len(parts) - 1) / 2) * 7, **CELL)
    # p3: the factory-default table
    p = doc.new_page(width=612, height=792)
    _lines(p, ["Table 4. Nonvolatile Memory Configuration Options",
               "            REGISTER      FACTORY",
               "ADDRESS",
               "              NAME        DEFAULT",
               "   19Fh      nThrCfg       0xA4C2",
               "   1B0h      nModeCfg      0x0000"])
    doc.set_toc([[1, "Register Descriptions", 1], [1, "Memory Map", 2]])
    doc.set_metadata({"title": "MXSYN Synthetic Gauge", "keywords": "MXSYN"})
    doc.save(path)
    return path


def build_mxhx(path: Path) -> Path:
    """The Maxim heading layout: `Name Register (0x..)` headings, one summary table with a DEFAULT column."""
    doc = pymupdf.open()
    p = doc.new_page(width=612, height=792)
    y = 90
    for text, font in [("Register Descriptions", BODY), ("StatReg Register (0x1A)", BOLD),
                       ("The StatReg register holds status bits.", BODY), ("", BODY),
                       ("LimitA Register (0x14)", BOLD), ("The LimitA register sets a limit.", BODY), ("", BODY),
                       ("CmdReg Register (0xFE)", BOLD), ("The CmdReg register takes commands.", BODY), ("", BODY),
                       ("OrphanH Register (0x30)", BOLD), ("A heading the summary table never lists.", BODY)]:
        if text:
            p.insert_text((54, y), text, **font)
        y += 14
    p = doc.new_page(width=612, height=792)
    p.insert_text((54, 80), "Table 1. Register Summary", **BODY)
    for x, t in ((54, "ADDRESS"), (110, "REGISTER"), (200, "16-BIT LSb"), (270, "DESCRIPTION"), (420, "READ/WRITE"),
                 (500, "DEFAULT")):
        p.insert_text((x, 100), t, **CELL)
    p.insert_text((110, 109), "NAME", **CELL)
    rows = [("0x0C", "CfgReg", "\u2014", "Compensation to optimize", "R/W", "0x971C"),
            ("0x14", "LimitA", "20 mV", "Voltage limit, two-line", "R/W", "0xFF00"),
            ("0x1A", "StatReg", "\u2014", "Status flags", "R/W", "0x0100"),
            ("0xFE", "CmdReg", "\u2014", "Command register", "W", "\u2014")]
    for i, (a, n, lsb, d, rw, df) in enumerate(rows):
        y = 130 + 16 * i
        for x, t in ((54, a), (110, n), (200, lsb), (270, d), (420, rw), (500, df)):
            p.insert_text((x, y), t, fontname="helv", fontsize=8)
    doc.set_metadata({"title": "MXHX Synthetic Gauge", "keywords": "MXHX"})
    doc.save(path)
    return path


BUILDERS = {"syn700@A": build_syn700, "syn257@A": build_syn257, "syn880@A": build_syn880, "mxsyn@1": build_mxsyn,
            "mxhx@1": build_mxhx}


def make(tmp: Path, doc_id: str) -> tuple[Path, Path]:
    """Build one synthetic PDF and run doc prep on it into <tmp>/atlas. Returns (atlas, docdir)."""
    from embarch_atlas import pdftext

    tmp = Path(tmp)
    pdf = BUILDERS[doc_id](tmp / f"{doc_id.split('@')[0].upper()}.pdf")
    atlas = tmp / "atlas"
    docdir = pdftext.prep(pdf, atlas, doc_id=doc_id, log=lambda *_: None)
    return atlas, docdir

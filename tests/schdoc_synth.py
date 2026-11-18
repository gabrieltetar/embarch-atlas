"""Synthetic Altium project for `cad` tests: a test-only compound-file writer and generic records.

The writer makes a version-3 compound file (512-byte sectors) with one stream, `FileHeader`. It has no
mini stream, so every `FileHeader` here is padded above the 4096-byte cutoff by a binary filler record
(which `parse_records` skips); `olefile` opening these files is the check on this writer. Nothing here is
a real schematic: designators, nets and part numbers are generic.
"""
from __future__ import annotations

import struct
from pathlib import Path

SECTOR = 512
FREE, END, FATSECT = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD
CUTOFF = 4096


def cfb(stream: bytes, name: str = "FileHeader") -> bytes:
    assert len(stream) >= CUTOFF, "stream must sit above the mini-stream cutoff"
    n = -(-len(stream) // SECTOR)
    assert n <= 126, "one FAT sector only"
    fat = [FATSECT, END] + [2 + i + 1 for i in range(n - 1)] + [END] + [FREE] * (128 - 2 - n)
    difat = [0] + [FREE] * 108
    header = struct.pack("<8s16sHHHHH6sIIIIIIIII", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", b"\0" * 16, 0x3E, 3, 0xFFFE, 9, 6,
                         b"\0" * 6, 0, 1, 1, 0, CUTOFF, END, 0, END, 0) + struct.pack("<109I", *difat)

    def entry(nm: str, typ: int, left: int, right: int, child: int, start: int, size: int) -> bytes:
        raw = nm.encode("utf-16-le")
        return (raw.ljust(64, b"\0") + struct.pack("<HBB", len(raw) + 2 if nm else 0, typ, 1 if typ else 0)
                + struct.pack("<III", left, right, child) + b"\0" * 36 + struct.pack("<IQ", start, size))

    blank = entry("", 0, FREE, FREE, FREE, 0, 0)
    directory = entry("Root Entry", 5, FREE, FREE, 1, END, 0) + entry(name, 2, FREE, FREE, FREE, 2, len(stream)) + blank + blank
    return header + struct.pack("<128I", *fat) + directory + stream.ljust(n * SECTOR, b"\0")


def stream_of(records: list[dict[str, object]]) -> bytes:
    """The FileHeader stream: the header record, then each record's text, then a binary filler up to the cutoff."""
    def text(d: dict[str, object]) -> bytes:
        body = ("|" + "|".join(f"{k}={v}" for k, v in d.items()) + "|").encode("cp1252") + b"\0"
        return len(body).to_bytes(3, "little") + b"\x00" + body

    out = text({"HEADER": "Protel for Windows - Schematic Capture Binary File Version 5.0", "WEIGHT": len(records)})
    out += b"".join(text(r) for r in records)
    pad = max(0, CUTOFF + 64 - len(out) - 4)
    return out + pad.to_bytes(3, "little") + b"\x01" + b"\xAA" * pad


class Sheet:
    """Records in order; `OWNERINDEX` counts from 0 after the header, as `parse_records` assumes."""

    def __init__(self) -> None:
        self.recs: list[dict[str, object]] = []

    def add(self, **kw: object) -> int:
        self.recs.append({k.replace("__", "."): v for k, v in kw.items()})
        return len(self.recs) - 1

    def comp(self, ref: str, uid: str, part: int = 1, mpn: str | None = None, value: str | None = None) -> int:
        i = self.add(RECORD=1, LIBREFERENCE="GENERIC", UNIQUEID=uid, CURRENTPARTID=part, PARTCOUNT=2, LOCATION__X=0, LOCATION__Y=0)
        self.add(RECORD=34, OWNERINDEX=i, NAME="Designator", TEXT=ref)
        if mpn:
            self.add(RECORD=41, OWNERINDEX=i, NAME="Manufacturer Part Number", TEXT=mpn)
        if value:
            self.add(RECORD=41, OWNERINDEX=i, NAME="Value", TEXT=value)
        return i

    def pin(self, owner: int, num: str, x: int, y: int, rot: int, part: int = 1, name: str = "", length: int = 10) -> None:
        self.add(RECORD=2, OWNERINDEX=owner, OWNERPARTID=part, DESIGNATOR=num, NAME=name, LOCATION__X=x, LOCATION__Y=y,
                 PINLENGTH=length, PINCONGLOMERATE=rot)

    def wire(self, *pts: tuple[int, int]) -> None:
        d: dict[str, object] = {"RECORD": 27, "LOCATIONCOUNT": len(pts)}
        for n, (x, y) in enumerate(pts, 1):
            d[f"X{n}"], d[f"Y{n}"] = x, y
        self.recs.append(d)

    def label(self, text: str, x: int, y: int) -> None:
        self.add(RECORD=25, TEXT=text, LOCATION__X=x, LOCATION__Y=y)

    def power(self, text: str, x: int, y: int) -> None:
        self.add(RECORD=17, TEXT=text, LOCATION__X=x, LOCATION__Y=y)

    def port(self, name: str, x: int, y: int) -> None:
        self.add(RECORD=18, NAME=name, LOCATION__X=x, LOCATION__Y=y)

    def file(self) -> bytes:
        return cfb(stream_of(self.recs))


def main_sheet() -> Sheet:
    s = Sheet()
    # U1 is a two-part symbol: two records of one designator, CURRENTPARTID 1 and 2
    u1a = s.comp("U1", "AAAAAAA1", part=1, mpn="PART-U1", value="U1 value")
    s.pin(u1a, "1", 100, 100, 0, name="IN")        # far end (110,100) meets W1
    s.pin(u1a, "2", 350, 210, 3, name="VCC")       # far end (350,200) meets the 3V3 wire
    s.pin(u1a, "8", 500, 500, 0, part=3)           # a part that is not the current one: never read
    u1b = s.comp("U1", "AAAAAAA2", part=2)
    s.pin(u1b, "3", 600, 600, 0, part=2, name="OUT")
    r1 = s.comp("R1", "BBBBBBB1", mpn="PART-R", value="10k")
    s.pin(r1, "1", 150, 100, 0)                    # location end (150,100) meets W1
    s.pin(r1, "2", 200, 100, 2)                    # extends -x; location end meets W3
    r2 = s.comp("R2", "BBBBBBB2", mpn="PART-R", value="10k")
    s.pin(r2, "1", 130, 50, 1)                     # far end (130,60) meets W2
    s.pin(r2, "2", 130, 40, 3)                     # far end (130,30) carries a net label
    c1 = s.comp("C1", "CCCCCCC1", value="100n")
    s.pin(c1, "1", 320, 200, 0)                    # lies on the 3V3 wire's segment
    s.pin(c1, "2", 320, 300, 0)                    # connects to nothing
    s.wire((110, 100), (150, 100))                 # W1
    s.label("SIG", 130, 100)                       # on W1's segment
    s.wire((130, 60), (130, 100))                  # W2: ends on W1's segment, a T-junction
    s.wire((200, 100), (250, 100))                 # W3
    s.port("LINK", 250, 100)
    s.label("NLBL", 130, 30)
    s.wire((300, 200), (350, 200))                 # W4
    s.power("3V3", 300, 200)
    return s


def second_sheet() -> Sheet:
    s = Sheet()
    r3 = s.comp("R3", "BBBBBBB3", mpn="PART-R", value="1k")
    s.pin(r3, "1", 10, 10, 0)
    s.pin(r3, "2", 10, 50, 0)
    s.wire((20, 10), (40, 10))
    s.port("LINK", 40, 10)
    s.label("NLBL", 10, 50)
    c2 = s.comp("C2", "EEEEEEE1", value="1u")
    s.pin(c2, "1", 60, 60, 0)
    s.power("3V3", 60, 60)
    return s


def stray_sheet() -> Sheet:
    s = Sheet()
    u = s.comp("U99", "FFFFFFF1")
    s.pin(u, "1", 0, 0, 0)
    return s


PRJ = """[Design]
Version=1.0
HierarchyMode=0

[Document1]
DocumentPath=Main.SchDoc

[Document2]
DocumentPath=sub\\Second.SchDoc

[Document3]
DocumentPath=Board.PcbDoc

[ProjectVariant1]
Description=NoOpts
Variation1=Designator=R2|UniqueId=\\BBBBBBB2|Kind=1
Variation2=Designator=R9|UniqueId=DDDDDDD9|Kind=1
Variation3=Designator=C1|UniqueId=CCCCCCC1|Kind=0

[ProjectVariant2]
Description=Alt
"""


def write_project(root: Path) -> Path:
    """Main.SchDoc, sub/Second.SchDoc, a Stray.SchDoc the project does not list, and Gen.PrjPcb."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "sub").mkdir(exist_ok=True)
    (root / "Main.SchDoc").write_bytes(main_sheet().file())
    (root / "sub" / "Second.SchDoc").write_bytes(second_sheet().file())
    (root / "Stray.SchDoc").write_bytes(stray_sheet().file())
    (root / "Gen.PrjPcb").write_text(PRJ)
    return root

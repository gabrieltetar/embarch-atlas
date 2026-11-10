"""coverage: BOM-driven matching, the one ✓/✕ table, and one source of truth for the join and net_trace.

Every part and document here is synthetic: invented part numbers shaped like the ones the token patterns
cannot read, built with PyMuPDF in the test.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_answers import atlas_dir  # noqa: E402,F401 - a pytest fixture: the two-board card system
from test_inventory import FILLER, make_docx, make_pdf  # noqa: E402
from test_join import make_project, run_join  # noqa: E402
from test_missing import board, comp  # noqa: E402

from embarch_atlas import answers, cli, common, coverage, inventory as inv, mapgen, missing as ms, nettrace, project, query, update  # noqa: E402
from embarch_atlas.common import read_json, write_json, write_jsonl  # noqa: E402

pytestmark = pytest.mark.skipif(not all(shutil.which(t) for t in ("pdfinfo", "pdftotext")),
                                reason="poppler CLI tools not installed")


def part(mpn: str, net: str = "SIG1") -> dict:
    return comp({"1": (None, net), "2": (None, "GND")}, mpn)


def project_with(tmp_path: Path, parts: dict[str, dict], extra: dict | None = None, mcu: str = 'part = "MCU123"') -> tuple[Path, Path]:
    adir, src = tmp_path / "atlas", tmp_path / "hw"
    adir.mkdir()
    src.mkdir()
    (adir / "atlas.toml").write_text(f'[project]\nfw = "{tmp_path}"\nsources = "hw"\n\n[mcu]\n{mcu}\n')
    board(adir, "M", {"U1": comp({"1": ("PA0", "SIG1"), "2": ("VDD", "3V3"), "3": ("VSS", "GND")}, "MCU123ABC"), **parts}, extra=extra)
    return adir, src


def rows_of(a: dict) -> dict[str, dict]:
    return {R["mpn"]: R for R in a["rows"]}


def ask(adir: Path, *argv: str) -> int:
    return cli.main(["--atlas-dir", str(adir), "missing", *argv])


@pytest.fixture()
def world(tmp_path):
    """One document per rung."""
    adir, src = project_with(tmp_path, {
        "U2": part("QRS7TUVTR"),
        "U3": part("88LOG2G11GT,115"),
        "U4": part("TUV4W25AB,L3F"),
        "U5": part("ABC-.250-6-12R-TR"),
        "U6": part("KM7RP2AC"),
        "U7": part("LN4SQ8BD"),
        "U8": part("MP3TV6CE"),
        "U9": part("NODOC9X1"),
        "U10": part("VW2XY4Z"),
        "F1": part("PQRLS0100YG00001B"),
        "F2": part("KLMA44002"),
    }, extra={"U5": {"datasheet_link": "https://vendor.example/pdf/ABX-12R.pdf?x=1"},
              "U10": {"datasheet_link": "https://vendor.example/get?id=88"}})
    make_pdf(src / "QRS7TUV.pdf", [f"QRS7TUV load switch\nFeatures\n{FILLER}"])
    make_pdf(src / "88log2g11.pdf", [f"88LOG2G11 quad gate\nFeatures\n{FILLER}"])
    make_pdf(src / "gate.pdf", [f"Features\n{FILLER}"], {"title": "TUV4W25AB buffer"})
    make_pdf(src / "abx-12r.pdf", [f"Fuse\nFeatures\n{FILLER}"])
    make_pdf(src / "switch-a.pdf", [f"Features\nOrder as KM7RP2AC\n{FILLER}"])
    make_pdf(src / "switch-b.pdf", [f"Features\n{FILLER}"] * 4 + [f"Ordering information\nLN4SQ8BD 8 pins\n{FILLER}"])
    make_pdf(src / "switch-c.pdf", [f"Features\n{FILLER}"] * 4 + [f"Typical application with an MP3TV6CE\n{FILLER}"])
    make_pdf(src / "pqr.pdf", [f"Features\n{FILLER}"], {"title": "PQR Series thermal fuses"})
    make_pdf(src / "klm.pdf", [f"Series KLMA fuses\nFeatures\n{FILLER}"])
    make_pdf(src / "vendor-file.pdf", [f"Features\n{FILLER}"])
    (src / "vendor-file.pdf:Zone.Identifier").write_text("[ZoneTransfer]\nHostUrl=https://vendor.example/get?id=88\n")
    inv.run(adir)
    return adir, src


# ---------------------------------------------------------------- the rungs

def test_part_stems():
    assert "QRS7TUV" in coverage.part_stems("QRS7TUVTR")
    assert coverage.part_stems("88LOG2G11GT,115")[0] == "88LOG2G11GT" and "88LOG2G11" in coverage.part_stems("88LOG2G11GT,115")
    assert coverage.part_stems("TUV4W25AB,L3F")[0] == "TUV4W25AB"
    assert coverage.part_stems("AB12+CD") == []                 # 4 characters once the packaging is cut
    assert coverage.part_stems("ABCDEFGH") == []                # no digit: not a stem
    assert coverage.part_stems("12345678") == []                # no letter
    stems = coverage.part_stems("QRS7TUVTR")
    assert stems == sorted(stems, key=len, reverse=True) and min(map(len, stems)) == 5


def test_each_rung_with_its_evidence(world):
    adir, _ = world
    R = rows_of(ms.analyse(adir))

    def prop(mpn):
        return [(p["file"], p["rung"], p["evidence"]) for p in R[mpn]["proposals"]]

    assert prop("QRS7TUVTR") == [("QRS7TUV.pdf", "stem", "stem QRS7TUV in the file name")]
    assert prop("88LOG2G11GT,115") == [("88log2g11.pdf", "stem", "stem 88LOG2G11 in the file name")]
    assert prop("TUV4W25AB,L3F") == [("gate.pdf", "stem", "stem TUV4W25AB in the PDF title")]
    assert prop("PQRLS0100YG00001B") == [("pqr.pdf", "series", "'PQR Series' in the PDF title")]
    assert prop("KLMA44002") == [("klm.pdf", "series", "'Series KLMA' on p1")]
    assert prop("MP3TV6CE") == [("switch-c.pdf", "exact", "MP3TV6CE on p5, not a front or ordering page")]
    assert R["MP3TV6CE"]["covered_by"] == []                    # a mention elsewhere is only a proposal
    assert R["KM7RP2AC"]["covered_by"] == ["switch-a.pdf"] and R["KM7RP2AC"]["proposals"] == []
    assert R["KM7RP2AC"]["covered_how"]["switch-a.pdf"]["evidence"] == "KM7RP2AC on p1"
    assert R["LN4SQ8BD"]["covered_by"] == ["switch-b.pdf"]
    assert R["LN4SQ8BD"]["covered_how"]["switch-b.pdf"]["evidence"] == "LN4SQ8BD on p5, an ordering-information page"
    assert R["ABC-.250-6-12R-TR"]["covered_by"] == ["abx-12r.pdf"]
    assert R["ABC-.250-6-12R-TR"]["covered_how"]["abx-12r.pdf"]["how"] == "link"
    assert R["VW2XY4Z"]["covered_by"] == ["vendor-file.pdf"]
    assert "HostUrl" in R["VW2XY4Z"]["covered_how"]["vendor-file.pdf"]["evidence"]
    assert R["NODOC9X1"]["covered_by"] == [] and R["NODOC9X1"]["proposals"] == []


@pytest.mark.parametrize("mpn,page,hit", [
    ("KM7RP2AC", "Order as KM7RP2AC", True),
    ("KM7RP2AC", "order as km7rp2ac", True),                    # case
    ("KM7-RP2AC", "KM7RP2AC", True),                            # punctuation in the BOM
    ("KM7RP2AC", "KM7-RP2-AC", True),                           # punctuation in the text
    ("KM7RP2AC", "KM7RP2-\nAC", True),                          # a line break in a table cell
    ("KM7RP2AC", "XKM7RP2AC", False),                           # inside a longer token, left
    ("KM7RP2AC", "KM7RP2AC9", False),                           # a longer part number, right
    ("KM7RP2AC", "KM7 and RP2 and AC", False),                  # words that happen to concatenate
])
def test_exact_is_a_whole_token(mpn, page, hit):
    m = coverage.match_document(mpn, [], {"file": "doc.pdf", "kind": "datasheet"}, [page])
    assert bool(m and m["rung"] == "exact") is hit


def test_a_word_is_not_a_part_number():
    """`BARREL` on a page says nothing; `exact` needs a letter, a digit and 5 characters."""
    assert coverage.match_document("BARREL", [], {"file": "doc.pdf", "kind": "datasheet"}, ["a BARREL jack"]) is None
    assert coverage.match_document("AB1", [], {"file": "doc.pdf", "kind": "datasheet"}, ["AB1"]) is None


@pytest.mark.parametrize("link,file,origin,hit", [
    ("https://vendor.example/pdf/ABX-12R.pdf?x=1", "abx-12r.pdf", None, True),
    ("https://vendor.example/pdf/ABX-12R.pdf#page=2", "docs/ABX-12R.PDF", None, True),
    ("https://vendor.example/pdf/My%20Part.pdf", "my part.pdf", None, True),
    ("https://vendor.example/pdf/ABX-12R.pdf", "abx-12.pdf", None, False),
    ("https://vendor.example/get?id=88", "file.pdf", "https://vendor.example/get?id=88", True),
    ("https://vendor.example/get?id=88", "file.pdf", "https://vendor.example/get?id=89", False),
])
def test_link_rung(link, file, origin, hit):
    doc = {"file": file, "kind": "datasheet", **({"origin": {"host_url": origin}} if origin else {})}
    m = coverage.match_document("ABC-.250-6-12R-TR", [link], doc, ["Nothing about it"])
    assert bool(m and m["rung"] == "link" and m["covered"]) is hit


def test_a_stem_is_a_token_or_token_prefix_only():
    doc = {"file": "x.pdf", "kind": "datasheet"}
    assert coverage.match_document("QRS7TUVTR", [], doc, ["The qrs7tuv family"])["evidence"] == "stem QRS7TUV on p1"
    assert coverage.match_document("QRS7TUVTR", [], doc, ["The QRS7TUVXY family"])["evidence"] == "stem QRS7TUV on p1"
    assert coverage.match_document("QRS7TUVTR", [], doc, ["The AQRS7TUV family"]) is None
    assert coverage.match_document("QRS7TUVTR", [], doc, ["x", "y", "The QRS7TUV family"]) is None   # p3 is too far
    assert coverage.match_document("QRS7TUVTR", [], doc, ["The QRS7 family"]) is None    # shorter than 5 characters


def test_a_series_needs_the_mpn_to_start_with_it():
    doc = {"file": "x.pdf", "kind": "datasheet"}
    assert coverage.match_document("PQRLS0100", [], doc, ["The PQR Series"])["rung"] == "series"
    assert coverage.match_document("PQRLS0100", [], doc, ["The XYZ Series"]) is None


def test_a_superseded_or_duplicate_file_is_not_offered(tmp_path):
    adir, src = project_with(tmp_path, {"U2": part("QRS7TUVTR")})
    make_pdf(src / "QRS7TUV.pdf", [f"QRS7TUV\nFeatures\n{FILLER}"])
    shutil.copy(src / "QRS7TUV.pdf", src / "z-copy-QRS7TUV.pdf")
    inv.run(adir)
    assert [p["file"] for p in rows_of(ms.analyse(adir))["QRS7TUVTR"]["proposals"]] == ["QRS7TUV.pdf"]


def test_strongest_proposal_comes_first(tmp_path):
    adir, src = project_with(tmp_path, {"U2": part("QRS7TUVTR")})
    make_pdf(src / "a-mentions.pdf", [f"Features\n{FILLER}"] * 4 + [f"With a QRS7TUVTR\n{FILLER}"])
    make_pdf(src / "b-short.pdf", [f"QRS7T\nFeatures\n{FILLER}"])
    make_pdf(src / "c-long.pdf", [f"QRS7TUV\nFeatures\n{FILLER}"])
    inv.run(adir)
    assert [(p["file"], p["rung"]) for p in rows_of(ms.analyse(adir))["QRS7TUVTR"]["proposals"]] == [
        ("a-mentions.pdf", "exact"), ("c-long.pdf", "stem"), ("b-short.pdf", "stem")]


# ---------------------------------------------------------------- the table

def test_missing_md_opens_with_one_table(world):
    adir, _ = world
    assert ask(adir) == 0
    md = (adir / "inventory" / "missing.md").read_text()
    assert md.startswith("# Missing documents: questions for the engineer\n\n**")
    assert "**10 active part numbers (U/Q/D) on the boards: 4 covered, 4 proposed, 2 missing, 2 of them firmware-visible.**" in md
    assert md.index("## Proposed matches: confirm or reject") < md.index("## How to answer") < md.index("## MCU documents")
    assert "| MPN | refs | proposed file | rung | evidence |" in md
    assert "| QRS7TUVTR | M:U2 | QRS7TUV.pdf | stem | stem QRS7TUV in the file name |" in md
    assert "| PQRLS0100YG00001B | M:F1 | pqr.pdf | series | 'PQR Series' in the PDF title |" in md
    assert "`embarch-atlas missing --confirm-all`" in md
    assert "`embarch-atlas missing --reject <MPN> --file <file>`" in md
    assert "**QRS7TUVTR** x1" not in md                         # not asked for: it is in the table
    assert "**NODOC9X1** x1" in md                              # still asked for
    assert "| KM7RP2AC | M:U6 | direct | switch-a.pdf (exact: KM7RP2AC on p1) |" in md
    assert "download" not in md.lower()


def test_stdout_counts_covered_proposed_and_missing(world, capsys):
    adir, _ = world
    assert ask(adir) == 0
    assert "10 active MPNs, 4 covered, 4 proposed, 2 without a document, 2 firmware-visible" in capsys.readouterr().out


def test_no_proposal_no_table(tmp_path):
    adir, _ = project_with(tmp_path, {"U2": part("NODOC9X1")})
    inv.run(adir)
    assert "## Proposed matches" not in ms.render_md(ms.analyse(adir))


def test_a_pipe_in_a_file_name_keeps_the_table_whole(tmp_path):
    adir, src = project_with(tmp_path, {"U2": part("QRS7TUVTR")})
    make_pdf(src / "a|QRS7TUV.pdf", [f"Features\n{FILLER}"])
    inv.run(adir)
    assert "a\\|QRS7TUV.pdf" in ms.render_md(ms.analyse(adir))


# ---------------------------------------------------------------- confirm, reject

def test_confirm_one_covers_it_and_stores_the_answer(world, capsys):
    adir, src = world
    assert ask(adir, "--confirm", "QRS7TUVTR") == 0
    assert "confirmed: QRS7TUVTR -> QRS7TUV.pdf" in capsys.readouterr().out
    a = answers.load(adir)["QRS7TUVTR"]
    assert a == {"answer": "document", "file": "QRS7TUV.pdf", "sha256": common.sha256_file(src / "QRS7TUV.pdf"), "rung": "stem"}
    R = rows_of(ms.analyse(adir))["QRS7TUVTR"]
    assert R["covered_by"] == ["QRS7TUV.pdf"] and R["proposals"] == [] and R["covered_how"]["QRS7TUV.pdf"]["how"] == "confirmed"
    md = (adir / "inventory" / "missing.md").read_text()
    assert "| QRS7TUVTR | M:U2 | QRS7TUV.pdf" not in md.split("## How to answer")[0]
    assert "| QRS7TUVTR | M:U2 | direct | QRS7TUV.pdf (confirmed) |" in md


def test_confirm_with_a_file_needs_no_proposal(world):
    adir, _ = world
    assert ask(adir, "--confirm", "NODOC9X1", "--file", "klm.pdf") == 0
    assert rows_of(ms.analyse(adir))["NODOC9X1"]["covered_by"] == ["klm.pdf"]
    assert answers.load(adir)["NODOC9X1"]["rung"] == "engineer"


def test_confirm_all_accepts_the_first_row_of_each_part(tmp_path, capsys):
    adir, src = project_with(tmp_path, {"U2": part("QRS7TUVTR"), "U3": part("KLMA44002"), "U4": part("NODOC9X1")})
    make_pdf(src / "a-long.pdf", [f"QRS7TUV\nFeatures\n{FILLER}"])
    make_pdf(src / "b-short.pdf", [f"QRS7T\nFeatures\n{FILLER}"])
    make_pdf(src / "klm.pdf", [f"Series KLMA\nFeatures\n{FILLER}"])
    inv.run(adir)
    assert ask(adir, "--confirm-all") == 0
    assert "2 proposals confirmed" in capsys.readouterr().out
    R = rows_of(ms.analyse(adir))
    assert R["QRS7TUVTR"]["covered_by"] == ["a-long.pdf"] and R["KLMA44002"]["covered_by"] == ["klm.pdf"]
    assert R["NODOC9X1"]["covered_by"] == []
    assert "## Proposed matches" not in (adir / "inventory" / "missing.md").read_text()
    assert ask(adir, "--confirm-all") == 0                      # idempotent, and said
    assert "nothing is proposed" in capsys.readouterr().out


def test_refused_confirmations(world, capsys):
    adir, _ = world
    assert ask(adir, "--confirm", "NOSUCHPART") == 2
    assert "no part on the boards has MPN" in capsys.readouterr().err
    assert ask(adir, "--confirm", "NODOC9X1") == 2
    assert "has no proposal" in capsys.readouterr().err
    assert ask(adir, "--confirm", "NODOC9X1", "--file", "nofile.pdf") == 2
    assert "not in the manifest" in capsys.readouterr().err
    assert ask(adir, "--reject", "QRS7TUVTR") == 2
    assert "--reject needs --file" in capsys.readouterr().err
    assert ask(adir, "--file", "klm.pdf") == 2
    assert "--file goes with" in capsys.readouterr().err
    assert ask(adir, "--kind", "klm.pdf", "memo") == 2
    assert "kind must be one of" in capsys.readouterr().err


def test_confirm_needs_a_document_kind(tmp_path, capsys):
    adir, src = project_with(tmp_path, {"U2": part("NODOC9X1")})
    (src / "bench.md").write_text("# Notes\n")
    inv.run(adir)
    assert ask(adir, "--confirm", "NODOC9X1", "--file", "bench.md") == 2
    err = capsys.readouterr().err
    assert "never covers a part" in err and "--kind bench.md datasheet" in err


def test_rejection_holds_until_the_file_changes(world):
    adir, src = world
    assert ask(adir, "--reject", "QRS7TUVTR", "--file", "QRS7TUV.pdf") == 0
    R = rows_of(ms.analyse(adir))["QRS7TUVTR"]
    assert R["proposals"] == [] and R["covered_by"] == []
    assert "**QRS7TUVTR** x1" in (adir / "inventory" / "missing.md").read_text()    # asked for again
    inv.run(adir)
    assert rows_of(ms.analyse(adir))["QRS7TUVTR"]["proposals"] == []                # a re-inventory changes nothing
    shutil.move(src / "QRS7TUV.pdf", src / "QRS7TUV-renamed.pdf")
    inv.run(adir)
    assert rows_of(ms.analyse(adir))["QRS7TUVTR"]["proposals"] == []                # nor a rename: same content
    make_pdf(src / "QRS7TUV-renamed.pdf", [f"QRS7TUV load switch, new revision\nFeatures\n{FILLER}"])
    inv.run(adir)
    assert [p["file"] for p in rows_of(ms.analyse(adir))["QRS7TUVTR"]["proposals"]] == ["QRS7TUV-renamed.pdf"]


def test_rejecting_the_strongest_offers_the_next(tmp_path):
    adir, src = project_with(tmp_path, {"U2": part("QRS7TUVTR")})
    make_pdf(src / "a-long.pdf", [f"QRS7TUV\nFeatures\n{FILLER}"])
    make_pdf(src / "b-short.pdf", [f"QRS7T\nFeatures\n{FILLER}"])
    inv.run(adir)
    assert ask(adir, "--reject", "QRS7TUVTR", "--file", "a-long.pdf") == 0
    assert [p["file"] for p in rows_of(ms.analyse(adir))["QRS7TUVTR"]["proposals"]] == ["b-short.pdf"]
    assert ask(adir, "--confirm", "QRS7TUVTR") == 0
    assert answers.load(adir)["QRS7TUVTR"]["file"] == "b-short.pdf"
    assert answers.load(adir)["QRS7TUVTR"]["rejected"][0]["file"] == "a-long.pdf"    # both in one table


def test_rejection_takes_off_a_covering_document_of_any_rung(tmp_path):
    adir, src = project_with(tmp_path, {"U2": part("ZX123456"), "U3": part("KM7RP2AC")})
    make_pdf(src / "other-part.pdf", [f"Features\nWorks with a ZX123456 and a KM7RP2AC\n{FILLER}"])
    inv.run(adir)
    R = rows_of(ms.analyse(adir))
    assert R["ZX123456"]["covered_by"] == ["other-part.pdf"] and R["KM7RP2AC"]["covered_by"] == ["other-part.pdf"]
    assert ask(adir, "--reject", "ZX123456", "--file", "other-part.pdf") == 0
    assert ask(adir, "--reject", "KM7RP2AC", "--file", "other-part.pdf") == 0
    R = rows_of(ms.analyse(adir))
    assert R["ZX123456"]["covered_by"] == [] and R["KM7RP2AC"]["covered_by"] == []
    assert "**ZX123456** x1" in (adir / "inventory" / "missing.md").read_text()


def test_reject_then_confirm_of_one_file_ends_the_rejection_and_the_reverse(world):
    adir, _ = world
    ask(adir, "--reject", "QRS7TUVTR", "--file", "QRS7TUV.pdf")
    ask(adir, "--confirm", "QRS7TUVTR", "--file", "QRS7TUV.pdf")
    a = answers.load(adir)["QRS7TUVTR"]
    assert a["answer"] == "document" and "rejected" not in a
    ask(adir, "--reject", "QRS7TUVTR", "--file", "QRS7TUV.pdf")
    a = answers.load(adir)["QRS7TUVTR"]
    assert "answer" not in a and a["rejected"][0]["file"] == "QRS7TUV.pdf"
    assert answers.find(answers.load(adir), "QRS7TUVTR") is None


def test_a_changed_file_sends_a_confirmation_back_to_proposed(world):
    adir, src = world
    ask(adir, "--confirm", "NODOC9X1", "--file", "klm.pdf")
    make_pdf(src / "klm.pdf", [f"Series KLMA fuses, rev 2\nFeatures\n{FILLER}"])
    inv.run(adir)
    R = rows_of(ms.analyse(adir))["NODOC9X1"]
    assert R["covered_by"] == []
    assert [(p["file"], p["rung"]) for p in R["proposals"]] == [("klm.pdf", "engineer")]
    assert "the file changed since" in R["proposals"][0]["evidence"]
    assert "| NODOC9X1 | M:U9 | klm.pdf | engineer | confirmed earlier; the file changed since" in ms.render_md(ms.analyse(adir))
    assert ask(adir, "--confirm-all") == 0                      # confirming takes the new content
    assert answers.load(adir)["NODOC9X1"]["sha256"] == common.sha256_file(src / "klm.pdf")
    assert rows_of(ms.analyse(adir))["NODOC9X1"]["covered_by"] == ["klm.pdf"]


def test_a_confirmed_file_that_is_gone_is_asked_for_again(world):
    adir, src = world
    ask(adir, "--confirm", "NODOC9X1", "--file", "klm.pdf")
    (src / "klm.pdf").unlink()
    inv.run(adir)
    a = ms.run(adir)
    R = rows_of(a)["NODOC9X1"]
    assert R["covered_by"] == [] and R["proposals"] == []
    assert "Confirmed earlier as klm.pdf, which is no longer in the sources folder." in (adir / "inventory" / "missing.md").read_text()


def test_a_confirmation_survives_a_rename(world):
    adir, src = world
    ask(adir, "--confirm", "QRS7TUVTR")
    shutil.move(src / "QRS7TUV.pdf", src / "renamed.pdf")
    inv.run(adir)
    assert rows_of(ms.analyse(adir))["QRS7TUVTR"]["covered_by"] == ["renamed.pdf"]


def test_an_answer_keeps_its_rejections_and_a_decided_part_gets_no_proposal(world):
    adir, _ = world
    ask(adir, "--reject", "QRS7TUVTR", "--file", "QRS7TUV.pdf")
    assert cli.main(["--atlas-dir", str(adir), "missing", "--answer", "QRS7TUVTR", "not-needed", "--why", "never touched"]) == 0
    a = answers.load(adir)["QRS7TUVTR"]
    assert a["answer"] == "not-needed" and a["rejected"][0]["file"] == "QRS7TUV.pdf"
    assert cli.main(["--atlas-dir", str(adir), "missing", "--answer", "KLMA44002", "not-needed", "--why", "a fuse"]) == 0
    R = rows_of(ms.run(adir))["KLMA44002"]
    assert R["proposals"] == [] and R["answer"]["answer"] == "not-needed"
    assert cli.main(["--atlas-dir", str(adir), "missing", "--answer", "TUV4W25AB,L3F", "supplied"]) == 0
    assert rows_of(ms.run(adir))["TUV4W25AB,L3F"]["proposals"]  # "I will supply it", and here it is


def test_answer_cannot_record_a_document(world):
    adir, _ = world
    assert cli.main(["--atlas-dir", str(adir), "missing", "--answer", "QRS7TUVTR", "document"]) == 2


def test_answers_round_trip_every_new_shape(world):
    adir, _ = world
    ask(adir, "--confirm", "QRS7TUVTR")
    ask(adir, "--reject", "KLMA44002", "--file", 'klm.pdf')
    d = coverage.load(adir).doc_named("pqr.pdf")
    answers.record_document(adir, d["sha256"], d["file"], kind="other", not_reference_manual=True)
    parts, docs = answers.load(adir), answers.load_documents(adir)
    answers._save(adir, parts, docs)
    assert answers.load(adir) == parts and answers.load_documents(adir) == docs
    assert docs[d["sha256"][:12]] == {"file": "pqr.pdf", "kind": "other", "not_reference_manual": True}


# ---------------------------------------------------------------- one source of truth: join, net_trace, MAP

ZQ_SHA = "2" * 64


def _zq_distillation(adir: Path, part_name: str = "ZQ9") -> None:
    """A translated document whose own part name is not the board's MPN, so only the engineer ties them."""
    d = adir / "docs" / "zq9@1"
    write_json(d / "doc.json", {"doc_id": "zq9@1", "part": part_name, "kind": "datasheet", "file": "zq-switch.pdf", "sha256": ZQ_SHA})
    base = {"kind": "pin", "doc": "zq9@1", "p": 3, "src": "table", "prov": "llm-verified"}
    write_jsonl(d / "records" / "pin.jsonl", [
        dict(base, id="zq9.pin.G", pin=1, name="G", dir="in", flow="sink", control=True),
        dict(base, id="zq9.pin.S", pin=2, name="S", dir="passive", flow="bidir"),
        dict(base, id="zq9.pin.D", pin=[3, 4], name="D", dir="passive", flow="bidir")])


def _manifest(adir: Path, *docs: dict) -> None:
    write_json(adir / "inventory" / "manifest.json", {"documents": list(docs)})


def test_a_confirmed_document_covers_the_part_in_net_trace(atlas_dir):  # noqa: F811
    _zq_distillation(atlas_dir)
    _manifest(atlas_dir, {"file": "zq-switch.pdf", "kind": "datasheet", "sha256": ZQ_SHA, "parts": [], "doc_id": "zq9@1"})
    before = query.net_trace(query.Atlas(atlas_dir), "B:5V0_SW")
    assert "- PFET9: A:Q1" in before and "fed by" not in before
    assert "no datasheet: role of every pin unknown" in query.net_trace(query.Atlas(atlas_dir), "A:Q1")

    assert ask(atlas_dir, "--confirm", "PFET9", "--file", "zq-switch.pdf") == 0
    after = query.net_trace(query.Atlas(atlas_dir), "B:5V0_SW")
    assert "- PFET9" not in after                               # no longer asked for
    assert "fed by A:Q1-3,4 PFET9 D flow=bidir (zq9@1 p3)" in after     # its pin records are read
    part = query.net_trace(query.Atlas(atlas_dir), "A:Q1")
    assert "datasheet zq9@1" in part and "A:Q1-3 D -> A:5V0_SW" in part
    J = nettrace.load(atlas_dir, None, read_json)
    assert J.part_docs[("A", "Q1")] == ["zq9@1"]
    assert "PFET9" not in nettrace.walk_stops(J)


def test_a_rejected_document_comes_off_the_join_too(atlas_dir):  # noqa: F811
    _zq_distillation(atlas_dir, "PFET9")                        # its identity says it is this part's
    _manifest(atlas_dir, {"file": "zq-switch.pdf", "kind": "datasheet", "sha256": ZQ_SHA, "parts": ["PFET9"], "doc_id": "zq9@1"})
    assert "datasheet zq9@1" in query.net_trace(query.Atlas(atlas_dir), "A:Q1")
    assert ask(atlas_dir, "--reject", "PFET9", "--file", "zq-switch.pdf") == 0
    out = query.net_trace(query.Atlas(atlas_dir), "A:Q1")
    assert "no datasheet: role of every pin unknown" in out
    assert "- PFET9: A:Q1" in query.net_trace(query.Atlas(atlas_dir), "B:5V0_SW")


def test_a_supplied_document_without_a_translation_is_not_asked_for_again(atlas_dir):  # noqa: F811
    _manifest(atlas_dir, {"file": "zq-switch.pdf", "kind": "datasheet", "sha256": ZQ_SHA, "parts": []})
    assert ask(atlas_dir, "--confirm", "PFET9", "--file", "zq-switch.pdf") == 0
    out = query.net_trace(query.Atlas(atlas_dir), "B:5V0_SW")
    assert "- PFET9" not in out.split("## Documents supplied")[0]
    assert "## Documents supplied, not translated yet" in out and "- PFET9: A:Q1 (zq-switch.pdf)" in out
    assert "parts here with a document supplied, not translated yet: A:Q1 PFET9" in out
    part = query.net_trace(query.Atlas(atlas_dir), "A:Q1")
    assert "document supplied, not translated yet (zq-switch.pdf): role of every pin unknown" in part
    assert "PFET9" not in nettrace.walk_stops(nettrace.load(atlas_dir, None, read_json))


def test_a_covering_rung_reaches_the_join_but_a_part_token_alone_does_not(atlas_dir):  # noqa: F811
    """A token in the manifest stops the asking; a pin map is taken only from what names the MPN."""
    _zq_distillation(atlas_dir)
    write_json(atlas_dir / "boards" / "A" / "bom.json", [
        {"ref": r, "mpn": m, "dni": False, "in_bom": True} for r, m in (("Q1", "PFET99X1"), ("U7", "INVX1"), ("U9", "DEMO-MCU"))])
    nl = read_json(atlas_dir / "boards" / "A" / "netlist.json")
    nl["components"]["Q1"]["mpn"] = "PFET99X1"
    write_json(atlas_dir / "boards" / "A" / "netlist.json", nl)
    _manifest(atlas_dir, {"file": "zq-switch.pdf", "kind": "datasheet", "sha256": ZQ_SHA, "parts": ["PFET99X"], "doc_id": "zq9@1"})
    J = nettrace.load(atlas_dir, None, read_json)
    assert J.part_docs.get(("A", "Q1")) is None and J.part_supplied[("A", "Q1")] == ["zq-switch.pdf"]
    inv.write_text_cache(inv.text_cache_path(atlas_dir, ZQ_SHA), ["Ordering: PFET99X1 in a small package"])
    (atlas_dir / "inventory" / "matches.json").unlink(missing_ok=True)
    _manifest(atlas_dir, {"file": "zq-switch.pdf", "kind": "datasheet", "sha256": ZQ_SHA, "parts": [], "doc_id": "zq9@1", "pages": 1})
    J = nettrace.load(atlas_dir, None, read_json)
    assert J.part_docs[("A", "Q1")] == ["zq9@1"]


def test_a_rung_never_adds_to_a_part_that_has_a_document(atlas_dir):  # noqa: F811
    """Another datasheet's front page may name this part; its pin records must not shadow the part's own."""
    _zq_distillation(atlas_dir, "PFET9")
    other = "3" * 64
    write_json(atlas_dir / "docs" / "oth1@1" / "doc.json", {"doc_id": "oth1@1", "part": "OTH1", "kind": "datasheet", "file": "oth.pdf", "sha256": other})
    inv.write_text_cache(inv.text_cache_path(atlas_dir, other), ["Typical application with a PFET9 switch and INVX1"])
    _manifest(atlas_dir, {"file": "zq-switch.pdf", "kind": "datasheet", "sha256": ZQ_SHA, "parts": [], "doc_id": "zq9@1"},
              {"file": "oth.pdf", "kind": "datasheet", "sha256": other, "parts": [], "doc_id": "oth1@1", "pages": 1})
    J = nettrace.load(atlas_dir, None, read_json)
    assert J.part_docs[("A", "Q1")] == ["zq9@1"]


def test_confirmation_reaches_pins_json_map_and_status(tmp_path):
    ad, atlas = make_project(tmp_path)
    write_json(ad / "boards" / "M" / "bom.json", [{"ref": "U2", "mpn": "LOGIC-INV1", "dni": False, "in_bom": True}])
    write_json(ad / "boards" / "S" / "bom.json", [])
    _manifest(ad, {"file": "inv.pdf", "kind": "datasheet", "sha256": ZQ_SHA, "parts": [], "doc_id": "inv9@1"})
    write_json(ad / "docs" / "inv9@1" / "doc.json", {"doc_id": "inv9@1", "part": "INV9", "kind": "datasheet", "file": "inv.pdf", "sha256": ZQ_SHA})
    run_join(ad, atlas)
    assert "M:U2" not in read_json(atlas / "pins.json")["part_docs"]
    assert "LOGIC-INV1" in (atlas / "MAP.md").read_text().split("## Active parts without a document")[1]
    stale_now = project.status(ad)["atlases"][0]["stale"]
    assert not any("confirmed or rejected" in w for w in stale_now)

    assert ask(ad, "--confirm", "LOGIC-INV1", "--file", "inv.pdf") == 0
    assert any("confirmed or rejected" in w for w in project.status(ad)["atlases"][0]["stale"])   # says so until a join
    run_join(ad, atlas)
    assert read_json(atlas / "pins.json")["part_docs"]["M:U2"] == ["inv9@1"]
    text = (atlas / "MAP.md").read_text()
    assert "| LOGIC-INV1 | M:U2 | `inv9@1` |" in text
    assert "LOGIC-INV1" not in text.split("## Active parts without a document")[1]
    assert not any("confirmed or rejected" in w for w in project.status(ad)["atlases"][0]["stale"])
    assert query.Atlas(ad, atlas.name).find_doc("LOGIC-INV1").name == "inv9@1"     # part_card follows pins.json


def test_map_lists_a_supplied_part_apart_from_a_missing_one(tmp_path):
    ad, atlas = make_project(tmp_path)
    write_json(ad / "boards" / "M" / "bom.json", [{"ref": "U2", "mpn": "LOGIC-INV1", "dni": False, "in_bom": True}])
    write_json(ad / "boards" / "S" / "bom.json", [])
    _manifest(ad, {"file": "inv.pdf", "kind": "datasheet", "sha256": ZQ_SHA, "parts": [], "doc_id": "inv9@1"})
    ask(ad, "--confirm", "LOGIC-INV1", "--file", "inv.pdf")
    run_join(ad, atlas)
    text = (atlas / "MAP.md").read_text()
    assert "## Active parts with a document supplied, not translated yet (1)" in text and "LOGIC-INV1 (inv.pdf)" in text
    assert "LOGIC-INV1" not in text.split("## Active parts without a document")[1]
    one = (atlas / "MAP.md").read_bytes()
    run_join(ad, atlas)
    assert (atlas / "MAP.md").read_bytes() == one


def test_atlas_without_answers_is_not_made_stale(tmp_path):
    ad, atlas = make_project(tmp_path)
    run_join(ad, atlas)
    assert "answers" not in read_json(atlas / "inputs.json")
    assert coverage.answers_fingerprint(ad) is None
    assert not any("confirmed or rejected" in w for w in project.status(ad)["atlases"][0]["stale"])


# ---------------------------------------------------------------- app notes never cover

def test_an_appnote_supports_a_part_and_never_covers_it(tmp_path):
    adir, src = project_with(tmp_path, {"U2": part("ZX123456"), "U3": part("KM7RP2AC")})
    make_pdf(src / "an9988-layout.pdf", [f"AN9988\nApplication note\nLayout for ZX123456 and KM7RP2AC\n{FILLER}\nAN9988 Rev 3"])
    inv.run(adir)
    assert ms.analyse(adir)["rows"]
    R = rows_of(ms.run(adir))
    for mpn in ("ZX123456", "KM7RP2AC"):
        assert R[mpn]["covered_by"] == [] and R[mpn]["proposals"] == [] and R[mpn]["supporting"] == ["an9988-layout.pdf"]
    md = (adir / "inventory" / "missing.md").read_text()
    assert "Supporting material, not its datasheet: an9988-layout.pdf." in md and "**ZX123456** x1" in md
    assert "## Supplied documents nothing uses" not in md       # it is in use, beside the part


def test_an_appnote_shows_beside_a_covered_part(tmp_path):
    adir, src = project_with(tmp_path, {"U2": part("ZX123456")})
    make_pdf(src / "zx123456.pdf", [f"ZX123456\nFeatures\n{FILLER}"])
    make_pdf(src / "guide.pdf", [f"User guide\nUsing the ZX123456\n{FILLER}"])
    inv.run(adir)
    assert "| ZX123456 | M:U2 | direct | zx123456.pdf; supporting: guide.pdf |" in ms.render_md(ms.analyse(adir))


def test_confirming_an_appnote_is_refused_and_names_the_way(tmp_path, capsys):
    adir, src = project_with(tmp_path, {"U2": part("ZX123456")})
    make_pdf(src / "guide.pdf", [f"User guide\nUsing the ZX123456\n{FILLER}"])
    inv.run(adir)
    assert ask(adir, "--confirm", "ZX123456", "--file", "guide.pdf") == 2
    assert "--kind guide.pdf datasheet" in capsys.readouterr().err
    assert ask(adir, "--kind", "guide.pdf", "datasheet") == 0
    assert rows_of(ms.analyse(adir))["ZX123456"]["covered_by"] == ["guide.pdf"]


def test_a_distilled_appnote_is_supporting_in_the_join(atlas_dir):  # noqa: F811
    d = atlas_dir / "docs" / "an9988@3"
    write_json(d / "doc.json", {"doc_id": "an9988@3", "part": "PFET9", "kind": "appnote", "file": "an9988.pdf", "sha256": ZQ_SHA})
    write_jsonl(d / "records" / "pin.jsonl", [{"kind": "pin", "doc": "an9988@3", "p": 3, "id": "x", "pin": 3, "name": "D", "flow": "source"}])
    J = nettrace.load(atlas_dir, None, read_json)
    assert ("A", "Q1") not in J.part_docs and J.part_support[("A", "Q1")] == ["an9988@3"]
    out = query.net_trace(query.Atlas(atlas_dir), "A:Q1")
    assert "no datasheet: role of every pin unknown; supporting: an9988@3" in out
    assert "- PFET9: A:Q1" in query.net_trace(query.Atlas(atlas_dir), "B:5V0_SW")


def test_the_map_shows_an_appnote_beside_its_part(tmp_path):
    ad, atlas = make_project(tmp_path)
    write_json(ad / "docs" / "an9988@3" / "doc.json", {"doc_id": "an9988@3", "part": "LOGIC-INV1", "kind": "appnote", "file": "an9988.pdf", "sha256": ZQ_SHA})
    run_join(ad, atlas)
    text = (atlas / "MAP.md").read_text()
    assert "| LOGIC-INV1 | M:U2 | `an9988@3` (supporting) |" in text
    assert "| `an9988@3` | appnote |" not in text               # not under "tied to no part"
    assert mapgen.parts_table(nettrace.load(ad, None, read_json))[0]["support"] in ([], ["an9988@3"])


def test_find_doc_prefers_the_datasheet_over_an_appnote_naming_the_part(atlas_dir):  # noqa: F811
    write_json(atlas_dir / "docs" / "an1111@1" / "doc.json", {"doc_id": "an1111@1", "part": "ZQ9", "kind": "appnote"})
    write_json(atlas_dir / "docs" / "zq9@1" / "doc.json", {"doc_id": "zq9@1", "part": "ZQ9", "kind": "datasheet"})
    assert query.Atlas(atlas_dir).find_doc("ZQ9").name == "zq9@1"


# ---------------------------------------------------------------- documents nothing uses

def test_supplied_documents_nothing_uses(world):
    adir, src = world
    make_pdf(src / "wrong-part.pdf", [f"YY889900 Features\n{FILLER}"])
    make_pdf(src / "guide-unrelated.pdf", [f"User guide\n{FILLER}"])
    make_pdf(src / "rm-other.pdf", [f"RM7777\nReference manual\nWW1234 MCUs\n{FILLER}"])
    make_docx(src / "minutes.docx")
    (src / "notes.md").write_text("# Bench notes\n")
    make_pdf(src / "qrs-copy.pdf", [f"QRS7TUV load switch\nFeatures\n{FILLER}"])
    inv.run(adir)
    a = ms.run(adir)
    unused = {d["file"] for d in a["unused"]}
    assert unused == {"wrong-part.pdf", "guide-unrelated.pdf", "rm-other.pdf", "minutes.docx"}
    md = (adir / "inventory" / "missing.md").read_text()
    assert "## Supplied documents nothing uses (4)" in md and md.index("## Covered") < md.index("## Supplied documents nothing uses")
    assert "- wrong-part.pdf (datasheet, 1 page); part tokens read: YY889900" in md
    assert "- minutes.docx (other)" in md


def test_a_rejected_document_with_no_other_use_is_unused_and_a_covering_passive_is_not(tmp_path):
    adir, src = project_with(tmp_path, {"U2": part("QRS7TUVTR"), "R9": part("RN123456", "SIG2")})
    make_pdf(src / "QRS7TUV.pdf", [f"QRS7TUV\nFeatures\n{FILLER}"])
    make_pdf(src / "rn123456.pdf", [f"RN123456 network\nFeatures\n{FILLER}"])
    inv.run(adir)
    assert ms.analyse(adir)["unused"] == []
    ask(adir, "--reject", "QRS7TUVTR", "--file", "QRS7TUV.pdf")
    assert [d["file"] for d in ms.analyse(adir)["unused"]] == ["QRS7TUV.pdf"]


# ---------------------------------------------------------------- Word PRDs

def test_a_word_prd_is_a_question_until_its_pdf_arrives(tmp_path):
    adir, src = project_with(tmp_path, {"U2": part("NODOC9X1")})
    make_docx(src / "QQ7 PRD.docx")
    inv.run(adir)
    a = ms.run(adir)
    assert a["word_prds"] == ["QQ7 PRD.docx"] and a["unused"] == []
    md = (adir / "inventory" / "missing.md").read_text()
    assert "## PRD in Word" in md and "**QQ7 PRD.docx** is a PRD in Word" in md and "Export it to PDF" in md
    make_pdf(src / "QQ7 PRD.pdf", [f"QQ7 Product Requirements Document\nVersion: 1.0\n{FILLER}"])
    inv.run(adir)
    a = ms.run(adir)
    D = {d["file"]: d for d in read_json(adir / "inventory" / "manifest.json")["documents"]}
    assert D["QQ7 PRD.docx"]["superseded_by"] == "QQ7 PRD.pdf" and "needs_export" not in D["QQ7 PRD.docx"]
    assert a["word_prds"] == []
    md = (adir / "inventory" / "missing.md").read_text()
    assert "PRD in Word" not in md and "Export it to PDF" not in md
    assert update.stale_translations(adir) == []


def test_a_word_file_is_offered_by_its_name_until_its_pdf_arrives(tmp_path):
    adir, src = project_with(tmp_path, {"U2": part("QRS7TUVTR")})
    make_docx(src / "QRS7TUV.docx")
    inv.run(adir)
    a = ms.analyse(adir)
    assert [p["file"] for p in rows_of(a)["QRS7TUVTR"]["proposals"]] == ["QRS7TUV.docx"]   # by its name; kind other
    make_pdf(src / "QRS7TUV.pdf", [f"QRS7TUV\nFeatures\n{FILLER}"])
    inv.run(adir)
    assert [p["file"] for p in rows_of(ms.analyse(adir))["QRS7TUVTR"]["proposals"]] == ["QRS7TUV.pdf"]


# ---------------------------------------------------------------- the MCU document

MCU_SHA = "4" * 64


def _long_mcu(adir: Path, pages_text: list[str], file: str = "abc-spec.pdf", kind: str = "datasheet", pages: int = 940,
              title: str | None = None, sha: str = MCU_SHA) -> dict:
    inv.write_text_cache(inv.text_cache_path(adir, sha), pages_text)
    return {"file": file, "kind": kind, "sha256": sha, "parts": [], "pages": pages, "doc_id": "abc@1",
            **({"meta": {"Title": title}} if title else {})}


def mcu_project(tmp_path: Path, extra_mcu: str = "") -> Path:
    adir = tmp_path / "atlas"
    adir.mkdir()
    (adir / "atlas.toml").write_text(f'[project]\nfw = "{tmp_path}"\nsources = "hw"\n\n[mcu]\npart = "abC54X20"\n{extra_mcu}')
    board(adir, "M", {"U1": comp({"1": ("P0.01", "SIG1"), "2": ("VDD", "3V3"), "3": ("VSS", "GND")}, "ABC54X20QAAA"),
                      "U2": part("NODOC9X1")})
    return adir


@pytest.mark.parametrize("pages_text,file,title,where", [
    (["ABC54X20 product specification"], "spec.pdf", None, "p1"),
    (["cover", "the abc54x20 and its registers"], "spec.pdf", None, "p2"),
    (["cover"], "spec.pdf", "abC54X20 Product Specification", "the PDF title"),
    (["cover"], "abc54x20_ps_v1.pdf", None, "the file name"),
])
def test_a_long_document_naming_the_mcu_is_offered_as_its_reference_manual(tmp_path, pages_text, file, title, where):
    adir = mcu_project(tmp_path)
    _manifest(adir, _long_mcu(adir, pages_text, file=file, title=title))
    a = ms.run(adir)
    assert [(r["file"], r["where"]) for r in a["rm_rows"]] == [(file, where)]
    md = (adir / "inventory" / "missing.md").read_text()
    assert f"| MCU reference manual | M:U1 | {file} | long MCU document | {file} serves as the reference manual (940 pages" in md
    assert f"{file} is proposed above" in md and "**MCU reference manual**: none" not in md


@pytest.mark.parametrize("mutate", [
    {"pages": 299},
    {"kind": "appnote"},
    {"kind": "errata"},
    {"superseded_by": "x.pdf"},
])
def test_no_reference_manual_row_when_it_does_not_qualify(tmp_path, mutate):
    adir = mcu_project(tmp_path)
    _manifest(adir, {**_long_mcu(adir, ["ABC54X20"]), **mutate})
    a = ms.run(adir)
    assert a["rm_rows"] == []
    assert "**MCU reference manual**: none" in (adir / "inventory" / "missing.md").read_text()


def test_no_row_when_a_document_does_not_name_the_mcu(tmp_path):
    adir = mcu_project(tmp_path)
    _manifest(adir, _long_mcu(adir, ["cover", "contents", "the ABC54X20 on page three is too late"]))
    assert ms.run(adir)["rm_rows"] == []


def test_a_reference_manual_in_the_manifest_ends_the_question_and_the_row(tmp_path):
    adir = mcu_project(tmp_path)
    _manifest(adir, _long_mcu(adir, ["ABC54X20"]),
              _long_mcu(adir, ["abC54X20 reference manual"], file="rm.pdf", kind="reference-manual", pages=500, sha="5" * 64))
    a = ms.run(adir)
    assert a["rm_rows"] == [] and a["mcu_docs"]["reference-manual"] == ["rm.pdf"]    # found though no token reads the name
    assert "- reference manual: rm.pdf" in (adir / "inventory" / "missing.md").read_text()


def test_an_errata_sheet_naming_a_mixed_case_mcu_is_found(tmp_path):
    adir = mcu_project(tmp_path)
    _manifest(adir, _long_mcu(adir, ["abC54X20 errata"], file="es.pdf", kind="errata", pages=20))
    assert ms.run(adir)["mcu_docs"]["errata"] == ["es.pdf"]


def test_confirm_reference_manual_sets_the_kind_through_the_override(tmp_path, capsys):
    """End to end over a real 300-page PDF: the manifest, its report and missing.md all move."""
    adir = mcu_project(tmp_path)
    src = tmp_path / "hw"
    src.mkdir()
    make_pdf(src / "abc-spec.pdf", [f"abC54X20 product specification\nFeatures\n{FILLER}"] + ["x"] * 299)
    inv.run(adir)
    a = ms.run(adir)
    assert [r["file"] for r in a["rm_rows"]] == ["abc-spec.pdf"]
    doc = read_json(adir / "inventory" / "manifest.json")["documents"][0]
    assert ask(adir, "--confirm", "reference-manual") == 0
    assert "abc-spec.pdf serves as the reference manual" in capsys.readouterr().out
    after = read_json(adir / "inventory" / "manifest.json")["documents"][0]
    assert after["kind"] == "reference-manual" and after["kind_classified"] == "datasheet"
    assert after["doc_id"] == doc["doc_id"]                     # a frozen translation stays where it is
    assert "engineer override, classified datasheet" in (adir / "inventory" / "manifest.md").read_text()
    assert answers.load_documents(adir)[doc["sha256"][:12]]["kind"] == "reference-manual"
    a = ms.run(adir)
    assert a["rm_rows"] == [] and a["mcu_docs"]["reference-manual"] == ["abc-spec.pdf"]
    mj, mm = adir / "inventory" / "manifest.json", adir / "inventory" / "manifest.md"
    first = (mj.read_bytes(), mm.read_bytes())
    inv.run(adir)
    assert (mj.read_bytes(), mm.read_bytes()) == first


def test_confirm_all_takes_the_reference_manual_row_too(tmp_path, capsys):
    adir = mcu_project(tmp_path)
    src = tmp_path / "hw"
    src.mkdir()
    make_pdf(src / "abc54x20-spec.pdf", [f"abC54X20 product specification\nFeatures\n{FILLER}"] + ["x"] * 299)
    inv.run(adir)
    assert ask(adir, "--confirm-all") == 0
    out = capsys.readouterr().out
    assert "abc54x20-spec.pdf serves as the reference manual" in out
    assert read_json(adir / "inventory" / "manifest.json")["documents"][0]["kind"] == "reference-manual"


def test_the_kind_applies_before_the_next_inventory_when_the_sources_are_away(tmp_path, capsys):
    adir = mcu_project(tmp_path)
    _manifest(adir, _long_mcu(adir, ["ABC54X20"]))
    assert ask(adir, "--confirm", "reference-manual", "--file", "abc-spec.pdf") == 0
    assert "manifest not rewritten" in capsys.readouterr().out
    assert ms.run(adir)["mcu_docs"]["reference-manual"] == ["abc-spec.pdf"]


def test_reject_reference_manual_brings_the_question_back_until_the_file_changes(tmp_path):
    adir = mcu_project(tmp_path)
    _manifest(adir, _long_mcu(adir, ["ABC54X20"]))
    assert ask(adir, "--reject", "reference-manual", "--file", "abc-spec.pdf") == 0
    assert ms.run(adir)["rm_rows"] == []
    assert "**MCU reference manual**: none" in (adir / "inventory" / "missing.md").read_text()
    _manifest(adir, _long_mcu(adir, ["ABC54X20"], sha="6" * 64))
    assert [r["file"] for r in ms.run(adir)["rm_rows"]] == ["abc-spec.pdf"]


def test_several_long_documents_need_a_file(tmp_path, capsys):
    adir = mcu_project(tmp_path)
    _manifest(adir, _long_mcu(adir, ["ABC54X20"]), _long_mcu(adir, ["ABC54X20"], file="b.pdf", pages=1200, sha="7" * 64))
    assert [r["file"] for r in ms.run(adir)["rm_rows"]] == ["b.pdf", "abc-spec.pdf"]
    assert ask(adir, "--confirm", "reference-manual") == 2
    assert "name one with --file" in capsys.readouterr().err


def test_a_module_keeps_its_own_datasheet_question(tmp_path):
    adir = tmp_path / "atlas"
    adir.mkdir()
    (adir / "atlas.toml").write_text(f'[project]\nfw = "{tmp_path}"\n\n[mcu]\npart = "abC54X20"\nref = "M:U1"\n')
    board(adir, "M", {"U1": comp({"1": ("P0.01", "SIG1"), "2": ("VDD", "3V3"), "3": ("VSS", "GND")}, "MODX-15"), "U2": part("NODOC9X1")})
    _manifest(adir, _long_mcu(adir, ["abC54X20 product specification"]))
    a = ms.run(adir)
    assert [r["file"] for r in a["rm_rows"]] == ["abc-spec.pdf"]
    assert ask(adir, "--confirm", "reference-manual", "--file", "abc-spec.pdf") == 0
    a = ms.run(adir)
    assert a["mcu_docs"]["reference-manual"] == ["abc-spec.pdf"]
    R = rows_of(a)["MODX-15"]
    assert R["tier"] == "mcu" and R["covered_by"] == [] and R["proposals"] == []
    assert "**MODX-15** x1 (M:U1): the MCU itself." in (adir / "inventory" / "missing.md").read_text()


# ---------------------------------------------------------------- cost and stability

def test_missing_runs_no_poppler_over_the_documents(world, monkeypatch):
    adir, _ = world

    def no_poppler(*a, **k):
        raise AssertionError("page text comes from the cache")
    monkeypatch.setattr(inv, "_run", no_poppler)
    assert rows_of(ms.run(adir))["QRS7TUVTR"]["proposals"]


def test_a_second_read_uses_the_stored_matches(world, monkeypatch):
    adir, _ = world
    ms.analyse(adir)

    def no_text(*a, **k):
        raise AssertionError("the matches are stored")
    monkeypatch.setattr(inv, "document_pages", no_text)
    assert coverage.load(adir).status("QRS7TUVTR")["proposals"]
    ask(adir, "--confirm", "QRS7TUVTR")                         # an answer never rescans


def test_stored_matches_follow_the_documents_and_the_bom(world):
    adir, src = world
    assert rows_of(ms.analyse(adir))["NODOC9X1"]["proposals"] == []
    make_pdf(src / "NODOC9X.pdf", [f"NODOC9X\nFeatures\n{FILLER}"])
    inv.run(adir)
    assert [p["file"] for p in rows_of(ms.analyse(adir))["NODOC9X1"]["proposals"]] == ["NODOC9X.pdf"]
    bom = read_json(adir / "boards" / "M" / "bom.json")
    next(e for e in bom if e["ref"] == "U9")["datasheet_link"] = "https://vendor.example/klm.pdf"
    write_json(adir / "boards" / "M" / "bom.json", bom)
    assert rows_of(ms.analyse(adir))["NODOC9X1"]["covered_by"] == ["klm.pdf"]


def test_inventory_stays_byte_identical_with_matching_in_use(world):
    adir, _ = world
    ms.run(adir)
    ask(adir, "--confirm", "QRS7TUVTR")
    mj, mm = adir / "inventory" / "manifest.json", adir / "inventory" / "manifest.md"
    first = (mj.read_bytes(), mm.read_bytes())
    inv.run(adir)
    assert (mj.read_bytes(), mm.read_bytes()) == first
    md = (adir / "inventory" / "missing.md").read_bytes()
    ms.run(adir)
    assert (adir / "inventory" / "missing.md").read_bytes() == md


def test_a_malformed_answers_file_is_said_everywhere(world, capsys):
    adir, _ = world
    answers.path_of(adir).write_text('[parts."X"]\nanswer = "document"\n')
    assert ask(adir) == 2
    assert "`document` needs a `file`" in capsys.readouterr().err
    assert ask(adir, "--confirm-all") == 2
    assert nettrace.load(adir, None, read_json).answers_error    # the lookups go on without it, and say so


def _prep_into(adir: Path, src: Path, name: str) -> Path:
    from embarch_atlas import pdftext
    pdftext.prep(src / name, adir, log=lambda *_: None)
    return next((adir / "docs").glob("*/doc.json"))


def test_a_kind_override_reaches_a_prepped_documents_doc_json(tmp_path, capsys):
    adir, src = project_with(tmp_path, {"U2": part("ZX123456")})
    make_pdf(src / "guide.pdf", [f"User guide\nUsing the ZX123456\n{FILLER}"])
    inv.run(adir)
    dj = _prep_into(adir, src, "guide.pdf")
    assert read_json(dj)["kind"] == "appnote"
    capsys.readouterr()
    assert ask(adir, "--kind", "guide.pdf", "datasheet") == 0
    assert read_json(dj)["kind"] == "datasheet"
    assert "kind appnote -> datasheet" in capsys.readouterr().out
    man = read_json(adir / "inventory" / "manifest.json")
    assert [d["kind"] for d in man["documents"]] == ["datasheet"]


def test_a_kind_override_on_a_document_not_prepped_yet_leaves_no_doc_json(tmp_path, capsys):
    adir, src = project_with(tmp_path, {"U2": part("ZX123456")})
    make_pdf(src / "guide.pdf", [f"User guide\nUsing the ZX123456\n{FILLER}"])
    inv.run(adir)
    assert ask(adir, "--kind", "guide.pdf", "datasheet") == 0
    assert not (adir / "docs").exists() or not list((adir / "docs").glob("*/doc.json"))
    from embarch_atlas import pdftext
    pdftext.prep(src / "guide.pdf", adir, log=lambda *_: None)
    assert read_json(next((adir / "docs").glob("*/doc.json")))["kind"] == "datasheet"


def test_confirming_the_reference_manual_reaches_a_prepped_doc_json(tmp_path):
    adir, src = project_with(tmp_path, {"U2": part("ZX123456")})
    make_pdf(src / "rm.pdf", [f"Reference manual\n{FILLER}"])
    inv.run(adir)
    dj = _prep_into(adir, src, "rm.pdf")
    assert ask(adir, "--confirm", ms.RM_TOKEN, "--file", "rm.pdf") == 0
    assert read_json(dj)["kind"] == "reference-manual"


# ---------------------------------------------------------------- a part named in passing

def _passing(tmp_path):
    adir, src = project_with(tmp_path, {"U2": part("ALPHA12X"), "U3": part("BRAVO34Y")})
    make_pdf(src / "alpha.pdf", [f"ALPHA12X load switch\nFeatures\n{FILLER}", f"Compare with the BRAVO34Y\n{FILLER}"])
    inv.run(adir)
    return adir


def test_a_part_named_on_page_2_of_another_datasheet_is_not_covered(tmp_path):
    rows = rows_of(ms.analyse(_passing(tmp_path)))
    assert rows["ALPHA12X"]["covered_by"] == ["alpha.pdf"]
    assert rows["BRAVO34Y"]["covered_by"] == []
    assert [(p["file"], p["rung"]) for p in rows["BRAVO34Y"]["proposals"]] == [("alpha.pdf", "exact")]


def test_a_part_named_on_page_2_is_not_a_part_token_of_the_document(tmp_path):
    adir = _passing(tmp_path)
    doc = read_json(adir / "inventory" / "manifest.json")["documents"][0]
    assert "ALPHA12X" in doc["parts"] and "BRAVO34Y" not in doc["parts"]


def test_a_part_in_the_title_or_on_page_1_still_covers(tmp_path):
    adir, src = project_with(tmp_path, {"U2": part("ALPHA12X"), "U3": part("BRAVO34Y")})
    make_pdf(src / "a.pdf", [f"Features\n{FILLER}"], {"title": "ALPHA12X load switch"})
    make_pdf(src / "b.pdf", [f"BRAVO34Y buffer\nFeatures\n{FILLER}"])
    inv.run(adir)
    rows = rows_of(ms.analyse(adir))
    assert rows["ALPHA12X"]["covered_by"] == ["a.pdf"] and rows["BRAVO34Y"]["covered_by"] == ["b.pdf"]

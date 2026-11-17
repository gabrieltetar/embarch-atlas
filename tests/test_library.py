"""The shared datasheet library and the copy of every file an atlas read (DESIGN.md "Where documents come from").

Every part and document is synthetic, built with PyMuPDF in the test.
"""
from __future__ import annotations

import os
import shutil
import socket
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_inventory import FILLER, make_pdf  # noqa: E402
from test_matching import ask, part, project_with, rows_of  # noqa: E402

from embarch_atlas import cli, common, inventory as inv, library, missing as ms, pdftext, secondlook  # noqa: E402
from embarch_atlas.common import read_json  # noqa: E402

pytestmark = pytest.mark.skipif(not all(shutil.which(t) for t in ("pdfinfo", "pdftotext")),
                                reason="poppler CLI tools not installed")


def with_library(tmp_path: Path, parts: dict, libname: str = "lib") -> tuple[Path, Path, Path]:
    adir, src = project_with(tmp_path, parts)
    lib = tmp_path / libname
    lib.mkdir()
    toml = adir / "atlas.toml"
    toml.write_text(toml.read_text().replace('sources = "hw"', f'sources = "hw"\nlibrary = "{lib}"'))
    return adir, src, lib


def manifest(adir: Path) -> dict:
    return read_json(adir / "inventory" / "manifest.json")


def snapshot(folder: Path) -> list:
    return sorted((str(p.relative_to(folder)), p.stat().st_size, p.stat().st_mtime_ns) for p in folder.rglob("*"))


def test_unset_library_writes_what_it_wrote_before(tmp_path):
    adir, src = project_with(tmp_path, {"U2": part("KM7RP2AC")})
    make_pdf(src / "switch-a.pdf", [f"Features\nOrder as KM7RP2AC\n{FILLER}"])
    inv.run(adir)
    assert "library" not in manifest(adir) and not any("library" in d for d in manifest(adir)["documents"])
    assert not (adir / "inventory" / library.INDEX).exists() and not (adir / "files").exists()
    assert rows_of(ms.analyse(adir))["KM7RP2AC"]["covered_by"] == ["switch-a.pdf"]


def test_a_library_only_datasheet_covers_a_board_part(tmp_path):
    adir, src, lib = with_library(tmp_path, {"U2": part("KM7RP2AC")})
    (lib / "vendor").mkdir()
    make_pdf(lib / "vendor" / "switch-a.pdf", [f"Features\nOrder as KM7RP2AC\n{FILLER}"])
    inv.run(adir)
    assert ask(adir) == 0
    man = manifest(adir)
    (d,) = man["documents"]
    assert d["file"] == "vendor/switch-a.pdf" and d["library"] is True and man["library"] == str(lib)
    R = rows_of(ms.analyse(adir))
    assert R["KM7RP2AC"]["covered_by"] == ["vendor/switch-a.pdf"]


def test_a_library_file_no_part_is_tied_to_stays_out(tmp_path):
    adir, src, lib = with_library(tmp_path, {"U2": part("KM7RP2AC")})
    make_pdf(lib / "switch-a.pdf", [f"Features\nOrder as KM7RP2AC\n{FILLER}"])
    make_pdf(lib / "other.pdf", [f"OTHER99 regulator\nFeatures\n{FILLER}"])
    inv.run(adir)
    assert ask(adir) == 0
    assert [d["file"] for d in manifest(adir)["documents"]] == ["switch-a.pdf"]
    assert ms.analyse(adir)["unused"] == []
    assert "other.pdf" not in (adir / "inventory" / "missing.md").read_text()


def test_the_mcu_role_ties_a_library_manual(tmp_path):
    adir, src, lib = with_library(tmp_path, {"U2": part("KM7RP2AC")})
    make_pdf(lib / "mcu123-errata.pdf", [f"MCU123 errata sheet\nErrata\n{FILLER}"], {"title": "MCU123 errata"})
    inv.run(adir)
    assert ask(adir) == 0
    assert [d["file"] for d in manifest(adir)["documents"]] == ["mcu123-errata.pdf"]


def test_the_sources_folder_wins_an_identical_file(tmp_path):
    adir, src, lib = with_library(tmp_path, {"U2": part("KM7RP2AC")})
    make_pdf(src / "switch-a.pdf", [f"Features\nOrder as KM7RP2AC\n{FILLER}"])
    shutil.copyfile(src / "switch-a.pdf", lib / "switch-a.pdf")
    inv.run(adir)
    a, b = manifest(adir)["documents"]
    assert "library" not in a and b["library"] is True and b["duplicate_of"] == "switch-a.pdf"
    assert b["file"] == "library/switch-a.pdf" and b["library_path"] == "switch-a.pdf"
    assert rows_of(ms.analyse(adir))["KM7RP2AC"]["covered_by"] == ["switch-a.pdf"]


def test_a_name_in_both_folders_with_different_content_is_kept_apart(tmp_path):
    adir, src, lib = with_library(tmp_path, {"U2": part("KM7RP2AC"), "U3": part("LN4SQ8BD")})
    make_pdf(src / "switch.pdf", [f"Features\nOrder as KM7RP2AC\n{FILLER}"])
    make_pdf(lib / "switch.pdf", [f"Features\nOrder as LN4SQ8BD\n{FILLER}"])
    inv.run(adir)
    assert ask(adir) == 0
    R = rows_of(ms.analyse(adir))
    assert R["KM7RP2AC"]["covered_by"] == ["switch.pdf"] and R["LN4SQ8BD"]["covered_by"] == ["library/switch.pdf"]
    man = manifest(adir)
    doc = next(d for d in man["documents"] if d.get("library"))
    assert inv.doc_path(adir, doc, man) == lib / "switch.pdf"


def test_the_index_keeps_a_large_library_from_being_read_again(tmp_path, monkeypatch):
    adir, src, lib = with_library(tmp_path, {"U2": part("KM7RP2AC")})
    make_pdf(lib / "switch-a.pdf", [f"Features\nOrder as KM7RP2AC\n{FILLER}"])
    make_pdf(lib / "other.pdf", [f"OTHER99 regulator\nFeatures\n{FILLER}"])
    inv.run(adir)
    assert ask(adir) == 0
    first = (adir / "inventory" / "manifest.json").read_bytes()
    calls = []
    real = inv.inventory_file
    monkeypatch.setattr(inv, "inventory_file", lambda *a, **k: calls.append(a[1]) or real(*a, **k))
    hashed = []
    real_sha = common.sha256_file
    monkeypatch.setattr(common, "sha256_file", lambda p, *a, **k: hashed.append(Path(p).name) or real_sha(p, *a, **k))
    inv.run(adir)
    assert calls == [] and "other.pdf" not in hashed and "switch-a.pdf" not in hashed
    assert (adir / "inventory" / "manifest.json").read_bytes() == first


def test_prep_copies_the_file_and_every_later_step_reads_the_copy(tmp_path):
    adir, src, lib = with_library(tmp_path, {"U2": part("KM7RP2AC")})
    make_pdf(lib / "switch-a.pdf", [f"Features\nOrder as KM7RP2AC\n{FILLER}"])
    inv.run(adir)
    assert ask(adir) == 0
    docdir = pdftext.prep(lib / "switch-a.pdf", adir, log=lambda *_: None)
    meta = read_json(docdir / "doc.json")
    assert meta["path"] == f"files/{meta['sha256']}/switch-a.pdf"
    assert (adir / meta["path"]).is_file()
    (lib / "switch-a.pdf").unlink()                                  # deleted ...
    assert pdftext.resolve_pdf_path(docdir, meta) == (adir / meta["path"]).resolve()
    assert secondlook._pdf(adir, meta) == str((adir / meta["path"]).resolve())
    make_pdf(lib / "switch-a.pdf", ["replaced"])                     # ... or replaced: the copy is the bytes prepped
    assert common.sha256_file(pdftext.resolve_pdf_path(docdir, meta)) == meta["sha256"]
    assert cli.main(["--atlas-dir", str(adir), "doc", "check", meta["doc_id"]]) in (0, 1)


def test_a_document_prepped_before_the_copy_is_copied_at_the_next_inventory(tmp_path):
    adir, src = project_with(tmp_path, {"U2": part("KM7RP2AC")})
    make_pdf(src / "switch-a.pdf", [f"Features\nOrder as KM7RP2AC\n{FILLER}"])
    inv.run(adir)
    docdir = pdftext.prep(src / "switch-a.pdf", adir, log=lambda *_: None)
    old = read_json(docdir / "doc.json")
    shutil.rmtree(adir / "files")
    common.write_json(docdir / "doc.json", {**old, "path": "../hw/switch-a.pdf"})
    inv.run(adir)
    assert read_json(docdir / "doc.json")["path"] == old["path"] and (adir / old["path"]).is_file()
    # a file that has changed since is not copied under the old sha256
    shutil.rmtree(adir / "files")
    common.write_json(docdir / "doc.json", {**old, "path": "../hw/switch-a.pdf"})
    make_pdf(src / "switch-a.pdf", ["changed"])
    inv.run(adir)
    assert read_json(docdir / "doc.json")["path"] == "../hw/switch-a.pdf" and not (adir / "files").exists()


def test_a_changed_library_file_shows_in_the_diff_and_a_confirmation_goes_back_to_proposed(tmp_path):
    adir, src, lib = with_library(tmp_path, {"U2": part("QRS7TUVTR")})
    make_pdf(lib / "QRS7TUV.pdf", [f"QRS7TUV load switch\nFeatures\n{FILLER}"])
    inv.run(adir)
    assert ask(adir) == 0
    assert rows_of(ms.analyse(adir))["QRS7TUVTR"]["proposals"][0]["file"] == "QRS7TUV.pdf"
    assert ask(adir, "--confirm", "QRS7TUVTR", "--file", "QRS7TUV.pdf") == 0
    assert rows_of(ms.analyse(adir))["QRS7TUVTR"]["covered_by"] == ["QRS7TUV.pdf"]
    make_pdf(lib / "QRS7TUV.pdf", [f"QRS7TUV load switch, revision B\nFeatures\n{FILLER}"])
    inv.run(adir)
    assert manifest(adir)["diff"]["changed"] == ["QRS7TUV.pdf"]
    row = rows_of(ms.analyse(adir))["QRS7TUVTR"]
    assert row["covered_by"] == [] and "the file changed since" in row["proposals"][0]["evidence"]


def test_a_read_only_library_and_no_network_nothing_fails_or_writes(tmp_path, monkeypatch):
    adir, src, lib = with_library(tmp_path, {"U2": part("KM7RP2AC")})
    (lib / "sub").mkdir()
    make_pdf(lib / "sub" / "switch-a.pdf", [f"Features\nOrder as KM7RP2AC\n{FILLER}"])
    make_pdf(lib / "other.pdf", [f"OTHER99 regulator\nFeatures\n{FILLER}"])
    before = snapshot(lib)
    for p in sorted(lib.rglob("*"), reverse=True):
        p.chmod(stat.S_IRUSR | stat.S_IXUSR if p.is_dir() else stat.S_IRUSR)
    lib.chmod(stat.S_IRUSR | stat.S_IXUSR)

    def no_network(*a, **k):
        raise AssertionError("a network call")
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    try:
        inv.run(adir)
        assert ask(adir) == 0
        docdir = pdftext.prep(lib / "sub" / "switch-a.pdf", adir, log=lambda *_: None)
        assert (docdir / "doc.json").is_file()
        assert snapshot(lib) == before
    finally:
        for p in [lib, *lib.rglob("*")]:
            p.chmod(stat.S_IRWXU if p.is_dir() else stat.S_IRUSR | stat.S_IWUSR)
    assert os.access(lib, os.W_OK)


def test_a_library_dir_that_is_not_there_is_said(tmp_path, capsys):
    adir, src, lib = with_library(tmp_path, {"U2": part("KM7RP2AC")})
    lib.rmdir()
    assert cli.main(["--atlas-dir", str(adir), "inventory"]) == 2
    assert "library dir" in capsys.readouterr().err

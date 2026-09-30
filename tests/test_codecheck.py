"""codecheck: register constants in driver sources vs the datasheet register map.

Everything is synthetic: an invented buck-boost part (doc `demo-bb@A`, part `DEMOBB1`,
compatible `acme,demobb1`) with six registers, and a driver whose header carries one
of each thing the check must catch: an address with no register, a mask narrower than
its field, a mask on a reserved bit, a field defined under the wrong register, a mask
applied to the wrong register at a call site, a reset constant that disagrees, and a
comment that says register bits come from a pin at power-on.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from embarch_atlas import codecheck as cc  # noqa: E402
from embarch_atlas.common import read_json, write_json, write_jsonl  # noqa: E402

DOC = "demo-bb@A"

REGS_H = """\
/* Register map of the demo buck-boost part (invented for tests). */
#pragma once

#include <zephyr/sys/util.h>

#define DEMOBB_REG_VREF_LO   0x00
#define DEMOBB_REG_VREF_HI   0x01
#define DEMOBB_REG_ILIM      0x02
#define DEMOBB_REG_CTRL1     0x05
#define DEMOBB_REG_CTRL2     0x06
#define DEMOBB_REG_STATUS    0x07   /* reset 0x00 */
#define DEMOBB_REG_SPARE     0x0C
#define DEMOBB_REG_LAST      DEMOBB_REG_STATUS

/* VREF (00h/01h) - a 10-bit word, low byte first */
#define DEMOBB_VREF_MASK     GENMASK(9, 0)
#define DEMOBB_VREF_STEP_MV  20U

/* ILIM (02h) [reset = 0xE4] - bit 7 enables the limit */
#define DEMOBB_LIM_EN        BIT(7)
#define DEMOBB_LIM_SET_MASK  GENMASK(6, 0)
#define DEMOBB_LIM_SET_MAX   0x7FU

/* CTRL1 (05h) - the other bits are latched from the CFG pin at power-on,
 * so every write is read-modify-write. Bits 6:4 FREQ.
 */
#define DEMOBB_CTRL1_OE         BIT(7)
#define DEMOBB_CTRL1_FREQ_MASK  GENMASK(5, 4)
#define DEMOBB_CTRL1_FREQ_FAST  BIT(5)
#define DEMOBB_CTRL1_HIZ        BIT(3)
#define DEMOBB_CTRL1_RESET_VAL  0x21

/* CTRL2 (06h) - bits 6:5 DEAD */
#define DEMOBB_CTRL2_SLEW_SHIFT 1
#define DEMOBB_CTRL2_SLEW_MASK  (0x3u << DEMOBB_CTRL2_SLEW_SHIFT)
#define DEMOBB_CTRL2_SLEW_FAST  FIELD_PREP(DEMOBB_CTRL2_SLEW_MASK, 3)
#define DEMOBB_CTRL2_SLEW_SLOW  FIELD_PREP(DEMOBB_CTRL2_SLEW_MASK, 1) /* (POR default) */
#define DEMOBB_CTRL2_TEST_BIT   BIT(0)
#define DEMOBB_CTRL2_DEAD_MASK  GENMASK(6, 5)  /* default 0x2 */

#define DEMOBB_STATUS_FLT_MASK  ((uint8_t)(DEMOBB_BITS(2) << 6))
#define DEMOBB_BITS(n)          ((1u << (n)) - 1)
#define DEMOBB_TIMEOUT          K_MSEC(5)
#define DEMOBB_NAME             "demo"
"""

DRIVER_C = """\
#define DT_DRV_COMPAT acme_demobb1

#include "demo_bb_regs.h"

static int reg_update(const struct device *dev, uint8_t reg, uint8_t mask, uint8_t val)
{
	uint8_t old;
	int ret = reg_read(dev, reg, &old);

	LOG_DBG("reg %02x", DEMOBB_REG_CTRL1);
	return ret ? ret : reg_write(dev, reg, (old & ~mask) | (val & mask));
}

int demobb_enable(const struct device *dev, bool on)
{
	return reg_update(dev, DEMOBB_REG_CTRL1, DEMOBB_CTRL1_OE, on ? DEMOBB_CTRL1_OE : 0);
}

int demobb_slew(const struct device *dev)
{
	/* the slew field lives in CTRL2, not CTRL1 */
	return reg_update(dev, DEMOBB_REG_CTRL1, DEMOBB_CTRL2_SLEW_MASK, DEMOBB_CTRL2_SLEW_FAST);
}

int demobb_dump(const struct device *dev, uint8_t *out)
{
	for (uint8_t i = 0; i < 8; i++) {
		reg_read(dev, i, &out[i]);
	}
	WRITE_BIT(out[0], 7, true);
	return 0;
}
"""


def _reg(name, addr, reset, p, prov="llm-verified"):
    return {"id": f"demobb.reg.{name}", "kind": "register", "doc": DOC, "p": p, "q": f"{name} {addr:02X}h", "src": "table",
            "prov": prov, "name": name, "addr": f"0x{addr:02X}", "width": 8, "reset": reset, "access": "rw"}


def _fld(reg, name, hi, lo, reset, p, prov="llm-verified"):
    return {"id": f"demobb.reg.{reg}.{name}", "kind": "field", "doc": DOC, "p": p, "q": f"{hi}-{lo} {name}", "src": "table",
            "prov": prov, "reg": f"demobb.reg.{reg}", "name": name, "bits": [hi, lo], "reset": reset, "access": "rw"}


def demo_records() -> tuple[list[dict], list[dict]]:
    regs = [_reg("VREF_LO", 0x00, "0b11010010", 20), _reg("VREF_HI", 0x01, "0b00000000", 20),
            _reg("ILIM", 0x02, "0b11100100", 21), _reg("CTRL1", 0x05, "0b00100000", 22),
            _reg("CTRL2", 0x06, "0b01000010", 23), _reg("STATUS", 0x07, "0b00000000", 24)]
    fields = [_fld("VREF_LO", "VREF_7_0", 7, 0, "0b11010010", 20), _fld("VREF_HI", "RESERVED_7_2", 7, 2, "0b000000", 20),
              _fld("VREF_HI", "VREF_9_8", 1, 0, "0b00", 20),
              _fld("ILIM", "LIM_EN", 7, 7, "0b1", 21), _fld("ILIM", "LIM_SET", 6, 0, "0b1100100", 21),
              _fld("CTRL1", "OE", 7, 7, "0b0", 22), _fld("CTRL1", "FREQ", 6, 4, "0b010", 22),
              _fld("CTRL1", "BOOST", 3, 3, "0b0", 22), _fld("CTRL1", "RESERVED_2_0", 2, 0, "0b000", 22),
              _fld("CTRL2", "RESERVED_7", 7, 7, "0b0", 23), _fld("CTRL2", "DEAD", 6, 5, "0b10", 23),
              _fld("CTRL2", "RESERVED_4", 4, 4, "0b0", 23), _fld("CTRL2", "HIZ", 3, 3, "0b0", 23),
              _fld("CTRL2", "SLEW", 2, 1, "0b01", 23), _fld("CTRL2", "RESERVED_0", 0, 0, "0b0", 23),
              _fld("STATUS", "FLT", 7, 6, "0b00", 24), _fld("STATUS", "RESERVED_5_0", 5, 0, "0b000000", 24)]
    return regs, fields


def make_atlas(tmp: Path, regs_h: str = REGS_H, driver_c: str = DRIVER_C) -> tuple[Path, Path]:
    """(atlas_dir, atlas): a fw tree with the driver, the doc's records, a stamp and a dt.json."""
    fw = tmp / "fw"
    (fw / "drivers" / "demobb").mkdir(parents=True)
    (fw / "drivers" / "demobb" / "demo_bb_regs.h").write_text(regs_h, encoding="utf-8")
    (fw / "drivers" / "demobb" / "demo_bb.c").write_text(driver_c, encoding="utf-8")
    ad = fw / "embarch" / "atlas"
    d = ad / "docs" / DOC
    write_json(d / "doc.json", {"doc_id": DOC, "kind": "datasheet", "part": "DEMOBB1", "file": "demobb.pdf", "pages": 40})
    regs, fields = demo_records()
    write_jsonl(d / "records" / "register.jsonl", regs)
    write_jsonl(d / "records" / "field.jsonl", fields)
    # a deterministic twin of CTRL2 and its fields: merged, not double counted
    write_jsonl(d / "records" / "regparse.jsonl", [dict(r, prov="deterministic+crosschecked") for r in regs[4:5]]
                + [dict(f, prov="deterministic+crosschecked") for f in fields if f["reg"].endswith("CTRL2")])
    atlas = ad / "atlases" / "demo@000000000001"
    write_json(atlas / "atlas.json", {"id": "demo@000000000001", "target": "demo", "board": "demo_board",
                                      "commit": "0" * 40, "build": {"fw": str(fw), "worktree": "../.."}})
    write_json(atlas / "dt.json", {
        "nodes": {"/soc/i2c@40005400": {"compats": ["vendor,soc-i2c"], "okay": True, "labels": ["i2c1"]},
                  "/soc/i2c@40005400/bb@74": {"compats": ["acme,demobb1"], "okay": True, "labels": ["bb0"]},
                  "/soc/i2c@40005400/bb@75": {"compats": ["acme,demobb1"], "okay": True, "labels": ["bb1"]},
                  "/soc/i2c@40005400/off@76": {"compats": ["acme,demobb1"], "okay": False, "labels": []}},
        "drivers": {"acme_demobb1": ["fw:drivers/demobb/demo_bb.c"], "vendor_soc_i2c": ["zephyr:drivers/i2c/soc_i2c.c"]},
        "drivers_not_built": {}})
    return ad, atlas


@pytest.fixture
def rep(tmp_path):
    ad, atlas = make_atlas(tmp_path)
    return cc.run(ad, atlas, log=lambda *a: None), atlas


def _by(rep, cls):
    return [f for f in rep["findings"] if f["class"] == cls]


def _row(rows, key, value):
    return next(r for r in rows if r[key] == value)


# ---------------------------------------------------------------- the evaluator

@pytest.mark.parametrize("expr,want", [
    ("0x1F", 0x1F), ("0b101", 5), ("010", 8), ("42U", 42), ("0x80UL", 0x80),
    ("BIT(7)", 0x80), ("GENMASK(6, 0)", 0x7F), ("GENMASK(15, 12)", 0xF000), ("(1u << 3)", 8),
    ("BIT(3) | BIT(1)", 0xA), ("FIELD_PREP(GENMASK(2, 1), 3)", 6), ("FIELD_GET(0x30, 0x20)", 2),
    ("(uint8_t)(0x1234)", 0x34), ("~BIT(0) & 0xFF", 0xFE), ("(0x3u << 4) >> 2", 0xC), ("1 ? 5 : 6", 5),
    ("-7 / 2", -3), ("7 % 3", 1), ("(A + 1) * 2", 8), ("TWICE(A)", 6),
])
def test_evaluator_values(expr, want):
    ev = cc.Evaluator({"A": "3"}, {"TWICE": (["x"], "((x) * 2)")})
    assert ev.eval(expr) == want


@pytest.mark.parametrize("expr,why", [
    ("K_MSEC(5)", "unknown macro"), ("NOPE", "unknown identifier"), ('"   "', "not an integer"),
    ("sizeof(int)", "sizeof"), ("BIT(70)", "out of range"), ("1 / 0", "division by zero"), ("1.5", "not an integer"),
])
def test_evaluator_unresolved(expr, why):
    with pytest.raises(cc.Unresolved, match=why):
        cc.Evaluator({}, {}).eval(expr)


def test_evaluator_cycle_is_unresolved():
    ev = cc.Evaluator({"A": "B + 1", "B": "A"}, {})
    with pytest.raises(cc.Unresolved, match="itself"):
        ev.value("A")


def test_helpers():
    assert cc.runs(0b1110_0011) == [[7, 5], [1, 0]]
    assert cc.bits_str(0x80) == "7" and cc.bits_str(0x70) == "6:4"
    assert cc.reg_refs("CTRL1 (05h) - text") == [("CTRL1", [5])]
    assert cc.reg_refs("ChargeCfg0 (01/00h)") == [("ChargeCfg0", [1, 0])]
    assert cc.reg_refs("High byte (0x01)") == []            # a common word is not a register name
    assert cc.reg_refs("see note (each)") == []
    assert cc.bit_claims("Bits 6:4 FREQ, and Bit 3, BOOST.")[:2] == [("FREQ", 6, 4, "Bits 6:4 FREQ"), ("BOOST", 3, 3, "Bit 3, BOOST")]
    assert cc.pin_set_sentences("bits are latched from the CFG pin at power-on. Other text.")
    assert not cc.pin_set_sentences("the INT pin is open-drain")
    assert cc.claim_number("11b") == 3 and cc.claim_number("0Ah") == 10 and cc.claim_number("0xE4") == 0xE4
    assert cc.reg_short("REG_CTRL1") == "CTRL1" and cc.reg_short("CTRL1_REG") == "CTRL1"


def test_strip_c_keeps_offsets_and_lines():
    text = 'int a; /* one\n two */ char *s = "x, y"; // tail\n#define X 1 /* t */\n'
    code, comments = cc.strip_c(text)
    assert len(code) == len(text) and code.count("\n") == text.count("\n")
    assert '"x, y"' not in code and "one" not in code
    assert [(c.start, c.end, c.standalone) for c in comments] == [(1, 2, False), (2, 2, False), (3, 3, False)]


# ---------------------------------------------------------------- the analysis

def test_discovery_and_coverage(rep):
    r, atlas = rep
    [u] = r["units"]
    assert u["doc"] == DOC and u["compat"] == "acme,demobb1" and u["doc_via"] == "compatible acme,demobb1"
    assert u["nodes"] == ["/soc/i2c@40005400/bb@74", "/soc/i2c@40005400/bb@75"]      # the disabled node is not used
    assert u["files"] == ["fw:drivers/demobb/demo_bb.c", "fw:drivers/demobb/demo_bb_regs.h"]
    mc = u["macros"]
    assert mc["function_like"] == 1
    unresolved = {x["name"]: x["why"] for x in mc["unresolved"]}
    assert set(unresolved) == {"DEMOBB_TIMEOUT", "DEMOBB_NAME"}
    assert "K_MSEC" in unresolved["DEMOBB_TIMEOUT"]
    assert mc["resolved"] == mc["found"] - 2
    assert mc["by_kind"]["register"] == 7 and mc["by_kind"]["alias"] == 1 and mc["by_kind"]["reset"] == 1
    assert u["registers_matched"] == 6 and u["register_macros"] == 7
    assert u["datasheet"] == {"registers": 6, "fields": 17}                             # the CTRL2 twin is merged
    assert (atlas / "codecheck.json").is_file() and (atlas / "codecheck.md").is_file()
    assert read_json(atlas / "codecheck.json")["summary"] == r["summary"]


def test_masks_join_by_address_and_bits(rep):
    r, _ = rep
    masks = r["units"][0]["masks"]
    vref = _row(masks, "macro", "DEMOBB_VREF_MASK")
    assert vref["status"] == "match" and vref["order"] == "le"                         # a word over two byte registers
    assert vref["fields"] == ["demobb.reg.VREF_LO.VREF_7_0", "demobb.reg.VREF_HI.VREF_9_8"]
    lim = _row(masks, "macro", "DEMOBB_LIM_EN")                                         # tied by the section comment
    assert lim["status"] == "match" and lim["register"] == "0x02" and lim["how"].startswith("comment ILIM")
    assert _row(masks, "macro", "DEMOBB_CTRL1_OE")["how"] == "prefix DEMOBB_REG_CTRL1"
    assert _row(masks, "macro", "DEMOBB_CTRL2_SLEW_MASK")["fields"] == ["demobb.reg.CTRL2.SLEW"]
    assert _row(masks, "macro", "DEMOBB_CTRL2_SLEW_SLOW")["status"] == "value of SLEW"
    assert _row(masks, "macro", "DEMOBB_CTRL1_FREQ_FAST")["status"] == "value of FREQ"
    assert _row(masks, "macro", "DEMOBB_STATUS_FLT_MASK")["fields"] == ["demobb.reg.STATUS.FLT"]   # cast + local macro
    t = r["totals"]
    assert t["fields_matched"] >= 9 and t["registers_matched"] == 6


def test_findings(rep):
    r, _ = rep
    unknown = _by(r, "reg-address-unknown")
    assert [f["key"] for f in unknown] == ["DEMOBB_REG_SPARE"]
    narrow = [f for f in _by(r, "reg-mask-vs-field") if f["key"] == "DEMOBB_CTRL1_FREQ_MASK"]
    assert narrow and "not the whole field" in narrow[0]["summary"] and "FREQ[6:4]" in narrow[0]["summary"]
    reserved = [f for f in _by(r, "reg-mask-vs-field") if f["key"] == "DEMOBB_CTRL2_TEST_BIT"]
    assert reserved and "reserved bits RESERVED_0[0:0]" in reserved[0]["summary"]
    wrong = {f["key"]: f for f in _by(r, "reg-field-in-other-register")}
    hiz = wrong["DEMOBB_CTRL1_HIZ"]
    assert "CTRL2 (0x06) bits 3:3 (the same bits)" in hiz["summary"] and "BOOST[3:3]" in hiz["summary"]
    assert any("demobb.reg.CTRL2.HIZ" in e and f"{DOC} p23" in e for e in hiz["evidence"])
    site = [f for k, f in wrong.items() if k.startswith("DEMOBB_CTRL2_SLEW_MASK@")]
    assert site and "applies DEMOBB_CTRL2_SLEW_MASK" in site[0]["summary"] and "to DEMOBB_REG_CTRL1 (0x05)" in site[0]["summary"]
    assert site[0]["key"].endswith("demo_bb.c:22")
    reset = _by(r, "reg-reset-vs-datasheet")
    assert [f["key"] for f in reset] == ["DEMOBB_CTRL1_RESET_VAL"]
    assert "0x21" in reset[0]["summary"] and "0x20" in reset[0]["summary"]
    assert any(e.startswith("fw:drivers/demobb/demo_bb_regs.h:") for e in reset[0]["evidence"])
    pin = [f for f in _by(r, "reg-comment-vs-datasheet") if f["key"] == "CTRL1.pin-set-reset"]
    assert pin and pin[0]["severity"] == "info" and "0b00100000" in pin[0]["summary"]
    for f in r["findings"]:
        assert f["evidence"] and f["settle"], f
        assert any(":" in e and e.startswith("fw:") for e in f["evidence"]), f      # every finding cites file:line


def test_passes_are_counted_not_listed(rep):
    r, _ = rep
    checks = {c["check"]: c for c in r["checks"]}
    assert checks[cc.CHECKS["addr"]]["ran"] == 7 and checks[cc.CHECKS["addr"]]["passed"] == 6
    resets = r["units"][0]["resets"]
    assert _row(resets, "what", "ILIM")["status"] == "match"                          # [reset = 0xE4]
    assert _row(resets, "what", "DEMOBB_CTRL2_SLEW_SLOW")["status"] == "match"        # (POR default) -> SLEW = 0b01
    assert _row(resets, "what", "DEMOBB_CTRL2_DEAD_MASK")["status"] == "match"        # default 0x2 -> DEAD = 0b10
    assert _row(resets, "what", "DEMOBB_REG_STATUS")["status"] == "match"
    comments = r["units"][0]["comments"]
    assert any(c["claim"].startswith("Bits 6:4 FREQ") and c["status"] == "match" for c in comments)
    assert any(c["claim"].startswith("bits 6:5 DEAD") and c["status"] == "match" for c in comments)
    sites = r["units"][0]["sites"]
    assert sites["named"] == 2 and sites["inside_wrappers"] == 2                     # reg_read/reg_write inside reg_update()
    assert [x.split(" ")[1] for x in sites["unresolved"]] == ["reg_read()"]          # a loop variable; WRITE_BIT is not an access
    assert checks[cc.CHECKS["site"]]["ran"] >= 3 and checks[cc.CHECKS["site"]]["passed"] >= 1


def test_no_register_records_is_not_run(tmp_path):
    ad, atlas = make_atlas(tmp_path)
    for f in (ad / "docs" / DOC / "records").glob("*.jsonl"):
        f.unlink()
    r = cc.run(ad, atlas, log=lambda *a: None)
    assert r["units"] == []
    assert any("acme,demobb1" in n["check"] and "no register records" in n["why"] for n in r["not_run"])


def test_disagreeing_prefix_and_comment_are_recorded(tmp_path):
    h = REGS_H.replace("/* CTRL2 (06h) - bits 6:5 DEAD */", "/* ILIM (02h) - misplaced heading */")
    ad, atlas = make_atlas(tmp_path, regs_h=h)
    r = cc.run(ad, atlas, log=lambda *a: None)
    row = _row(r["units"][0]["masks"], "macro", "DEMOBB_CTRL2_DEAD_MASK")
    assert row["how"] == "prefix DEMOBB_REG_CTRL2" and "comment above names ILIM" in row["note"]
    assert row["status"] == "match"


def test_reads_the_commit_when_the_worktree_is_gone(tmp_path):
    import subprocess
    ad, atlas = make_atlas(tmp_path)
    fw = tmp_path / "fw"
    run = lambda *a: subprocess.run(["git", "-C", str(fw), *a], capture_output=True, text=True, check=True).stdout.strip()  # noqa: E731
    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    run("add", "drivers")
    run("commit", "-q", "-m", "drivers")
    stamp = read_json(atlas / "atlas.json")
    stamp["commit"] = run("rev-parse", "HEAD")
    stamp["build"]["worktree"] = "../gone"
    write_json(atlas / "atlas.json", stamp)
    (fw / "drivers" / "demobb" / "demo_bb_regs.h").write_text("#define DEMOBB_REG_CTRL1 0x44\n", encoding="utf-8")
    r = cc.run(ad, atlas, log=lambda *a: None)
    assert r["units"][0]["registers_matched"] == 6                  # the committed header, not the edited file


# ---------------------------------------------------------------- the join folds it into the mismatch report

def test_join_reports_the_class(tmp_path):
    from test_join import make_project, run_join
    from embarch_atlas import mismatch
    ad, atlas = make_project(tmp_path)
    fw = ad.parents[1]
    (fw / "drivers" / "demobb").mkdir(parents=True)
    (fw / "drivers" / "demobb" / "demo_bb_regs.h").write_text(REGS_H, encoding="utf-8")
    (fw / "drivers" / "demobb" / "demo_bb.c").write_text(DRIVER_C, encoding="utf-8")
    d = ad / "docs" / DOC
    write_json(d / "doc.json", {"doc_id": DOC, "kind": "datasheet", "part": "DEMOBB1", "file": "demobb.pdf", "pages": 40})
    regs, fields = demo_records()
    write_jsonl(d / "records" / "register.jsonl", regs)
    write_jsonl(d / "records" / "field.jsonl", fields)
    dt = read_json(atlas / "dt.json")
    dt["nodes"]["/soc/i2c@40005400/bb@74"] = {"labels": ["bb0"], "compats": ["acme,demobb1"], "okay": True, "ordinal": 50,
                                              "regs": [[0x74, None]], "parent": "/soc/i2c@40005400", "props": {}}
    dt["drivers"]["acme_demobb1"] = ["fw:drivers/demobb/demo_bb.c"]
    write_json(atlas / "dt.json", dt)
    run_join(ad, atlas)
    mm = read_json(atlas / "mismatches.json")
    classes = {c["class"] for c in mm["candidates"]}
    assert {"reg-field-in-other-register", "reg-mask-vs-field", "reg-reset-vs-datasheet", "reg-address-unknown",
            "reg-comment-vs-datasheet"} <= classes
    checks = {c["check"]: c for c in mm["checks"]}
    assert checks[cc.CHECKS["addr"]]["passed"] == 6
    assert not any(n["check"] == "register constants vs datasheet" for n in mm["not_run"])
    text = (atlas / "checks.md").read_text()
    assert "### reg-field-in-other-register" in text and "fw:drivers/demobb/demo_bb_regs.h:" in text
    assert text.index("### reg-field-in-other-register") < text.index("### reg-comment-vs-datasheet")   # ranked
    assert (atlas / "codecheck.json").is_file()
    assert all(c in mismatch.ORDER for c in cc.ORDER)


def test_nv_backup_mirror_shares_fields():
    from embarch_atlas.codecheck import RegMap
    regs = [{"id": "demo-fg.reg.PackCfg", "kind": "register", "name": "PackCfg", "addr": "0xbd", "width": 16,
             "nv_backup": "demo-fg.reg.nPackCfg"},
            {"id": "demo-fg.reg.nPackCfg", "kind": "register", "name": "nPackCfg", "addr": "0x1b5", "width": 16,
             "mirror_of": "demo-fg.reg.PackCfg"}]
    fields = [{"id": "demo-fg.reg.PackCfg.NCELLS", "kind": "field", "reg": "demo-fg.reg.PackCfg", "name": "NCELLS",
               "bits": [3, 0]}]
    rm = RegMap("demo-fg@1", regs, fields)
    assert [f.name for f in rm.fields(0x1b5)] == ["NCELLS"]
    assert rm.fields(0x1b5)[0].rec.get("via_mirror") is True
    assert [f.name for f in rm.fields(0xbd)] == ["NCELLS"]

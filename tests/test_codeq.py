"""code and states over synthetic builds (invented sources) and a trimmed copy of Zephyr's USB-C stack."""
import os
import struct
import time
from pathlib import Path

import pytest

from embarch_atlas import codeq, query
from embarch_atlas.common import write_json, write_text

FIXTURE = Path(__file__).parent / "fixtures" / "zephyr_usb_c"

SAMPLE = """/* demo */
#include <zephyr/demo.h>

#define DEMO_LIMIT 4
#define DEMO_SCALE(x) \\
	((x) * DEMO_LIMIT)

#ifdef __cplusplus
extern "C" {
#endif

struct demo_cfg {
	int rate_hz;
	const char *name;
};

typedef struct {
	int a;
} demo_pair_t;

typedef void (*demo_cb_t)(int code);

enum demo_mode {
	DEMO_IDLE,
	DEMO_RUN = 3,
	DEMO_MODE_MAX,
};

int demo_start(const struct demo_cfg *cfg);

static struct demo_cfg tentative;

K_SEM_DEFINE(demo_sem, 0, 1);

/** Starts the demo. */
int demo_start(const struct demo_cfg *cfg)
{
	if (cfg->rate_hz > DEMO_LIMIT) {
		return -1;
	}
	return demo_helper(cfg->rate_hz, "{");
}

#if defined(DEMO_A)
static int split(int x) {
#else
static int split(int x, int y) {
#endif
	return x;
}

#if 0
int never_built(void) { return 0; }
#endif

#ifdef CONFIG_DEMO_OFF
int off_fn(void)
{
	return 1;
}
#endif

static const int table[] = { 1, 2, 3 };

#ifdef __cplusplus
}
#endif
"""


def _kinds(ex: dict) -> dict:
    return {(d[0], d[1]) for d in ex["d"]}


def test_extract_finds_each_kind_of_definition():
    ex = codeq.extract(SAMPLE)
    k = _kinds(ex)
    for want in [("DEMO_LIMIT", "macro"), ("DEMO_SCALE", "macro"), ("demo_cfg", "struct"), ("demo_pair_t", "typedef"),
                 ("demo_cb_t", "typedef"), ("demo_mode", "enum"), ("DEMO_RUN", "enumerator"), ("DEMO_MODE_MAX", "enumerator"),
                 ("demo_start", "declaration"), ("demo_start", "function"), ("tentative", "variable"),
                 ("demo_sem", "macro-object"), ("split", "function"), ("off_fn", "function"), ("table", "variable")]:
        assert want in k, want
    assert ("never_built", "function") not in k                       # #if 0 is never compiled
    fn = next(d for d in ex["d"] if d[0] == "demo_start" and d[1] == "function")
    assert (fn[2], fn[3], fn[5]) == (SAMPLE.split("\n").index("int demo_start(const struct demo_cfg *cfg)") + 1,
                                     SAMPLE.split("\n").index("\treturn demo_helper(cfg->rate_hz, \"{\");") + 2,
                                     SAMPLE.split("\n").index("/** Starts the demo. */") + 1)
    macro = next(d for d in ex["d"] if d[0] == "DEMO_SCALE")
    assert macro[3] == macro[2] + 1 and macro[6] == "("                 # continuation line, function-like
    # the split #if/#else heads keep the brace depth right: what follows is still found at file scope
    assert ("table", "variable") in k and ("off_fn", "function") in k
    off = [c for c in ex["c"] if "defined(CONFIG_DEMO_OFF)" in c[2]]
    assert off and off[0][0] <= next(d for d in ex["d"] if d[0] == "off_fn")[2] <= off[0][1]


def test_cond_value_decides_config_only_conditions():
    cfg = {"ON": "y", "OFF": "n", "NUM": "3", "S": "text"}
    assert codeq.cond_value("defined(CONFIG_ON)", cfg) is True
    assert codeq.cond_value("defined(CONFIG_OFF)", cfg) is False
    assert codeq.cond_value("!defined(CONFIG_MISSING)", cfg) is True
    assert codeq.cond_value("IS_ENABLED(CONFIG_ON) && CONFIG_NUM > 2", cfg) is True
    assert codeq.cond_value("CONFIG_NUM == 2 || defined(CONFIG_OFF)", cfg) is False
    assert codeq.cond_value("DT_HAS_COMPAT_STATUS_OKAY(acme_x)", cfg) is None     # not decidable from .config
    assert codeq.cond_value("defined(CONFIG_ON) && defined(__GNUC__)", cfg) is None
    assert codeq.cond_value("CONFIG_S", cfg) is None


def _ninja_deps(path: Path, records: dict[str, list[str]]) -> None:
    """A `.ninja_deps` log, format 4, as ninja writes it."""
    ids: dict[str, int] = {}
    out = bytearray(b"# ninjadeps\n" + struct.pack("<i", 4))

    def pid(p: str) -> int:
        if p not in ids:
            ids[p] = len(ids)
            raw = p.encode()
            raw += b"\0" * ((4 - len(raw) % 4) % 4)
            out.extend(struct.pack("<I", len(raw) + 4) + raw + struct.pack("<I", ~ids[p] & 0xFFFFFFFF))
        return ids[p]

    for o, ins in records.items():
        oid = pid(o)
        iids = [pid(i) for i in ins]
        out.extend(struct.pack("<I", 0x80000000 | (4 + 8 + 4 * len(iids))) + struct.pack("<iq", oid, 123)
                   + struct.pack(f"<{len(iids)}i", *iids))
    path.write_bytes(bytes(out))


def test_read_ninja_deps_round_trips(tmp_path):
    _ninja_deps(tmp_path / ".ninja_deps", {"a.c.obj": ["/s/a.c", "/s/a.h"], "b.c.obj": ["/s/b.c"]})
    assert codeq.read_ninja_deps(tmp_path / ".ninja_deps") == {"a.c.obj": ["/s/a.c", "/s/a.h"], "b.c.obj": ["/s/b.c"]}
    (tmp_path / "bad").write_bytes(b"not a log")
    assert codeq.read_ninja_deps(tmp_path / "bad") is None


# ---------------------------------------------------------------- a fw repo, a workspace Zephyr and a patched worktree

THING_WS = """#include <zephyr/thing.h>

int thing_limit(void)
{
	return THING_MAX;
}
"""

THING_PATCHED = """#include <zephyr/thing.h>

int thing_limit(void)
{
	/* patched: clamp to the board's limit */
	return THING_MAX - 1;
}
"""

MAIN_C = """#include <zephyr/thing.h>

static int init(void)
{
	return thing_limit();
}

#ifdef CONFIG_EXTRA
int extra(void)
{
	return 2;
}
#endif

int long_one(void)
{
	int x = 0;
""" + "".join(f"\tx += {i};\n" for i in range(120)) + """	return x;
}
"""

DRIVER_C = """static int init(void)
{
	return 7;
}

int driver_use(void)
{
	return init() + thing_limit();
}
"""

HEADER = """#ifndef THING_H
#define THING_H
/** The most things. */
#define THING_MAX 8
int thing_limit(void);
#endif
"""


def _tree(root: Path, name: str, thing: str) -> Path:
    z = root / name / "zephyr"
    write_text(z / "lib" / "thing.c", thing)
    write_text(z / "include" / "zephyr" / "thing.h", HEADER)
    return z


def _build(root: Path, bdir: Path, z: Path, fw: Path, deps: bool, version: str) -> None:
    write_text(bdir / "build.ninja", f"  COMMAND = /usr/bin/cmake -E env ZEPHYR_BASE={z} /py "
                                     f"{z}/scripts/kconfig/menuconfig.py {z}/Kconfig\n")
    write_text(bdir / "zephyr" / ".config", "CONFIG_DEMO=y\n# CONFIG_EXTRA is not set\n")
    write_text(bdir / "zephyr" / "include" / "generated" / "zephyr" / "version.h", f"#define BUILD_VERSION {version}\n")
    units = [(z / "lib" / "thing.c", "zephyr/lib/thing.c.obj"), (fw / "app" / "main.c", "app/main.c.obj"),
             (fw / "app" / "driver.c", "app/driver.c.obj")]
    write_json(bdir / "compile_commands.json", [
        {"directory": str(bdir), "file": str(f), "output": o,
         "command": f"gcc -I{z}/include -c {f} -o {o}"} for f, o in units])
    if deps:
        _ninja_deps(bdir / ".ninja_deps", {o: [str(f), str(z / "include" / "zephyr" / "thing.h")] for f, o in units})
    write_text(bdir / "zephyr" / "zephyr.elf", "")
    past = time.time() - 3600
    os.utime(bdir / "zephyr" / "zephyr.elf", (past, past))
    for p in [*z.rglob("*.[ch]"), *(fw / "app").rglob("*.c")]:
        os.utime(p, (past - 60, past - 60))


@pytest.fixture()
def project(tmp_path: Path) -> dict:
    fw = tmp_path / "fw"
    write_text(fw / "app" / "main.c", MAIN_C)
    write_text(fw / "app" / "driver.c", DRIVER_C)
    ws = _tree(tmp_path, "ws", THING_WS)                  # the workspace's Zephyr
    scratch = _tree(tmp_path, "scratch", THING_PATCHED)   # a worktree with a patch applied
    a = fw / "embarch" / "atlas"
    at = a / "atlases" / "core@000000000004"
    pinned_build = tmp_path / "pinned-build"
    _build(tmp_path, pinned_build, ws, fw, True, "v9.9.0")
    (at / "build").mkdir(parents=True)
    (at / "build" / "compile_commands.json").write_bytes((pinned_build / "compile_commands.json").read_bytes())
    (at / "build" / ".config").write_bytes((pinned_build / "zephyr" / ".config").read_bytes())
    write_json(at / "atlas.json", {"id": "core@000000000004", "commit": "0" * 40, "created": "2020-01-01T00:00:00Z",
                                   "west": [{"name": "zephyr", "revision": "v9.9.0"}],
                                   "build": {"worktree": str(fw), "zephyr_base": str(ws), "dir": str(pinned_build)}})
    write_json(a / "current.json", {"matches": [{"atlas": "core@000000000004"}]})
    _build(tmp_path, fw / "build-patched", scratch, fw, True, "v9.9.0-1-gpatched")
    _build(tmp_path, tmp_path / "build-scan", scratch, fw, False, "v9.9.0-1-gpatched")
    return {"a": a, "fw": fw, "ws": ws, "scratch": scratch, "tmp": tmp_path}


def test_patched_worktree_build_returns_the_patched_lines(project):
    a = query.Atlas(project["a"])
    pinned = query.code(a, "thing_limit", show="def")
    patched = query.code(a, "thing_limit", build="build-patched", show="def")
    assert "return THING_MAX;" in pinned and "patched" not in pinned
    assert "/* patched: clamp to the board's limit */" in patched and "return THING_MAX - 1;" in patched
    assert pinned.startswith("src: atlas core@000000000004 (zephyr v9.9.0) sources, 3 units + 1 headers by ninja deps")
    assert patched.startswith(f"src: build {project['fw'] / 'build-patched'} (app ?, zephyr v9.9.0-1-gpatched;")
    assert "zephyr:lib/thing.c:3-7" in patched


def test_a_symbol_defined_in_several_units_lists_each(project):
    out = query.code(query.Atlas(project["a"]), "init", build="build-patched")
    assert "init: 2 definitions" in out
    assert "function fw:app/driver.c:1-4" in out and "function fw:app/main.c:3-6" in out
    assert "return 7;" in out and "return thing_limit();" in out
    assert "fw:app/driver.c:8 driver_use" in out                     # the call, as file:line function
    narrowed = query.code(query.Atlas(project["a"]), "driver.c:init", build="build-patched")
    assert "init: 1 definition" in narrowed and "return thing_limit();" not in narrowed
    uses = query.code(query.Atlas(project["a"]), "driver.c:thing_limit", build="build-patched", show="refs")
    assert "(not defined in driver.c: its definitions, then its uses there)" in uses
    assert "references (1):\nfw:app/driver.c:8 driver_use" in uses


def test_a_long_body_pages_to_the_uncut_answer(project, monkeypatch):
    from paging_synth import assert_exact
    a = query.Atlas(project["a"])
    assert_exact(monkeypatch, "code", lambda o: query.code(a, "long_one", build="build-patched", offset=o), tokens=200)
    assert_exact(monkeypatch, "states", lambda o: query.states(a, "usb_c", build=str(_usb_c_build(project)), offset=o),
                 tokens=300)


def test_headers_references_lines_files_and_patterns(project):
    a = query.Atlas(project["a"])
    q = lambda s, **k: query.code(a, s, build="build-patched", **k)   # noqa: E731
    mac = q("THING_MAX")
    assert "macro zephyr:include/zephyr/thing.h:4  (included by 3 compiled units" in mac
    assert "3 /** The most things. */" in mac                        # the comment above it comes along
    assert "zephyr:lib/thing.c:6 thing_limit" in mac
    refs = q("thing_limit", show="refs")
    assert "references (2):" in refs and "fw:app/main.c:5 init" in refs and "{" not in refs.split("references")[1]
    assert "holds line 5" in q("main.c:5") and "return thing_limit();" in q("main.c:5")
    outline = q("app/main.c")
    assert "fw:app/main.c: a compiled unit" in outline and "function long_one" in outline
    assert "thing_limit  function" in q("thing_*")
    assert "Nearest: thing_limit" in q("thing_limt")


def test_a_definition_under_a_config_this_build_turns_off(project):
    out = query.code(query.Atlas(project["a"]), "extra", build="build-patched")
    assert "extra: 0 definitions (+1 in #if branches this build does not compile)" in out
    assert "not compiled: function fw:app/main.c:9 (#if defined(CONFIG_EXTRA))" in out


def test_a_file_changed_since_the_build_is_said_so(project):
    t = project["scratch"] / "lib" / "thing.c"
    os.utime(t, None)
    out = query.code(query.Atlas(project["a"]), "thing_limit", build="build-patched", show="def")
    assert "1 changed since the build" in out.split("\n")[0]
    assert "CHANGED since the build: this is the file now" in out


def test_without_ninja_deps_headers_come_from_the_include_scan(project):
    out = query.code(query.Atlas(project["a"]), "THING_MAX", build=str(project["tmp"] / "build-scan"), show="def")
    assert "by #include scan (no .ninja_deps)" in out.split("\n")[0]
    assert "macro zephyr:include/zephyr/thing.h:4" in out


def test_code_and_states_need_compile_commands(project):
    (project["fw"] / "build-patched" / "compile_commands.json").unlink()
    out = query.code(query.Atlas(project["a"]), "init", build="build-patched")
    assert out.startswith("src: none") and "no compile_commands.json" in out


# ---------------------------------------------------------------- states on Zephyr's USB-C stack (trimmed, v4.4.0)

def _usb_c_build(project) -> Path:
    z = project["tmp"] / "zt" / "zephyr"
    d = z / "subsys" / "usb" / "usb_c"
    d.mkdir(parents=True, exist_ok=True)
    for f in FIXTURE.glob("*.c"):
        (d / f.name).write_bytes(f.read_bytes())
    b = project["tmp"] / "build-usbc"
    write_text(b / "build.ninja", f"  COMMAND = /usr/bin/cmake -E env ZEPHYR_BASE={z} /py "
                                  f"{z}/scripts/kconfig/menuconfig.py {z}/Kconfig\n")
    write_text(b / "zephyr" / ".config", "CONFIG_USBC_CSM_SUPPORTS_SINK=y\n# CONFIG_USBC_CSM_SUPPORTS_SOURCE is not set\n")
    write_text(b / "zephyr" / "include" / "generated" / "zephyr" / "version.h", "#define BUILD_VERSION v4.4.0\n")
    write_json(b / "compile_commands.json", [{"directory": str(b), "file": str(d / f.name), "output": f"{f.name}.obj",
                                              "command": f"gcc -c {d / f.name}"} for f in sorted(FIXTURE.glob("*.c"))])
    return b


def test_states_lists_the_pe_tc_and_prl_tables_with_their_transitions(project):
    a = query.Atlas(project["a"])
    out = query.states(a, "subsys/usb/usb_c", build=str(_usb_c_build(project)))
    assert "(app ?, zephyr v4.4.0;" in out.split("\n")[0]
    tables = [l for l in out.split("\n") if l.startswith("table ")]
    assert [t.split()[1] for t in tables] == ["pe_states", "prl_tx_states", "prl_hr_states", "tc_states"]
    assert ("table pe_states zephyr:subsys/usb/usb_c/usbc_pe_common.c:103  4 states "
            "(+1 in #if branches this build does not compile); set through pe_set_state") in out
    rows = {l.split(" | ")[0]: l for l in out.split("\n") if " | " in l}
    assert "PE_SRC_STARTUP" not in rows                                   # under a CONFIG off in this build
    assert rows["PE_SNK_STARTUP"].endswith("run -> PE_SNK_DISCOVERY")
    assert "run pe_snk_startup_run usbc_pe_snk_states.c:" in rows["PE_SNK_STARTUP"]
    assert "run -> PE_SNK_WAIT_FOR_CAPABILITIES (not in this build's table)" in rows["PE_SNK_DISCOVERY"]
    assert "PE_SNK_HARD_RESET via" not in rows["PE_SEND_SOFT_RESET"]
    assert "parent TC_CC_RD_SUPER_STATE" in rows["TC_UNATTACHED_SNK_STATE"]
    assert "run -> TC_ATTACH_WAIT_SNK_STATE" in rows["TC_UNATTACHED_SNK_STATE"]
    assert "run -> PRL_TX_SNK_PENDING" in rows["PRL_TX_SNK_START_AMS"]
    assert "run -> PRL_HR_RESET_LAYER" in rows["PRL_HR_WAIT_FOR_REQUEST"]
    one = query.states(a, "tc_states", build=str(_usb_c_build(project)))
    assert "table tc_states" in one and "pe_states" not in one
    assert "no SMF table or directory 'nope'" in query.states(a, "nope", build=str(_usb_c_build(project)))


SM_C = """struct ctx;
static const struct smf_state demo_states[DEMO_COUNT];

static void demo_set(struct ctx *c, enum demo_state s)
{
	smf_set_state(SMF_CTX(c), &demo_states[s]);
}

static void go_error(struct ctx *c)
{
	demo_set(c, DEMO_ERROR);
}

static void idle_run(void *o)
{
	if (ready(o)) {
		demo_set(o, DEMO_BUSY);
	} else {
		go_error(o);
	}
#ifdef CONFIG_DEMO_TURBO
	demo_set(o, DEMO_TURBO);
#endif
}

static void busy_entry(void *o)
{
	smf_set_state(SMF_CTX(o), &demo_states[DEMO_IDLE]);
}

static const struct smf_state demo_states[DEMO_COUNT] = {
	[DEMO_IDLE] = SMF_CREATE_STATE(NULL, idle_run, NULL, NULL, NULL),
	[DEMO_BUSY] = SMF_CREATE_STATE(busy_entry, NULL, NULL, &demo_states[DEMO_IDLE], NULL),
	[DEMO_ERROR] = SMF_CREATE_STATE(NULL, NULL, NULL, NULL, NULL),
};
"""


def test_states_reads_setters_one_helper_down_and_skips_off_branches(project, tmp_path):
    d = project["fw"] / "app"
    write_text(d / "demo_sm.c", SM_C)
    b = project["fw"] / "build-sm"
    _build(project["tmp"], b, project["scratch"], project["fw"], False, "v9.9.0")
    write_json(b / "compile_commands.json", [{"directory": str(b), "file": str(d / "demo_sm.c"), "output": "sm.obj",
                                              "command": f"gcc -c {d / 'demo_sm.c'}"}])
    out = query.states(query.Atlas(project["a"]), "demo_states", build="build-sm")
    rows = {l.split(" | ")[0]: l for l in out.split("\n") if " | " in l}
    assert "table demo_states fw:app/demo_sm.c:31  3 states; set through demo_set" in out
    assert rows["DEMO_IDLE"].endswith("run -> DEMO_BUSY; DEMO_ERROR via go_error")       # DEMO_TURBO's #if is off
    assert "parent DEMO_IDLE" in rows["DEMO_BUSY"] and rows["DEMO_BUSY"].endswith("entry -> DEMO_IDLE")
    assert "entry busy_entry demo_sm.c:26" in rows["DEMO_BUSY"]
    assert rows["DEMO_ERROR"] == "DEMO_ERROR | parent - | entry - | run - | exit -"

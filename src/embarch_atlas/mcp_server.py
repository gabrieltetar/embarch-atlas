"""`embarch-atlas mcp`: the atlas's twelve lookups as an MCP server over stdio, and the live
module's `reg_read` when embarch-core answers.

Thin on purpose: every tool is a function in `query.py`, which the `q` CLI verbs call
too, so an agent, a script and a human get the same bytes. Files stay the primary
door (an agent's own Read/Grep reach them for free); the server carries what files
cannot: joins, register lookups across documents, fact search, the mismatch report.

Register it for a project (local scope keeps it out of every tracked file):

    claude mcp add --scope local embarch-atlas -- embarch-atlas --atlas-dir <fw>/embarch/atlas mcp

and set `"alwaysLoad": true` on its entry so its tools skip deferred tool search.

A tool declares `readOnlyHint` only when it cannot change the board: a host (plan mode, an
auto-approval rule, a classifier) trusts the hint. `shell` runs only commands the `[live] read_only`
list marks as reads; `shell_run` (any command) and `reg_read_clear` (a read that clears something)
carry no hint, so the host asks.
"""
from __future__ import annotations

from pathlib import Path

from . import common, live as live_mod, query

INSTRUCTIONS = (
    "Hardware atlas of this firmware project: schematics, datasheets, the MCU reference "
    "manual and the PRD, distilled, page-cited and joined to one pinned firmware build. "
    "Use it before answering any hardware question. Protocol: MAP.md (already in context) "
    "-> part_card -> doc_section -> Read the PDF page to verify. Every answer starts with a "
    "`src:` line: cite it. prov=llm-unverified and figure facts are leads, not truth. "
    "Mismatches are candidates for the engineer, never facts."
)


def build_server(atlas_dir: Path, atlas: str | None = None, live: bool | None = None):
    """`live`: list the live tools; None asks embarch-core once, at start (off unless it answers)."""
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations

    server = MCPServer(name="embarch-atlas", instructions=INSTRUCTIONS)
    READ_ONLY = ToolAnnotations(read_only_hint=True)
    WRITE = ToolAnnotations(read_only_hint=False)

    def view() -> query.Atlas:
        # Re-read per call: an atlas rebuilt while the server runs is picked up.
        return query.Atlas(atlas_dir, atlas)

    @server.tool(annotations=READ_ONLY)
    def part_card(part: str, board: str | None = None) -> str:
        """A part's card (<=1.5K tokens): role, key limits, register summary, quirks, section index.
        `part` is an MPN, doc id or document number; `board` ("all" for every board, or one board's label) adds its refs and nets; omit it for the card alone."""
        return query.part_card(view(), part, board)

    @server.tool(annotations=READ_ONLY)
    def doc_toc(doc: str, under: str | None = None, depth: int = 2) -> str:
        """A document's outline: section id | title | pages | tokens | distilled file. `under` narrows to a section,
        by id, title, or a word of the title (the shallowest section holding it)."""
        return query.doc_toc(view(), doc, under, depth)

    @server.tool(annotations=READ_ONLY)
    def doc_section(doc: str, section: str | None = None, page: int | None = None,
                    tier: str = "distilled", offset: int = 0) -> str:
        """A distilled section file (tables one condition per row, page-cited), or tier="text" with `page`
        for one page's text layer. Long answers end with next=<offset>."""
        return query.doc_section(view(), doc, section, page, tier, offset)

    @server.tool(annotations=READ_ONLY)
    def find_fact(query_text: str, part: str | None = None, kind: str | None = None, limit: int | None = None) -> str:
        """Search every document's facts (one per line, with cite and provenance), ranked lexically.
        A part named in the query narrows to it, like `part`. Without `limit`: the best 10 above 0.4x the
        top score. `kind`: limit, timing, pin, rule, register, field, quirk, strap, requirement, conflict."""
        return query.find_fact(view(), query_text, part, kind, limit)

    @server.tool(annotations=READ_ONLY)
    def register(target: str, field: str | None = None) -> str:
        """A register with its fields, rules, quirks, SVD/RM conflicts and firmware references.
        `target` is `PERIPH.REG` for the MCU (I2C1.TIMINGR) or `<part>.<REG>` for an external part; a register
        split into `<REG>_MSB`/`_LSB` bytes is also found by `<REG>`, as one 16-bit view."""
        return query.register_lookup(view(), target, field)

    @server.tool(annotations=READ_ONLY)
    def pin_trace(pin_or_net: str, board: str | None = None) -> str:
        """MCU pin -> net -> far-end part pins (through connectors) -> DT use -> driver, one line per pin.
        Accepts an MCU port (PB9), a net name, or a board-qualified part pin (A:U3-6)."""
        return query.pin_trace(view(), pin_or_net, board)

    @server.tool(annotations=READ_ONLY)
    def net_trace(net_or_pin: str, board: str | None = None, offset: int = 0) -> str:
        """Any net (rails, sense lines, enables), part pin (A:U7-EN, A:U3-6) or part (A:R12) on any
        board: members across connector mates, then what feeds it (only from datasheet pin `flow`),
        its enables and where they come from, and the datasheets still needed. pin_trace covers MCU pins."""
        return query.net_trace(view(), net_or_pin, board, offset)

    @server.tool(annotations=READ_ONLY)
    def config(symbol: str, build: str | None = None) -> str:
        """A Kconfig symbol (CONFIG_X or X; `USB_*` lists matches): value, the fragment that set it,
        depends on and whether it is met, selected/implied by, the default that applies, where defined.
        `build`: a build dir (absolute or relative to the fw repo); default the atlas's pinned build."""
        return query.config(view(), symbol, build)

    @server.tool(annotations=READ_ONLY)
    def dt_node(node: str, build: str | None = None) -> str:
        """A devicetree node by path, label, alias, chosen name or compatible: status, binding, the driver
        compiled for it, whether its device is in the ELF, each property with the file:line that set it,
        its pins, children. `build`: a build dir; default the atlas's pinned build."""
        return query.dt_node(view(), node, build)

    @server.tool(annotations=READ_ONLY)
    def footprint(build: str | None = None, vs: str | None = None, by: str = "dir") -> str:
        """Flash image and RAM against the flash, SRAM and code partition (headroom left), then the largest
        contributors by dir, file or symbol; `vs` (a build dir, or "pinned") gives the change against that
        build instead. `build`: a build dir; default the atlas's pinned build."""
        return query.footprint(view(), build, vs, by)

    @server.tool(annotations=READ_ONLY)
    def stacks(build: str | None = None) -> str:
        """Every thread stack in the image: size, symbol, the line that defined it, and the *_STACK_SIZE
        option it most likely comes from (inference). Static sizes; high-water marks need the board."""
        return query.stacks(view(), build)

    @server.tool(annotations=READ_ONLY)
    def mismatch_report(scope: str | None = None, cls: str | None = None, offset: int = 0) -> str:
        """Checks run and passed, then candidate mismatches between schematic, DT, driver and datasheet,
        each with evidence and what would settle it. `scope` filters by substring, `cls` by class."""
        return query.mismatch_report(view(), scope, cls, offset)

    if live is None:
        live = live_mod.available(atlas_dir) is None
    if live:
        @server.tool(annotations=READ_ONLY)
        def reg_read(target: str, allow_clear: bool = False, role: str | None = None) -> str:
            """Live: an MCU register (`PERIPH.REG`, e.g. RCC.APBENR1) or a whole peripheral (`UCPD1`) read on
            the running board through embarch-core and decoded field by field against the SVD; or an external
            part's register (`<DT label>.<REG>`, e.g. vbus_sense.VBUS, or `<part>.<REG>` when it has one
            instance) read with the board shell's `i2c read`, decoded against its datasheet. `*` marks a
            field not at its reset value. A running core is halted for the read (about 20 ms over a J-Link) and let run
            again. A register whose read clears something (a FIFO data register, a read-to-clear flag) is
            refused, or left out of a peripheral read; `reg_read_clear` reads those."""
            if allow_clear:
                return ("src: none\n\nRefused: allow_clear=true can clear a latched flag or pop a FIFO, so it is "
                        "not a read-only call. Use `reg_read_clear`.\n")
            return live_mod.reg_read(atlas_dir, target, False, role)

        @server.tool(annotations=WRITE)
        def reg_read_clear(target: str, role: str | None = None) -> str:
            """Live: `reg_read` with allow_clear=true. Reads registers whose read clears a flag or pops a FIFO,
            and says what the read cleared. Not read-only: a host asks before it runs."""
            return live_mod.reg_read(atlas_dir, target, True, role)

        @server.tool(annotations=READ_ONLY)
        def live_stacks(build: str | None = None, role: str | None = None) -> str:
            """Live: every thread's stack peak on the board (`kernel thread stacks` over the shell), joined to
            its stack symbol, the line that defined it and the *_STACK_SIZE option (inference) once a few flash
            words read through embarch-core show the board runs that build; `!` marks 90 % and above.
            `build`: the build dir the board runs; default the atlas's pinned build."""
            return live_mod.live_stacks(atlas_dir, build, role)

        @server.tool(annotations=READ_ONLY)
        def shell(command: str, role: str | None = None, timeout_ms: int | None = None) -> str:
            """Live: one read-only command on the board's shell (the console signal the role declares), through
            embarch-core; returns its output with escape codes stripped. Runs only commands matching the
            `[live] read_only` list in atlas.toml (default: device list, kernel thread stacks, kernel uptime,
            i2c read, i2c read_byte); anything else is refused, use `shell_run`.
            timeout_ms (default 3000, max 10000) bounds the wait for the prompt."""
            return live_mod.shell_read(atlas_dir, command, role, timeout_ms)

        @server.tool(annotations=WRITE)
        def shell_run(command: str, role: str | None = None, timeout_ms: int | None = None) -> str:
            """Live: any command on the board's shell, through embarch-core. It can change the board's state
            (switch a power path, drive a GPIO), so it is not read-only: a host asks before it runs.
            timeout_ms (default 3000, max 10000) bounds the wait for the prompt."""
            return live_mod.shell(atlas_dir, command, role, timeout_ms)

    return server


def _cmd_mcp(args) -> int:
    from . import project
    adir = common.atlas_dir(args.atlas_dir)
    try:
        project.status(adir)    # current.json and the MAP's stale line are right for the session this starts
    except Exception:           # noqa: BLE001  (a status failure must not keep the lookups from serving)
        pass
    server = build_server(adir, args.atlas)
    server.run("stdio")
    return 0


def register(sub, groups) -> None:
    p = sub.add_parser("mcp", help="serve the atlas's twelve lookups as an MCP server over stdio")
    p.add_argument("--atlas", help="atlas id (default: the one matching HEAD, else the newest)")
    p.set_defaults(func=_cmd_mcp)

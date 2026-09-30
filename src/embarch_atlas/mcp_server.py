"""`embarch-atlas mcp`: the atlas's seven lookups as an MCP server over stdio.

Thin on purpose: every tool is a function in `query.py`, which the `q` CLI verbs call
too, so an agent, a script and a human get the same bytes. Files stay the primary
door (an agent's own Read/Grep reach them for free); the server carries what files
cannot: joins, register lookups across documents, fact search, the mismatch report.

Register it for a project (local scope keeps it out of every tracked file):

    claude mcp add --scope local embarch-atlas -- embarch-atlas --atlas-dir <fw>/embarch/atlas mcp

and set `"alwaysLoad": true` on its entry so its tools skip deferred tool search.
"""
from __future__ import annotations

from pathlib import Path

from . import common, query

INSTRUCTIONS = (
    "Hardware atlas of this firmware project: schematics, datasheets, the MCU reference "
    "manual and the PRD, distilled, page-cited and joined to one pinned firmware build. "
    "Use it before answering any hardware question. Protocol: MAP.md (already in context) "
    "-> part_card -> doc_section -> Read the PDF page to verify. Every answer starts with a "
    "`src:` line: cite it. prov=llm-unverified and figure facts are leads, not truth. "
    "Mismatches are candidates for the engineer, never facts."
)


def build_server(atlas_dir: Path, atlas: str | None = None):
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(name="embarch-atlas", instructions=INSTRUCTIONS)

    def view() -> query.Atlas:
        # Re-read per call: an atlas rebuilt while the server runs is picked up.
        return query.Atlas(atlas_dir, atlas)

    @server.tool()
    def part_card(part: str, board: str | None = None) -> str:
        """A part's card (<=1.5K tokens): role, key limits, register summary, quirks, section index.
        `part` is an MPN, doc id or document number; `board` ("" for all, or a label) adds its refs and nets."""
        return query.part_card(view(), part, board)

    @server.tool()
    def doc_toc(doc: str, under: str | None = None, depth: int = 2) -> str:
        """A document's outline: section id | title | pages | tokens | distilled file. `under` narrows to a section."""
        return query.doc_toc(view(), doc, under, depth)

    @server.tool()
    def doc_section(doc: str, section: str | None = None, page: int | None = None,
                    tier: str = "distilled", offset: int = 0) -> str:
        """A distilled section file (tables one condition per row, page-cited), or tier="text" with `page`
        for one page's text layer. Long answers end with next=<offset>."""
        return query.doc_section(view(), doc, section, page, tier, offset)

    @server.tool()
    def find_fact(query_text: str, part: str | None = None, kind: str | None = None, limit: int = 20) -> str:
        """Search every document's facts (one per line, with cite and provenance), ranked lexically.
        `kind`: limit, timing, pin, rule, register, field, quirk, strap, requirement, conflict."""
        return query.find_fact(view(), query_text, part, kind, limit)

    @server.tool()
    def register(target: str, field: str | None = None) -> str:
        """A register with its fields, rules, quirks, SVD/RM conflicts and firmware references.
        `target` is `PERIPH.REG` for the MCU (I2C1.TIMINGR) or `<part>.<REG>` for an external part."""
        return query.register_lookup(view(), target, field)

    @server.tool()
    def pin_trace(pin_or_net: str, board: str | None = None) -> str:
        """MCU pin -> net -> far-end part pins (through connectors) -> DT use -> driver, one line per pin.
        Accepts an MCU port (PB9), a net name, or a board-qualified part pin (A:U3-6)."""
        return query.pin_trace(view(), pin_or_net, board)

    @server.tool()
    def mismatch_report(scope: str | None = None, cls: str | None = None, offset: int = 0) -> str:
        """Checks run and passed, then candidate mismatches between schematic, DT, driver and datasheet,
        each with evidence and what would settle it. `scope` filters by substring, `cls` by class."""
        return query.mismatch_report(view(), scope, cls, offset)

    return server


def _cmd_mcp(args) -> int:
    server = build_server(common.atlas_dir(args.atlas_dir), args.atlas)
    server.run("stdio")
    return 0


def register(sub, groups) -> None:
    p = sub.add_parser("mcp", help="serve the atlas's seven lookups as an MCP server over stdio")
    p.add_argument("--atlas", help="atlas id (default: the one matching HEAD, else the newest)")
    p.set_defaults(func=_cmd_mcp)

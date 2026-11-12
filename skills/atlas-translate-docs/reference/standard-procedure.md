# Distil a standard or specification (lazily)

A standard (a bus, protocol or power specification, an IEC/IEEE/JEDEC standard) is hundreds of
pages, and a firmware project implements a few of its sections. So, as for the reference manual,
only the sections someone asks for are distilled; the rest stays readable through `toc.md` and the
page text.

Output, all under `docs/<std_doc_id>/`:

| File | Who writes it |
|---|---|
| `doc.json`, `pages/`, `toc.md`, `toc.json` | `doc prep` |
| `worklist.json` | `doc sections <doc_id> --add ...`: the sections asked for, why, pages, content key, status |
| `card.md` | you, on the first pass: what the standard covers, its roles, the revision; the list of distilled sections is a block `doc sections` and `doc render` write, do not edit it |
| `records/requirement.jsonl` | you: the normative statements of the sections asked for |
| `records/limit.jsonl`, `records/timing.jsonl` | you: the parameter tables of those sections, as in a datasheet |
| `records/quirk.jsonl`, `records/figure.jsonl` | you: statements that take back another, state machines and sequence figures |
| `s/<secid>.md` | you: one tier-2 file per section asked for, at most 4K tokens |

The distillation is **board-independent**, like a datasheet's: no board, no part number, no
"this firmware does X".

## Steps

```
- [ ] 1 prep      embarch-atlas doc prep <pdf> --kind standard   (when inventory did not classify it)
- [ ] 2 card      read the front matter and toc.md only; write card.md (scope, roles, revision, distilled: none yet)
- [ ] 3 ask       which sections does the firmware implement? ASK the engineer, or take the ones a session asked for;
                  `embarch-atlas doc sections <doc_id> --suggest` ranks the sections sessions' lookups landed in;
                  embarch-atlas doc sections <doc_id> --add <sec>[,<sec>...] --why "<reason>"
- [ ] 4 read      per section with status todo: its pages' text; image Reads only for figures and mangled tables
- [ ] 5 write     records (a fw_check on each rule a configuration value can break, below), then s/<secid>.md;
                  update the card's list of distilled sections
- [ ] 6 check     embarch-atlas doc render, doc check (repeat until exit 0 or only llm-unverified remain)
- [ ] 7 freeze    embarch-atlas doc freeze <doc_id> --model <model>; doc sections <doc_id> shows the sections frozen
```

## Records

- **requirement** is the main kind: one per normative statement (a sentence with Shall, Should,
  May, Shall Not, Should Not, as the standard capitalises them).
  - `id`: `<std>.req.<section>.<subject>`, `rid`: `<std>.<section>.<subject>`; never an ordinal.
  - `q`: the sentence, verbatim (a bullet's sentence, not the bullet's lead-in alone).
  - `modality`: the keyword as printed: `shall`, `shall-not`, `should`, `should-not`, `may`.
    `doc check` W verifies it against the quote.
  - `role`: whom it binds, as the standard names it (`Source`, `Sink`, `DRP`, `Cable`), when it says.
  - `cond`: when it applies (a mode, a state, a revision), as printed.
  - `fw_relevant`: true when firmware enforces, measures or reports it.
  - `text`: your one-line statement. Its numbers must be in `q`. A requirement read from a table row
    whose `text` names a column header's values quotes the header line as a second `q` part.
- **limit / timing**: parameter tables, one record per row and condition, as in
  `datasheet-procedure.md` (group `ec` for electrical parameters, so the column check K runs).
- **quirk**: a statement that takes back or narrows another (class `text-vs-text`, `narrow-condition`),
  a deprecated feature kept for compatibility (`variant`).
- **figure**: a state machine or a message sequence that a requirement depends on (`src: figure`).

## Rules a configuration value can break (`fw_check`)

A requirement whose force lands on a devicetree property or a Kconfig symbol gets a `fw_check`, and `join`
evaluates it against the build (DESIGN.md "Firmware configuration against a standard"). Put it on the
requirement when one statement is one rule; when a rule encodes a table of requirements (a power-rules table, one
row each), write a `check` record with the `fw_check` and `refs` naming those requirement ids.

- **Stay board-independent.** Name the devicetree by binding, never by a board's node label: `dt` is a
  compatible (`usb-c-connector`) and `prop` a binding property (`source-pdos`). Kconfig is named by the
  subsystem's symbol. A value the standard's rule needs and the firmware does not state (a port's maximum power)
  is `{"declared": "<key>"}`; the engineer declares it, and which node is which port, in `atlas.toml`.
- **`expr` is true when the configuration complies**, for a Shall Not as for a Shall. `applies_when` holds the
  condition the requirement's `cond` or `role` states (`CONFIG_<SYM> == 'y'`): false skips the rule, which is
  folded as expected, never a failure.
- **Decode before comparing.** `"decode": "pd_pdo"` turns each Source_Capabilities word into `type`, `mv`, `ma`,
  `min_mv`, `max_mv`, `pdp_w` ...; `pd_sink_pdo` reads Sink_Capabilities words.
- The numbers in an expression are not checked against the page yet: copy them from the quote, digit for digit.

Worked example, on an invented "Demo PD" standard. **Every value below is invented, not the standard's**: read the
real ones from the cited page.

1. Fixed supplies against the power rule for the declared power (a `check` naming the table's rows):

   ```json
   {"id": "demopd.check.fixed_at_pdp", "kind": "check", "doc": "demo-pd@r1.0", "p": 41, "q": null, "src": "table",
    "refs": ["demopd.req.10.2.fixed-5v", "demopd.req.10.2.fixed-above-5v"],
    "fw_check": {"inputs": {"src": {"dt": "usb-c-connector", "prop": "source-pdos", "decode": "pd_pdo"},
                            "pdp_w": {"declared": "pdp_w"}},
                 "expr": "all(p.ma <= min(2500, pdp_w * 1000000 // p.mv) for p in src if p.type == 'fixed')"}}
   ```

2. PPS and AVS APDO ranges against theirs (on the requirement):

   ```json
   "fw_check": {"inputs": {"src": {"dt": "usb-c-connector", "prop": "source-pdos", "decode": "pd_pdo"}},
                "expr": "all(3000 <= p.min_mv and p.max_mv <= 12500 for p in src if p.type == 'pps') and all(p.pdp_w <= 90 for p in src if p.type == 'epr-avs')"}
   ```

3. The first Source_Capabilities' 5 V current against the Rp current the port advertises:

   ```json
   "fw_check": {"inputs": {"src": {"dt": "usb-c-connector", "prop": "source-pdos", "decode": "pd_pdo"},
                           "rp": {"dt": "usb-c-connector", "prop": "typec-power-opmode"}},
                "applies_when": "CONFIG_USBC_CSM_SOURCE_ONLY == 'y'",
                "expr": "src[0].ma <= {'default': 400, '1.5A': 1200, '3.0A': 2600}[rp]"}
   ```

4. A role chosen in Kconfig against the sequences the standard makes normative for that role needs records saying
   which sequences apply to which role; until those exist, write the requirement without a `fw_check`.

The engineer declares, in `atlas.toml` (a port's value overrides the standard's):

```toml
[standards."demo-pd"]
pdp_w = 30
[standards."demo-pd".ports.left]
node = "usbc_left"
pdp_w = 15
```

`join` then lists each rule in `checks.md` ("Firmware configuration vs standards"), and
`mismatch_report cls=config-vs-standard` gives each failure with the requirement's page and the file:line that set
the value; `mismatch_report build=<dir>` runs the rules on a branch build without rebuilding the atlas.

## When a session asks

`atlas-use` reads an undistilled section from `toc.md` and the pages directly, and suggests
`/atlas-translate-docs standard <doc_id> <section>`. That runs steps 3-7 for the section only;
frozen sections are never redone (key: section id + page-text hashes + procedure version).

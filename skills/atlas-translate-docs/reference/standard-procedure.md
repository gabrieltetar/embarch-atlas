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
| `card.md` | you, on the first pass: what the standard covers, its roles, the revision, and which sections are distilled (the rest: `toc.md`) |
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
                  embarch-atlas doc sections <doc_id> --add <sec>[,<sec>...] --why "<reason>"
- [ ] 4 read      per section with status todo: its pages' text; image Reads only for figures and mangled tables
- [ ] 5 write     records, then s/<secid>.md; update the card's list of distilled sections
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

## When a session asks

`atlas-use` reads an undistilled section from `toc.md` and the pages directly, and suggests
`/atlas-translate-docs standard <doc_id> <section>`. That runs steps 3-7 for the section only;
frozen sections are never redone (key: section id + page-text hashes + procedure version).

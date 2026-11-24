---
name: prose
description: Draft, edit, or review English or Danish prose, such as reports, academic papers, theses, documentation, READMEs, interface copy, commit messages, messages sent in the user's name, and the text in HTML pages, charts, slides, and images. Use for British English or Danish house style, a proofread or language check (sprogtjek), APA or Chicago citations and references, terminology, numbers, and tables and figures. Excludes Prose maintenance.
---

# Prose

## Workflow

1. **Select routes.** Open the core reference below that matches the work, and open the
   [reference index](references/index.md) for any specialist branch (editing, technical or interface
   writing, citations, terminology, numbers, displays, publication, or named styles).
2. **Apply governing rules.** Follow [Authority and preservation](#authority-and-preservation),
   [House choices](#house-choices), and the rules in every opened reference.
3. **Scan prose.** Before handing over English or Danish prose longer than a few paragraphs, run
   `scripts/scan_prose.py FILE` on the HTML, Markdown, or text file (add `--lang da` for Danish)
   and settle each finding in context. It flags serial-comma gaps, British-spelling, date, clock,
   unit-spacing, and house-style slips, and filler, cliché, and conversational leftovers. Add
   `--no-serial-comma` when a style omits the serial comma, or `--us-english` when supplied or
   target prose keeps US English (or set `serial_comma = false`, `us_english = true`, or
   `lang = "da"` in `.prose.toml` at the repository root).
4. **Hand over.** Complete the task under [Hand over](#hand-over).

## Routes

| Work | Open |
| --- | --- |
| Clarity, organisation, or presentation | [Reader core](references/reader-core.md) |
| Essays, talks, personal narratives, or the argument of a paper, report, or thesis | [Authored prose](references/authored-prose.md) (`#development` for academic arguments) |
| Editing, reviewing, proofreading, or revising a draft after feedback | [Editorial practice](references/editorial-practice.md) |
| New or edited English prose | [English core](references/english-core.md) |
| New or edited Danish prose | [Danish core](references/danish-core.md) |
| Specialist work (technical, interface, citations, terminology, numbers, tables/figures, publication, or APA/Chicago/IBCS/Apple) | [Reference index](references/index.md) |

## Authority and preservation

- Follow the governing instructions and the editing scope authorised by the user.
- Preserve supplied meaning, claim scope, voice, dialect, genre, and established terminology.
- When editing or extending supplied prose, keep its established mechanics, such as serial-comma
  use, quotation marks, and date form, in rewritten and added sentences unless a selected style
  governs them or the user authorises normalisation. Treat the predominant form in the text, or a
  form set by its typesetting configuration, as established; report minority variants as
  inconsistencies, and use the house choice when the text sets none.
- Keep official names, source identities, quotations, and technical literals in their governed
  forms; the applicable quotation or citation convention controls permitted transformations.
- Project, publisher, product, and explicitly selected style requirements control their stated
  scope. Apply language and general prose preferences to the remainder. Keep different languages and
  styles within their requested parts. Resolve overlapping rules by their authority and specific
  scope; report an unresolved conflict only for the work it affects.
- A registered source supports a rule only within its [recorded role and
  scope](references/source-manifest.md#evidence-roles).

## House choices

- **English:** For new English prose, use British spelling, including *-ise*/*-isation*, *licence*
  as a noun, and *practise* as a verb. Use [English core](references/english-core.md) for word
  choice and mechanics. Supplied prose keeps its dialect unless a change is requested.
- **Danish:** For new Danish prose, use [Danish core](references/danish-core.md), including omission
  of the optional start comma, a decimal comma, and a fixed space before `%`. Preserve machine
  formats.

## Hand over

Return the requested artefact when each requested change is made, the rules of each opened
reference are applied, and each finding of `scan_prose.py` is settled. When the user asks for the
artefact alone, begin the reply with its first line. When you report a check's result, also say
what the check exempted, because a pass says nothing about exempted text. Disclose missing
information or unavailable guidance that materially limits the result. Include method when the user
requests it.

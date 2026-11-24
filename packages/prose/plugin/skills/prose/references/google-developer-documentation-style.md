# Google developer documentation style guide

Apply this module to original English developer documentation for software developers and other
technical practitioners. It covers API references, quickstarts, tutorials, how-to guides,
command-line documentation, and code-adjacent explanations. A software mention alone does not
select this module.

The Google authorities in [source provenance](source-manifest.md) identify the consulted pages,
adaptation boundary, and licences.

## Source map

Use the [Google Page Register](google-developer-documentation-pages.csv) for page-level provenance,
scope, disposition, and revision dates. It is evidence, not a rule selector.

Use this module for Google product rules, Google style choices adopted by Prose, headings,
procedures, placeholders, and notices. For Google UI element names or targeted Google word-list
lookups, open [Google UI terminology and word-list lookup](google-developer-ui-and-word-list.md).
Use the relevant references for instruction structure, interface text, technical content, APIs,
quantities, accessibility, source handling, and citations.

## Authority and scope

- Apply host, product, and project style before this module.
- For a new page in an existing documentation set, treat a term form that the set uses consistently
  as project terminology, even when Google's word list gives another form. Use that form throughout
  the page. Keep code, identifiers, and paths in their source form.
- Apply the [authority and preservation rules](../SKILL.md#authority-and-preservation) when
  requirements conflict or a Google preference would change a source or host-owned form.
- Apply [British English spelling](english-core.md#british-english-spelling) and [requirement
  language](instructional-prose.md#requirement-language). These local rules replace Google's
  conflicting US English and modal guidance. Preserve the actor and semantic role when changing a
  modal. Consult the word-list decision when the modal choice remains unresolved.
- Apply the house rules that replace further Google guidance: the [technical-prose contraction
  rule](technical-and-code-prose.md#contractions), the [quotation-mark
  default](english-core.md#punctuation-and-mechanics) for the placement of full stops and commas,
  and the [house date form](number-forms.md#dates-and-times) for day, month, and year order.
- When ASD-STE100 is explicitly selected, apply its [adopted rules](asd-ste100.md) before a
  conflicting Google recommendation.
- Keep every product rule inside its named scope. Keep code, syntax, identifiers, paths, filenames,
  markup, and rendered components under their host or project authority.
  Assess a general clause separately from a product-marker clause.
- When Diátaxis is selected, let it set the user need, documentation type, and type boundaries.
  Apply Google developer-documentation style within those boundaries.
- Use the Google source designation in [terminology records](terminology-records.csv) when its
  concept applies; otherwise apply [Terminology selection](terminology-selection.md), keeping
  source, conventional, and descriptive terms distinct.

## Accessibility scope

- Treat Google's accessibility guidance as editorial support. Apply the governing WCAG version to
  a web-conformance claim and cite the applicable success criterion.
- Apply WAI-ARIA only within its semantic scope. Host rules control DOM, CSS, focus behaviour,
  native elements, keyboard handling, and implementation details.

## Google product, tool, API, and feature names

- Apply this section only when Google is the owning authority for the designation.
- For full names, permitted short forms, governed literals, and trademark use, apply
  [Official names, generic terms, and trademarks](terminology-selection.md#official-names-generic-terms-and-trademarks).
- Write a Google product name without *the* unless its official name includes *the* or it
  modifies something else, and put *the* before a Google tool or API name.

## Google style choices

- Keep the voice direct, conversational, friendly, and respectful. Centre the developer's task,
  not the author's personality.
- Use straight quotation marks and apostrophes in original prose.

## Headings, titles, and procedures

- Write a document title for the document's primary purpose. Write each section heading for the
  type of content in that section.
- Start a task-based heading with a bare infinitive.
- Use a noun phrase that does not start with an *-ing* verb for a conceptual or non-task-based
  heading.
- Begin a section heading with *Optional:* when the section is not required for all readers or
  scenarios, rather than placing *(optional)* after the heading.
- Write unnumbered section headings rather than numbering headings to show a sequence of sections,
  preserving numbered divisions only where a governing document format requires them.
- Write headings as unlinked text.
- Write headings in plain prose rather than code items; when a code item is necessary for
  findability or accuracy, put it in code font and add a descriptive noun.
- Format single-step procedures as one sentence in a bulleted list; this overrides the one-item
  prose default in [Lists, comparisons, and next
  steps](reader-core.md#lists-comparisons-and-next-steps) within Google developer documentation.
  Use a numbered list for two or more ordered steps.

## Prose around code samples, commands, and placeholders

- In non-reference content, use only the command arguments required to complete the task in the
  recommended way.
- Link to the command reference for the complete list of arguments.
- Provide a click-to-copy command example that runs without editing after copying (except for
  replacing placeholders), keeping it to required, single-choice arguments rather than optional,
  mutually exclusive, or repeated arguments.
- Use uppercase characters with underscore delimiters for placeholder text; this replaces the
  italics placeholder default in [Placeholders](technical-and-code-prose.md#placeholders) within
  Google developer documentation. If that form is a poor fit for the context, use another clear
  form and apply it consistently.
- Use a more informative placeholder instead of a single `x` or a series of `x` characters. Retain
  the series only when a governing standard or exact source syntax requires it.
- Name placeholders with neutral role or value words rather than possessive prefixes such as `MY_`
  or `YOUR_`.
- Explain a placeholder the first time it appears. After a command with multiple placeholders,
  list them in their order of appearance. Introduce the list with *Replace the following:*.
- Distinguish placeholders in sample code and commands from placeholders in example output. Tell
  readers to replace only placeholders in sample code and commands, describing any placeholder in
  example output as a value that varies rather than an input to set.
- After example output, explain its placeholders in their order of appearance. Introduce the list
  with *This output includes the following values:*.

## Notes, cautions, warnings, and other notices

- Use a *Note* only for relevant, non-critical information that readers can skip and still succeed.
- Keep cross-references, prerequisites, prior steps, complete procedural steps, and information
  needed for success in the main flow. Keep continuations of the preceding text, including expected
  results, in the main flow.
- Keep alternative paths and risks outside a *Note*.
- Reserve a *Success* notice for dynamic or interactive content to report a successful action or
  error-free status, leaving ordinary static pages without one.
- Apply [Warnings and cautions](instructional-prose.md#warnings-and-cautions) to *Caution* and
  *Warning* notices.
  Route an actual visible status message to [Errors, statuses, and empty
  states](interface-copy.md#errors-statuses-and-empty-states).

Complete this branch when host, project, British English, contraction, quotation-mark, date, and
requirement-language overrides hold, Google product and API names follow their article rules,
headings and procedures use bare-infinitive/noun-phrase and bulleted/numbered forms, placeholders
are uppercase with underscores and explained in order, and any UI-element or word-list check uses
[Google UI terminology and word-list lookup](google-developer-ui-and-word-list.md).

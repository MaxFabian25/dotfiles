# Google developer documentation style guide

Apply this module to original English developer documentation for software developers and other
technical practitioners. It covers API references, quickstarts, tutorials, how-to guides,
command-line documentation, and code-adjacent explanations. A software mention alone does not
select this module.

The Google authorities in [source provenance](source-manifest.md) identify the consulted pages and
their licences. This module contains independently worded adaptations. It does not claim full
conformity with Google's house style.

## Source map

Use the [Google Page Register](google-developer-documentation-pages.csv) for page-level provenance,
scope, disposition, and revision dates. It is evidence, not a rule selector.

Use this module only for Google product rules, Google style choices adopted by Prose, interface
terminology, and word-list choices. Use the relevant references for instruction structure,
interface text, technical content, APIs, quantities, accessibility, source handling, and citations.

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
- Apply the house rules that replace further Google guidance: the contraction rule under [Google
  style choices](#google-style-choices), the [quotation-mark
  default](english-core.md#punctuation-and-mechanics) for the placement of full stops and commas,
  and the [house date form](number-forms.md#dates-and-times) for day, month, and year order.
- When ASD-STE100 is explicitly selected, apply its [adopted rules](asd-ste100.md) before a
  conflicting Google recommendation.
- Keep every product rule inside its named scope. Keep code, syntax, identifiers, paths, filenames,
  markup, and rendered components under their host or project authority.
  Assess a general clause separately from a product-marker clause.
- When Diátaxis is selected, let it set the user need, documentation type, and type boundaries.
  Apply Google developer-documentation style within those boundaries.

## Accessibility scope

- Treat Google's accessibility guidance as editorial support. Apply the governing WCAG version to
  a web-conformance claim and cite the applicable success criterion.
- Apply WAI-ARIA only within its semantic scope. Host rules control DOM, CSS, focus behaviour,
  native elements, keyboard handling, and implementation details.

## Source and conventional terminology

Use the source designation when its concept applies. Otherwise, apply [Terminology
selection](terminology-selection.md). Keep source, conventional, and descriptive terms distinct.

| Scope | Preferred designation |
| --- | --- |
| Official title of the source work | Google developer documentation style guide |
| Documentation for software developers and other technical practitioners within the guide's scope | developer documentation |
| Language variety in which Google states that it writes its developer documentation | US English |
| Heading for task content | task-based heading |
| Grammatical form that starts a task-based heading | bare infinitive |
| Heading for conceptual or non-task content | conceptual or non-task-based heading |
| Grammatical form used for a conceptual or non-task-based heading | noun phrase |
| Procedures that consist of one step | single-step procedures |
| Example source code in developer documentation | code sample |
| Runnable command example intended for readers to copy | click-to-copy command example |
| Reference that supplies the complete list of arguments for a command | command reference |
| Text that readers replace in sample input or that represents a variable value in example output | placeholder |
| Named component of a user interface | UI element |
| Documentation that recommends a way to complete tasks and achieve goals | prescriptive documentation |

## Google product, tool, API, and feature names

- Apply this section only when Google is the owning authority for the designation.
- For full names, permitted short forms, governed literals, and trademark use, apply
  [Official names, generic terms, and trademarks](terminology-selection.md#official-names-generic-terms-and-trademarks).
- Do not put *the* before a Google product name unless its official name includes it or it
  modifies something else. Put *the* before a Google tool or API name.

## Google style choices

- Keep the voice direct, conversational, friendly, and respectful. Centre the developer's task,
  not the author's personality.
- Use straight quotation marks and apostrophes in original prose.
- Unlike Google, expand contractions in original technical prose. Write *let us* only when that
  meaning is needed.

## Headings, titles, and procedures

- Write a document title for the document's primary purpose. Write each section heading for the
  type of content in that section.
- Start a task-based heading with a bare infinitive.
- Use a noun phrase that does not start with an *-ing* verb for a conceptual or non-task-based
  heading.
- Begin a section heading with *Optional:* when the section is not required for all readers or
  scenarios. Do not put *(optional)* after the heading.
- Do not use numbers in headings to indicate a sequence of sections. Preserve numbered divisions
  required by a governing document format.
- Write headings as unlinked text.
- Avoid code items in headings. When a code item is necessary for findability or accuracy, put it
  in code font and add a descriptive noun.
- Format single-step procedures as one sentence in a bulleted list. Use a numbered list for two or
  more ordered steps.

## Prose around code samples, commands, and placeholders

- In non-reference content, use only the command arguments required to complete the task in the
  recommended way.
- Link to the command reference for the complete list of arguments.
- Provide a click-to-copy command example that requires no editing after copying, except for
  replacing placeholders. Avoid optional arguments, mutually exclusive arguments, and repeated
  arguments in click-to-copy examples.
- Use uppercase characters with underscore delimiters for placeholder text. If that form is a poor
  fit for the context, use another clear form and apply it consistently.
- Use a more informative placeholder instead of a single `x` or a series of `x` characters. Retain
  the series only when a governing standard or exact source syntax requires it.
- Do not include possessive adjectives such as `MY_` or `YOUR_` in a placeholder.
- Explain a placeholder the first time it appears. After a command with multiple placeholders,
  list them in their order of appearance. Introduce the list with *Replace the following:*.
- Distinguish placeholders in sample code and commands from placeholders in example output. Tell
  readers to replace only placeholders in sample code and commands. A placeholder in example
  output represents a value that varies. Do not instruct readers to set it.
- After example output, explain its placeholders in their order of appearance. Introduce the list
  with *This output includes the following values:*.

## Notes, cautions, warnings, and other notices

- Use a *Note* only for relevant, non-critical information that readers can skip and still succeed.
- Keep cross-references, prerequisites, prior steps, complete procedural steps, and information
  needed for success in the main flow. Keep continuations of the preceding text, including expected
  results, in the main flow.
- Keep alternative paths and risks outside a *Note*.
- Use a *Success* notice only in dynamic or interactive content. Report a successful action or
  error-free status with it. Do not use it on an ordinary static page.
- Apply [Warnings and cautions](instructional-prose.md#warnings-and-cautions) to *Caution* and
  *Warning* notices.
  Route an actual visible status message to [interface copy](interface-copy.md).

## Google UI terminology adapter

Apply [Interface instructions](interface-instructions.md) to the task, labels, input methods, and
interaction verbs. Apply this adapter only to Google developer documentation.

- Use *command* for an item in a desktop menu. Keep *menu item* only for documentation about
  interface construction.
- Express a menu path in prose or by using angle brackets (`>`). Format the entire sequence
  together. Use this notation only for a menu path. Do not use it to describe a combination of
  different UI elements. Use the host's method for making a screen reader interpret each angle
  bracket as *and then*.
- Use *window* for an entire desktop application or a modular element that readers can open and
  close.
- Use *page* for a web page or a subpage of a console. Use *dialog* for a smaller window that appears
  in front of the main application window.
- Use *pane* or *panel* for a distinct region inside a larger window. Use *section* for a labelled
  grouping of options and controls.
- Use *navigation menu* for a control that contains a list of items. The items take readers to pages
  in an application or website. Use *toolbar* for a set of buttons for common user actions.
- Use *menu button* for a button that opens a menu. Use *button* for a control that starts an action.
- Use *icon* for a symbol or image representing an object or function. Use *tab* for a navigation
  element that looks like a file tab.
- Use *box* for a text box, combo box, or spin box. Use *list* for a list box when clearer.
- Use *checkbox* for a small box that indicates whether an option is on or off. Use *radio button*
  for one option in a mutually exclusive group.
- Use *expander arrow* for the UI element used to expand or collapse a section. Use *toggle* for
  the UI element that switches between on and off states.
- In Google Cloud and Google Workspace documentation, use *field* instead of *box*. Elsewhere,
  follow host terminology.

## Word-list evidence

Use this module's recorded adopted guidance for ordinary drafting. When project terminology and
that guidance leave a material word choice unresolved, use the [entry
register](google-developer-documentation-word-list-entries.csv). Find the exact anchor, then use every
`decision_ids` value to open the matching row in the [decision
register](google-developer-documentation-word-list-decisions.csv).
Use fixed-string search and read only matching rows. Do not load either register end-to-end.

Treat `source_status` as evidence about Google's heading, not as a local adoption decision.
`unmarked` records no source icon. `caution` and `do-not-use` record the corresponding source
markers. Apply `product_markers` only inside each named product scope.

The decision categories identify adaptation, conflict, product or host scope, exact text routed out
of prose, a governing source, or an added local rule. Read the matching treatments before following
their destinations. Select only decisions whose conditions match the word's use in the current
passage. For each selected decision, use its first destination for the shared rule and each
additional destination only under its stated condition. Reuse sections already read. Apply unique
adoption treatments within the entry's scope and the authority order.

Consult the current official [word-list entry](https://developers.google.com/style/word-list) when
its exact clause is needed to resolve that choice. Report a discrepancy that changes the affected
recommendation or its scope, and continue authorised work that does not depend on it. Distinguish
recorded guidance from what the live source verifies. If no anchor applies, use [terminology
selection](terminology-selection.md). Keep an established conventional term instead of inventing a
paraphrase.

Preserve a term covered by *Don't use* or *Use with caution* guidance when code or an interface
requires it. Format it as exact text. Apply the entry's guidance about alternative terms in prose
thereafter. Keep Google product terminology inside its product scope.

For a requested rule audit, record each recommendation's controlling rule, scope, and material
source conflicts. When updating adopted guidance or recorded evidence, apply the [source
maintenance procedure](source-manifest.md#source-drift).

# Durable document structure

Apply this branch when a durable reader-facing document or page has structural divisions or
navigation. Start by identifying the intended readers, the simplest use case, and the route by
which readers find it.

- Keep each document brief and utilitarian. Include only the author's knowledge that intended
  readers need.
- Make the simplest use case easy to find. Keep optional or advanced detail from interfering with
  it.
- When search or scanning matters, place relevant keywords near the beginning of titles, headings,
  table entries, and paragraphs.
- Give readers at least one way to navigate within each long online document. Use a linked contents
  list or another method that exposes the document's sections.
- Move background or supplementary material that interrupts the main argument to an appendix or
  linked unit. Choose the form that keeps relevant material findable.
- Open an overview page with a brief introduction for newcomers. Explain the subject, purpose, and
  relevance.
- Use the host format's built-in styles for headings, tables, lists, notes, and tips. Apply each
  style consistently.
- Give each document one title heading, marked as the level-1 heading in Markdown or HTML. Order
  the other headings under [Presentation choices](reader-core.md#presentation-choices). Format a
  selected style's heading levels, such as APA Level 1, under that style.
- Give every heading unique text within its document.
- Add introductory text between a heading and its first child heading when readers need context or
  navigation that the headings do not supply.
- Keep each heading's link target descriptive and stable. Update every controlled inbound link when
  the target changes.

After a change to structure, headings, links, or navigation, render or build the document and read
the output where its readers receive it. Check its headings, lists, links, navigation, and generated
structure after rendering. Also read the headings and each paragraph's first sentence separately.
Together they must convey the argument without gaps or avoidable repetition.

Run every configured formatter, linter, link checker, and spell checker. Confirm that affected
documentation agrees with changed behaviour and interfaces.

Complete this branch when the simplest use case is findable and every structural division is
navigable. Each link reaches its current target. When rendering was required, the rendered document
agrees with its source. Every configured document check passes.

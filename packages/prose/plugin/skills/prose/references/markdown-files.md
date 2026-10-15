# Markdown files

Apply this branch when a Markdown file is written or reviewed. Host, project, dialect, and
formatter rules control their stated scope. Apply these portable defaults otherwise.

## Readable source text

- Keep essential information understandable without rendering.
- Prefer standard Markdown syntax to HTML.
- Use HTML only when the host supports it and Markdown cannot express the requirement.
- Keep content and presentation separate.
- Follow the project's character line limit. Use 80 characters only when the project selects it.
- Remove trailing whitespace.
- Use a trailing backslash for a hard line break. Prefer a paragraph when it expresses the
  structure.

## Headings

- Use ATX-style headings. Put one space after the `#` characters.
- Put a blank line before and after each heading.
- For document titles, heading text, and hierarchy, apply [Durable document
  structure](durable-document-structure.md).

## Table of contents

- Add a table of contents only when the host supports it and the document needs it.
- Place a table of contents after the introduction and before the first level-2 heading, so that
  screen-reader and keyboard users reach it early.

## Lists

- Use lazy numbering for long ordered lists that may change. Write `1.` for each item when the
  formatter supports it.
- Use explicit numbers when values, rankings, counts, or later references depend on the written
  number.
- Indent wrapped text and nested lists to preserve their intended structure in the target Markdown
  renderer.

## Code

- Use a code span when text must not be processed as normal Markdown or become a bad autolink.
- Use fenced code blocks for multiline code, commands, configuration, or output.
- Explicitly declare the language for every fenced code block.
- Use `text` or the formatter's equivalent for plain text or output.
- Prefer fenced code blocks to indented code blocks.
- Nest fenced code blocks within lists. Indent each block enough to preserve the list.
- Escape newlines in copyable command-line snippets as the target shell requires.

## Links

- Keep each link's exact destination.
- Use a reference link when an inlined destination harms source readability or when a destination
  repeats.
- Follow the formatter's placement rule for reference link definitions.
- Otherwise, put each reference link definition at the end of the section where it first appears.
- Put reference link definitions used in multiple sections at the document end.
- Follow the target host's rules for paths in Markdown links.
- Otherwise, use a path from the repository or site root, such as `/docs/setup.md`, unless the
  target is in the same directory.

## Verification

- Read the source text.
- Render the document with the target renderer and inspect the formatted output.
- Run the project's configured Markdown formatter or linter when one exists.

Complete this branch when the source text remains readable and portable. The rendered output must
preserve every heading, list, code block, link, and required navigation element.

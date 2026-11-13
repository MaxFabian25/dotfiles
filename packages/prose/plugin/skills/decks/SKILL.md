---
name: decks
description: House rules for slide decks and presentations, in PowerPoint, pptxgenjs, python-pptx, Beamer, Keynote, or Google Slides. Use when making, reviewing, migrating, or fixing a deck, alongside any general slides skill, including a visual pass, a change of font, text that overflows its box, and briefs for a fresh reviewer.
---

# Decks

Apply these house rules to every slide deck you make, review, or fix. They work alongside a general
slides skill's method, such as the one that builds the file, and where their defaults differ, these
rules decide. A project's or organisation's own deck style controls its stated scope. A style file
or lint that earlier agent work wrote in the user's own repository, such as a `DESIGN.md`, is a
draft under review: where it blocks what the brief asks for, say so at the first page it constrains
and ask the user whether to widen it.

[Charts](../charts/SKILL.md) governs every data display on a slide, and [Prose](../prose/SKILL.md)
the words on and around it.

## Content

- **Main slides and appendix.** For a deck someone will present, keep each main slide to one visual
  and the words that change a decision. Put the narration in the speaker notes. Move the detail to
  appendix slides that the main slide links to, in the form that suits it, such as a
  spreadsheet-style table, and label that section "Appendix" in the deck, its contents page and its
  footers. Moving detail keeps it in the deck for questions and for people who read it alone, where
  dropping it or leaving it to be said aloud loses it.
- **Explaining a topic.** A request to explain a topic or its options gets a general concept page
  first, such as local against cloud, and then the options compared. Say that you read the request
  this way when you first show the pages.
- **Comparison pages.** Name on the page whose options and criteria it compares, and ask the deck's
  owner whether that source binds the comparison, as part of the round of questions in Prose's
  [reader-core](../prose/references/reader-core.md). Test every option the deck names on the same
  criteria, or ask before you leave one out. Cite beside each row who asked for it, and mark each
  row you added yourself. When the criteria favour one kind of option, such as hosted services over
  local ones, say so on the page.
- **Judged grades.** When a page grades options against a condition, such as "Meets" or "Falls
  short", write each grade's condition once beside the data and derive every cell from it. Before a
  count of grades goes into a title, group the cells by condition and confirm that each group has
  one grade. Fix an uneven grade that a reviewer finds by reapplying the rule to every row: in
  November 2026 a worker regraded only the rows a reviewer named, and one page's headline count
  moved from 4 to 7 to 9 of 13 while every check passed.
- **Cuts that keep the claim.** When text overflows its box, check what each word you would cut
  carries, such as billed against used, an estimate against a count, or who covers for whom. When a
  cut would change the claim, change the layout instead: widen or split the box, or move the detail
  to the notes or an appendix slide. Reread each shortened line for its meaning as well as its fit.
- **A table that becomes a drawing.** Before you redraw a table or a grid of marks as a drawing,
  list what a reader could conclude from the old page, such as which option is cheapest or which
  falls short on which criterion, and check each item on the new page. In November 2026 redrawn
  pages kept the facts and lost the comparison: one showed only prices, one label covered three
  grades, and one colour meant four things.
- **A word alone on a line.** When a box's last line holds one word, join its last two words with a
  non-breaking space before you reword anything, and check every box in the presenting app's own
  render, not only the one someone flagged. Reword only when that fails, and check a swapped or
  added word against the claim as you would a cut.
- **The subject, not the making.** A slide speaks about its subject. Lines about the making include
  what was checked and when, where a figure came from beyond the display's source line, what the
  writer could not find out, text that explains how to read a drawing beyond the labels and legend
  that Charts asks for, caveats that repeat, hedges, worked calculations, a "?", and a "to test" or
  "to confirm" stamp. Move each to the speaker notes, or, when it changes the decision, state it as
  the fact or as a limit of the subject. Prose's
  [reader-core](../prose/references/reader-core.md) rule on what only the writing process needs
  covers the same lines in documents.

## Build

- **Native charts.** Draw each chart the presenting app can draw natively as a native chart, so the
  presenter can edit it and its data travels with it. A check can read a native chart's values from
  the chart part's XML, so checks are no reason to draw a chart as shapes, or to keep a table that
  should be a drawing. When the cost of moving checks limits which pages change, say so in the plan
  before you build. When you draw one as shapes or as a picture, tell the user in your reply what
  you did and why.
- **Fonts outside the app's set.** Before you switch a deck to a font the presenting app does not
  ship, and before you tune line breaks in a deck that uses one while the choice between embedding
  and installing it is still open:
  - list every character the deck uses that the font lacks, from the font's character map, for
    example with fontTools `TTFont(path).getBestCmap()`, and choose a replacement for each with the
    user;
  - decide with the user between embedding the font and installing it on every machine that opens
    the deck, because a machine without it shows another font with other widths;
  - judge whether the app has the font from the app's own render or export, such as `pdffonts` on
    its PDF, because system font lists can miss a font installed for one user.

## Review

- **Fresh reviewer.** Brief a fresh reviewer, such as a subagent or a model from another maker, to
  check each page's facts against its sources and the impression each drawing gives, as well as its
  form. On a comparison page, ask it for each criterion who stated it and where. Scripted checks
  catch the errors their author thought of, so tell the reviewer what they cover as context and ask
  it to check everything. Give it the rendered pages and the sources, and ask for each problem with
  its page and evidence.
- **Reader as the audience.** Brief a second fresh reviewer with the rendered pages alone, to read
  them as the audience will, as Prose's [reader
  testing](../prose/references/publication-verification.md#reader-testing) does, and to mark every
  line about the making.
- **A correction names a class.** When the user overrides a recommendation or repeats a request,
  such as drawings over tables, take it as naming a class of pages. Have a fresh reviewer sweep
  every page for that class before you fix the pages the user named, and include the option that
  grants the request in full in every later question on that subject.

## Looking at pages

Look at the pages as the presenting app draws them, on contact sheets of a dozen pages each, and
after a change only at the pages whose render changed. Open a page at full size where a check or a
sheet shows a problem. A full page costs about as much context as a sheet of twelve, so reading
every page at full size after every change fills the context and forces early compactions.

As soon as changed pages render, show them to the user in a message as images, with each reading of
the request you chose, before you go on to planted-error runs and evidence records. Do this also
when the user asked for no pages to approve: in November 2026 a user who had started the thread
from a phone got a file path, saw the pages only after asking for them, and reversed two of the
readings that a 4.5-hour pass had chosen without asking.

For a generated deck with its own checks, keep the design, the renders and the logged result lines
in your own context, and have a worker write the page code and check code, one committed unit at a
time. Offer the user a new thread once a pass is pushed. In November 2026 a deck thread that held
page code, renders and check output compacted nine times, and none once it handed each unit to a
worker.

Complete the work when every slide meets each rule above and Charts and Prose govern their parts,
you have looked at each changed page as the presenting app draws it, and the user has seen the
changed pages and your chosen readings.

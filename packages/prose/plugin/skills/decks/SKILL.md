---
name: decks
description: House rules for slide decks and presentations, in PowerPoint, pptxgenjs, python-pptx, Beamer, Keynote, or Google Slides. Use when making, reviewing, migrating, or fixing a deck, alongside any general slides skill, including a visual pass, a change of font, text that overflows its box, and briefs for a fresh reviewer.
---

# Decks

Apply these house rules to every slide deck you make, review, or fix. They work alongside a general
slides skill's method, such as the one that builds the file, and where their defaults differ, these
rules decide. A project's or organisation's own deck style controls its stated scope.

[Charts](../charts/SKILL.md) governs every data display on a slide, and [Prose](../prose/SKILL.md)
the words on and around it.

## Content

- **Main slides and backup.** For a deck someone will present, keep each main slide to one visual
  and the words that change a decision. Put the narration in the speaker notes. Move the detail to
  backup slides that the main slide links to, in the form that suits it, such as a spreadsheet-style
  table. Moving detail keeps it in the deck for questions and for people who read it alone, where
  dropping it or leaving it to be said aloud loses it.
- **Cuts that keep the claim.** When text overflows its box, check what each word you would cut
  carries, such as billed against used, an estimate against a count, or who covers for whom. When a
  cut would change the claim, change the layout instead: widen or split the box, or move the detail
  to the notes or a backup slide. Reread each shortened line for its meaning as well as its fit.
- **A word alone on a line.** When a box's last line holds one word, join its last two words with a
  non-breaking space before you reword anything, and check every box in the presenting app's own
  render, not only the one someone flagged. Reword only when that fails, and check a swapped or
  added word against the claim as you would a cut.

## Build

- **Native charts.** Draw each chart the presenting app can draw natively as a native chart, so the
  presenter can edit it and its data travels with it. A check can read a native chart's values from
  the chart part's XML, so checks are no reason to draw a chart as shapes. When you draw one as
  shapes or as a picture, tell the user in your reply what you did and why.
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
  form. Scripted checks catch the errors their author thought of, so tell the reviewer what they
  cover as context and ask it to check everything. Give it the rendered pages and the sources, and
  ask for each problem with its page and evidence.

## Looking at pages

Look at the pages as the presenting app draws them, on contact sheets of a dozen pages each, and
after a change only at the pages whose render changed. Open a page at full size where a check or a
sheet shows a problem. A full page costs about as much context as a sheet of twelve, so reading
every page at full size after every change fills the context and forces early compactions.

Complete the work when every slide meets each rule above and Charts and Prose govern their parts,
and you have looked at each changed page as the presenting app draws it.

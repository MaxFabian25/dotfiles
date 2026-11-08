---
name: planted-error-checks
description: Prove that a set of checks catches real errors by planting known errors in copies and running every check on each copy. Use for checks on a generated document, such as a deck, report, site, or data export, and for a code repository's test suite. Use when writing or trusting fact, figure, link, or layout checks, or a checker, scorer or answer key, before you call one complete, when testing one on defective copies, when checks pass but a reviewer, user or production still finds errors, when deciding which tests earn their keep or whether to delete a class of tests, and when someone asks for mutation testing of a document build or a test suite.
---

# Planted-error checks

A passing check suite proves only that the checks found nothing. Plant known errors in copies of
the document and run every check on each copy. A check that stays green on a planted error is
blind to it.

## Write checks that can fail

- **Bind each check to its place.** Check a value in the shape, cell, element, or field that shows
  it, together with the words that name its scope, such as the plan, period, or row. A check that
  looks for the expected words anywhere on the page passes when a correct copy elsewhere, in the
  notes, a second label, or a quote about something else, vouches for a wrong value.
- **Keep expected values apart from the build's inputs.** Give the checks their own constants or
  their own answers file. A check that reads the file the build reads passes a wrong value planted
  in that file.
- **Check each source quote against its scope.** When a check confirms a claim from a saved source,
  match both the quote and the subject it is about, such as the tier or product, because a quote
  true of one tier passes for another.

## Plant errors

1. List the errors people actually make, from three places:
   - the source data, such as a wrong figure, a dropped row, or a reworded note;
   - the generator code, such as a wrong colour, a moved shape, or a broken link;
   - the finished file, typed over the way someone editing it in its app would.

   Add every error a reviewer or the user has found. Plant each one where it really arises, because
   an error planted in generated output is often caught by a different check than the one meant to
   catch it.
2. Make each plant one small change that names what it changes. Have it fail when its target is
   missing, such as a string replace that raises when the old text is absent, and report that plant
   as NOT APPLIED.
3. Build each copy and compare its output with a clean build of the same tree. A plant that leaves
   the output unchanged is a NO CHANGE plant, and it proves nothing.
4. Include at least one clean copy with nothing planted for each check path, such as the fast path
   and a real-render path. Every clean copy must pass, which proves the checks do not fail on
   everything.

## Run

- Run each plant in its own copy of the project, so one plant cannot leak into another. Copy the
  sources the checks read into the copy, or freeze them for the run, because a symlinked source
  edited during the run changes every copy at once.
- Run copies in parallel where the tools allow it, and one at a time for a tool that holds one
  instance, such as desktop PowerPoint.
- Write the results to a folder that survives a restart, such as the project's verification folder.
  On WSL, `/tmp` is emptied at every boot.
- For a new check, first plant its error with the check absent and see it pass, then add the check
  and see it fail. A check you never saw fail may be unable to.

## Test suites

The same method shows which tests in a code repository earn their keep. The source code is where the
faults go, and each test is a check.

- Choose faults at random from points in the code, such as a flipped comparison, a negated condition
  or a dropped return value, so the sample holds more than the faults you thought of. Record which
  tests fail for each one.
- Judge a class of tests by the faults only it caught, as well as by its time.
- Run each code fault in a sandbox with a read-only root and no network, such as
  `bwrap --ro-bind / / --dev /dev --proc /proc --tmpfs /tmp --bind <copy> <copy> --unshare-all --die-with-parent`,
  with `HOME` at a folder of its own, because a fault can flip a guard in code that manages real config.
- Run the clean copy in that sandbox first and leave the tests that fail there out of every score,
  because the sandbox made them fail. Add one copy with a crash planted, which proves the harness
  sees a failure.
- `maintenance/ci-census/mutate.py` in the private dotfiles repository does this for the public
  dotfiles suite.

## Report

Report CAUGHT, MISSED, NOT APPLIED and NO CHANGE separately, and the clean copies' result. The score
is caught out of the plants that applied and changed the output. Fix or remove each NOT APPLIED and
NO CHANGE plant before you quote a score.

Say what the score cannot measure: errors nobody planted, and whether a page gives a fair impression.
Pair the checks with a fresh reviewer who checks facts and impressions, and turn each error that
reviewer finds into a new plant.

The work is complete when every plant applied and changed the output, every clean copy passed,
every missed plant has a fixed check, and the results are saved with the date they ran.

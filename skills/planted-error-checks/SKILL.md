---
name: planted-error-checks
description: Planted errors and mutation testing of checks and test suites. Use before you quote an "N of N caught" score or decide what counts as caught, break code by hand to see a test fail, or run or change a planted-error harness, when you build or trust a guard, hook, monitor, acceptance gate, lint, probe, checker, scorer or answer key, or the fact, figure, link or layout checks on a deck, report, site or export, before you call such a check or a test suite complete, when checks pass but errors still reach a reviewer, user or production, and to decide which tests earn their keep.
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
2. Make each plant one small change that names what it changes and the check it is aimed at, such
   as in its label for `plant.py`. Have it fail when its target is missing, such as a string replace
   that raises when the old text is absent, and report that plant as NOT APPLIED.
3. Build each copy and compare its output with a clean build of the same tree. A plant that leaves
   the output unchanged is a NO CHANGE plant, and it proves nothing.
4. Include at least one clean copy with nothing planted for each check path, such as the fast path
   and a real-render path. Every clean copy must pass, which proves the checks do not fail on
   everything. A check that judges live input, such as a hook that denies, a monitor or a scorer,
   passes a clean copy its author made too easily, so take its clean side from outside your own
   work: a replay over real history, the fixes its own deny message recommends, a reviewer's
   near-misses, and correct outputs made by someone who never saw the check. Each wrong alarm on
   that clean side is a clean copy that failed, so fix the check, and report the wrong alarms out of
   the clean cases beside the caught count.

## Run

- Run each plant in its own copy of the project, so one plant cannot leak into another. Copy the
  sources the checks read into the copy, or freeze them for the run, because a symlinked source
  edited during the run changes every copy at once.
- Before a run of more than a handful of plants, read the estimate that `plant.py` prints once the
  unchanged copy passes, its wall and CPU time multiplied by the number of plants, and the load
  warning it prints at start. With another tool, time one plant yourself and multiply. Choose the
  number of workers and the machine from that total, and, for another tool, whether to start the
  run with `agent-job`.
- Run copies in parallel where the tools and the machine's load allow it, and one at a time for a
  tool that holds one instance, such as desktop PowerPoint. On a machine that other sessions or CI
  runners share, cap the number of workers and run them at low priority, such as under
  `nice -n 10`, or move a run of many plants to the Omarchy laptop with the `offload-to-omarchy`
  skill.
- While you iterate, rerun only the plants your edits touch, by name: those whose plant, check,
  page or planted source lines changed. To run only the tests of each plant's behaviour, give each
  group of plants that share a behaviour its own `plant.py` call with `-- -k NAME`, because the
  arguments after `--` go to every plant in that call. Start the full run only when nothing can
  still change what it reads: the user or a reviewer has seen the output, the last review's fixes
  are in, and no open question, probe or answer you wait for can change the files.
- Write the results to a folder that survives a restart, such as the project's verification folder.
  On WSL, `/tmp` is emptied at every boot. A restart of the agent app also cancels the shell tool's
  background jobs, so start every `plant.py` run, and any other run longer than a few minutes, with
  `agent-job start NAME -- COMMAND` and wait with `agent-job wait NAME --timeout SECONDS` in
  background mode, as `~/dotfiles-private/config/agent-rules/waiting.md` says, or run it on the
  laptop with `omarchy-run start`.
- For a new check, first plant its error with the check absent and see it pass, then add the check
  and see it fail. A check you never saw fail may be unable to.
- When you brief a builder or subagent to prove its tests or checks, give it this skill's
  `SKILL.md` path, so that it plants with this method and `scripts/plant.py` instead of by hand.

## Test suites

When planting faults into a code repository's test suite (random mutations, sandboxed runs with `bwrap`, `mutate.py`, diff-derived fault lists, or running `scripts/plant.py`), read [references/test-suites.md](references/test-suites.md).

## Report

Once the plant run finishes and before quoting any score or closing a MISSED, CRASHED, TIMED OUT or "aimed check did not fire" result, read [references/reporting.md](references/reporting.md).

The work is complete when every plant applied and changed the output, every clean copy passed,
every plant is caught, closed as [references/reporting.md](references/reporting.md) says or a crash reported as "aimed check did
not fire", and the results are saved with the date they ran.

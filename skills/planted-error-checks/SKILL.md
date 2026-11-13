---
name: planted-error-checks
description: Planted errors and mutation testing of checks and test suites. Use before you quote an "N of N caught" score or decide what counts as caught, before you break code by hand to see a test fail, before you run or change a planted-error harness, when you build or trust a hook or guard that denies, a monitor, an acceptance gate, a lint or browser probe for a redesign or migration, a checker, scorer or answer key, or the fact, figure, link or layout checks on a deck, report, site or data export, and before you call such a check or a test suite complete. Use also when checks pass but a reviewer, user or production still finds errors, and when you decide which tests earn their keep or whether to delete a class of tests.
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
- Before a run of more than a handful of plants, time one plant's CPU and wall-clock cost and
  multiply it by the number of plants. Choose the number of workers, the machine, and whether to
  start the run with `agent-job`, from that total.
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
  background jobs, so start a run longer than a few minutes with `agent-job start NAME -- COMMAND`
  and wait with `agent-job wait NAME --timeout SECONDS` in background mode, as
  `~/dotfiles-private/config/agent-rules/waiting.md` says, or run it on the laptop with
  `omarchy-run`.
- For a new check, first plant its error with the check absent and see it pass, then add the check
  and see it fail. A check you never saw fail may be unable to.
- When you brief a builder or subagent to prove its tests or checks, give it this skill's
  `SKILL.md` path, so that it plants with this method and `scripts/plant.py` instead of by hand.

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
- Build the list of faults you choose yourself from the diff, with one plant for each of these:
  each branch the change adds; each failure of each call that leaves the process, such as a
  subprocess, the network or a file another program writes; each point where the code can be
  killed, refused or throw, with a test that asserts a non-zero exit and leftovers that the next run
  cannot read as success; and, for each new guard, a swapped argument of the same type, a changed
  constant, a member dropped from a list the code reads, a statement moved past its neighbour, and a
  secret added to an output.
- For the faults you choose yourself, run `python3 -I
  scripts/plant.py SOURCE TEST PLANTS_JSON [ROOT] [-- TEST_ARGUMENT...]` from this skill's folder.
  The JSON file lists `[label, old, new]` plants. Each runs TEST in its own copy of ROOT, without a
  sandbox, after the unchanged copy has passed, and prints as CAUGHT, MISSED, TIMED OUT or NOT
  APPLIED. ROOT must hold every file TEST reads, or the unchanged copy fails and the script prints
  the end of that run. Everything after `--` goes to TEST as its arguments, such as `-- -k finish`
  to run only some tests of a unittest file. Write `{fresh}` in an argument for a TEST that needs a
  folder that does not exist yet, such as `-- {fresh}`: each run gets the path of a new one, which
  is removed when the run ends. A CAUGHT line names the tests that failed, from unittest's `FAIL:`
  and `ERROR:` headings in a unittest run and otherwise from lines a TEST prints as `FAIL`, two
  spaces and the check's name: check that they are the tests of the planted behaviour, because a
  test that fails for another reason, such as one that fails now and then on a busy machine, is
  printed as CAUGHT too.

## Report

Report CAUGHT, MISSED, TIMED OUT, NOT APPLIED, NO CHANGE and "aimed check did not fire" separately,
and the clean copies' result. The score is caught out of the plants that applied and changed the
output. Fix or remove each NOT APPLIED and NO CHANGE plant before you quote a score.

A plant that names the check it is aimed at, as step 2 asks, counts as CAUGHT only when that check
fails. `plant.py` prints CAUGHT for any non-zero exit, a crash included, and counts every such line
in its closing "N of M plants caught" line and its exit status, so read the test names on each
CAUGHT line and count the score yourself. A CAUGHT line names at most three tests, so when the aimed
test is not among them, rerun that plant with `-- -k` and the test's name. When another check, a
build guard or a crash catches the plant instead of the aimed check, or stops the run before the
aimed check runs, report it as "aimed check did not fire", outside the caught count. Rerun the same
plant in a copy without the check or guard that caught it. The plant is CAUGHT when the aimed check
then fails, and MISSED when it passes, so close it as the MISSED paragraph below says. A crash
leaves nothing to remove, so it stays "aimed check did not fire". Never edit a plant or a record so
that the tally reads N of N. Count a TIMED OUT plant from `plant.py` as not caught, and close it as
the MISSED paragraph says, such as with a test that fails within a time limit.

A fault chosen at random names no check, so count it as caught when any test fails, and say beside
its score how its tool counted a crash or a time-out, because `mutate.py` lists both among a fault's
failing tests.

Name who chose the plants beside every score. A score on plants the author chose shows only that
the tests can fail, so quote it beside the score of a random or reviewer-chosen sample, or label it
as the author's own.

A MISSED plant shows that the tests would let that fault through. It says nothing about what the
code does today until you run the unchanged code on an input that reaches the planted line. Close
each MISSED plant in one of four ways: with a test that catches it; by deleting the code when it is
dead, which no caller or input reaches, or redundant with code that a test covers; with a recorded
reason when the fault cannot change what a user sees; or, past the stop point in the reviewer
paragraph below, on the recorded list that paragraph names.

Say what the score cannot measure: errors nobody planted, and whether a page gives a fair impression.
Quote a score only after one run of the change over real input of its kind, in the state you found
it, such as the whole journal, every line shape the real data holds, or the real app. When that run
cannot happen, write "not run live" in place of the score. For a filter that withholds, such as a
credential filter, also run the base and the new version over the same real input and compare what
each withholds. A score earned on stand-in inputs, such as fixtures or a saved copy of a page,
carries the label "stand-in only, last run live on DATE".

Pair the checks with a fresh reviewer who checks facts and impressions. Before the first round,
write down that, until the stop point, a reviewer's plant the tests miss is closed with a test, or
by deleting dead or redundant code as the MISSED paragraph says, when it changes what a user sees on
an input that occurs. The stop point comes when none of the author's
plants or one reviewer's plants is left open: each one is caught, closed in one of the MISSED
paragraph's first three ways, or reported as "aimed check did not fire" after a crash. Past it, put
each later gap that only a new plant shows on a recorded list, unless it shows behaviour the code
gets wrong today.

The work is complete when every plant applied and changed the output, every clean copy passed,
every plant is caught, closed as the MISSED paragraph says or a crash reported as "aimed check did
not fire", and the results are saved with the date they ran.

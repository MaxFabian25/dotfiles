# Scoring and reporting planted-error runs

Read this once a planted-error run finishes, before quoting any score or closing a MISSED, CRASHED, TIMED OUT or "aimed check did not fire" result.

Report CAUGHT, CRASHED, MISSED, TIMED OUT, NOT APPLIED, NO CHANGE and "aimed check did not fire"
separately, and the clean copies' result. The score is caught out of the plants that applied and changed the
output. Fix or remove each NOT APPLIED and NO CHANGE plant before you quote a score.

A plant that names the check it is aimed at, as step 2 asks, counts as CAUGHT only when that check
fails. `plant.py` prints CRASHED for a run that fails without naming a failed test, such as a plant
that breaks the import, and leaves it out of its count. It prints CAUGHT for any other non-zero
exit, an error inside a test or a test that fails now and then on a busy machine included, and
counts every such line in its closing "N of M plants caught" line and its exit status, so read the
test names on each CAUGHT line and count the score yourself. A CAUGHT line names at most three tests, so when the aimed
test is not among them, rerun that plant with `-- -k` and the test's name. When another check, a
build guard or a crash catches the plant instead of the aimed check, or stops the run before the
aimed check runs, report it as "aimed check did not fire", outside the caught count. Rerun the same
plant in a copy without the check or guard that caught it. The plant is CAUGHT when the aimed check
then fails, and MISSED when it passes, so close it as the MISSED paragraph below says. A crash, a
CRASHED plant included, leaves nothing to remove, so it stays "aimed check did not fire". Never edit
a plant or a record so that the tally reads N of N. Count a TIMED OUT plant from `plant.py` as not caught, and close it as
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

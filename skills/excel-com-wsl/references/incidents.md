# Incidents behind this skill's rules

A dated record of what happened before a rule in `SKILL.md` was written. The rule, and any test date a sentence about Excel or the scripts needs, stays in `SKILL.md`; [ref-parsing.md](ref-parsing.md) and [error-scan-history.md](error-scan-history.md) keep what each release changed for callers. Read this before you propose changing or dropping one of those rules.

| Rule | What happened |
| --- | --- |
| A read that failed fails the job | Before 12 November 2026 such a job exited 0 with `ok: true` and showed the failure only in that range's `read_error` and the summary's `read_errors`. On that day the `unread` case of `test_fixtures.py` ended `ok: true` on the script as it was, before the change that fails such a job. |
| Delete each job's Office logs once its Excel has ended | On 13 November 2026 the log files took C: down to 162 MB free. |
| `test_fixtures.py` keeps each job's stderr as `NAME.stderr` | Until 13 November 2026 the suite dropped it: one of 21 runs the day before ended at the busy case with `no job folder for busy.xlsx appeared`, an empty `busy-2.out` and no `busy-2.json`, and nothing recorded why. The stderr check came later that day, and a planted defect showed that the suite before it missed such a job. |

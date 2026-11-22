# Incidents behind this skill's rules

A dated record of what happened before a rule in `SKILL.md` was written. The rule stays in `SKILL.md`, and so does any test date a sentence there needs, apart from the test runs in the last section, which moved here with [testing.md](testing.md); [ref-parsing.md](ref-parsing.md) and [error-scan-history.md](error-scan-history.md) keep what each release changed for callers. Read this before you propose changing or dropping one of those rules.

| Rule | What happened |
| --- | --- |
| A read that failed fails the job | Before 12 November 2026 such a job exited 0 with `ok: true` and showed the failure only in that range's `read_error` and the summary's `read_errors`. On that day the `unread` case of `test_fixtures.py` ended `ok: true` on the script as it was, before the change that fails such a job. |
| Delete each job's Office logs once its Excel has ended | On 13 November 2026 the log files took C: down to 162 MB free. |
| `test_fixtures.py` keeps each job's stderr as `NAME.stderr` | Until 13 November 2026 the suite dropped it: one of 21 runs the day before ended at the busy case with `no job folder for busy.xlsx appeared`, an empty `busy-2.out` and no `busy-2.json`, and nothing recorded why. The stderr check came later that day, and a planted defect showed that the suite before it missed such a job. |
| Double-quoted `REF` shell probes and startup PID wait | On 12 November 2026 bash probes verified `--read "'It''s'!B2"`, interactive bash `!` history expansion, and a 60 s startup wait after killing PowerShell early left a hidden Excel running; the error scan also replaced `SpecialCells` that day. On 13 November 2026 bash 5.2 turned `"'a=b'!$A$1=5"` into `'a=b'!=5`. |

## Test runs moved from SKILL.md

### A read that failed fails the job

On 12 November 2026 the `unread` case of `test_fixtures.py`, which reads a name the workbook lacks beside one it has, ended with status 1, that `error`, the other name's value and the saved copy ([incidents](../references/incidents.md) has the earlier behaviour).

### An Excel with `/restore` on its command line is the user's to close

On 13 November 2026 the busy case's Excel crashed in `combase.dll` at its quit after 63 s, and 8 s later Windows started such an Excel with a window and no workbook, which stayed open for hours and which no session took for a job's.

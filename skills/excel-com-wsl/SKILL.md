---
name: excel-com-wsl
description: Open, recalculate, read, change and save an .xlsx in desktop Excel on Windows from WSL, over COM through powershell.exe. Use when a workbook must be checked as Excel itself calculates it, such as openpyxl output with no cached values, results LibreOffice may calculate differently, whether a named input still drives a result, or whether Excel must repair the file, and before writing or debugging PowerShell that drives Excel's COM objects, such as a script failing with 0x800A03EC, a cast error or a calculation timeout.
---

# Excel over COM from WSL

A clean LibreOffice recalculation doesn't show that Excel opens a file without a repair or calculates the same numbers. Desktop Excel on the Windows host is the authority. Drive it over COM from WSL with Windows PowerShell.

## Check a workbook

```sh
python3 -I scripts/excel.py model.xlsx --read Result --read 'Model!B2:B4' --set Growth=0.1 --save baseline.xlsx
```

`REF` is a workbook-scoped name or a `Sheet!A1` range, and names one cell or one rectangle. A sheet name goes in single quotes unless it holds only letters, digits and underscores, as in `'My sheet'!A1`, and each apostrophe in a quoted name is doubled, as in `'It''s'!B2`. The letters can be of any script, with their accents, so `Økonomi!A1` needs no quotes. In a shell script or an agent's shell, put a quoted `REF` in double quotes, as in `--read "'It''s'!B2"`, which one `excel.py` job read through bash on 12 November 2026. An interactive bash takes the `!` in double quotes for a history reference and runs nothing, as a probe in bash 5.2 showed that day. Inside double quotes a shell also takes the `$` of an `address` such as `'It''s'!$B$2` for a variable, so leave each `$` out, because `B2` names the same cell, or write it as `\$`: a probe in bash 5.2 on 13 November 2026 turned `"'a=b'!$A$1=5"` into `'a=b'!=5`. `--set REF=VALUE` takes a number, `true`, `false`, `null` or text, and `--set REF==0.1` writes the formula `=0.1`. The `REF` ends at the first `=`, or at the first `=` after the quoted sheet name it opens with, so `--set "'a=b'!A1=5"` sets `A1` on the sheet `a=b`.

The script copies the workbook and `scripts/excel-job.ps1` into a job folder in the Windows user's temp folder and runs them there. Excel gets a hidden instance of its own, with macros, link updates, events and alerts off. It opens the copy, recording any repair, rebuilds the calculation and reads each `REF`. With `--set` it changes those cells, recalculates and reads again. It saves the copy, reopens it, reads once more and quits, then the script deletes the job folder.

For how the job waits for a busy Excel, and what `busy_retries` and a `read_error` of `Excel gave no answer for …` mean, read [references/busy-excel.md](references/busy-excel.md).

It prints JSON, or writes it to `--json OUT.json` and prints one line:

| Field | Holds |
|---|---|
| `open` | `load` (`normal`, `repair` or `extract data`), `repaired`, `failed_loads`, `external_links` |
| `settings` | calculation mode as opened, iteration, force full calculation |
| `base`, `after`, `reopened.values` | each `REF` at open, after the sets, and after the save and reopen: its `address`, such as `'Model'!$B$4`, and `value`, `text`, `formula`, or `values` for a range |
| `ok` | true when Excel finished the job, every read gave an answer and the error scan could tell for every sheet |
| `errors` | the cells holding an error value that a formula made, by sheet |
| `errors_unscanned` | the sheets whose error cells the scan could not list, each with its `sheet` and the `reason` |
| `error`, `error_line` | why the job failed, and the line of `excel-job.ps1` that stopped it, if one did |
| `busy_retries` | how many times a call waited for a busy Excel |
| `runner` | `timed_out`, and `stopped`, the processes the script tried to stop, each with its outcome |
| `excel` | Excel's `version`, `build`, `os`, `pid`, its Office log `session`, and a `session_reason` when the job found none |
| `office_logs`, `runner.office_logs` | the Office log files of the job's Excel session that the job deleted, or that the script deleted after it stopped that Excel: `session`, `removed`, `bytes`, `left`, and a `reason` when it left any, found none or found no session |
| `timings_ms` | elapsed time at each step |

An error value reads as `{"error": "#DIV/0!"}`. The exit status is 0, with `ok` true, when Excel finished the job, every read gave an answer and the error scan could tell for every sheet. Otherwise it is 1 and `error` says why, so a damaged file exits 1 with its `failed_loads` filled in. `--job-timeout` (300 s) stops only this job's Excel and PowerShell, and `--calc-timeout` (120 s) limits each calculation and each call's wait for a busy Excel, so several long waits can add up to the job's limit. When the job's PowerShell ends without a result, the script stops that job's Excel by its process ID, and `runner.stopped` lists it. A limit that falls while Excel is still starting waits up to 60 s for that process ID, because ending PowerShell first left a hidden Excel running in a fixture run on 12 November 2026.

A read that failed, such as a `REF` the workbook lacks, fails the job. `error` says how many reads failed and names the first, as in `2 of 4 reads failed; the first is NoSuchName at open: …`, where a `REF` that fails at open and after the reopen counts twice. The reads that worked keep their values in `base`, `after` and `reopened.values`, and `--save` still keeps the copy, because Excel finished it. So a caller that tolerates a bad reference has to read the ranges of a failed job.

A `REF` reads as follows. [references/ref-parsing.md](references/ref-parsing.md) keeps what each release changed for callers and the runs that showed it: read it before you change how `excel-job.ps1` or `excel.py` takes a `REF` apart or looks it up, and when a `REF` fails or reads otherwise than this list says or than an older script read it.

The list is under "How a REF reads now" in [references/ref-parsing.md](references/ref-parsing.md): read it before you copy an `errors` entry or an `address` back as a `REF`, and when a `REF` names a sheet in quotes or with letters outside A to Z, a name scoped to a sheet, several rectangles, a table column, or a name with no range.

For what `errors` lists, how the scan asks Excel and when a sheet goes into `errors_unscanned`, read "What the scan does now" in [references/error-scan-history.md](references/error-scan-history.md).

The scan replaced the `SpecialCells` listing of the releases before the last one of 12 November 2026. What a caller sees changed, and the cases that showed it through real Excel and on stand-ins, are in [references/error-scan-history.md](references/error-scan-history.md): read it when you compare a result with one from before that release, or change the scan.

`--save` keeps the copy Excel wrote. A workbook from openpyxl has no cached values until Excel or another engine saves it, so check and deliver that copy when readers need numbers without recalculating.

Office leaves four log files of each Excel session in the Windows user's temp folder, 80 MiB after a crash or a stop, enough to fill C: over a run of jobs ([incidents](references/incidents.md)). So once the job's Excel has ended, `scripts/office-logs.ps1` deletes that session's files and no others. `office_logs`, and `runner.office_logs` for an Excel the script had to stop, say what it deleted, and a failure is a `reason` there and in `excel.session_reason`, which fails no job. Read [references/office-logs.md](references/office-logs.md) when one of them gives a `reason` or `left` files, before you change `office-logs.ps1`, and before your own script starts Excel.

Excel runs in the user's interactive Windows session, which Microsoft doesn't support for unattended automation. The time limit and the stop of this job's own process are the guard, so run one job at a time.

Before a job, take the `windows-desktop` claim that the `work-across-tailscale-machines` skill asks for, with a note that says what the job leaves free, so that another thread does not wait on it to use the screen: `--note 'excel-com-wsl: a hidden Excel over COM; the foreground and the clipboard stay free'`.

## Prove a change to the scripts

```sh
python3 -I scripts/test_fixtures.py EVIDENCE_DIR
```

It needs `openpyxl`, builds workbooks that have each tripped this COM layer, runs them through real Excel and prints PASS or FAIL per check.

Run it after any change to a script, and before you run it read [references/testing.md](references/testing.md), which says how to run it as a job of its own, what `EVIDENCE_DIR` keeps and what its cases check.

An agent in a sandbox reaches Excel through a file queue that a runner outside the sandbox serves. Run these same cases through that queue from inside the sandbox, because a run from outside doesn't exercise the agent's route.

## Rules for your own scripts

Before writing or debugging your own PowerShell script that drives Excel over COM (process ownership, COM release order, busy-Excel message filter, `CorruptLoad`, `InvokeMember` typing, `CalculationState`, error codes, or UTF-8 JSON output), read [references/own-scripts.md](references/own-scripts.md).

The job is done when the results are saved outside the job folder, the job folder is gone, the Office logs of its Excel's session are gone, and the `EXCEL` process count is back to what it was before the job.

Before you change or drop one of these rules, read [references/incidents.md](references/incidents.md) for the dated runs behind it.

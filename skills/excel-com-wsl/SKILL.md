---
name: excel-com-wsl
description: Open, recalculate, read, change and save an .xlsx in desktop Excel on Windows from WSL, over COM through powershell.exe. Use when a workbook must be checked as Excel itself calculates it, such as one written by openpyxl with no cached values, a model whose results LibreOffice may calculate differently, a check that a named input still drives the result, or a question of whether Excel has to repair the file. Also use before writing or debugging PowerShell that drives Excel's COM objects, or when such a script fails with 0x800A03EC, a cast error or a calculation timeout.
---

# Excel over COM from WSL

A clean LibreOffice recalculation doesn't show that Excel opens a file without a repair or calculates the same numbers. Desktop Excel on the Windows host is the authority. Drive it over COM from WSL with Windows PowerShell.

## Check a workbook

```sh
python3 -I scripts/excel.py model.xlsx --read Result --read 'Model!B2:B4' --set Growth=0.1 --save baseline.xlsx
```

`REF` is a workbook-scoped name or a `Sheet!A1` range. A sheet name goes in single quotes unless it holds only the letters A to Z, digits and underscores, as in `'My sheet'!A1`, and each apostrophe in a quoted name is doubled, as in `'It''s'!B2`. In a shell script or an agent's shell, put such a `REF` in double quotes, as in `--read "'It''s'!B2"`, which one `excel.py` job read through bash on 12 November 2026. An interactive bash takes the `!` in double quotes for a history reference and runs nothing, as a probe in bash 5.2 showed that day. `--set REF=VALUE` takes a number, `true`, `false`, `null` or text, and `--set REF==0.1` writes the formula `=0.1`.

The script copies the workbook and `scripts/excel-job.ps1` into a job folder in the Windows user's temp folder and runs them there. Excel gets a hidden instance of its own, with macros, link updates, events and alerts off. It opens the copy, recording any repair, rebuilds the calculation and reads each `REF`. With `--set` it changes those cells, recalculates and reads again. It saves the copy, reopens it, reads once more and quits, then the script deletes the job folder.

While Excel calculates a large workbook it turns calls away. The job registers a COM message filter, so a call that Excel turns away waits 200 ms and goes again, for up to `--calc-timeout`, and `busy_retries` counts those waits. Without the filter the job ended with `Call was rejected by callee` (`RPC_E_CALL_REJECTED`, 0x80010001), or a read came back as `read_error: You cannot call a method on a null-valued expression`, because PowerShell hands back `$null`, with no error, for a property that a busy Excel refused. On 12 November 2026 the busy case of `test_fixtures.py`, in which a second client keeps the job's Excel rebuilding a workbook of 160,000 formulas, ended with the first error before the filter and passed with 24 waits after it. A probe that day, polling a range on such an Excel 83 times without the filter, got the first error 3 times and the second twice. A call whose wait runs out can still come back as `$null`, so a read checks the answers Excel always gives, such as the cell count, and reports `read_error: Excel gave no answer for …` where the script before recorded an empty value: on 12 November 2026 `scripts/test-read-guard.ps1`, which runs the read on stand-in workbooks with no Excel, got an empty value for four of six refused reads before the check and none after it. A refused error scan ends the job, and the one-line summary counts `read_errors`.

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
| `timings_ms` | elapsed time at each step |

An error value reads as `{"error": "#DIV/0!"}`. The exit status is 0, with `ok` true, when Excel finished the job, every read gave an answer and the error scan could tell for every sheet. Otherwise it is 1 and `error` says why, so a damaged file exits 1 with its `failed_loads` filled in. `--job-timeout` (300 s) stops only this job's Excel and PowerShell, and `--calc-timeout` (120 s) limits each calculation and each call's wait for a busy Excel, so several long waits can add up to the job's limit. When the job's PowerShell ends without a result, the script stops that job's Excel by its process ID, and `runner.stopped` lists it. A limit that falls while Excel is still starting waits up to 60 s for that process ID, because ending PowerShell first left a hidden Excel running in a fixture run on 12 November 2026.

A read that failed, such as a `REF` the workbook lacks, fails the job. `error` says how many reads failed and names the first, as in `2 of 4 reads failed; the first is NoSuchName at open: …`, where a `REF` that fails at open and after the reopen counts twice. The reads that worked keep their values in `base`, `after` and `reopened.values`, and `--save` still keeps the copy, because Excel finished it. Before 12 November 2026 such a job exited 0 with `ok: true` and showed the failure only in that range's `read_error` and the summary's `read_errors`, so a caller that tolerates a bad reference now has to read the ranges of a failed job. On that day the `unread` case of `test_fixtures.py`, which reads a name the workbook lacks beside one it has, ended `ok: true` on the script as it was, and with status 1, that `error`, the other name's value and the saved copy on the changed one.

`errors` and a read's `address` name a sheet as Excel does, in single quotes with each apostrophe in its name doubled, so an `errors` entry such as `'It''s'!B2` and a read's `address` such as `'It''s'!$B$2` are each a `REF` that reads back, and `--set` takes a cell named that way. On 12 November 2026 the `quoted` cases of `test_fixtures.py` took `'It''s'!B2` from the `errors` of a job through real Excel, read it back, set a cell on that sheet, and read back the `address` that read gave, `'It''s'!$B$2`. On the script as released before, every one of those reads and the set failed with `Exception from HRESULT: 0x800A03EC`, because that script took no apostrophe inside a quoted sheet name and looked such a `REF` up as a workbook name. A read through a workbook name that points at such a sheet gave its `address` with the apostrophe single on that script, which no `REF` accepted: one `excel.py` job that day on the case's workbook gave `'It's'!$C$3` for its name `Tripled`, where the `quoted` case gets `'It''s'!$C$3`.

A `REF` that opens with a quote and is no quoted sheet name with a range after it is refused, as `'It's'!B2` is, with its single apostrophe inside the quotes. The read fails with `'It's'!B2 is no sheet name in single quotes with a range after it: double each apostrophe inside the quotes, as in 'It''s'!B2`, and a `--set` on such a `REF` ends the job with that error, without saving. `'It's'` is not taken for the sheet `It's`, because `'It''s'` would then name two sheets. The `quoted` cases showed the refused read and the refused set through real Excel on 12 November 2026, where the released script had failed the read of `'It's'!B2` with `0x800A03EC` and no hint, and `scripts/test-read-guard.ps1` showed the reads, the addresses and the refusals on a stand-in workbook with no Excel that day (`quoted`). Its stand-in sheet with a letter outside A to Z in its name reads in quotes, and without them is taken for a workbook name, which fails as a missing name does. A `REF` whose quoted sheet name ended early, at an apostrophe before a `!`, as in `'A'!B'C'!D1`, is refused in that case too, and its table columns `'My sheet'!T[Owner''s share]` and `S!T['#Items]`, which hold an apostrophe elsewhere in the range, reach their sheets. No test reads a table column through real Excel.

Copy one cell or rectangle of an `errors` entry at a time, because a `REF` of several is not read whole. On 12 November 2026 one `excel.py` job on a synthetic workbook read `S!A1:A2,C1:C2` as the two values of `A1:A2` under the `address` of both rectangles, with no sign that two were missing. It failed the read of `S!A1,C1:C2`, whose first rectangle is one cell, with `Excel gave no answer for the values`, and it ended on `--set 'S!A1:A2,C1:C2=5'` with `set target S!A1:A2,C1:C2 is not one cell`. A workbook name that refers to two rectangles read the same way in another job that day: the first rectangle's values under the address of both.

`errors` lists, by sheet, the cells that hold an error value a formula made: the formula cells, and the cells into which a dynamic array formula in another cell spilled an error. An entry holds one sheet's cells in reading order, as in `'Model'!C1,A2:A4`, with a merged cell given as its first cell and an apostrophe in the sheet's name doubled. Typed-in error values are left out, as they always were, and a sheet whose only error values are typed in is clean.

The scan asks Excel in formulas, which Excel answers on a protected sheet as on any other. `ISERROR(SUM(range))` says in milliseconds whether a range holds an error value, so a sheet with none costs one question. A range that holds one is halved until a piece is small enough for Excel to say of each cell whether it holds an error value and whether it is a formula cell, and `HasSpill` then tells a spilled error from a typed-in one. The questions are in A1 addresses, so the scan puts an Excel that a workbook opened in R1C1 reference style into A1 style and back, and the saved copy keeps R1C1. A sheet goes into `errors_unscanned` with the reason, and fails the job as a failed read does, because `errors` is then not the whole answer, in two cases:

- Excel gave something that is no answer to one of those questions, such as an error value.
- Listing the sheet's cells would take more than the scan's limit of 2,000,000 cells read, where a question counts as 1,000 cells and `HasSpill` as 4,000. The limit keeps one sheet from using up the job's time limit: a sheet that reaches it has taken 3 to 32 s. The reason then begins `its error values are spread over too many cells to list`.

`error` names the first such sheet and the summary counts `unscanned_sheets`. `timings_ms.scan` less `timings_ms.read` is the time the scan took. No test covers a picture in a cell or a linked data type such as Stocks, which need a web request, nor a what-if data table, so whether the scan lists such a cell is not known: it lists a cell where Excel's `ISERROR` is true. No test covers an Excel from before dynamic arrays either. It has no `HasSpill`, so there a typed-in error value probably ends the job with `Excel gave no answer for whether … is spilled`.

Until the last release of 12 November 2026, which brought this scan, it listed what Excel's `SpecialCells` gave. `SpecialCells` cannot look at a protected sheet, gives only the first cell of a spilled range, and gives an address that Excel cuts at 256 characters. A caller sees these changes:

- A workbook with a formula error on a protected sheet, such as a lookup's `#N/A`, passes with those cells in `errors`. Such a job failed with the sheet named since the release before it, the same evening, and before that exited 0 with the sheet unmentioned. A protected sheet whose only error values are typed in is clean, where that release named it.
- `errors` holds spilled errors, which it left out, so it can list more cells for the same workbook. A sheet whose only errors are spilled lists them, where it failed the job.
- A list of many separate cells is whole, where it ended at 256 characters with no sign.
- A merged formula cell is `A1` where it was `A1:B2`, an apostrophe in a sheet name is doubled, and the cells of a sheet can be split into other rectangles than Excel's.
- A workbook in R1C1 reference style passes, where a job on one with a sheet free of formula errors failed since that same release.
- One change fails a job that passed. A sheet whose error cells take more than the limit to list is named in `errors_unscanned`, where the job passed with its list, whole or cut. The `scattered` case's 120 error cells, scattered singly over a whole sheet, reach it, and so can a few hundred separate groups of spilled errors, each of which costs a `HasSpill` question.

On 12 November 2026 `test_fixtures.py` ran these cases through real Excel on the script as released before this change and on the changed one. The `r1c1` row's released result is from one `excel.py` job on that case's workbook:

| Case | Sheet | Released script | Changed script |
|---|---|---|---|
| `protected` | formula errors on a protected sheet, one with a hidden formula, beside a typed-in one | sheet named, status 1 | `'Locked'!A1,C5`, status 0 |
| `protected` | a protected sheet whose only error value is typed in | sheet named | clean |
| `scan` | an error spilled below a clean first cell, beside a formula error | `'Spill'!C1` | `'Spill'!C1,A2` |
| `scan` | the same with no other error on the sheet | sheet named, status 1 | `'SpillOnly'!B4:B5` |
| `scan` | a spilled error beside a typed-in error in a merged cell | clean, with no sign | `'Merged'!D3` |
| `scan` | 150 separate error cells | the first 62, in 256 characters | all 150 |
| `scan` | a sheet named `It's` | `'It's'!B2` | `'It''s'!B2` |
| `scan` | two error cells on a protected sheet used to its last row and column | sheet named | `'Far'!A1,ZZ900000` |
| `r1c1` | a workbook in R1C1 style with a clean sheet and errors in `C10:C12` | clean sheet named, status 1 | `'Column'!C10:C12`, status 0 |
| `scattered` | 120 error cells scattered over a whole sheet | the first 27, status 0 | sheet named for the limit, status 1, the other sheet's cell listed |

The `r1c1` case also showed why the scan changes the reference style: before it did, the changed script named the clean sheet and passed the sheet with the three error cells as clean. The same day `scripts/test-read-guard.ps1`, on stand-in sheets with no Excel, showed that the scan asks one question of a sheet with no error value (`no_error_cells`), names a sheet whose answer is no answer (`any_is_an_error` to `spill_is_odd`) or whose listing passes the limit (`over_the_limit`), and ends the job when Excel refused a call or handed back `$null` (`no_sheets` to `no_spill`). Probes in real Excel that day showed what the scan rests on: `SpecialCells` gave an address of 256 characters for 150 cells and counted a merged typed-in error four times, `ISFORMULA` is false for a spilled cell, and `HasSpill` is true or false for a range whose cells agree and `DBNull` where they differ.

`--save` keeps the copy Excel wrote. A workbook from openpyxl has no cached values until Excel or another engine saves it, so check and deliver that copy when readers need numbers without recalculating.

Excel runs in the user's interactive Windows session, which Microsoft doesn't support for unattended automation. The time limit and the stop of this job's own process are the guard, so run one job at a time.

## Prove a change to the scripts

```sh
python3 -I scripts/test_fixtures.py EVIDENCE_DIR
```

It needs `openpyxl`, builds workbooks that have each tripped this COM layer, runs them through real Excel and prints PASS or FAIL per check. With `--no-excel` after `EVIDENCE_DIR` it runs only the cases that start no Excel, which takes under a minute and is enough while you change the error scan's handling of an answer; the full run is still the proof. Run it after any change to a script, because "a formula calculated" passes while the cases below still fail. Its busy case runs `scripts/test-busy.ps1` beside the job, which finds the job's Excel through the job's open workbook and its process ID and starts no Excel of its own. Whether that helper's rebuilds land is a matter of timing, so the case runs the job up to three times, and prints a `NOTE` line when none landed on the reopened copy. The reads that Excel refuses are tested without Excel, by `scripts/test-read-guard.ps1`. An agent in a sandbox reaches Excel through a file queue that a runner outside the sandbox serves. Run these same cases through that queue from inside the sandbox, because a run from outside doesn't exercise the agent's route.

## Rules for your own scripts

- **Own instance, own process.** `New-Object -ComObject Excel.Application` starts a new Excel, unlike PowerPoint. Get its process ID from `GetWindowThreadProcessId($xl.Hwnd)`, and after `Quit()` and releasing the COM objects, stop that ID alone if it lingers, checking first that the process is still `EXCEL`. The user may be working in another Excel.
- **Wait for a busy Excel.** Register a message filter before the first call, as `XlCom.BusyFilter` in `scripts/excel-job.ps1` does, on a single-threaded apartment thread, which Windows PowerShell's is. A check for `RPC_E_CALL_REJECTED` around each call misses the properties that come back as `$null`.
- **Pass `CorruptLoad` by name.** Excel rejects it by position with `0x800A03EC`. Call `Workbooks.Open` through `InvokeMember` with named arguments, `[string[]]@('Filename', 'UpdateLinks', 'ReadOnly', 'IgnoreReadOnlyRecommended', 'AddToMru', 'CorruptLoad')`. Try 0 (normal), then 1 (repair), then 2 (extract data), and record which one opened it.
- **Return a range with a comma.** `return , $range`, because PowerShell unrolls a returned multi-cell range into its cells.
- **Set values through `InvokeMember`.** Several sets of different types in one job fail with "Specified cast is not valid" or "Unable to cast Int32 to String", because PowerShell's COM binder reuses the first assignment's value type at the same call site. Send whole numbers as doubles.
- **Don't wait on `CalculationState` alone.** A workbook with force full calculation on reports `xlPending` (2) for as long as it is open, although each calculate call has finished when it returns, so count 2 as done for those workbooks only.
- **Rebuild, not calculate, on a file another tool wrote.** `CalculateFullRebuild()` after opening, then `CalculateFull()` after a set.
- **Read errors by type.** `Value2` returns error cells as `Int32`, and numbers as doubles. Add 2146828288 to get the code: 2007 is `#DIV/0!`, 2042 is `#N/A`.
- **Turn off what can run or prompt.** `Visible`, `DisplayAlerts`, `ScreenUpdating`, `AskToUpdateLinks` and `EnableEvents` false, `AutomationSecurity` 3, and `UpdateLinks` 0 on open.
- **Files and output.** Copy the workbook into a Windows folder and pass Windows paths. `Set-Content -Encoding UTF8` adds a byte-order mark, so read the JSON in Python with `encoding="utf-8-sig"`.

The job is done when the results are saved outside the job folder, the job folder is gone, and the `EXCEL` process count is back to what it was before the job.

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

`REF` is a workbook-scoped name or a `Sheet!A1` range, with a sheet name that has spaces in single quotes, as in `'My sheet'!A1`. `--set REF=VALUE` takes a number, `true`, `false`, `null` or text, and `--set REF==0.1` writes the formula `=0.1`.

The script copies the workbook and `scripts/excel-job.ps1` into a job folder in the Windows user's temp folder and runs them there. Excel gets a hidden instance of its own, with macros, link updates, events and alerts off. It opens the copy, recording any repair, rebuilds the calculation and reads each `REF`. With `--set` it changes those cells, recalculates and reads again. It saves the copy, reopens it, reads once more and quits, then the script deletes the job folder.

While Excel calculates a large workbook it turns calls away. The job registers a COM message filter, so a call that Excel turns away waits 200 ms and goes again, for up to `--calc-timeout`, and `busy_retries` counts those waits. Without the filter the job ended with `Call was rejected by callee` (`RPC_E_CALL_REJECTED`, 0x80010001), or a read came back as `read_error: You cannot call a method on a null-valued expression`, because PowerShell hands back `$null`, with no error, for a property that a busy Excel refused. On 12 November 2026 the busy case of `test_fixtures.py`, in which a second client keeps the job's Excel rebuilding a workbook of 160,000 formulas, ended with the first error before the filter and passed with 24 waits after it. A probe that day, polling a range on such an Excel 83 times without the filter, got the first error 3 times and the second twice. A call whose wait runs out can still come back as `$null`, so a read checks the answers Excel always gives, such as the cell count, and reports `read_error: Excel gave no answer for …` where the script before recorded an empty value: on 12 November 2026 `scripts/test-read-guard.ps1`, which runs the read on stand-in workbooks with no Excel, got an empty value for four of six refused reads before the check and none after it. A refused error scan ends the job, and the one-line summary counts `read_errors`.

It prints JSON, or writes it to `--json OUT.json` and prints one line:

| Field | Holds |
|---|---|
| `open` | `load` (`normal`, `repair` or `extract data`), `repaired`, `failed_loads`, `external_links` |
| `settings` | calculation mode as opened, iteration, force full calculation |
| `base`, `after`, `reopened.values` | each `REF` at open, after the sets, and after the save and reopen: `value`, `text`, `formula`, or `values` for a range |
| `errors` | the cells holding error values |
| `error`, `error_line` | why the job stopped, and the line of `excel-job.ps1` that stopped it |
| `busy_retries` | how many times a call waited for a busy Excel |
| `runner` | `timed_out`, and `stopped`, the processes the script tried to stop, each with its outcome |
| `timings_ms` | elapsed time at each step |

An error value reads as `{"error": "#DIV/0!"}`. The exit status is 0 when Excel finished the job and 1 when it did not, so a damaged file exits 1 with its `failed_loads` filled in. `--job-timeout` (300 s) stops only this job's Excel and PowerShell, and `--calc-timeout` (120 s) limits each calculation and each call's wait for a busy Excel, so several long waits can add up to the job's limit. When the job's PowerShell ends without a result, the script stops that job's Excel by its process ID, and `runner.stopped` lists it. A limit that falls while Excel is still starting waits up to 60 s for that process ID, because ending PowerShell first left a hidden Excel running in a fixture run on 12 November 2026.

`--save` keeps the copy Excel wrote. A workbook from openpyxl has no cached values until Excel or another engine saves it, so check and deliver that copy when readers need numbers without recalculating.

Excel runs in the user's interactive Windows session, which Microsoft doesn't support for unattended automation. The time limit and the stop of this job's own process are the guard, so run one job at a time.

## Prove a change to the scripts

```sh
python3 -I scripts/test_fixtures.py EVIDENCE_DIR
```

It needs `openpyxl`, builds workbooks that have each tripped this COM layer, runs them through real Excel and prints PASS or FAIL per check. Run it after any change to a script, because "a formula calculated" passes while the cases below still fail. Its busy case runs `scripts/test-busy.ps1` beside the job, which finds the job's Excel through the job's open workbook and its process ID and starts no Excel of its own. Whether that helper's rebuilds land is a matter of timing, so the case runs the job up to three times, and prints a `NOTE` line when none landed on the reopened copy. The reads that Excel refuses are tested without Excel, by `scripts/test-read-guard.ps1`. An agent in a sandbox reaches Excel through a file queue that a runner outside the sandbox serves. Run these same cases through that queue from inside the sandbox, because a run from outside doesn't exercise the agent's route.

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

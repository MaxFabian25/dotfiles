---
name: excel-com-wsl
description: Open, recalculate, read, change and save an .xlsx in desktop Excel on Windows from WSL, over COM through powershell.exe. Use when a workbook must be checked as Excel itself calculates it, such as openpyxl output with no cached values, results LibreOffice may calculate differently, whether a named input still drives a result, or whether Excel must repair the file, and before writing or debugging PowerShell that drives Excel's COM objects, such as a script failing with 0x800A03EC, a cast error or a calculation timeout.
---

# Excel over COM from WSL

Desktop Excel on Windows is the authority for workbook repair and calculation results (LibreOffice recalculation does not prove Excel opens a file cleanly or produces identical numbers). Drive it from WSL over COM through Windows PowerShell.

## Check a workbook

```sh
python3 -I scripts/excel.py model.xlsx --read Result --read 'Model!B2:B4' --set Growth=0.1 --save baseline.xlsx
```

### Reference syntax and quoting

- `REF` is a workbook-scoped name or a `Sheet!A1` cell/rectangle. Quote sheet names containing characters outside letters (any script/accent, e.g. `Økonomi!A1`), digits, and underscores (e.g. `'My sheet'!A1`), doubling embedded apostrophes (`'It''s'!B2`).
- In shell scripts and agent shells, wrap a quoted `REF` in double quotes (`--read "'It''s'!B2"`) and omit `$` anchors (`B2` instead of `$B$2`, or escape as `\$`) so double-quoted shells do not expand variables ([incidents](references/incidents.md)); in interactive bash, avoid double-quoted `!` history expansion.
- `--set REF=VALUE` accepts numbers, `true`, `false`, `null`, or text; `--set REF==0.1` writes formula `=0.1`. `REF` ends at the first `=` outside a leading quoted sheet name (`--set "'a=b'!A1=5"` sets `A1` on sheet `a=b`).
- Read "How a REF reads now" in [references/ref-parsing.md](references/ref-parsing.md) before copying an `errors` entry or `address` back as a `REF`, using quoted/non-ASCII sheets, sheet-scoped names, multi-area ranges, table columns, or non-range names, or changing `REF` parsing in `excel-job.ps1`/`excel.py`.

### Execution and JSON output

The script copies the workbook and `scripts/excel-job.ps1` into a Windows temp job folder, starts an isolated hidden Excel (macros, link updates, events, alerts off), opens the copy (recording repairs), recalculates, reads each `REF`, applies any `--set` edits and recalculates, saves, reopens, reads again, quits Excel, and deletes the job folder.

- For busy-Excel retries and `Excel gave no answer for …` read errors, read [references/busy-excel.md](references/busy-excel.md).
- For `errors` scanning and `errors_unscanned` rules (or comparing against pre-refactor `SpecialCells` output), read [references/error-scan-history.md](references/error-scan-history.md).

`excel.py` prints JSON (or writes `--json OUT.json` and prints one line):

| Field | Holds |
|---|---|
| `open` | `load` (`normal`, `repair` or `extract data`), `repaired`, `failed_loads`, `external_links` |
| `settings` | calculation mode as opened, iteration, force full calculation |
| `base`, `after`, `reopened.values` | each `REF` at open, after sets, and after save/reopen (`address` plus `value`, `text`, `formula`, or `values`) |
| `ok` | true when Excel finished, every read succeeded, and error scanning covered every sheet |
| `errors` | cells holding formula error values (e.g. `{"error": "#DIV/0!"}`), by sheet |
| `errors_unscanned` | sheets the scan could not list (`sheet` and `reason`) |
| `error`, `error_line` | failure reason and stopping line of `excel-job.ps1` |
| `busy_retries` | count of busy-Excel wait retries |
| `runner` | `timed_out` and `stopped` process outcomes |
| `excel` | Excel `version`, `build`, `os`, `pid`, Office log `session`, and `session_reason` if absent |
| `office_logs`, `runner.office_logs` | session Office logs deleted (`session`, `removed`, `bytes`, `left`, `reason`) |
| `timings_ms` | elapsed milliseconds per step |

### Failure handling, caching, logs, and claims

- **Exit status:** 0 (`ok: true`) only when Excel finished, all reads succeeded, and all sheets scanned; otherwise 1 with `error` (damaged files populate `failed_loads`). A failed read (such as a missing `REF`) fails the job while preserving successful reads and any `--save` output (a `REF` failing at both open and reopen counts twice).
- **Timeouts and process cleanup:** `--job-timeout` (300 s) stops only this job's Excel and PowerShell; `--calc-timeout` (120 s) caps each calculation and busy wait. If PowerShell ends without a result, the script kills its Excel PID (waiting up to 60 s if Excel is still starting; [incidents](references/incidents.md)).
- **Cached values:** `--save` keeps Excel's saved copy with populated formula caches (needed for `openpyxl` outputs delivered to readers that do not recalculate).
- **Office logs:** Excel writes ~80 MiB of temp logs per crashed/stopped session ([incidents](references/incidents.md)); `scripts/office-logs.ps1` deletes only this job's session logs without failing the job. Read [references/office-logs.md](references/office-logs.md) if `office_logs` reports a `reason` or `left` files, before editing `office-logs.ps1`, or before starting Excel from a custom script.
- **Serialization and claims:** run one Excel job at a time (Microsoft does not support unattended interactive COM). Before starting, take the `windows-desktop` claim per `work-across-tailscale-machines` with `--note 'excel-com-wsl: a hidden Excel over COM; the foreground and the clipboard stay free'`, and release it when done.

## Prove a change to the scripts

```sh
python3 -I scripts/test_fixtures.py EVIDENCE_DIR
```

Requires `openpyxl`, runs real-Excel regression fixtures, and prints PASS/FAIL per check. Read [references/testing.md](references/testing.md) before running it after any script edit. From a sandboxed agent, run the same cases through the file queue served outside the sandbox so the agent route itself is tested.

## Rules for your own scripts

Before writing or debugging custom PowerShell COM automation (process ownership, COM release order, busy-Excel message filter, `CorruptLoad`, `InvokeMember` typing, `CalculationState`, error codes, or UTF-8 JSON), read [references/own-scripts.md](references/own-scripts.md).

**Done check:** results are saved outside the job folder, the job folder and session Office logs are gone, the `EXCEL` process count matches its pre-job baseline, and the `windows-desktop` claim is released.

Read [references/incidents.md](references/incidents.md) before changing or dropping a rule here.

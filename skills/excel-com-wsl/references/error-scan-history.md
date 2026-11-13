# The error scan's history

The [skill](../SKILL.md) says what the error scan does now. This file keeps what it changed for callers and the runs that showed it.

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

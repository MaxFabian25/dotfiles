# Quitting Excel over COM

Read this before changing COM cleanup, writing another Excel COM driver, or investigating a crash at `Quit()`.

## Release order

Close the workbooks. Release the workbook objects held by variables with `FinalReleaseComObject`, then clear those variables and any arrays that retain them. In `excel-job.ps1`, `$opened` retains the last workbook after `$wb` has been cleared. Clear PowerShell's error records after copying the job's error into its result.

Run `[GC]::Collect()` and `[GC]::WaitForPendingFinalizers()` twice before calling `Quit()`. Then release the application, clear its variable, and collect again. Keep the existing process-ID check and time limit. Release only objects from the instance that this script owns.

## Evidence and limits

On 13 November 2026, captured Excel dumps placed the fault on the main thread during COM shutdown, including `CoUninitialize` and `DisconnectSrvIPIDs`. The original script released the application before garbage collection had released its remaining child wrappers. Moving child-wrapper cleanup ahead of `Quit()` and releasing the application last stopped the observed crashes in the tested jobs.

A full `test_fixtures.py` run with the fixed order printed `0 failed` and passed `no job's Excel crashed`. A following run with the old quit order failed that check for the `mixed` and `scan` jobs, both in `combase.dll`. Add-ins and Office settings were unchanged. This comparison supports the cleanup fix in that configuration. It does not isolate whether an add-in contributed, nor establish the exact allocation and freeing sequence inside Excel or Windows. The same crash signature existed before the most recent Office update, so the error bucket name alone is not evidence of a partly applied update.

A crash during shutdown can leave a successful job result because the calculations and reads already finished. The suite therefore reads Application Error event 1000 after its cases and matches Excel process IDs to its job results. Failure to read the log fails the check. Readable results from other jobs do not substitute for that check.

## Busy test helper

`test-busy.ps1` is a second COM client. It can miss the brief interval in which the job reopens and closes its saved copy. It releases its objects after observing that copy close, or after observing no open workbook for a second following the saved copy's close. The second condition is a test-helper fallback, not proof that the job reached a particular step. The suite records when rebuilds did not reach the reopened copy.

On 13 November 2026, repeated busy-case runs with this helper finished without the job having to force-stop Excel. The fixed full-suite run also recorded a rebuild on the reopened copy. Only the helper uses this fallback. The production job's calculation and read sequence is unchanged.

# Excel's Office logs

The [skill](../SKILL.md) says that each job deletes its Excel's Office logs. This file says why, how `scripts/office-logs.ps1` finds them, what its tests show, and how a script of your own does the same.

## What Office leaves

Office writes a log of each Excel session to `Diagnostics\EXCEL` in the Windows user's temp folder: four files named after the session's GUID, each made at 20 MiB. A clean quit cuts them to what they hold, 0.9 to 2.5 MB in all in the sessions measured on 13 November 2026, and an Excel that crashes or is stopped leaves 80 MiB. They stay after the session ends: that day two threads' planted-error rounds of `test_fixtures.py` left 11.93 GiB there from 756 sessions, among them 113 Excel crashes, and C: fell to 162 MB free. Office's own cleanup is no guard: that day an Excel that started with no other Excel running deleted the empty files of three earlier sessions and kept their files with content, and two jobs that ran while another Excel was open deleted none. No setting found in Office's registry keys or its policies that day turns them off.

## How the job deletes them

The job notes its Excel's session, which Office records under the process ID in `HKCU\Software\Microsoft\Office\Common\CrashPersistence\EXCEL\<PID>`, and once that Excel has ended, `scripts/office-logs.ps1` deletes the files named after that session and no others. `office_logs` says what it deleted, and `runner.office_logs` does the same for a job whose Excel the script had to stop. A failure in that bookkeeping is a `reason` there and in `excel.session_reason`, and fails no job. Where Office writes no such entry, as another build might, each job waits 10 s for it and deletes nothing.

On 13 November 2026 a full run of `test_fixtures.py` deleted 288.5 MB of these logs from 22 job results this way, 1.4 MB for the smallest and 80 MiB for each of the three Excels it stopped, and left none of its sessions' files. Its stand-in cases, with session entries under a registry key of their own, show without Excel that `office-logs.ps1` refuses an entry stamped before the job's `ps.pid` or after its `excel.pid`, or for another process, keeps another session's files and a file another process holds, and says why when it found no session or could not list the folder.

## In a script of your own

While your Excel runs, read its session's GUID from the `4` value of `HKCU\Software\Microsoft\Office\Common\CrashPersistence\EXCEL\<PID>`, whose JSON holds `ProcessId`, `SessionId` and `SessionInitTime`, and once it has ended delete the files under `Diagnostics\EXCEL` in the temp folder whose names end in `_<GUID>.log`, as `scripts/office-logs.ps1` does. Office deletes that key when Excel quits, and keeps it when Excel crashes or is stopped, so an entry can be left by an earlier Excel with the same process ID. Take an entry only when its `ProcessId` is your Excel's and its `SessionInitTime`, a UTC FILETIME, falls between your script's start and the moment `New-Object` returned: in two probes on 13 November 2026 Office stamped it 0.26 and 0.30 s after Excel started and 1.6 and 2.4 s before `New-Object` returned.

<#
Test helper for test_fixtures.py. Keeps the Excel of one running job busy, the way a large workbook does on its own:
it finds the job's open workbook in the running object table, takes that Excel's Application object and rebuilds the
calculation, again and again, so the job's own calls meet a calculating Excel. It never starts an Excel. It lets go
once the job has closed the copy it reopened, because Excel can't quit while another client holds it, and ends at the
latest when the job's Excel is gone. It writes what it did to -Log as JSON.
#>
param([Parameter(Mandatory = $true)] [string] $JobDir, [Parameter(Mandatory = $true)] [string] $Log, [int] $Seconds = 240)
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
namespace XlBusy {
  public static class Rot {
    [DllImport("ole32.dll")] static extern int GetRunningObjectTable(int reserved, out IRunningObjectTable table);
    [DllImport("ole32.dll")] static extern int CreateBindCtx(int reserved, out IBindCtx context);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr window, out uint processId);
    // The object registered under this file path, or null. Unlike BindToMoniker, it can't start an Excel.
    public static object Find(string path) {
      IRunningObjectTable table; IBindCtx context; IEnumMoniker running;
      GetRunningObjectTable(0, out table); CreateBindCtx(0, out context); table.EnumRunning(out running);
      IMoniker[] one = new IMoniker[1];
      while (running.Next(1, one, IntPtr.Zero) == 0) {
        string name;
        try { one[0].GetDisplayName(context, null, out name); } catch (Exception) { continue; }
        if (!String.Equals(name, path, StringComparison.OrdinalIgnoreCase)) { continue; }
        object found; table.GetObject(one[0], out found); return found;
      }
      return null;
    }
  }
}
"@
$report = [ordered]@{ bound = $false; excel_pid = $null; calls = 0; calls_on_saved_copy = 0; refused = 0; ended = 'time limit'; error = $null }
$deadline = (Get-Date).AddSeconds($Seconds)
$book = Join-Path $JobDir 'in.xlsx'; $saved = Join-Path $JobDir 'out.xlsx'; $lock = Join-Path $JobDir '~$out.xlsx'
$wb = $null; $app = $null
try {
  $jobPid = 0
  while (-not $jobPid) {
    if ((Get-Date) -gt $deadline) { throw 'the job wrote no excel.pid' }
    try { $jobPid = [int](Get-Content -LiteralPath (Join-Path $JobDir 'excel.pid') -ErrorAction Stop | Select-Object -First 1) } catch { }
    if (-not $jobPid) { Start-Sleep -Milliseconds 200 }
  }
  $report.excel_pid = $jobPid
  # in.xlsx is in the job's own folder, so only the job's Excel has it open. A busy Excel can hand back $null for a
  # property, with no error, so ask until it answers.
  while (-not $report.bound) {
    if ((Get-Date) -gt $deadline) { throw "no Excel with $book open answered" }
    if (-not (Get-Process -Id $jobPid -ErrorAction SilentlyContinue)) { throw "the job's Excel ended before it opened $book" }
    try {
      if (-not $wb) { $wb = [XlBusy.Rot]::Find($book) }
      if ($wb -and -not $app) { $app = $wb.Application }
      $hwnd = if ($app) { $app.Hwnd }
      if ($hwnd) {
        $p = [uint32]0; [void][XlBusy.Rot]::GetWindowThreadProcessId([IntPtr]$hwnd, [ref]$p)
        if ([int]$p -ne $jobPid) { $report.error = "the workbook is open in Excel $p, not in the job's Excel $jobPid"; break }
        $report.bound = $true
      }
    } catch { }
    if (-not $report.bound) { Start-Sleep -Milliseconds 200 }
  }
  # The job saves out.xlsx, which has a lock file while it is open for writing, closes it, reopens it read-only, with
  # no lock file, and closes it again. Only a count that Excel really answered tells an open workbook from none.
  $closedAfterSave = $false; $reopened = $false
  while ($report.bound -and (Get-Date) -lt $deadline) {
    if (-not (Get-Process -Id $jobPid -ErrorAction SilentlyContinue)) { $report.ended = 'Excel gone'; break }
    if ((Test-Path -LiteralPath $saved) -and -not (Test-Path -LiteralPath $lock)) { $closedAfterSave = $true }
    $count = $null
    try { $count = $app.Workbooks.Count } catch { }
    if ($closedAfterSave -and $count -is [int] -and $count -ge 1) { $reopened = $true }
    if ($reopened -and $count -is [int] -and $count -eq 0) { $report.ended = 'the job closed the copy it reopened'; break }
    try { $app.CalculateFullRebuild(); $report.calls++; if ($reopened) { $report.calls_on_saved_copy++ } } catch { $report.refused++ }
    Start-Sleep -Milliseconds 100
  }
} catch {
  $report.error = $_.Exception.Message
} finally {
  foreach ($o in @($app, $wb)) { if ($o) { try { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($o) } catch { } } }
  $app = $null; $wb = $null; [GC]::Collect(); [GC]::WaitForPendingFinalizers()
  $report | ConvertTo-Json | Set-Content -LiteralPath $Log -Encoding UTF8
}

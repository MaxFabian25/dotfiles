<#
Runs one fixed Excel job. excel.py writes job.json and calls this with Windows PowerShell. It never runs anything the
job names: it opens the given workbook copy in a new, hidden Excel
instance with macros, link updates and alerts off, rebuilds the calculation, reads and sets the given references,
saves a copy, reopens that copy, and quits. It writes result.json beside job.json, and excel.pid and ps.pid first,
so excel.py can stop this instance, and only this instance, after a timeout. A call that Excel turns away because it
is busy waits and goes again, for up to the calculation timeout.
#>
param([Parameter(Mandatory = $true)] [string] $Job)
$ErrorActionPreference = 'Stop'
$dir = Split-Path -Parent $Job
$spec = Get-Content -Raw -LiteralPath $Job | ConvertFrom-Json
Set-Content -LiteralPath (Join-Path $dir 'ps.pid') -Value $PID
$sw = [Diagnostics.Stopwatch]::StartNew()
$result = [ordered]@{ ok = $false; error = $null; error_line = $null; busy_retries = 0; excel = $null; open = $null; settings = $null; base = $null; errors = $null; after = $null; reopened = $null; timings_ms = [ordered]@{} }
$missing = [Type]::Missing
$errorNames = @{ 2000 = '#NULL!'; 2007 = '#DIV/0!'; 2015 = '#VALUE!'; 2023 = '#REF!'; 2029 = '#NAME?'; 2036 = '#NUM!'; 2042 = '#N/A'; 2043 = '#GETTING_DATA'; 2045 = '#SPILL!'; 2046 = '#CONNECT!'; 2047 = '#BLOCKED!'; 2048 = '#UNKNOWN!'; 2049 = '#FIELD!'; 2050 = '#CALC!' }

Add-Type -Namespace XlCom -Name Win -MemberDefinition '[DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint p);'

# While Excel calculates it turns calls away. Without this filter a call then fails with RPC_E_CALL_REJECTED, or a
# property comes back as $null with no error, which shows later as "You cannot call a method on a null-valued
# expression". With it, COM asks RetryRejectedCall, which has the call wait 200 ms and go again until LimitMs. The
# filter belongs to this thread, which has to be a single-threaded apartment, as Windows PowerShell's is.
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
namespace XlCom {
  [ComImport, Guid("00000016-0000-0000-C000-000000000046"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  public interface IOleMessageFilter {
    [PreserveSig] int HandleInComingCall(int callType, IntPtr caller, int tick, IntPtr info);
    [PreserveSig] int RetryRejectedCall(IntPtr callee, int tick, int rejectType);
    [PreserveSig] int MessagePending(IntPtr callee, int tick, int pendingType);
  }
  public class BusyFilter : IOleMessageFilter {
    [DllImport("ole32.dll")] static extern int CoRegisterMessageFilter(IOleMessageFilter filter, out IOleMessageFilter previous);
    public static int LimitMs;
    public static int Retries;
    static BusyFilter registered;
    public static int Register(int limitMs) {
      IOleMessageFilter previous; LimitMs = limitMs; registered = new BusyFilter();
      return CoRegisterMessageFilter(registered, out previous);
    }
    public int HandleInComingCall(int callType, IntPtr caller, int tick, IntPtr info) { return 0; }
    // rejectType 2 is SERVERCALL_RETRYLATER, Excel's answer while busy. tick is the time this call has waited.
    public int RetryRejectedCall(IntPtr callee, int tick, int rejectType) {
      if (rejectType != 2 || tick >= LimitMs) { return -1; }
      Retries++; return 200;
    }
    public int MessagePending(IntPtr callee, int tick, int pendingType) { return 2; }
  }
}
"@

function Convert-Cell($v) {
  # Excel returns numbers as doubles, so an Int32 here is an error value.
  if ($v -is [int]) { $code = $v + 2146828288; if ($errorNames.ContainsKey($code)) { return @{ error = $errorNames[$code] } } ; return @{ error = "code $code" } }
  return $v
}

function Get-Target($wb, [string] $ref) {
  if ($ref -match '^(?:''(?<q>[^'']+)''|(?<s>[A-Za-z0-9_]+))!(?<a>.+)$') {
    $sheet = if ($Matches['q']) { $Matches['q'] } else { $Matches['s'] }
    return , $wb.Worksheets.Item($sheet).Range($Matches['a'])
  }
  # The comma keeps PowerShell from unrolling a multi-cell range into its cells.
  return , $wb.Names.Item($ref).RefersToRange
}

function Confirm-Answer($value, [string] $what) {
  # When a call's wait for a busy Excel runs out, PowerShell hands back $null for the property, with no error. An
  # answer Excel always gives, such as a cell count, is checked here, so a refused read is an error and not an empty value.
  if ($null -eq $value) { throw "Excel gave no answer for $what" }
}

function Read-Refs($wb, $refs) {
  $out = [ordered]@{}
  foreach ($ref in $refs) {
    try {
      $r = Get-Target $wb $ref
      Confirm-Answer $r 'the range'
      $count = $r.Count; Confirm-Answer $count 'the cell count'
      if ($count -gt 2000) { throw "range has $count cells; the limit is 2000" }
      $sheet = $r.Worksheet.Name; Confirm-Answer $sheet 'the sheet name'
      $address = $r.Address(); Confirm-Answer $address 'the address'
      $entry = [ordered]@{ address = "'" + $sheet + "'!" + $address }
      if ($count -eq 1) {
        # An empty cell has no value, but its formula is an empty string, so that tells it from a refused read.
        $formula = $r.Formula2
        if ($null -eq $formula) { $formula = $r.Formula }  # An Excel from before dynamic arrays has no Formula2.
        Confirm-Answer $formula 'the formula'
        $value = $r.Value2
        if ($null -eq $value -and $formula -ne '') { throw 'Excel gave no answer for the value' }
        $entry.value = Convert-Cell $value
        $entry.text = $r.Text
        $entry.formula = $formula
        $entry.has_spill = $r.HasSpill
      } else {
        $v = $r.Value2
        if ($v -isnot [array]) { throw 'Excel gave no answer for the values' }
        # The size comes from the values themselves: a refused Rows or Columns would count as 0 with no error.
        $rows = @()
        for ($i = 1; $i -le $v.GetLength(0); $i++) { $row = @(); for ($j = 1; $j -le $v.GetLength(1); $j++) { $row += , (Convert-Cell $v[$i, $j]) }; $rows += , $row }
        $entry.values = $rows
      }
      $out[$ref] = $entry
    } catch { $out[$ref] = [ordered]@{ read_error = $_.Exception.Message } }
  }
  return $out
}

function Test-Refused($caught) {
  # RPC_E_CALL_REJECTED and RPC_E_SERVERCALL_RETRYLATER, wherever PowerShell wrapped them.
  for ($e = $caught.Exception; $e; $e = $e.InnerException) { if ($e.HResult -in -2147418111, -2147417846) { return $true } }
  return $false
}

function Find-Errors($wb) {
  $found = @()
  $sheets = $wb.Worksheets; Confirm-Answer $sheets 'the sheets'
  foreach ($ws in $sheets) {
    $used = $ws.UsedRange; Confirm-Answer $used 'the used range of a sheet'
    # SpecialCells throws when a sheet has no error cells. A call that a busy Excel refused is no such answer.
    $cells = $null
    try { $cells = $used.SpecialCells(-4123, 16) } catch { if (Test-Refused $_) { throw }; continue }
    Confirm-Answer $cells 'the error cells of a sheet'
    $name = $ws.Name; Confirm-Answer $name 'the sheet name'
    $address = $cells.Address($false, $false); Confirm-Answer $address 'the address of the error cells'
    $found += "'$name'!" + $address
  }
  return , $found
}

function Wait-Calculation($xl, $wb, [int] $seconds) {
  # The calculate methods return when they finish; this waits for anything still running after them. A workbook set
  # to force full calculation reports xlPending (2) for as long as it is open, so that state counts as done there.
  $deadline = (Get-Date).AddSeconds($seconds)
  while ($true) {
    $state = $xl.CalculationState
    if ($null -eq $state) { throw 'Excel gave no calculation state' }
    $state = [int]$state
    if ($state -eq 0 -or ($state -eq 2 -and $wb.ForceFullCalculation)) { return @('done', 'calculating', 'pending')[$state] }
    if ((Get-Date) -gt $deadline) { throw "calculation still $(@('done', 'calculating', 'pending')[$state]) after $seconds s" }
    Start-Sleep -Milliseconds 200
  }
}

function Set-Cell($cell, $value) {
  # Through InvokeMember, because PowerShell's COM binder reuses the first assignment's value type for later ones at
  # the same call site, and fails on a second set of a different type. Whole numbers go to Excel as doubles.
  if ($value -is [int] -or $value -is [long] -or $value -is [decimal]) { $value = [double]$value }
  [void]$cell.GetType().InvokeMember('Value2', [Reflection.BindingFlags]::SetProperty, $null, $cell, [object[]]@($value))
}

function Invoke-Open($xl, [string] $path, [bool] $readOnly, [int] $corruptLoad) {
  # Named arguments through InvokeMember: Excel rejects CorruptLoad when it is passed by position.
  $books = $xl.Workbooks
  $names = [string[]]@('Filename', 'UpdateLinks', 'ReadOnly', 'IgnoreReadOnlyRecommended', 'AddToMru', 'CorruptLoad')
  $values = [object[]]@([string]$path, [int]0, [bool]$readOnly, [bool]$true, [bool]$false, [int]$corruptLoad)
  return $books.GetType().InvokeMember('Open', [Reflection.BindingFlags]::InvokeMethod, $null, $books, $values, $null, $null, $names)
}

function Open-Book($xl, [string] $path, [bool] $readOnly) {
  # Normal load first; then Excel's repair (xlRepairFile), then data extraction (xlExtractData). Each is recorded.
  $info = [ordered]@{ load = $null; failed_loads = [ordered]@{} }
  $wb = $null
  foreach ($mode in @(@('normal', 0), @('repair', 1), @('extract data', 2))) {
    try { $wb = Invoke-Open $xl $path $readOnly $mode[1]; $info.load = $mode[0]; break }
    catch { $info.failed_loads[$mode[0]] = $(if ($_.Exception.InnerException) { $_.Exception.InnerException.Message } else { $_.Exception.Message }) }
  }
  if (-not $wb) { return $null, $info }
  $info.repaired = ($info.load -ne 'normal')
  $links = $wb.LinkSources(1)
  $info.external_links = if ($links) { @($links) } else { @() }
  $info.file_format = $wb.FileFormat
  return $wb, $info
}

$xl = $null; $wb = $null; $excelPid = 0
try {
  $registered = [XlCom.BusyFilter]::Register(1000 * [int]$spec.calc_timeout_s)
  if ($registered -ne 0) { throw ('the busy filter did not register: 0x{0:X8}' -f $registered) }
  $xl = New-Object -ComObject Excel.Application
  $p = [uint32]0; [void][XlCom.Win]::GetWindowThreadProcessId([IntPtr]$xl.Hwnd, [ref]$p); $excelPid = [int]$p
  Set-Content -LiteralPath (Join-Path $dir 'excel.pid') -Value $excelPid
  $xl.Visible = $false; $xl.DisplayAlerts = $false; $xl.ScreenUpdating = $false; $xl.AskToUpdateLinks = $false
  $xl.EnableEvents = $false; $xl.AutomationSecurity = 3
  $result.excel = [ordered]@{ version = $xl.Version; build = $xl.Build; os = $xl.OperatingSystem; pid = $excelPid }
  $result.timings_ms.start = $sw.ElapsedMilliseconds

  $opened = Open-Book $xl $spec.workbook $false; $wb = $opened[0]; $result.open = $opened[1]
  if (-not $wb) { throw "Excel could not open the workbook in any load mode" }
  $result.timings_ms.open = $sw.ElapsedMilliseconds
  if ($spec.hold_s -gt 0) { Start-Sleep -Seconds $spec.hold_s }
  $result.settings = [ordered]@{ calculation_as_opened = $xl.Calculation; iteration = $xl.Iteration; max_iterations = $xl.MaxIterations; max_change = $xl.MaxChange; force_full_calculation = $wb.ForceFullCalculation }
  $xl.Calculation = -4105
  $xl.CalculateFullRebuild(); $result.settings.calculation_state = Wait-Calculation $xl $wb $spec.calc_timeout_s
  $result.timings_ms.rebuild = $sw.ElapsedMilliseconds
  $result.base = Read-Refs $wb $spec.read
  $result.errors = Find-Errors $wb

  if ($spec.set.Count -gt 0) {
    foreach ($s in $spec.set) { $t = Get-Target $wb $s.ref; if ($t.Count -ne 1) { throw "set target $($s.ref) is not one cell" }; Set-Cell $t $s.value }
    $xl.CalculateFull(); [void](Wait-Calculation $xl $wb $spec.calc_timeout_s)
    $result.after = Read-Refs $wb $spec.read
    $result.timings_ms.after = $sw.ElapsedMilliseconds
  }

  $wb.SaveAs($spec.out, 51); $wb.Close($false); $wb = $null
  $opened = Open-Book $xl $spec.out $true; $wb = $opened[0]; $reopenInfo = $opened[1]
  if (-not $wb) { throw "Excel could not reopen the saved copy" }
  $xl.CalculateFullRebuild(); [void](Wait-Calculation $xl $wb $spec.calc_timeout_s)
  $result.reopened = [ordered]@{ open = $reopenInfo; values = (Read-Refs $wb $spec.read) }
  $wb.Close($false); $wb = $null
  $result.timings_ms.reopen = $sw.ElapsedMilliseconds
  $result.ok = $true
} catch {
  $result.error = $_.Exception.Message
  $result.error_line = $_.InvocationInfo.ScriptLineNumber
} finally {
  # Closing and quitting wait for a busy Excel too, but briefly: the stop by process ID below is what ends it.
  [XlCom.BusyFilter]::LimitMs = [Math]::Min([XlCom.BusyFilter]::LimitMs, 10000)
  try { if ($wb) { $wb.Close($false) } } catch { }
  try { if ($xl) { $xl.Quit() } } catch { }
  foreach ($o in @($wb, $xl)) { if ($o) { try { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($o) } catch { } } }
  [GC]::Collect(); [GC]::WaitForPendingFinalizers()
  if ($excelPid) {
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-Process -Id $excelPid -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 300 }
    $left = Get-Process -Id $excelPid -ErrorAction SilentlyContinue
    if ($left -and $left.ProcessName -eq 'EXCEL') { Stop-Process -Id $excelPid -Force; $result.killed_after_quit = $true }
  }
  $result.timings_ms.end = $sw.ElapsedMilliseconds
  $result.busy_retries = [XlCom.BusyFilter]::Retries
  $result | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $dir 'result.json') -Encoding UTF8
}

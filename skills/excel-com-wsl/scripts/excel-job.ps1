<#
Runs one fixed Excel job. excel.py writes job.json and calls this with Windows PowerShell. It never runs anything the
job names: it opens the given workbook copy in a new, hidden Excel
instance with macros, link updates and alerts off, rebuilds the calculation, reads and sets the given references,
saves a copy, reopens that copy, and quits. It writes result.json beside job.json, and excel.pid and ps.pid first,
so excel.py can stop this instance, and only this instance, after a timeout.
#>
param([Parameter(Mandatory = $true)] [string] $Job)
$ErrorActionPreference = 'Stop'
$dir = Split-Path -Parent $Job
$spec = Get-Content -Raw -LiteralPath $Job | ConvertFrom-Json
Set-Content -LiteralPath (Join-Path $dir 'ps.pid') -Value $PID
$sw = [Diagnostics.Stopwatch]::StartNew()
$result = [ordered]@{ ok = $false; error = $null; excel = $null; open = $null; settings = $null; base = $null; errors = $null; after = $null; reopened = $null; timings_ms = [ordered]@{} }
$missing = [Type]::Missing
$errorNames = @{ 2000 = '#NULL!'; 2007 = '#DIV/0!'; 2015 = '#VALUE!'; 2023 = '#REF!'; 2029 = '#NAME?'; 2036 = '#NUM!'; 2042 = '#N/A'; 2043 = '#GETTING_DATA'; 2045 = '#SPILL!'; 2046 = '#CONNECT!'; 2047 = '#BLOCKED!'; 2048 = '#UNKNOWN!'; 2049 = '#FIELD!'; 2050 = '#CALC!' }

Add-Type -Namespace XlCom -Name Win -MemberDefinition '[DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint p);'

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

function Read-Refs($wb, $refs) {
  $out = [ordered]@{}
  foreach ($ref in $refs) {
    try {
      $r = Get-Target $wb $ref
      if ($r.Count -gt 2000) { throw "range has $($r.Count) cells; the limit is 2000" }
      $entry = [ordered]@{ address = "'" + $r.Worksheet.Name + "'!" + $r.Address() }
      if ($r.Count -eq 1) {
        $entry.value = Convert-Cell $r.Value2
        $entry.text = $r.Text
        $entry.formula = $r.Formula2
        $entry.has_spill = $r.HasSpill
      } else {
        $v = $r.Value2; $rows = @()
        for ($i = 1; $i -le $r.Rows.Count; $i++) { $row = @(); for ($j = 1; $j -le $r.Columns.Count; $j++) { $row += , (Convert-Cell $v[$i, $j]) }; $rows += , $row }
        $entry.values = $rows
      }
      $out[$ref] = $entry
    } catch { $out[$ref] = [ordered]@{ read_error = $_.Exception.Message } }
  }
  return $out
}

function Find-Errors($wb) {
  $found = @()
  foreach ($ws in $wb.Worksheets) {
    try { $found += "'$($ws.Name)'!" + $ws.UsedRange.SpecialCells(-4123, 16).Address($false, $false) } catch { }
  }
  return , $found
}

function Wait-Calculation($xl, $wb, [int] $seconds) {
  # The calculate methods return when they finish; this waits for anything still running after them. A workbook set
  # to force full calculation reports xlPending (2) for as long as it is open, so that state counts as done there.
  $deadline = (Get-Date).AddSeconds($seconds)
  while ($true) {
    $state = [int]$xl.CalculationState
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
} finally {
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
  $result | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $dir 'result.json') -Encoding UTF8
}

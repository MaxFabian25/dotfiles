<#
Test helper for test_fixtures.py. Runs Read-Refs and Find-Errors from excel-job.ps1 on stand-in workbooks, with no
Excel: some answer as a healthy Excel does, some hand back $null for one property or throw a rejected call, as
PowerShell does when a busy Excel refused it and the wait ran out, and some sheets cannot list their error cells, as a
protected sheet cannot, or hold error values that Excel does not list, as a spilled range does. It writes what each made of them to -Log as JSON.
#>
param([Parameter(Mandatory = $true)] [string] $JobScript, [Parameter(Mandatory = $true)] [string] $Log)
$ErrorActionPreference = 'Stop'
$ast = [Management.Automation.Language.Parser]::ParseFile($JobScript, [ref]$null, [ref]$null)
foreach ($function in $ast.FindAll({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] }, $false)) {
  . ([scriptblock]::Create($function.Extent.Text))
}
$errorNames = @{}

function New-Range($count, $value, $formula, $rows, $columns) {
  $range = [pscustomobject]@{
    Count = $count; Value2 = $value; Formula2 = $formula; Text = 'text'; HasSpill = $false
    Worksheet = [pscustomobject]@{ Name = 'S' }; Rows = $(if ($rows) { [pscustomobject]@{ Count = $rows } }); Columns = $(if ($columns) { [pscustomobject]@{ Count = $columns } })
  }
  return $range | Add-Member -MemberType ScriptMethod -Name Address -Value { '$A$1' } -PassThru
}

$twoRows = [Array]::CreateInstance([object], [int[]](2, 1), [int[]](1, 1))
$twoRows[1, 1] = 1.0; $twoRows[2, 1] = 2.0
$ranges = [ordered]@{
  healthy_cell = New-Range 1 5.0 '5' 1 1
  empty_cell   = New-Range 1 $null '' 1 1
  healthy_rows = New-Range 2 $twoRows $null 2 1
  no_range     = $null
  no_count     = New-Range $null 5.0 '5' 1 1
  no_formula   = New-Range 1 $null $null 1 1
  no_value     = New-Range 1 $null '=A1' 1 1
  no_values    = New-Range 2 $null $null 2 1
  no_rows      = New-Range 2 $twoRows $null $null $null
}
$names = [pscustomobject]@{ Ranges = $ranges } | Add-Member -MemberType ScriptMethod -Name Item -Value { param($name) [pscustomobject]@{ RefersToRange = $this.Ranges[$name] } } -PassThru
$workbook = [pscustomobject]@{ Names = $names }

# A stand-in sheet lists its formula cells holding errors and its typed-in error values through SpecialCells of its
# used range, and counts its cells holding error values through Evaluate, as Excel does. $none, $protected and
# $refused are what those calls throw.
function New-Sheet([string] $name, $formulas, $total, $typed) {
  $used = [pscustomobject]@{ Formulas = $formulas; Typed = $typed } | Add-Member -MemberType ScriptMethod -Name Address -Value { 'A1:B2' } -PassThru
  $used | Add-Member -MemberType ScriptMethod -Name SpecialCells -Value {
    param($type, $value)
    $answer = if ($type -eq -4123) { $this.Formulas } else { $this.Typed }
    if ($answer -is [scriptblock]) { return & $answer }
    return $answer
  }
  $sheet = [pscustomobject]@{ Name = $name; UsedRange = $(if ($formulas -ne 'no used range') { $used }); Total = $total }
  return $sheet | Add-Member -MemberType ScriptMethod -Name Evaluate -Value { if ($this.Total -is [scriptblock]) { & $this.Total } else { $this.Total } } -PassThru
}
$errorCells = [pscustomobject]@{ Count = 2 } | Add-Member -MemberType ScriptMethod -Name Address -Value { 'A1:A2' } -PassThru
$oneTyped = [pscustomobject]@{ Count = 1 }
$none = { throw [Runtime.InteropServices.COMException]::new('No cells were found.', -2146827284) }
$protected = { throw [Runtime.InteropServices.COMException]::new('You cannot use this command on a protected sheet.', -2146827284) }
$refused = { throw [Runtime.InteropServices.COMException]::new('refused', -2147418111) }
$uncounted = { throw 'the scan counted the cells of a sheet whose error cells Excel listed' }
$sheets = [ordered]@{
  finds_cells       = @((New-Sheet 'S' $errorCells $uncounted $none))
  no_error_cells    = @((New-Sheet 'S' $none 0.0 $none))
  typed_only        = @((New-Sheet 'S' $none 1.0 $oneTyped))
  cannot_list       = @((New-Sheet 'Open' $errorCells $uncounted $none), (New-Sheet 'Locked' $protected 1.0 $protected), (New-Sheet 'Clean' $protected 0.0 $protected))
  spilled_only      = @((New-Sheet 'Spill' $none 3.0 $oneTyped))
  spilled_untyped   = @((New-Sheet 'Spill' $none 2.0 $none))
  locked_typed_only = @((New-Sheet 'Locked' $protected 1.0 $protected))
  negative_count    = @((New-Sheet 'Odd' $none ([double]-1) $none))
  count_is_an_error = @((New-Sheet 'Old' $none ([int]-2146826259) $none))
  count_throws      = @((New-Sheet 'Odd' $none $protected $none))
  no_sheets         = $null
  no_used_range     = @((New-Sheet 'S' 'no used range' 1.0 $none))
  no_cells          = @((New-Sheet 'S' $null 1.0 $none))
  refused_call      = @((New-Sheet 'S' $refused 1.0 $none))
  no_count          = @((New-Sheet 'S' $none $null $none))
  refused_count     = @((New-Sheet 'S' $none $refused $none))
  no_typed_count    = @((New-Sheet 'S' $none 1.0 ([pscustomobject]@{ Count = $null })))
  refused_typed     = @((New-Sheet 'S' $none 1.0 $refused))
  no_typed_cells    = @((New-Sheet 'S' $none 1.0 { $null }))
}
$scans = [ordered]@{}
foreach ($case in $sheets.Keys) {
  try { $scan = Find-Errors ([pscustomobject]@{ Worksheets = $sheets[$case] }); $scans[$case] = [ordered]@{ found = $scan.found; unscanned = $scan.unscanned } }
  catch { $scans[$case] = [ordered]@{ scan_error = $_.Exception.Message } }
}
[ordered]@{ reads = (Read-Refs $workbook @($ranges.Keys)); scans = $scans } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $Log -Encoding UTF8

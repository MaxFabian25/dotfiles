<#
Test helper for test_fixtures.py. Runs Read-Refs and Find-Errors from excel-job.ps1 on stand-in workbooks, with no
Excel: some answer as a healthy Excel does, and some hand back $null for one property or throw a rejected call, as
PowerShell does when a busy Excel refused it and the wait ran out. It writes what each made of them to -Log as JSON.
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

function New-Sheet([string] $name, $used) { return [pscustomobject]@{ Name = $name; UsedRange = $used } }
function New-Used([scriptblock] $specialCells) { return [pscustomobject]@{} | Add-Member -MemberType ScriptMethod -Name SpecialCells -Value $specialCells -PassThru }
$errorCells = [pscustomobject]@{} | Add-Member -MemberType ScriptMethod -Name Address -Value { 'A1:A2' } -PassThru
$withErrors = New-Used { $this.Cells }; $withErrors | Add-Member -NotePropertyName Cells -NotePropertyValue $errorCells
$sheets = [ordered]@{
  finds_cells   = @((New-Sheet 'S' $withErrors))
  no_error_cells = @((New-Sheet 'S' (New-Used { throw [Runtime.InteropServices.COMException]::new('No cells were found.', -2146827284) })))
  no_sheets     = $null
  no_used_range = @((New-Sheet 'S' $null))
  refused_call  = @((New-Sheet 'S' (New-Used { throw [Runtime.InteropServices.COMException]::new('refused', -2147418111) })))
  no_cells      = @((New-Sheet 'S' (New-Used { $null })))
}
$scans = [ordered]@{}
foreach ($case in $sheets.Keys) {
  try { $scans[$case] = [ordered]@{ found = (Find-Errors ([pscustomobject]@{ Worksheets = $sheets[$case] })) } }
  catch { $scans[$case] = [ordered]@{ scan_error = $_.Exception.Message } }
}
[ordered]@{ reads = (Read-Refs $workbook @($ranges.Keys)); scans = $scans } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $Log -Encoding UTF8

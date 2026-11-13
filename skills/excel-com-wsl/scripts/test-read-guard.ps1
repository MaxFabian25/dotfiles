<#
Test helper for test_fixtures.py. Runs Read-Refs, Get-Cell and Find-Errors from excel-job.ps1 on stand-in workbooks,
with no Excel: some answer as a healthy Excel does, some hand back $null for one property or throw a rejected call, as
PowerShell does when a busy Excel refused it and the wait ran out, and some sheets answer the error scan's questions
with something that is no answer. A stand-in range knows how many rectangles it has, and a stand-in name its text.
A stand-in workbook with sheets takes the references that name a sheet. It writes what each made of them to -Log as
JSON.
#>
param([Parameter(Mandatory = $true)] [string] $JobScript, [Parameter(Mandatory = $true)] [string] $Log)
$ErrorActionPreference = 'Stop'
$ast = [Management.Automation.Language.Parser]::ParseFile($JobScript, [ref]$null, [ref]$null)
foreach ($function in $ast.FindAll({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] }, $false)) {
  . ([scriptblock]::Create($function.Extent.Text))
}
$errorNames = @{}

function New-Range($count, $value, $formula, $rows, $columns, $areas = 1) {
  # $areas is the count of rectangles, $null for a count that Excel refused, or 'refused' for refused Areas.
  $range = [pscustomobject]@{
    Count = $count; Value2 = $value; Formula2 = $formula; Text = 'text'; HasSpill = $false
    Areas = $(if ('refused' -ne $areas) { [pscustomobject]@{ Count = $areas } })
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
  # Two rectangles: Value2 is the first one's, two cells here and one cell there, and Count covers both.
  two_areas      = New-Range 4 $twoRows $null 2 1 2
  one_cell_first = New-Range 3 101.0 '101' 1 1 2
  no_areas       = New-Range 1 5.0 '5' 1 1 'refused'
  no_area_count  = New-Range 1 5.0 '5' 1 1 $null
  # Names for which Excel has no range: long_name has a text, as a name of too many rectangles or of no cells has,
  # and late_range gets its range on the second ask, as after a call that a busy Excel refused.
  long_name      = $null
  late_range     = $null
  # Names whose text lists 40 rectangles, for which Excel gave a range of fewer: one, or 17 as on 12 November 2026.
  # no_text has a range and a text that Excel refused.
  cut_to_one     = New-Range 1 5.0 '5' 1 1
  cut_short      = New-Range 34 $twoRows $null 2 1 17
  no_text        = New-Range 1 5.0 '5' 1 1
}
# Each name has a text of one rectangle unless it is named here. The texts of the last nine have commas that are
# no union, inside a quoted sheet name, a function's parentheses, a string or a table's brackets, and three of them a
# union whose comma follows a parenthesis inside quotes, an apostrophe inside brackets or a function's parentheses.
$texts = @{ long_name = '=' + ('S!$A$1,' * 40); two_areas = '=S!$A$1:$A$2,S!$C$1:$C$2'; one_cell_first = '=S!$A$1,S!$C$1:$C$2'
  cut_to_one = '=' + (@(foreach ($row in 1..40) { "S!`$A`$$row" }) -join ','); no_range = $null; no_text = $null }
$texts.cut_short = $texts.cut_to_one
$commas = [ordered]@{
  in_quotes = '=''a,b''!$A$1'; doubled_in_quotes = '=''It''''s, ok''!$A$1'; in_parentheses = '=OFFSET(S!$A$1,0,0,1,1)'
  in_string = '=IF(S!$A$1=")",S!$A$1,S!$B$1)'; nested_columns = '=T[[#This Row],[a]]'; escaped_column = '=T[x''],y]'
  parenthesis_in_quotes = '=''a)b''!$A$1,S!$B$1'; apostrophe_in_column = '=T[''#x],S!$A$1'; after_parentheses = '=OFFSET(S!$A$1,0,0,1,1),S!$B$1'
}
foreach ($name in $commas.Keys) { $ranges[$name] = New-Range 1 5.0 '5' 1 1; $texts[$name] = $commas[$name] }
foreach ($name in @($ranges.Keys)) { if (-not $texts.ContainsKey($name)) { $texts[$name] = '=S!$A$1' } }
$later = @{ late_range = New-Range 1 5.0 '5' 1 1 }
$names = [pscustomobject]@{ Ranges = $ranges; Texts = $texts; Later = $later } | Add-Member -MemberType ScriptMethod -Name Item -Value {
  param($name)
  $second = if ($this.Later.ContainsKey($name)) { $this.Later[$name] } else { $this.Ranges[$name] }
  $entry = [pscustomobject]@{ RefersTo = $this.Texts[$name]; Asked = 0; First = $this.Ranges[$name]; Second = $second }
  return $entry | Add-Member -MemberType ScriptProperty -Name RefersToRange -Value { $this.Asked++; if ($this.Asked -eq 1) { $this.First } else { $this.Second } } -PassThru
} -PassThru
$workbook = [pscustomobject]@{ Names = $names }

# A stand-in sheet knows which of its cells hold an error value and of which kind: 'formula', 'typed' or 'spilled'.
# It answers the scan's two formulas through Evaluate and HasSpill through Range, in the shapes Excel gave on
# 12 November 2026: a number for one cell, a list for one row, a grid from 1 otherwise, and DBNull from HasSpill where
# the cells of a range differ. An entry in $answers replaces one of those answers, with a value or a script block
# that may throw. It reads addresses with its own code, not the job script's.
function ConvertFrom-Address([string] $address) {
  $ends = @($address -split ':'); if ($ends.Count -eq 1) { $ends = $ends[0], $ends[0] }
  $corners = foreach ($end in $ends) {
    if ($end -notmatch '^([A-Z]+)(\d+)$') { throw "the stand-in cannot read the address $address" }
    $column = 0; foreach ($letter in $Matches[1].ToCharArray()) { $column = $column * 26 + [int]$letter - 64 }
    , @([int]$Matches[2], $column)
  }
  return @{ row = $corners[0][0]; column = $corners[0][1]; rows = $corners[1][0] - $corners[0][0] + 1; columns = $corners[1][1] - $corners[0][1] + 1 }
}

function Get-Overlap($one, $other) {
  $rows = [Math]::Min($one.row + $one.rows, $other.row + $other.rows) - [Math]::Max($one.row, $other.row)
  $columns = [Math]::Min($one.column + $one.columns, $other.column + $other.columns) - [Math]::Max($one.column, $other.column)
  if ($rows -le 0 -or $columns -le 0) { return $null }
  return @{ row = [Math]::Max($one.row, $other.row); column = [Math]::Max($one.column, $other.column); rows = $rows; columns = $columns }
}

function Get-StandIn($sheet, [string] $question, [scriptblock] $otherwise) {
  [void]$sheet.Asked.Add($question)
  if (-not $sheet.Answers.ContainsKey($question)) { return & $otherwise }
  $answer = $sheet.Answers[$question]
  if ($answer -is [scriptblock]) { return & $answer }
  return $answer
}

function New-Sheet([string] $name, [string] $usedAddress, $cells, $answers = @{}) {
  $areas = @(foreach ($address in $cells.Keys) { $area = ConvertFrom-Address $address; $area.kind = $cells[$address]; $area })
  $box = ConvertFrom-Address $usedAddress
  $used = [pscustomobject]@{ Row = $box.row; Column = $box.column; Rows = [pscustomobject]@{ Count = $box.rows }; Columns = [pscustomobject]@{ Count = $box.columns } }
  if ($answers.ContainsKey('used')) { $used = $answers.used }
  $sheet = [pscustomobject]@{ Name = $(if ($answers.ContainsKey('name')) { $answers.name } else { $name }); UsedRange = $used; Areas = $areas; Answers = $answers; Asked = (New-Object Collections.ArrayList) }
  $sheet | Add-Member -MemberType ScriptMethod -Name Evaluate -Value {
    param($formula)
    $sheet = $this
    if ($formula -match '^ISERROR\(SUM\(([A-Z0-9:]+)\)\)$') {
      $box = ConvertFrom-Address $Matches[1]
      return Get-StandIn $sheet 'any' { [bool]@($sheet.Areas | Where-Object { Get-Overlap $_ $box }).Count }
    }
    if ($formula -match '^ISERROR\(([A-Z0-9:]+)\)\*\(1\+ISFORMULA\(\1\)\)$') {
      $box = ConvertFrom-Address $Matches[1]
      return Get-StandIn $sheet 'cells' {
        $kinds = New-Object 'double[]' ($box.rows * $box.columns)
        foreach ($area in $sheet.Areas) {
          $shared = Get-Overlap $area $box
          if (-not $shared) { continue }
          $kind = if ($area.kind -eq 'formula') { 2.0 } else { 1.0 }
          for ($row = $shared.row; $row -lt $shared.row + $shared.rows; $row++) { for ($column = $shared.column; $column -lt $shared.column + $shared.columns; $column++) { $kinds[($row - $box.row) * $box.columns + $column - $box.column] = $kind } }
        }
        if ($kinds.Length -eq 1) { return $kinds[0] }
        if ($box.rows -eq 1) { return , [object[]]$kinds }
        $grid = [Array]::CreateInstance([object], [int[]]($box.rows, $box.columns), [int[]](1, 1))
        for ($at = 0; $at -lt $kinds.Length; $at++) { $grid[(1 + ($at - $at % $box.columns) / $box.columns), (1 + $at % $box.columns)] = $kinds[$at] }
        return , $grid
      }
    }
    throw "the stand-in does not know the formula $formula"
  }
  $sheet | Add-Member -MemberType ScriptMethod -Name Range -Value {
    param($address)
    $sheet = $this; $box = ConvertFrom-Address $address
    return Get-StandIn $sheet 'range' {
      $range = [pscustomobject]@{ Sheet = $sheet; Box = $box }
      $range | Add-Member -MemberType ScriptProperty -Name HasSpill -Value {
        $range = $this
        Get-StandIn $range.Sheet 'spilled' {
          $spilled = 0; foreach ($area in $range.Sheet.Areas) { $shared = Get-Overlap $area $range.Box; if ($shared -and $area.kind -eq 'spilled') { $spilled += $shared.rows * $shared.columns } }
          if ($spilled -eq 0) { return $false }
          if ($spilled -eq $range.Box.rows * $range.Box.columns) { return $true }
          return [DBNull]::Value
        }
      } -PassThru
    }
  }
  return $sheet
}

$many = [ordered]@{}; for ($i = 1; $i -le 150; $i++) { $many[[string][char](65 + ($i % 7) * 2) + (2 * $i)] = 'formula' }
$protected = { throw [Runtime.InteropServices.COMException]::new('You cannot use this command on a protected sheet.', -2146827284) }
$refused = { throw [Runtime.InteropServices.COMException]::new('refused', -2147418111) }
$nameError = [int]-2146826259  # The error value #NAME?, as Evaluate hands it back.
$typed = @{ A1 = 'typed' }; $both = @{ A1 = 'formula'; B2 = 'typed' }; $spilled = @{ A2 = 'spilled' }
$sheets = [ordered]@{
  finds_cells          = @((New-Sheet 'S' 'A1:B3' @{ 'A1:A2' = 'formula' }))
  no_error_cells       = @((New-Sheet 'S' 'A1:B3' @{}))
  typed_only           = @((New-Sheet 'S' 'A1:B3' $typed))
  several_sheets       = @((New-Sheet 'Open' 'A1:B3' @{ 'A1:A2' = 'formula' }), (New-Sheet 'Clean' 'A1:B3' @{}), (New-Sheet 'Other' 'A1' @{ A1 = 'formula' }))
  spilled              = @((New-Sheet 'Spill' 'A1:E8' @{ 'A2:A3' = 'spilled'; C1 = 'formula'; A8 = 'typed' }))
  spilled_only         = @((New-Sheet 'Spill' 'B2:B5' @{ 'B4:B5' = 'spilled' }))
  typed_beside_spilled = @((New-Sheet 'Mix' 'A1:C40' @{ A2 = 'spilled'; B2 = 'typed'; A4 = 'spilled'; B4 = 'typed'; 'C7:C9' = 'spilled'; C10 = 'typed' }))
  long_list            = @((New-Sheet 'Many' 'A2:M300' $many))
  reading_order        = @((New-Sheet 'Block' 'B2:F6' @{ B6 = 'formula'; 'F2:F4' = 'formula'; 'B2:D4' = 'formula' }))
  apostrophe           = @((New-Sheet "It's" 'B2' @{ B2 = 'formula' }))
  far_apart            = @((New-Sheet 'Far' 'A1:XFD1048576' @{ A1 = 'formula'; ZZ900000 = 'formula'; XFD1048576 = 'typed' }))
  column_of_errors     = @((New-Sheet 'Column' 'A1:AD40000' @{ 'C1:C40000' = 'formula' }))
  row_of_errors        = @((New-Sheet 'Row' 'A1:KN20001' @{ 'A20001:KN20001' = 'formula' }))
  over_the_limit       = @((New-Sheet 'Wide' 'A1:AD40000' @{ 'C1:C40000' = 'formula' }), (New-Sheet 'Small' 'A1:B3' @{ A1 = 'formula' }))
  any_is_an_error      = @((New-Sheet 'Old' 'A1:B3' $both @{ any = $nameError }))
  any_is_no_boolean    = @((New-Sheet 'Odd' 'A1:B3' $both @{ any = 1.0 }))
  any_throws           = @((New-Sheet 'Odd' 'A1:B3' $both @{ any = $protected }))
  cells_are_an_error   = @((New-Sheet 'Old' 'A1:B3' $both @{ cells = $nameError }))
  cells_throw          = @((New-Sheet 'Odd' 'A1:B3' $both @{ cells = $protected }))
  too_few_cells        = @((New-Sheet 'Odd' 'A1:B3' $both @{ cells = { , [object[]](2.0, 0.0, 0.0) } }))
  one_cell_is_odd      = @((New-Sheet 'Odd' 'A1:B3' $both @{ cells = { , [object[]](2.0, 0.0, 3.0, 0.0, 0.0, 0.0) } }))
  one_cell_is_an_error = @((New-Sheet 'Odd' 'A1:B3' $both @{ cells = { , [object[]](2.0, 0.0, $nameError, 0.0, 0.0, 0.0) } }))
  one_cell_is_missing  = @((New-Sheet 'Odd' 'A1:B3' $both @{ cells = { , [object[]](2.0, 0.0, $null, 0.0, 0.0, 0.0) } }))
  spill_is_odd         = @((New-Sheet 'Odd' 'A1:B3' $spilled @{ spilled = [DBNull]::Value }))
  no_sheets            = $null
  no_name              = @((New-Sheet 'S' 'A1:B3' $both @{ name = $null }))
  no_used_range        = @((New-Sheet 'S' 'A1:B3' $both @{ used = $null }))
  no_first_row         = @((New-Sheet 'S' 'A1:B3' $both @{ used = [pscustomobject]@{ Row = $null; Column = 1; Rows = [pscustomobject]@{ Count = 3 }; Columns = [pscustomobject]@{ Count = 2 } } }))
  no_rows              = @((New-Sheet 'S' 'A1:B3' $both @{ used = [pscustomobject]@{ Row = 1; Column = 1; Rows = $null; Columns = [pscustomobject]@{ Count = 2 } } }))
  no_columns           = @((New-Sheet 'S' 'A1:B3' $both @{ used = [pscustomobject]@{ Row = 1; Column = 1; Rows = [pscustomobject]@{ Count = 3 }; Columns = $null } }))
  no_row_count         = @((New-Sheet 'S' 'A1:B3' $both @{ used = [pscustomobject]@{ Row = 1; Column = 1; Rows = [pscustomobject]@{ Count = $null }; Columns = [pscustomobject]@{ Count = 2 } } }))
  no_column_count      = @((New-Sheet 'S' 'A1:B3' $both @{ used = [pscustomobject]@{ Row = 1; Column = 1; Rows = [pscustomobject]@{ Count = 3 }; Columns = [pscustomobject]@{ Count = $null } } }))
  no_any               = @((New-Sheet 'S' 'A1:B3' $both @{ any = $null }))
  refused_any          = @((New-Sheet 'S' 'A1:B3' $both @{ any = $refused }))
  no_cells             = @((New-Sheet 'S' 'A1:B3' $both @{ cells = $null }))
  refused_cells        = @((New-Sheet 'S' 'A1:B3' $both @{ cells = $refused }))
  no_range             = @((New-Sheet 'S' 'A1:B3' $spilled @{ range = $null }))
  refused_range        = @((New-Sheet 'S' 'A1:B3' $spilled @{ range = $refused }))
  no_spill             = @((New-Sheet 'S' 'A1:B3' $spilled @{ spilled = $null }))
}
$limits = @{ over_the_limit = 30000 }
$scans = [ordered]@{}
foreach ($case in $sheets.Keys) {
  try {
    $book = [pscustomobject]@{ Worksheets = $sheets[$case] }
    $scan = if ($limits.ContainsKey($case)) { Find-Errors $book $limits[$case] } else { Find-Errors $book }
    $scans[$case] = [ordered]@{ found = $scan.found; unscanned = $scan.unscanned; asked = @($sheets[$case] | ForEach-Object { $_.Asked.Count }) }
  } catch { $scans[$case] = [ordered]@{ scan_error = $_.Exception.Message } }
}

# A stand-in workbook with sheets, for a reference that names its sheet, in quotes or not. Worksheets.Item knows a
# sheet by its exact name and nothing else, a range gives its address as Excel does, such as $B$2 or $B$2:$C$3, and
# what is no cell or rectangle, such as a table column, as it was asked. A sheet turns away Elsewhere and Nowhere, as
# Excel's does a name whose cells lie on another sheet. The workbook has four names, each scoped to the sheet
# Okonomi: Elsewhere, whose cell lies on the sheet S, FY2024, which is also the address of a cell, LocalConst,
# which has a text and no range, and Cut, whose text lists two cells where its range is one. Its name lookup keeps what it was asked for. Okonomi is spelt with the letter O with
# a stroke and Cafe with an accent as a combining mark, built from their codes because Windows PowerShell reads a
# script without a byte-order mark in the ANSI code page. The second round reads the addresses that the first round
# gave.
function New-Cell($sheet, [string] $address) {
  $cell = [pscustomobject]@{ Count = 1; Value2 = 5.0; Formula2 = '5'; Text = '5'; HasSpill = $false; Worksheet = $sheet; Asked = $address; Areas = [pscustomobject]@{ Count = 1 } }
  return $cell | Add-Member -MemberType ScriptMethod -Name Address -Value { $this.Asked.Replace('$', '') -replace '([A-Z]+)(\d+)', '$$$1$$$2' } -PassThru
}
$stroked = [string][char]0xD8 + 'konomi'; $marked = 'Cafe' + [string][char]0x301
$known = @{
  "$stroked!Elsewhere"  = [pscustomobject]@{ RefersTo = '=S!$A$1'; RefersToRange = (New-Cell ([pscustomobject]@{ Name = 'S' }) 'A1') }
  "$stroked!FY2024"     = [pscustomobject]@{ RefersTo = "=$stroked!`$B`$2"; RefersToRange = (New-Cell ([pscustomobject]@{ Name = $stroked }) 'B2') }
  "$stroked!LocalConst" = [pscustomobject]@{ RefersTo = '=5'; RefersToRange = $null }
  "$stroked!Cut"        = [pscustomobject]@{ RefersTo = '=S!$A$1,S!$C$1'; RefersToRange = (New-Cell ([pscustomobject]@{ Name = 'S' }) 'A1') }
}
$scoped = [pscustomobject]@{ Known = $known; Asked = (New-Object Collections.ArrayList) } | Add-Member -MemberType ScriptMethod -Name Item -Value {
  param($name)
  [void]$this.Asked.Add($name)
  # 0x800A03EC is what Excel's Names.Item gives for a name the workbook lacks.
  if (-not $this.Known.ContainsKey($name)) { throw [Runtime.InteropServices.COMException]::new("the stand-in workbook has no name $name", -2146827284) }
  return $this.Known[$name]
} -PassThru
$titled = [pscustomobject]@{ Titles = @("It's", "O'Neil's", 'My sheet', 'S', 'A', $stroked, 'Data_1', $marked) } | Add-Member -MemberType ScriptMethod -Name Item -Value {
  param($name)
  if ($this.Titles -cnotcontains $name) { throw "the stand-in workbook has no sheet $name" }
  return [pscustomobject]@{ Name = $name } | Add-Member -MemberType ScriptMethod -Name Range -Value {
    param($address)
    if ($address -in 'Elsewhere', 'Nowhere') { throw "the stand-in sheet $($this.Name) has no range $address" }
    New-Cell $this $address
  } -PassThru
} -PassThru
$quotedBook = [pscustomobject]@{ Names = $scoped; Worksheets = $titled }
$quoted = Read-Refs $quotedBook @("'It''s'!B2", "'O''Neil''s'!C7", "'My sheet'!A1", 'S!A1', "'$stroked'!A1", "'My sheet'!T[Owner''s share]", "S!T['#Items]", "$stroked!B3", 'Data_1!A1', "$marked!A2", "$stroked!Elsewhere", "'$stroked'!Elsewhere", 'S!Elsewhere', "$stroked!FY2024", "'$stroked'!FY2024", "$stroked!LocalConst", "$stroked!Cut", 'S!Nowhere', 'My sheet!A1', 'My-sheet!A1', "'It's'!B2", "'O'Neil''s'!C7", "'It''s!B2", "'A'!B'C'!D1")
$lookups = @($scoped.Asked)
# Workbooks whose name lookup a busy Excel refused: the call is rejected, hands back $null, or Names itself is $null.
# The sheet knows FY2024 as a cell, so a read that went on to the sheet would give that cell.
$rejected = [pscustomobject]@{} | Add-Member -MemberType ScriptMethod -Name Item -Value { param($name) throw [Runtime.InteropServices.COMException]::new('refused', -2147418111) } -PassThru
$silent = [pscustomobject]@{} | Add-Member -MemberType ScriptMethod -Name Item -Value { param($name) $null } -PassThru
$refusedLookups = [ordered]@{}
foreach ($case in @(@('rejected', $rejected), @('no_name', $silent), @('no_names', $null))) {
  $refusedLookups[$case[0]] = (Read-Refs ([pscustomobject]@{ Names = $case[1]; Worksheets = $titled }) @("$stroked!FY2024"))["$stroked!FY2024"]
}
# Workbooks whose name lookup fails with an error other than a missing name's: 0x800AC472, which Excel's object model
# gives while it takes no calls, 0x800706BA (the server is gone), 0x80010105 (the server failed), and one that is no
# COM error at all.
$failedLookups = [ordered]@{}
foreach ($code in -2146777998, -2147023174, -2147417851, 0) {
  $failing = [pscustomobject]@{ Code = $code } | Add-Member -MemberType ScriptMethod -Name Item -Value {
    param($name)
    if ($this.Code -eq 0) { throw 'the stand-in lookup broke' }
    throw [Runtime.InteropServices.COMException]::new(('the stand-in lookup failed with 0x{0:X8}' -f $this.Code), $this.Code)
  } -PassThru
  $failedLookups[$(if ($code) { '0x{0:X8}' -f $code } else { 'other' })] = (Read-Refs ([pscustomobject]@{ Names = $failing; Worksheets = $titled }) @("$stroked!FY2024"))["$stroked!FY2024"]
}
# What a --set makes of a cell, a name whose text lists 40 rectangles where Excel gave one cell, and two rectangles.
$sets = [ordered]@{}
foreach ($name in 'healthy_cell', 'cut_to_one', 'two_areas') {
  try { $cell = Get-Cell $workbook $name; $sets[$name] = "cells: $($cell.Count)" } catch { $sets[$name] = $_.Exception.Message }
}
$quotedAgain = Read-Refs $quotedBook @($quoted.Values | Where-Object { $_.Contains('address') } | ForEach-Object { $_.address })
[ordered]@{ reads = (Read-Refs $workbook @($ranges.Keys)); scans = $scans; quoted = $quoted; name_lookups = $lookups; refused_lookups = $refusedLookups; failed_lookups = $failedLookups; sets = $sets; quoted_again = $quotedAgain } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $Log -Encoding UTF8

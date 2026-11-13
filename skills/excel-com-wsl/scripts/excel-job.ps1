<#
Runs one fixed Excel job. excel.py writes job.json and calls this with Windows PowerShell. It never runs anything the
job names: it opens the given workbook copy in a new, hidden Excel
instance with macros, link updates and alerts off, rebuilds the calculation, reads and sets the given references,
saves a copy, reopens that copy, and quits. It writes result.json beside job.json, and excel.pid and ps.pid first,
so excel.py can stop this instance, and only this instance, after a timeout. A call that Excel turns away because it
is busy waits and goes again, for up to the calculation timeout. ok here says that Excel finished the job; excel.py
turns it to false when a read failed or the error scan could not tell for a sheet.
#>
param([Parameter(Mandatory = $true)] [string] $Job)
$ErrorActionPreference = 'Stop'
$dir = Split-Path -Parent $Job
$spec = Get-Content -Raw -LiteralPath $Job | ConvertFrom-Json
Set-Content -LiteralPath (Join-Path $dir 'ps.pid') -Value $PID
$sw = [Diagnostics.Stopwatch]::StartNew()
$result = [ordered]@{ ok = $false; error = $null; error_line = $null; busy_retries = 0; excel = $null; open = $null; settings = $null; base = $null; errors = $null; errors_unscanned = $null; after = $null; reopened = $null; timings_ms = [ordered]@{} }
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

function Measure-References([string] $text) {
  # How many references a name's text lists: one more than its commas outside quotes, brackets and parentheses, which
  # in the English, A1-style text that RefersTo gives are union operators. A doubled quote inside quotes ends them
  # and opens them again, an array constant sits inside a function's parentheses, and inside a table's brackets an
  # apostrophe marks the next character as part of a column's name. A reference that is another name or a function's
  # result counts as one, so this is the least the name has.
  $count = 1; $depth = 0; $brackets = 0; $quote = $null
  for ($at = 0; $at -lt $text.Length; $at++) {
    $c = $text[$at]
    if ($quote) { if ($c -eq $quote) { $quote = $null } }
    elseif ($brackets -gt 0) { if ($c -eq "'") { $at++ } elseif ($c -eq '[') { $brackets++ } elseif ($c -eq ']') { $brackets-- } }
    elseif ($c -eq "'" -or $c -eq '"') { $quote = $c }
    elseif ($c -eq '[') { $brackets++ }
    elseif ($c -eq '(') { $depth++ }
    elseif ($c -eq ')') { $depth-- }
    elseif ($c -eq ',' -and $depth -eq 0) { $count++ }
  }
  return $count
}

function Get-NameRange($name, [string] $ref) {
  # The range a name refers to, and how many references its text lists. $name is what Names.Item gave for $ref.
  $range = $name.RefersToRange
  # For a name of many rectangles Excel can give a range of fewer, with no error, and the text whole: on 12 November
  # 2026 a name of 40 gave a range of 17 and its text of 568 characters. So the text is asked for every name.
  $text = $name.RefersTo
  if ($null -eq $range) {
    # Excel has no range, and no error, for a name that refers to no cells, such as a constant or a formula, to cells
    # on several sheets, or to more rectangles than it gives as one range. It answers for such a name's text, so the
    # text and a second ask tell that from a call a busy Excel refused, which comes back as $null too.
    $range = $name.RefersToRange
    if ($null -eq $range -and $text -is [string]) { throw "Excel gave no range for the name $ref, whose text has $($text.Length) characters: it has none for a name that refers to no cells, to cells on several sheets, or to more rectangles than it gives as one range, so give each rectangle as a REF of its own" }
    if ($null -eq $range) { return $null, 0 }
  }
  Confirm-Answer $text "the text of the name $ref"
  return $range, (Measure-References $text)
}

function Get-Target($wb, [string] $ref) {
  # The range a REF names, and for a name how many references its text lists, else 0. A sheet name in quotes has
  # each apostrophe in it doubled, as Excel writes it and as errors and a read's address give it. A reference that
  # opens with a quote and is not such a name and a range is refused: no workbook name starts with a quote, and taking
  # 'It's' for the sheet It's would leave 'It''s' meaning two sheets. No range holds an apostrophe before a !, so a
  # quoted name that an undoubled apostrophe before a ! ended early is refused too. A table column can hold an
  # apostrophe elsewhere, as in T['#Items]. A sheet name without quotes holds letters of any script with their marks,
  # digits and underscores.
  if ($ref -match '^(?:''(?<q>(?:[^'']|'''')+)''|(?<s>[\p{L}\p{M}\p{Nd}_]+))!(?<a>(?:[^'']|''(?!!))+)$') {
    $quoted = $Matches['q']; $unquoted = $Matches['s']; $address = $Matches['a']
    # Until 13 November 2026 an unquoted sheet name held A to Z, digits and underscores only, and a reference with
    # any other letter before its ! was looked up as a name, which finds a name scoped to that sheet wherever its
    # cells lie. Such a reference still goes to the name lookup first, so that what read then reads the same, and
    # to the sheet when the workbook has no such name. With A to Z alone, a sheet named in Danish never got there.
    # A lookup that failed for another reason, such as a busy Excel's refusal, ends the read: going on to the sheet
    # would read the cell FY2024 where the workbook has a name of that spelling.
    if (-not $quoted -and $unquoted -notmatch '^[A-Za-z0-9_]+$') {
      $names = $wb.Names; Confirm-Answer $names 'the names'
      $known = $true
      try { $name = $names.Item($ref) } catch { if (-not (Test-Missing $_)) { throw }; $known = $false }
      if ($known) { return Get-NameRange $name $ref }
    }
    $sheet = if ($quoted) { $quoted.Replace("''", "'") } else { $unquoted }
    # Returned beside the count, a multi-cell range is not unrolled into its cells: PowerShell unrolls one level only.
    return $wb.Worksheets.Item($sheet).Range($address), 0
  }
  if ($ref.StartsWith("'")) { throw "$ref is no sheet name in single quotes with a range after it: double each apostrophe inside the quotes, as in 'It''s'!B2" }
  return Get-NameRange ($wb.Names.Item($ref)) $ref
}

function Get-Cell($wb, [string] $ref) {
  # The one cell a --set writes to. A name whose text lists several references is not one cell, even where Excel gave
  # a range of one cell for it.
  $cell, $listed = Get-Target $wb $ref
  if ($cell.Count -ne 1 -or $listed -gt 1) { throw "set target $ref is not one cell" }
  return , $cell
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
      $r, $listed = Get-Target $wb $ref
      Confirm-Answer $r 'the range'
      # Value2 of several rectangles is the first one's values, while Count and Address cover them all, so such a
      # read would give part of the range under the address of the whole. A name's text can list more rectangles than
      # the range Excel gives for it, which may have only one.
      $areas = $r.Areas; Confirm-Answer $areas 'the rectangles'
      $areaCount = $areas.Count; Confirm-Answer $areaCount 'the count of rectangles'
      if ($listed -gt $areaCount) { $areaCount = $listed }
      if ($areaCount -gt 1) { throw "range has $areaCount rectangles; read each rectangle by its own REF" }
      $count = $r.Count; Confirm-Answer $count 'the cell count'
      if ($count -gt 2000) { throw "range has $count cells; the limit is 2000" }
      $sheet = $r.Worksheet.Name; Confirm-Answer $sheet 'the sheet name'
      $address = $r.Address(); Confirm-Answer $address 'the address'
      $entry = [ordered]@{ address = "'" + $sheet.Replace("'", "''") + "'!" + $address }
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

function Test-Missing($caught) {
  # 0x800A03EC, wherever PowerShell wrapped it, which Excel's Names.Item gives for a name the workbook lacks. Excel
  # gives that code for many other failures too, so this cannot tell a missing name from them.
  for ($e = $caught.Exception; $e; $e = $e.InnerException) { if ($e.HResult -eq -2146827284) { return $true } }
  return $false
}

function ConvertTo-ColumnName([int] $number) {
  $name = ''
  while ($number -gt 0) { $number--; $name = [string][char](65 + $number % 26) + $name; $number = [int][Math]::Floor($number / 26) }
  return $name
}

function Get-Address($piece) {
  # A piece is a rectangle of a sheet: its first row and column, and how many rows and columns it has.
  $first = (ConvertTo-ColumnName $piece.column) + $piece.row
  if ($piece.rows -eq 1 -and $piece.columns -eq 1) { return $first }
  return $first + ':' + (ConvertTo-ColumnName ($piece.column + $piece.columns - 1)) + ($piece.row + $piece.rows - 1)
}

function Get-Answer($sheet, [string] $formula) {
  # What Excel makes of a formula on a sheet: its value, or why it gave none. Evaluate hands back an error value, not
  # an exception, for a formula Excel cannot work out. A call that a busy Excel refused ends the job.
  try { $value = $sheet.Evaluate($formula) } catch { if (Test-Refused $_) { throw }; return @{ failed = $_.Exception.Message } }
  Confirm-Answer $value "the formula $formula"
  if ($value -is [int]) { return @{ failed = "$formula came back as " + (Convert-Cell $value).error } }
  return @{ value = $value }
}

function Add-Work($scan, [long] $amount) {
  $scan.work += $amount
  if ($scan.work -gt $scan.limit -and -not $scan.why) { $scan.why = "its error values are spread over too many cells to list: the scan reads at most $($scan.limit) cells of one sheet" }
}

function Test-ErrorValue($sheet, $piece, $scan) {
  # Whether a piece holds an error value. SUM passes over texts and booleans and is an error when a cell it adds
  # holds one, on a protected sheet too, and answers in milliseconds for a whole sheet.
  Add-Work $scan $scan.question
  if ($scan.why) { return $false }
  $area = Get-Address $piece
  $asked = Get-Answer $sheet "ISERROR(SUM($area))"
  if (-not $asked.failed -and $asked.value -isnot [bool]) { $asked.failed = "ISERROR(SUM($area)) came back as $($asked.value)" }
  if ($asked.failed) { $scan.why = "Excel could not say whether $area holds an error value: $($asked.failed)"; return $false }
  return $asked.value
}

function Select-Halves($sheet, $first, $second, $scan) {
  # The halves that hold an error value, of a piece that holds one: where the first has none, the second does.
  if (-not (Test-ErrorValue $sheet $first $scan)) { return , @($second) }
  if (Test-ErrorValue $sheet $second $scan) { return , @($first, $second) }
  return , @($first)
}

function Read-Piece($sheet, $piece, $scan) {
  # Excel answers for every cell of the piece: 0 for no error value, 2 for a formula cell holding one, and 1 for an
  # error value in a cell with no formula of its own, which is typed in or spilled from another cell's formula.
  $cells = $piece.rows * $piece.columns
  Add-Work $scan $cells
  if ($scan.why) { return }
  $area = Get-Address $piece
  $asked = Get-Answer $sheet "ISERROR($area)*(1+ISFORMULA($area))"
  if (-not $asked.failed) {
    $answers = if ($asked.value -is [array]) { $asked.value.Length } else { 1 }
    if ($answers -ne $cells) { $asked.failed = "$answers answers for $cells cells" }
  }
  if ($asked.failed) { $scan.why = "Excel could not say which cells of $area hold error values: $($asked.failed)"; return }
  $at = 0; $width = $piece.columns; $formulas = $scan.formulas; $others = $scan.others
  foreach ($kind in $asked.value) {
    if ($kind -ne 0) {
      $across = $at % $width
      $key = ([long]($piece.column + $across) -shl 21) + $piece.row + ($at - $across) / $width
      if ($kind -is [double] -and $kind -eq 2) { $formulas.Add($key) }
      elseif ($kind -is [double] -and $kind -eq 1) { $others.Add($key) }
      else { $scan.why = "Excel could not say which cells of $area hold error values: one answer is '$kind'"; return }
    }
    $at++
  }
}

function Join-Cells($keys) {
  # The cells as addresses of rectangles, in reading order: runs down each column, joined across neighbouring columns
  # where the runs match. A key is a cell's column times 2^21 plus its row, so sorted keys run down each column in
  # turn, and the cell below a cell has the next key.
  $keys.Sort()
  $runs = New-Object Collections.ArrayList; $run = $null; $last = -2
  foreach ($key in $keys) {
    if ($key -eq $last + 1) { $run.rows++ }
    else { $run = @{ row = [int]($key -band 2097151); column = [int]($key -shr 21); rows = 1; columns = 1 }; [void]$runs.Add($run) }
    $last = $key
  }
  $open = @{}; $blocks = @{}; $order = New-Object 'Collections.Generic.List[long]'
  foreach ($run in $runs) {
    $span = "$($run.row) $($run.rows)"; $block = $open[$span]
    if ($block -and $block.column + $block.columns -eq $run.column) { $block.columns++; continue }
    $open[$span] = $run; $place = ([long]$run.row -shl 21) + $run.column; $blocks[$place] = $run; $order.Add($place)
  }
  $order.Sort()
  return @(foreach ($place in $order) { Get-Address $blocks[$place] }) -join ','
}

function Add-Spilled($sheet, $scan) {
  # Moves the spilled cells among scan.others to scan.formulas; the rest are typed in. HasSpill of a range is true
  # or false when all its cells agree and DBNull when they differ. So Excel is asked about the smallest range that
  # holds a run of the sorted cells, and where its cells differ, about each half of the run.
  $keys = $scan.others; $keys.Sort()
  $runs = New-Object Collections.Stack; $runs.Push(@(0, $keys.Count))
  while ($runs.Count -gt 0) {
    $from, $to = $runs.Pop()
    Add-Work $scan (4 * $scan.question)
    if ($scan.why) { return }
    $box = @{ column = [int]($keys[$from] -shr 21); row = 2097151; rows = 0; columns = 0 }
    $box.columns = [int]($keys[$to - 1] -shr 21) - $box.column + 1
    for ($at = $from; $at -lt $to; $at++) { $row = [int]($keys[$at] -band 2097151); if ($row -lt $box.row) { $box.row = $row }; if ($row -gt $box.rows) { $box.rows = $row } }
    $box.rows = $box.rows - $box.row + 1
    $area = Get-Address $box
    $range = $sheet.Range($area); Confirm-Answer $range "the range $area"
    $spilled = $range.HasSpill; Confirm-Answer $spilled "whether $area is spilled"
    if ($spilled -is [bool]) { if ($spilled) { $scan.formulas.AddRange($keys.GetRange($from, $to - $from)) } }
    elseif ($to - $from -gt 1) { $half = $from + [int][Math]::Floor(($to - $from) / 2); $runs.Push(@($from, $half)); $runs.Push(@($half, $to)) }
    else { $scan.why = "Excel could not say whether $area is spilled"; return }
  }
}

function Find-Errors($wb, [long] $limit = 2000000) {
  # The cells holding an error value that a formula made, on each sheet, and the sheets where this could not tell.
  # Typed-in error values are left out, as they always were. Excel is asked in formulas, which it answers on a
  # protected sheet too, where SpecialCells cannot look; SpecialCells also lists only the first cell of a spilled
  # range and its Address is cut at 256 characters. A piece of the used range that holds an error value is halved
  # until it is small enough to read cell by cell. Halving the columns first keeps a column of errors in a long sheet
  # to its own cells, and where both of those halves hold one, halving the rows does the same for a row of errors.
  #
  # limit protects the job's time limit from one sheet. It counts cells read, with a question about a piece as 1000
  # and one about spilling as 4000. On 12 November 2026 a cell read cost about 3 microseconds, or 20 where it holds
  # an error, and a question 2 to 5 ms on an idle PC and 15 ms on a busy one, so a sheet of scattered error cells
  # reached the limit after 9 s in one job and 32 s in another. A sheet where every cell read holds an error takes
  # up to a minute. A piece is read at 8192 cells, about 25 ms, because halving it again would cost up to four
  # questions.
  $found = @(); $unscanned = @()
  $sheets = $wb.Worksheets; Confirm-Answer $sheets 'the sheets'
  foreach ($ws in $sheets) {
    $name = $ws.Name; Confirm-Answer $name 'the sheet name'
    $used = $ws.UsedRange; Confirm-Answer $used 'the used range of a sheet'
    # The counts are asked of Rows and Columns themselves: PowerShell counts a refused Rows as 0, with no error.
    $rows = $used.Rows; Confirm-Answer $rows 'the rows of the used range'
    $columns = $used.Columns; Confirm-Answer $columns 'the columns of the used range'
    $whole = @{ row = $used.Row; column = $used.Column; rows = $rows.Count; columns = $columns.Count }
    foreach ($side in 'row', 'column', 'rows', 'columns') { Confirm-Answer $whole[$side] "the $side of the used range" }
    $scan = @{ work = 0; limit = $limit; question = 1000; why = $null; formulas = (New-Object 'Collections.Generic.List[long]'); others = (New-Object 'Collections.Generic.List[long]') }
    $pieces = New-Object Collections.Stack
    if (Test-ErrorValue $ws $whole $scan) { $pieces.Push($whole) }
    while ($pieces.Count -gt 0 -and -not $scan.why) {
      $piece = $pieces.Pop()
      if ([long]$piece.rows * $piece.columns -le 8192) { Read-Piece $ws $piece $scan; continue }
      $next = $null
      if ($piece.columns -gt 1) {
        $half = [int][Math]::Floor($piece.columns / 2)
        $next = Select-Halves $ws @{ row = $piece.row; column = $piece.column; rows = $piece.rows; columns = $half } @{ row = $piece.row; column = $piece.column + $half; rows = $piece.rows; columns = $piece.columns - $half } $scan
      }
      if ($piece.rows -gt 1 -and ($null -eq $next -or $next.Count -eq 2)) {
        $half = [int][Math]::Floor($piece.rows / 2)
        $across = Select-Halves $ws @{ row = $piece.row; column = $piece.column; rows = $half; columns = $piece.columns } @{ row = $piece.row + $half; column = $piece.column; rows = $piece.rows - $half; columns = $piece.columns } $scan
        if ($null -eq $next -or $across.Count -eq 1 -or $piece.rows -gt $piece.columns) { $next = $across }
      }
      foreach ($part in $next) { $pieces.Push($part) }
    }
    if (-not $scan.why -and $scan.others.Count -gt 0) { Add-Spilled $ws $scan }
    if ($scan.why) { $unscanned += [ordered]@{ sheet = $name; reason = $scan.why }; continue }
    if ($scan.formulas.Count -eq 0) { continue }
    $found += "'" + $name.Replace("'", "''") + "'!" + (Join-Cells $scan.formulas)
  }
  return @{ found = $found; unscanned = $unscanned }
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
  $result.timings_ms.read = $sw.ElapsedMilliseconds
  # The scan asks in A1 addresses. A workbook saved in R1C1 reference style puts this Excel in that style, where
  # C10:C12 means columns 10 to 12, so the scan runs in A1 style and the style is set back for the save.
  $style = $xl.ReferenceStyle; Confirm-Answer $style 'the reference style'
  try { if ($style -ne 1) { $xl.ReferenceStyle = 1 }; $scan = Find-Errors $wb } finally { if ($style -ne 1) { $xl.ReferenceStyle = $style } }
  $result.errors = $scan.found; $result.errors_unscanned = $scan.unscanned
  $result.timings_ms.scan = $sw.ElapsedMilliseconds

  if ($spec.set.Count -gt 0) {
    foreach ($s in $spec.set) { Set-Cell (Get-Cell $wb $s.ref) $s.value }
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

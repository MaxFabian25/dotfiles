# Open a deck in desktop PowerPoint without a window and report where PowerPoint put everything.
# powerpoint.py copies the deck beside this script and runs it. It stops when PowerPoint is already
# running unless -AllowRunning is given, and it quits only a PowerPoint it started.
param([Parameter(Mandatory = $true)][string]$Name, [switch]$Pdf, [switch]$AllowRunning)
$ErrorActionPreference = 'Stop'
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$running = @(Get-Process POWERPNT -ErrorAction SilentlyContinue).Count
if ($running -ne 0 -and -not $AllowRunning) { "PowerPoint is already running, so the job stops."; exit 3 }
# New-Object attaches to a running PowerPoint when there is one, so Quit() is only for an instance this script started.
$app = New-Object -ComObject PowerPoint.Application
# ReadOnly, not Untitled, no window.
$deck = $app.Presentations.Open((Join-Path $dir "$Name.pptx"), -1, 0, 0)

# Without a window, BoundWidth, BoundHeight and Lines() are right, and BoundLeft and BoundTop are not.
function Size($range) { @{ width = [double]$range.BoundWidth; height = [double]$range.BoundHeight } }

# Lines PowerPoint broke inside a word, as "end of one line | start of the next".
function Splits($range, $count) {
  $found = New-Object System.Collections.ArrayList
  for ($i = 1; $i -lt $count; $i++) {
    $a = $range.Lines($i).Text; $b = $range.Lines($i + 1).Text
    if ($a -cmatch '[\p{L}\p{N}]-?$' -and $b -cmatch '^[\p{L}\p{N}]') { [void]$found.Add($a.Trim() + ' | ' + $b.Trim()) }
  }
  , $found
}

# Text size and lines, with the frame's inner margins and word wrap, which the fit depends on.
function TextReport($frame) {
  $range = $frame.TextRange
  $item = Size $range
  $item.lines = [int]$range.Lines().Count
  if ($item.lines -gt 1) { $splits = Splits $range $item.lines; if ($splits.Count -gt 0) { $item.splits = $splits } }
  $item.margins = @{ left = [double]$frame.MarginLeft; right = [double]$frame.MarginRight; top = [double]$frame.MarginTop; bottom = [double]$frame.MarginBottom }
  $item.wordwrap = ([int]$frame.WordWrap -ne 0)
  $item
}

$pages = New-Object System.Collections.ArrayList
foreach ($slide in $deck.Slides) {
  $shapes = New-Object System.Collections.ArrayList
  foreach ($shape in $slide.Shapes) {
    $item = @{ name = $shape.Name; type = [int]$shape.Type; left = [double]$shape.Left; top = [double]$shape.Top; width = [double]$shape.Width; height = [double]$shape.Height }
    if ([int]$shape.HasTable -ne 0) {
      $table = $shape.Table
      $rows = New-Object System.Collections.ArrayList
      for ($r = 1; $r -le $table.Rows.Count; $r++) {
        $cells = New-Object System.Collections.ArrayList
        for ($c = 1; $c -le $table.Columns.Count; $c++) {
          $frame = $table.Cell($r, $c).Shape.TextFrame
          if ($frame.TextRange.Length -gt 0) { [void]$cells.Add((TextReport $frame)) } else { [void]$cells.Add(@{ lines = 0 }) }
        }
        [void]$rows.Add(@{ height = [double]$table.Rows($r).Height; cells = $cells })
      }
      $columns = New-Object System.Collections.ArrayList
      for ($c = 1; $c -le $table.Columns.Count; $c++) { [void]$columns.Add([double]$table.Columns($c).Width) }
      $item.rows = $rows
      $item.columns = $columns
    }
    elseif ([int]$shape.HasTextFrame -ne 0 -and [int]$shape.TextFrame.HasText -ne 0) {
      $item.text = TextReport $shape.TextFrame
      # 0 is no autofit, 1 grows the shape to fit its text, 2 shrinks the text to fit the shape.
      $item.text.autosize = [int]$shape.TextFrame2.AutoSize
    }
    [void]$shapes.Add($item)
  }
  # A link to another slide reads "slideId,slideIndex,title" in SubAddress.
  $links = New-Object System.Collections.ArrayList
  foreach ($link in $slide.Hyperlinks) { [void]$links.Add(@{ address = $link.Address; subaddress = $link.SubAddress }) }
  $section = if ($deck.SectionProperties.Count -gt 0) { $deck.SectionProperties.Name($slide.sectionIndex) } else { $null }
  [void]$pages.Add(@{ page = [int]$slide.SlideIndex; layout = $slide.CustomLayout.Name; section = $section; shapes = $shapes; links = $links })
}
$report = @{ width = [double]$deck.PageSetup.SlideWidth; height = [double]$deck.PageSetup.SlideHeight; pages = $pages; version = $app.Version + '.' + $app.Build }
# Windows PowerShell's own UTF-8 writers add a byte-order mark, so write without one.
[IO.File]::WriteAllText((Join-Path $dir "$Name.json"), ($report | ConvertTo-Json -Depth 12 -Compress), (New-Object Text.UTF8Encoding $false))
# 32 is ppSaveAsPDF.
if ($Pdf) { $deck.SaveAs((Join-Path $dir "$Name.pdf"), 32) }
$deck.Close()
if ($running -eq 0) {
  $app.Quit()
  # POWERPNT can take a few seconds to exit after Quit(), so wait for it before the next job starts.
  for ($i = 0; $i -lt 60 -and @(Get-Process POWERPNT -ErrorAction SilentlyContinue).Count -ne 0; $i++) { Start-Sleep -Milliseconds 500 }
}
"measured $($pages.Count) pages"

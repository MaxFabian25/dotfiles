<#
Finds and deletes the Office log files of the Excel session that the job in DIR started. Office writes four log files
for each Excel session, two to Diagnostics\EXCEL in the user's temp folder and two to its Additional folder. Each file
starts at 20 MiB. A clean quit cuts the files to what they hold, and an Excel that crashes or is stopped leaves them at
20 MiB. They stay after the session ends, so on 13 November 2026 two threads' planted-error rounds left 11.93 GiB
there. Office records each running Excel under its process ID in the key SESSIONS, with the session's GUID that the
log files are named after. It deletes that entry when Excel quits and keeps it when Excel crashes or is stopped.

With -Record, it waits up to 10 s for that entry while the job's Excel runs, and writes the GUID to excel.session in
DIR. Without it, once that Excel has ended, it deletes the files named after the GUID, retrying for 5 s while they are
still locked. It takes the GUID from excel.session, or from the entry when the job was stopped before it wrote that
file. It deletes no other session's files. Either way it prints what it did as one line of JSON, with a reason when
it found no session or left a file, and fails no job.
#>
param([Parameter(Mandatory = $true)] [string] $Dir, [switch] $Record,
      [string] $Sessions = 'HKCU:\Software\Microsoft\Office\Common\CrashPersistence\EXCEL')
$ErrorActionPreference = 'Stop'

function Find-Session {
  # A job's PowerShell writes ps.pid before it starts Excel, Office stamps the session 0.3 s after Excel starts, and
  # the job writes excel.pid 1.6 to 2.4 s after that (13 November 2026). So an entry stamped before ps.pid, left by an
  # earlier Excel with the same ID, or after excel.pid, written by a later one, is not this job's.
  $pidFile = Get-Item -LiteralPath (Join-Path $Dir 'excel.pid')
  $id = [int](Get-Content -LiteralPath $pidFile.FullName -Raw).Trim()
  $after = (Get-Item -LiteralPath (Join-Path $Dir 'ps.pid')).LastWriteTimeUtc.ToFileTimeUtc()
  $before = $pidFile.LastWriteTimeUtc.ToFileTimeUtc()
  $key = Join-Path $Sessions $id
  if (-not (Test-Path -LiteralPath $key)) { return $null }
  $info = [Text.Encoding]::UTF8.GetString((Get-ItemProperty -LiteralPath $key -Name '4').'4') | ConvertFrom-Json
  $stamped = [int64]$info.SessionInitTime
  if ($info.ProcessId -ne $id -or $stamped -lt $after -or $stamped -gt $before) { return $null }
  return $info.SessionId.Trim('{}')
}

$file = Join-Path $Dir 'excel.session'
if ($Record) {
  $out = [ordered]@{ session = $null; reason = $null }
  $failed = $null
  $deadline = (Get-Date).AddSeconds(10)
  do {
    # Office makes the entry before it writes its value, so a read that fails is tried again too.
    try { $out.session = Find-Session; $failed = $null } catch { $failed = $_.Exception.Message }
    if ($out.session) { break }
    Start-Sleep -Milliseconds 100
  } while ((Get-Date) -lt $deadline)
  if (-not $out.session) {
    $out.reason = if ($failed) { $failed } else { 'Office recorded no session for this Excel within 10 s' }
  } else {
    try { Set-Content -LiteralPath $file -Value $out.session } catch { $out.session = $null; $out.reason = $_.Exception.Message }
  }
  return ($out | ConvertTo-Json -Compress)
}

$out = [ordered]@{ session = $null; removed = 0; bytes = 0; left = 0; reason = $null }
try {
  $session = if (Test-Path -LiteralPath $file) { "$(Get-Content -LiteralPath $file -Raw)".Trim() }
  if (-not $session) { $session = Find-Session }
  $folder = Join-Path ([IO.Path]::GetTempPath()) 'Diagnostics\EXCEL'
  if ($session -notmatch '^[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}$') {
    $out.reason = 'no Office session was recorded for the job''s Excel'
  } elseif (-not (Test-Path -LiteralPath $folder)) {
    $out.session = $session; $out.reason = 'Office wrote no log folder'
  } else {
    $out.session = $session
    $failed = $null
    $deadline = (Get-Date).AddSeconds(5)
    do {
      foreach ($log in @(Get-ChildItem -LiteralPath $folder -Recurse -File -Filter "*_$session.log")) {
        $size = $log.Length
        try { Remove-Item -LiteralPath $log.FullName -Force; $out.removed++; $out.bytes += $size } catch { $failed = $_.Exception.Message }
      }
      $out.left = @(Get-ChildItem -LiteralPath $folder -Recurse -File -Filter "*_$session.log").Count
      if ($out.left -eq 0) { break }
      Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    if ($out.left) { $out.reason = "$($out.left) left: $failed" }
    elseif (-not $out.removed) { $out.reason = 'no log file of the session was found' }
  }
} catch {
  $out.reason = $_.Exception.Message
}
$out | ConvertTo-Json -Compress

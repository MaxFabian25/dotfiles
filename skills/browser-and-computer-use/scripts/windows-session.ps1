param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-zA-Z0-9_-]+$')]
    [string]$Session,
    [string]$Url = 'about:blank',
    [string[]]$LaunchArguments = @()
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8

function Get-Sessions {
    $listing = agent-browser session list --json | ConvertFrom-Json
    if ($LASTEXITCODE -or -not $listing.success) { throw 'Cannot inspect existing agent-browser sessions' }
    @($listing.data.sessions)
}

if ((Get-Sessions) -contains $Session) {
    throw "Session '$Session' already exists; use it or close it first"
}

# Windows OpenSSH ends every process in its connection's job when the connection exits,
# including the agent-browser daemon. A process created through WMI starts outside that job,
# so the daemon it starts keeps serving later connections until the session closes or idles out.
# The daemon inherits the opener's output handles. The opener therefore writes to files,
# because a pipe would stay open for as long as the daemon runs. A running daemon keeps its
# files locked, so stale files are removed on a later start once they are released.
$logs = Join-Path ([IO.Path]::GetTempPath()) 'agent-browser-sessions'
New-Item -ItemType Directory -Force $logs | Out-Null
Get-ChildItem $logs -File | Where-Object { $_.BaseName -eq $Session -or $_.LastWriteTime -lt (Get-Date).AddDays(-1) } |
    Remove-Item -ErrorAction SilentlyContinue
$log = Join-Path $logs $Session

function Quote([string]$Value) { "'" + ($Value -replace "'", "''") + "'" }
function Quote-Native([string]$Value) { '"' + ($Value -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"' }
$cli = (Get-Command agent-browser -CommandType Application | Select-Object -First 1).Source
$arguments = @('--session', $Session) + $LaunchArguments + @('open', $Url) | ForEach-Object { Quote (Quote-Native $_) }
$script = "`$opener = Start-Process -FilePath $(Quote $cli) -ArgumentList @($($arguments -join ', ')) -PassThru -NoNewWindow " +
          "-RedirectStandardOutput $(Quote "$log.out") -RedirectStandardError $(Quote "$log.err"); " +
          "`$opener.WaitForExit(); Set-Content $(Quote "$log.exit") `$opener.ExitCode"
$shell = [Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($script))
$created = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine = "`"$shell`" -NoProfile -NonInteractive -EncodedCommand $encoded"
    CurrentDirectory = (Get-Location).ProviderPath
}
if ($created.ReturnValue) { throw "Cannot start the session outside SSH (WMI error $($created.ReturnValue))" }

Wait-Process -Id $created.ProcessId -Timeout 120 -ErrorAction SilentlyContinue
if (-not (Test-Path "$log.exit")) { throw "agent-browser open did not finish within 120 seconds; see $log.out and $log.err" }
Get-Content -Encoding utf8 "$log.out", "$log.err" -ErrorAction SilentlyContinue
$code = [int](Get-Content "$log.exit")
Remove-Item "$log.*" -ErrorAction SilentlyContinue
if ($code -or (Get-Sessions) -notcontains $Session) { throw "Session '$Session' did not start (exit code $code)" }

param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-zA-Z0-9_-]+$')]
    [string]$Session,
    [Parameter(Mandatory = $true)]
    [string]$OutputDirectory,
    [string]$Url = 'about:blank',
    [switch]$Persistent,
    [switch]$Headed
)

$ErrorActionPreference = 'Stop'
$before = playwright-cli list --json | ConvertFrom-Json
if ($LASTEXITCODE) { throw 'Cannot inspect existing Playwright sessions' }
if (@($before.browsers | Where-Object name -eq $Session).Count) {
    throw "Session '$Session' already exists; use its current owner connection"
}

$env:PLAYWRIGHT_MCP_OUTPUT_DIR = $OutputDirectory
$arguments = @("-s=$Session", 'open', $Url)
if ($Persistent) { $arguments += '--persistent' }
if ($Headed) { $arguments += '--headed' }
playwright-cli @arguments
if ($LASTEXITCODE) { throw 'Playwright browser failed to open' }

# Windows OpenSSH ends its child processes when the SSH job exits.
# Keep this connection until another CLI call closes this named session.
try {
    do {
        Start-Sleep -Seconds 3
        $sessions = playwright-cli list --json | ConvertFrom-Json
        if ($LASTEXITCODE) { throw 'Cannot inspect the owned browser session' }
    } while (@($sessions.browsers | Where-Object name -eq $Session).Count)
} finally {
    playwright-cli "-s=$Session" close
}

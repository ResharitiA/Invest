[CmdletBinding()]
param(
    [ValidateSet("Shugg", "Local", "Current")]
    [string]$Provider = "Shugg",
    [string]$DominoServiceName = "HCLDominoServer(CProgramFilesHCLDominoData)",
    [switch]$NoBrowser,
    [switch]$NoDominoLog,
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"
$gardenUrl = "http://127.0.0.1:8000"

function Test-Garden {
    try {
        $nodes = Invoke-RestMethod "$gardenUrl/api/nodes" -TimeoutSec 2
        return ($nodes -is [array] -and $nodes -contains "manual" -and $nodes -contains "webhook")
    } catch { return $false }
}

function Test-Domino {
    $server = Get-Process -Name nserver -ErrorAction SilentlyContinue
    if (-not $server) { return $false }
    $client = New-Object Net.Sockets.TcpClient
    try {
        $connection = $client.ConnectAsync("127.0.0.1", 1352)
        if (-not $connection.Wait(2000)) { return $false }
        return $client.Connected
    } catch { return $false }
    finally { $client.Dispose() }
}

if (-not $CheckOnly -and -not (Test-Path "$PSScriptRoot\.venv\Scripts\python.exe")) {
    throw "Missing .venv\Scripts\python.exe. Install project dependencies first."
}

if (-not (Test-Domino)) {
    if ($CheckOnly) { throw "Domino NRPC is not ready on localhost:1352." }
    Write-Host "Waiting for Domino NRPC (up to 90 seconds)..."
    $service = Get-Service -Name $DominoServiceName -ErrorAction Stop
    if (-not (Get-Process -Name nserver -ErrorAction SilentlyContinue) -and $service.Status -ne "Running") {
        $escapedName = $DominoServiceName.Replace("'", "''")
        $command = '$ErrorActionPreference = ''Stop''; Start-Service -Name ''' + $escapedName + ''''
        $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
        $elevated = Start-Process powershell.exe -Verb RunAs -ArgumentList @("-NoProfile", "-EncodedCommand", $encoded) -PassThru -Wait
        if ($elevated.ExitCode -ne 0) { throw "Could not start the Domino service." }
    }
    $deadline = (Get-Date).AddSeconds(90)
    while (-not (Test-Domino)) {
        if ((Get-Date) -gt $deadline) { throw "Domino NRPC did not become ready on localhost:1352. Check the server console." }
        Start-Sleep -Seconds 2
    }
}
Write-Host "Domino NRPC: OK"
try {
    $http = Invoke-WebRequest "http://127.0.0.1/" -UseBasicParsing -TimeoutSec 3
    if ($http.Headers["Server"] -notmatch "Domino") { throw "Unexpected HTTP server" }
    Write-Host "Domino HTTP: OK"
} catch {
    Write-Warning "Domino HTTP is unavailable. Outbound agent requests to Garden do not require it; NSF web pages remain unavailable."
}

$agentDeadline = (Get-Date).AddSeconds(30)
while (-not (Get-Process -Name namgr -ErrorAction SilentlyContinue)) {
    if ($CheckOnly -or (Get-Date) -gt $agentDeadline) {
        throw "Agent Manager is not running. In the Domino console run: load amgr"
    }
    Write-Host "Waiting for Agent Manager..."
    Start-Sleep -Seconds 2
}
Write-Host "Agent Manager: running (NSF agent execution is checked separately)"

if (-not (Test-Garden)) {
    if ($CheckOnly) { throw "Garden API is not ready." }
    if (Get-NetTCPConnection -State Listen -LocalPort 8000 -ErrorAction SilentlyContinue) {
        throw "Port 8000 is occupied but the Garden API did not pass its check."
    }
    $childCommand = switch ($Provider) {
        "Shugg" { "& '$($PSScriptRoot.Replace("'", "''"))\start-shugg.ps1'" }
        "Local" { "& '$($PSScriptRoot.Replace("'", "''"))\start-local.ps1'" }
        "Current" { "& '$($PSScriptRoot.Replace("'", "''"))\.venv\Scripts\python.exe' -m uvicorn app:app --host 127.0.0.1 --port 8000" }
    }
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($childCommand))
    $backend = Start-Process powershell.exe -WorkingDirectory $PSScriptRoot -ArgumentList @("-NoProfile", "-NoExit", "-EncodedCommand", $encoded) -PassThru
    $deadline = (Get-Date).AddSeconds(120)
    while (-not (Test-Garden)) {
        if ($backend.HasExited) { throw "Garden launcher exited. Check its window." }
        if ((Get-Date) -gt $deadline) { throw "Garden API did not become ready. Check its window for a key prompt or an error." }
        Start-Sleep -Seconds 2
    }
}
Write-Host "Garden API: OK"
Write-Host "Panel: $gardenUrl"
Write-Host "Existing processes were reused. Provider changes require a separate backend restart."
if (-not $NoDominoLog -and -not $CheckOnly) {
    try {
        $viewer = Join-Path $PSScriptRoot "show-domino-log.ps1"
        if (-not (Test-Path -LiteralPath $viewer)) { throw "Missing log viewer: $viewer" }
        $logCommand = "& '" + $viewer.Replace("'", "''") + "'"
        $logEncoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($logCommand))
        Start-Process powershell.exe -Verb RunAs -ArgumentList @("-NoProfile", "-NoExit", "-EncodedCommand", $logEncoded) | Out-Null
    } catch {
        Write-Warning ("Could not open Domino log viewer: " + $_.Exception.Message)
    }
}
if (-not $NoBrowser -and -not $CheckOnly) { Start-Process $gardenUrl }

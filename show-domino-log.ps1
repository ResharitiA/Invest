[CmdletBinding()]
param(
    [string]$DominoDirectory = "C:\Program Files\HCL\Domino",
    [switch]$Snapshot
)

$ErrorActionPreference = "Stop"
$Host.UI.RawUI.WindowTitle = "Domino server log"
$dataDirectory = Join-Path $DominoDirectory "Data"
$logPath = Join-Path $dataDirectory "IBM_TECHNICAL_SUPPORT\console.log"
if (-not (Get-Process -Name nserver -ErrorAction SilentlyContinue)) {
    throw "Domino is not running. Start it with start-garden.ps1 first."
}
Write-Host "If no new lines appear, run 'start consolelog' in Domino Administrator Server Console."
Write-Host "If Domino rotates the log, reopen this viewer to follow the new file."

$deadline = (Get-Date).AddSeconds(15)
while (-not (Test-Path -LiteralPath $logPath)) {
    if ((Get-Date) -gt $deadline) { throw "Console log was not created: $logPath" }
    Start-Sleep -Seconds 1
}

Write-Host "Log: $logPath"
Write-Host "Read-only viewer. Close this window to stop viewing; Domino keeps running."
if ($Snapshot) {
    Get-Item -LiteralPath $logPath | Select-Object FullName,LastWriteTime,Length
    Get-Content -LiteralPath $logPath -Tail 15
} else {
    Get-Content -LiteralPath $logPath -Tail 40 -Wait
}

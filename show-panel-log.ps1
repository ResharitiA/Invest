[CmdletBinding()]
param()
$ErrorActionPreference = "Stop"
$Host.UI.RawUI.WindowTitle = "Garden panel log"
$path = Join-Path $PSScriptRoot "garden-panel.log"
if (-not (Test-Path -LiteralPath $path)) {
    Write-Warning "No panel log yet. Restart the backend using start-gmail.ps1 to enable logging."
    return
}
Write-Host "Read-only log viewer. Closing this window does not stop Garden."
Get-Content -LiteralPath $path -Tail 40 -Wait

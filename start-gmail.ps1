[CmdletBinding()]
param([switch]$NoBrowser)
$ErrorActionPreference = "Stop"
$names = @("GARDEN_GMAIL_PASSWORD", "GARDEN_GMAIL_SENDER", "GARDEN_GMAIL_RECIPIENT")
$previous = @{}
foreach ($name in $names) { $previous[$name] = [Environment]::GetEnvironmentVariable($name, "Process") }
try {
    $secure = Read-Host "Gmail app password for morpalex5@gmail.com (hidden)" -AsSecureString
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { $env:GARDEN_GMAIL_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer).Replace(" ", "") }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
    if (-not $env:GARDEN_GMAIL_PASSWORD) { throw "App password is empty" }
    $env:GARDEN_GMAIL_SENDER = "morpalex5@gmail.com"
    $env:GARDEN_GMAIL_RECIPIENT = "ar2338881@gmail.com"
    $listeners = @(Get-NetTCPConnection -State Listen -LocalPort 8000 -ErrorAction SilentlyContinue)
    $backendProcesses = @()
    $python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
    foreach ($ownerId in @($listeners.OwningProcess | Select-Object -Unique)) {
        $backendProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $ownerId"
        $parent = Get-CimInstance Win32_Process -Filter "ProcessId = $($backendProcess.ParentProcessId)"
        if ($backendProcess.CommandLine -notmatch '\s-m\s+uvicorn\s+app:app\s' -or
            ($backendProcess.ExecutablePath -ne $python -and
             ($parent.ExecutablePath -ne $python -or $parent.CommandLine -notmatch '\s-m\s+uvicorn\s+app:app\s'))) {
            throw "Port 8000 belongs to an unrecognized process. Stop it manually before starting Gmail."
        }
        $backendProcesses += Get-Process -Id $ownerId -ErrorAction Stop
    }
    if ($backendProcesses.Count) {
        Write-Host "Restarting Garden backend to apply Gmail settings..."
        foreach ($backendProcess in $backendProcesses) {
            Stop-Process -InputObject $backendProcess -ErrorAction Stop
            if (-not $backendProcess.WaitForExit(15000)) { throw "Garden backend did not stop." }
        }
        if (Get-NetTCPConnection -State Listen -LocalPort 8000 -ErrorAction SilentlyContinue) {
            throw "Port 8000 is still occupied. Garden was not restarted."
        }
    }
    & "$PSScriptRoot\start-garden.ps1" -NoBrowser:$NoBrowser
} finally {
    foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $previous[$name], "Process") }
    if ($secure) { $secure.Dispose() }
}

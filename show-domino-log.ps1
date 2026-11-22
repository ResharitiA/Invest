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
Write-Host "The viewer follows new console records and reconnects after log rotation."

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
    $position = 0L
    $identity = $null
    $first = $true
    while ($true) {
        $stream = $null
        $reader = $null
        try {
            $item = Get-Item -LiteralPath $logPath -ErrorAction Stop
            if ($identity -ne $item.CreationTimeUtc.Ticks -or $item.Length -lt $position) {
                $position = 0L
                $identity = $item.CreationTimeUtc.Ticks
            }
            $stream = [IO.File]::Open($logPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
            $null = $stream.Seek($position, [IO.SeekOrigin]::Begin)
            $reader = New-Object IO.StreamReader($stream, [Text.Encoding]::Default, $true)
            $chunk = $reader.ReadToEnd()
            $position = $stream.Position
            if ($chunk.Length) {
                if ($first) {
                    ($chunk -split "`n" | Select-Object -Last 40) -join "`n" | Write-Host -NoNewline
                } else {
                    Write-Host -NoNewline $chunk
                }
            }
            $first = $false
        } catch [IO.FileNotFoundException] {
        } catch [System.Management.Automation.ItemNotFoundException] {
        } finally {
            if ($reader) { $reader.Dispose() }
            elseif ($stream) { $stream.Dispose() }
        }
        Start-Sleep -Seconds 1
    }
}

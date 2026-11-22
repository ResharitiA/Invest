$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$env:LLM_PROVIDER = "ollama"
$env:LLM_BASE_URL = "http://127.0.0.1:11434/v1"
$env:LLM_API_KEY = "ollama"
$env:LLM_MODEL = "qwen2.5:7b"

try {
    $null = Invoke-RestMethod "http://127.0.0.1:11434/api/tags" -TimeoutSec 3
} catch {
    $ollama = Get-Command ollama -ErrorAction SilentlyContinue
    $executable = if ($ollama) { $ollama.Source } else { Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe" }
    if (-not (Test-Path $executable)) { throw "Ollama is not installed." }
    Start-Process -FilePath $executable -ArgumentList "serve" -WindowStyle Hidden
    $ready = $false
    for ($i = 0; $i -lt 30; $i++) {
        try {
            $null = Invoke-RestMethod "http://127.0.0.1:11434/api/tags" -TimeoutSec 1
            $ready = $true
            break
        } catch { Start-Sleep -Seconds 1 }
    }
    if (-not $ready) { throw "Ollama did not start on port 11434." }
}
$models = Invoke-RestMethod "http://127.0.0.1:11434/api/tags" -TimeoutSec 5
if ($env:LLM_MODEL -notin $models.models.name) {
    throw "Model missing. Run: ollama pull qwen2.5:7b"
}
Write-Host "Garden + local Ollama: http://127.0.0.1:8000"
& "$PSScriptRoot\.venv\Scripts\python.exe" -m uvicorn app:app --host 127.0.0.1 --port 8000

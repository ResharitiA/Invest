$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$previous = @{}
foreach ($name in @("LLM_PROVIDER", "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY")) {
    $previous[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
}
try {
    $env:LLM_PROVIDER = "shugg"
    $env:LLM_BASE_URL = "https://api.shugg.ru/v1"
    $env:LLM_MODEL = "shugg"
    $key = $env:SHUGG_API_KEY
    if (-not $key) { $key = $env:SHUGG_KEY }
    $keyFile = Join-Path $env:LOCALAPPDATA "garden-shugg-key.xml"
    if (-not $key) {
        $secure = if (Test-Path -LiteralPath $keyFile) { Import-Clixml -LiteralPath $keyFile } else { Read-Host "Shugg API key" -AsSecureString }
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try { $key = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
    }
    if (-not $key.Trim()) { throw "API key is empty." }
    $env:LLM_API_KEY = $key.Trim()
    Write-Host "Garden + Shugg: http://127.0.0.1:8000"
    & "$PSScriptRoot\.venv\Scripts\python.exe" -m uvicorn app:app --host 127.0.0.1 --port 8000
} finally {
    foreach ($name in $previous.Keys) {
        [Environment]::SetEnvironmentVariable($name, $previous[$name], "Process")
    }
    $key = $null
}

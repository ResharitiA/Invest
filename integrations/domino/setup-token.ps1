[CmdletBinding()]
param(
    [string]$DominoData = "C:\Program Files\HCL\Domino\Data",
    [string]$ServiceName = "HCLDominoServer(CProgramFilesHCLDominoData)"
)

$ErrorActionPreference = "Stop"
$folder = Join-Path $env:LOCALAPPDATA "Garden"
$source = Join-Path $folder "domino-token.txt"
$target = Join-Path $DominoData "garden_scheduler.token"
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
$null = New-Item -ItemType Directory -Path $folder -Force

function Protect-Token($Path) {
    $acl = New-Object Security.AccessControl.FileSecurity
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($identity in @($sid, [Security.Principal.SecurityIdentifier]"S-1-5-18", [Security.Principal.SecurityIdentifier]"S-1-5-32-544")) {
        $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($identity, "FullControl", "Allow")))
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

if (-not (Test-Path -LiteralPath $source)) {
    $stream = [IO.File]::Open($source, [IO.FileMode]::CreateNew)
    $stream.Dispose()
    Protect-Token $source
    $bytes = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    [IO.File]::WriteAllText($source, [Convert]::ToBase64String($bytes), (New-Object Text.UTF8Encoding($false)))
} else {
    Protect-Token $source
}
if ([IO.File]::ReadAllText($source).Trim().Length -lt 32) {
    throw "Existing token is invalid. No server changes were made."
}

$command = @'
$ErrorActionPreference = "Stop"
try {
    $source = '__SOURCE__'
    $target = '__TARGET__'
    $service = Get-CimInstance Win32_Service | Where-Object { $_.Name -eq '__SERVICE__' }
    if (-not $service) { throw "Domino service not found" }
    $account = if ($service.StartName -eq "LocalSystem") { "NT AUTHORITY\SYSTEM" } else { $service.StartName }
    $serviceSid = (New-Object Security.Principal.NTAccount($account)).Translate([Security.Principal.SecurityIdentifier])
    if (Test-Path -LiteralPath $target) {
        if ([IO.File]::ReadAllText($target).Trim() -ne [IO.File]::ReadAllText($source).Trim()) {
            throw "Existing server token differs. Automatic rotation refused."
        }
    } else {
        $stream = [IO.File]::Open($target, [IO.FileMode]::CreateNew)
        $stream.Dispose()
    }
    $acl = New-Object Security.AccessControl.FileSecurity
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($id in @([Security.Principal.SecurityIdentifier]"S-1-5-18", [Security.Principal.SecurityIdentifier]"S-1-5-32-544")) {
        $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($id, "FullControl", "Allow")))
    }
    $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($serviceSid, "Read", "Allow")))
    Set-Acl -LiteralPath $target -AclObject $acl
    [IO.File]::WriteAllText($target, [IO.File]::ReadAllText($source).Trim(), (New-Object Text.UTF8Encoding($false)))
    Write-Host "Domino token configured. No secret was printed."
} catch {
    Write-Error $_
    exit 1
}
'@
$command = $command.Replace('__SOURCE__', $source.Replace("'", "''")).Replace('__TARGET__', $target.Replace("'", "''")).Replace('__SERVICE__', $ServiceName.Replace("'", "''"))
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
$process = Start-Process powershell.exe -Verb RunAs -ArgumentList @("-NoProfile", "-EncodedCommand", $encoded) -PassThru
$process.WaitForExit()
if ($process.ExitCode -ne 0) { throw "Domino token setup failed. Local token preserved; check the elevated window." }
Write-Host "Garden token: $source"
Write-Host "Domino token configured. Restart Garden if its account or token environment overrides changed."

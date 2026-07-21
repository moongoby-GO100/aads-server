param(
    [Parameter(Mandatory = $true)]
    [string]$LauncherPath,
    [string]$ServerUrl = "wss://aads.newtalk.kr/api/v1/pc-agent/ws",
    [string]$InstallDir = "$env:LOCALAPPDATA\KakaoBot",
    [string]$AgentId = "windows-e2e-$env:COMPUTERNAME"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $LauncherPath -PathType Leaf)) {
    throw "Launcher not found: $LauncherPath"
}

$secureToken = Read-Host "AADS PC Agent token" -AsSecureString
$tokenPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
try {
    $plainToken = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($tokenPtr)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($tokenPtr)
}
if ([string]::IsNullOrWhiteSpace($plainToken)) {
    throw "Token is required"
}

New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
$configPath = Join-Path $InstallDir "config.json"
$config = [ordered]@{
    server_url = $ServerUrl
    agent_token = $plainToken
    agent_id = $AgentId.ToLowerInvariant()
    node_role = "windows_e2e"
    registered = $false
}
$config | ConvertTo-Json | Set-Content -LiteralPath $configPath -Encoding UTF8
$plainToken = $null

$taskName = "AADSWindowsE2ENode"
$taskCommand = '"' + (Resolve-Path -LiteralPath $LauncherPath).Path + '"'
& schtasks.exe /Create /TN $taskName /TR $taskCommand /SC ONSTART /RL HIGHEST /F | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Failed to create scheduled task: $taskName"
}

& schtasks.exe /Run /TN $taskName | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Scheduled task was created but could not be started: $taskName"
}

Write-Host "Windows E2E node configured."
Write-Host "Agent ID: $($config.agent_id)"
Write-Host "Verify: GET https://aads.newtalk.kr/api/v1/pc-agent/e2e-node/status"

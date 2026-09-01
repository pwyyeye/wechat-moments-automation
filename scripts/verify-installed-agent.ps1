[CmdletBinding()]
param(
    [string]$ExpectedSourceId = "",
    [switch]$RequireWechatReady,
    [ValidateRange(1, 120)]
    [int]$TimeoutSeconds = 20
)

$ErrorActionPreference = "Stop"
$installRoot = Split-Path -Parent $PSScriptRoot
$agentExe = Join-Path $installRoot "WechatPublisherAgent.exe"
$helperExe = Join-Path $installRoot "_internal\src\cs_uia_service\publish\WeChatUIA.exe"
$momentsTemplate = Join-Path $installRoot "_internal\templates\icons\moments_tab.png"
$startupName = "WechatPublisherAgent"
$failures = [System.Collections.Generic.List[string]]::new()

if (-not (Test-Path -LiteralPath $agentExe)) {
    $failures.Add("Agent executable is missing.")
}
if (-not (Test-Path -LiteralPath $helperExe)) {
    $failures.Add("Bundled UIA helper is missing.")
}
if (-not (Test-Path -LiteralPath $momentsTemplate)) {
    $failures.Add("Moments navigation template is missing.")
}

$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$startupCommand = Get-ItemPropertyValue `
    -Path $runKey `
    -Name $startupName `
    -ErrorAction SilentlyContinue
$expectedStartupCommand = '"' + $agentExe + '" --agent'
if ($startupCommand -ne $expectedStartupCommand) {
    $failures.Add("User logon startup registry entry is missing or points to an unexpected executable.")
}

$status = $null
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
do {
    try {
        $status = Invoke-RestMethod -Uri "http://127.0.0.1:17821/api/status" -TimeoutSec 3
    } catch {
        Start-Sleep -Milliseconds 500
    }
} until ($status -or (Get-Date) -ge $deadline)

if (-not $status) {
    $failures.Add("Local Agent API did not become ready.")
}

$matchedSource = $null
if ($status -and $ExpectedSourceId) {
    $matchedSource = @($status.sources) | Where-Object { $_.id -eq $ExpectedSourceId } | Select-Object -First 1
    if (-not $matchedSource) {
        $failures.Add("Expected data source was not found.")
    } elseif ($matchedSource.healthState -ne "healthy") {
        $failures.Add("Expected data source is not healthy.")
    }
}

if ($status -and $RequireWechatReady) {
    if (-not $status.wechat.running) {
        $failures.Add("WeChat is not running.")
    }
    if (-not $status.wechat.loggedIn) {
        $failures.Add("WeChat is not logged in.")
    }
    if (-not $status.wechat.desktopUnlocked) {
        $failures.Add("Windows desktop is locked.")
    }
    if (-not $status.wechat.momentsWindowReady) {
        $failures.Add("Moments window is not ready.")
    }
}

$result = [ordered]@{
    ok = $failures.Count -eq 0
    checkedAt = (Get-Date).ToUniversalTime().ToString("o")
    installRoot = $installRoot
    files = [ordered]@{
        agent = Test-Path -LiteralPath $agentExe
        uiaHelper = Test-Path -LiteralPath $helperExe
        momentsTemplate = Test-Path -LiteralPath $momentsTemplate
    }
    startup = if ($startupCommand) {
        [ordered]@{
            type = "hkcu_run"
            name = $startupName
            command = $startupCommand
        }
    } else {
        $null
    }
    agent = if ($status) { $status.agent } else { $null }
    wechat = if ($status) { $status.wechat } else { $null }
    source = $matchedSource
    outbox = if ($status) { $status.outbox } else { $null }
    failures = @($failures)
}

$result | ConvertTo-Json -Depth 8
if ($failures.Count -gt 0) {
    exit 1
}

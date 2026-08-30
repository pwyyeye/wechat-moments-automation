$ErrorActionPreference = "Stop"
$task = Get-ScheduledTask -TaskName "WechatPublisherAgent" -ErrorAction SilentlyContinue
if ($task) {
    Unregister-ScheduledTask -TaskName "WechatPublisherAgent" -Confirm:$false
}

$dataRoot = Join-Path $env:LOCALAPPDATA "WechatPublisherAgent"
Write-Host "Startup task removed. Local ledger and credentials were preserved at: $dataRoot"

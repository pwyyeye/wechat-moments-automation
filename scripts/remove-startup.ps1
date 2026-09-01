$ErrorActionPreference = "Stop"
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$runValueName = "WechatPublisherAgent"
Remove-ItemProperty `
    -Path $runKey `
    -Name $runValueName `
    -ErrorAction SilentlyContinue

$task = Get-ScheduledTask -TaskName "WechatPublisherAgent" -ErrorAction SilentlyContinue
if ($task) {
    try {
        Unregister-ScheduledTask `
            -TaskName "WechatPublisherAgent" `
            -Confirm:$false `
            -ErrorAction Stop
    } catch {
        Write-Warning "Legacy startup task could not be removed: $($_.Exception.Message)"
    }
}

$dataRoot = Join-Path $env:LOCALAPPDATA "WechatPublisherAgent"
Write-Host "Startup registration removed. Local ledger and credentials were preserved at: $dataRoot"

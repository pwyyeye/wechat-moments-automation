param(
    [Parameter(Mandatory = $true)]
    [string]$ExecutablePath
)

$ErrorActionPreference = "Stop"
$resolvedExecutable = (Resolve-Path -LiteralPath $ExecutablePath).Path
$action = New-ScheduledTaskAction `
    -Execute $resolvedExecutable `
    -Argument "--agent" `
    -WorkingDirectory (Split-Path -Parent $resolvedExecutable)
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Days 3) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName "WechatPublisherAgent" `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Starts the WeChat Moments publisher in the signed-in user's desktop session." `
    -Force | Out-Null

Write-Host "WechatPublisherAgent will start when $env:USERNAME signs in."

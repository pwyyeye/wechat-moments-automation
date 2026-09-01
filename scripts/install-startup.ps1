param(
    [Parameter(Mandatory = $true)]
    [string]$ExecutablePath
)

$ErrorActionPreference = "Stop"
$resolvedExecutable = (Resolve-Path -LiteralPath $ExecutablePath).Path
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$runValueName = "WechatPublisherAgent"
$runCommand = '"' + $resolvedExecutable + '" --agent'
New-Item -Path $runKey -Force | Out-Null
Set-ItemProperty -Path $runKey -Name $runValueName -Value $runCommand -Type String

# Remove the legacy task when upgrading. HKCU Run is sufficient because the
# Agent must run in the signed-in user's interactive desktop session.
$legacyTask = Get-ScheduledTask -TaskName $runValueName -ErrorAction SilentlyContinue
if ($legacyTask) {
    try {
        Unregister-ScheduledTask `
            -TaskName $runValueName `
            -Confirm:$false `
            -ErrorAction Stop
    } catch {
        Write-Warning "Legacy startup task could not be removed: $($_.Exception.Message)"
    }
}

$registeredCommand = Get-ItemPropertyValue -Path $runKey -Name $runValueName
if ($registeredCommand -ne $runCommand) {
    throw "Startup registry verification failed after registration."
}

Write-Host "WechatPublisherAgent will start when $env:USERNAME signs in."

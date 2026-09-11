[CmdletBinding()]
param(
    [string]$TaskName = "HotGap-Purchased-Tables-0730",
    [string]$RunAt = "07:30"
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runner = Join-Path $projectRoot "scripts\run_wanqing_sync.ps1"
if (-not (Test-Path -LiteralPath $runner)) {
    throw "Runner not found: $runner"
}

$runTime = [datetime]::ParseExact($RunAt, "HH:mm", [Globalization.CultureInfo]::InvariantCulture)
$account = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$powershell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"

$action = New-ScheduledTaskAction `
    -Execute $powershell `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`"" `
    -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $runTime
$settings = New-ScheduledTaskSettingsSet `
    -WakeToRun `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 45) `
    -RestartCount 1 `
    -RestartInterval (New-TimeSpan -Minutes 30) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal `
    -UserId $account `
    -LogonType Interactive `
    -RunLevel Limited

$task = New-ScheduledTask `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Wake at 07:30 daily, capture Wanqing and Gongkao Sheet, retry once after 30 minutes, then refresh S1 and Feishu."

Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
$legacyTask = "HotGap-Wanqing-Feishu-0700"
if ($TaskName -ne $legacyTask -and (Get-ScheduledTask -TaskName $legacyTask -ErrorAction SilentlyContinue)) {
    Unregister-ScheduledTask -TaskName $legacyTask -Confirm:$false
}
$registered = Get-ScheduledTask -TaskName $TaskName
$info = Get-ScheduledTaskInfo -TaskName $TaskName

[pscustomobject]@{
    TaskName           = $registered.TaskName
    State              = $registered.State
    RunAs              = $registered.Principal.UserId
    WakeToRun          = $registered.Settings.WakeToRun
    StartWhenAvailable = $registered.Settings.StartWhenAvailable
    NextRunTime        = $info.NextRunTime
} | Format-List

param(
    [ValidateSet('Inspect', 'Set')][string]$Action = 'Inspect',
    [string]$At,
    [string]$Days,
    [string]$TaskName = 'ChaoXingReserveSeat'
)

$ErrorActionPreference = 'Stop'
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

if ($Action -eq 'Inspect' -and -not $task) {
    [pscustomobject]@{
        state = 'Missing'
        daysOfWeek = 0
        at = $null
        nextRunTime = $null
        lastRunTime = $null
        lastTaskResult = $null
    } | ConvertTo-Json -Compress
    exit 0
}

if ($Action -eq 'Set') {
    if ($At -notmatch '^([01]\d|2[0-3]):[0-5]\d$') { throw 'Time must be HH:mm.' }
    $selectedDays = @($Days -split ',' | Where-Object { $_ })
    $validDays = @('Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday')
    if ($selectedDays.Count -eq 0 -or @($selectedDays | Where-Object { $_ -notin $validDays }).Count -gt 0) {
        throw 'Select at least one valid weekday.'
    }
    $runTime = [datetime]::ParseExact($At, 'HH:mm', [Globalization.CultureInfo]::InvariantCulture)
    $trigger = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek $selectedDays -At $runTime
    if ($task) {
        Set-ScheduledTask -TaskName $TaskName -Trigger $trigger | Out-Null
    } else {
        $project = $PSScriptRoot
        $script = Join-Path $project 'run-local.ps1'
        $arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $script + '"'
        $taskAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments -WorkingDirectory $project
        $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -WakeToRun -MultipleInstances IgnoreNew
        $principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask -TaskName $TaskName -Action $taskAction -Trigger $trigger -Settings $settings -Principal $principal | Out-Null
    }
}

$task = Get-ScheduledTask -TaskName $TaskName
$info = Get-ScheduledTaskInfo -TaskName $TaskName
$trigger = @($task.Triggers)[0]
[pscustomobject]@{
    state = [string]$task.State
    daysOfWeek = [int]$trigger.DaysOfWeek
    at = [string]$trigger.StartBoundary
    nextRunTime = if ($info.NextRunTime.Year -gt 2000) { $info.NextRunTime.ToString('yyyy-MM-dd HH:mm:ss') } else { $null }
    lastRunTime = if ($info.LastRunTime.Year -gt 2000) { $info.LastRunTime.ToString('yyyy-MM-dd HH:mm:ss') } else { $null }
    lastTaskResult = $info.LastTaskResult
} | ConvertTo-Json -Compress

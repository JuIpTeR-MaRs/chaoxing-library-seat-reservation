param(
    [ValidateSet('Inspect', 'Set', 'Remove')][string]$Action = 'Inspect',
    [ValidateSet('reserve', 'transfer')][string]$Kind = 'reserve',
    [string]$At,
    [string]$Days,
    [ValidateRange(1, 2147483647)][int]$Account = 1,
    [string]$TaskName
)

$ErrorActionPreference = 'Stop'
$project = $PSScriptRoot
$isTransfer = $Kind -eq 'transfer'
if (-not $TaskName) {
    if ($isTransfer) {
        $TaskName = 'ChaoXingTransferSeat'
    } elseif ($Account -eq 1) {
        $TaskName = 'ChaoXingReserveSeat'
    } else {
        $TaskName = "ChaoXingReserveSeat-$Account"
    }
}
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

function Get-TaskState([string]$Name) {
    $current = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if (-not $current) {
        return [pscustomobject]@{
            state = 'Missing'
            daysOfWeek = 0
            at = $null
            nextRunTime = $null
            lastRunTime = $null
            lastTaskResult = $null
        }
    }
    $info = Get-ScheduledTaskInfo -TaskName $Name
    $trigger = @($current.Triggers)[0]
    return [pscustomobject]@{
        state = [string]$current.State
        daysOfWeek = [int]$trigger.DaysOfWeek
        at = [string]$trigger.StartBoundary
        nextRunTime = if ($info.NextRunTime.Year -gt 2000) { $info.NextRunTime.ToString('yyyy-MM-dd HH:mm:ss') } else { $null }
        lastRunTime = if ($info.LastRunTime.Year -gt 2000) { $info.LastRunTime.ToString('yyyy-MM-dd HH:mm:ss') } else { $null }
        lastTaskResult = $info.LastTaskResult
    }
}

if ($Action -eq 'Remove') {
    if ($task) { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false }
    Get-TaskState $TaskName | ConvertTo-Json -Compress
    exit 0
}

if ($Action -eq 'Inspect') {
    Get-TaskState $TaskName | ConvertTo-Json -Compress
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
    if ($isTransfer) {
        # The transfer task fires exactly at the configured time, after the account
        # tasks at 22:00 have finished and the platform shows their real records.
        $script = Join-Path $project 'run-transfer.ps1'
        $arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $script + '"'
        $limitMinutes = 20
    } else {
        # Start one minute before the opening time so the runner can pre-warm login,
        # page token and captcha and fire its first submit right after the opening.
        $runTime = $runTime.AddMinutes(-1)
        $script = Join-Path $project 'run-local.ps1'
        $arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $script + '" -Account ' + $Account
        $limitMinutes = 30
    }
    $trigger = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek $selectedDays -At $runTime
    $taskAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments -WorkingDirectory $project
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes $limitMinutes) -WakeToRun -MultipleInstances IgnoreNew
    if ($task) {
        Set-ScheduledTask -TaskName $TaskName -Trigger $trigger -Action $taskAction -Settings $settings | Out-Null
    } else {
        $principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask -TaskName $TaskName -Action $taskAction -Trigger $trigger -Settings $settings -Principal $principal | Out-Null
    }
}

Get-TaskState $TaskName | ConvertTo-Json -Compress

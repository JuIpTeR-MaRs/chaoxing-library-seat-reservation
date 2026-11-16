param(
    [ValidateSet('Inspect', 'Set', 'Remove')][string]$Action = 'Inspect',
    [ValidateSet('reserve', 'transfer', 'pod')][string]$Kind = 'reserve',
    [string]$At,
    [string]$Days,
    [ValidateRange(1, 2147483647)][int]$Account = 1,
    [string]$TaskName
)

$ErrorActionPreference = 'Stop'
$project = $PSScriptRoot
$isTransfer = $Kind -eq 'transfer'
if (-not $TaskName) {
    if ($Kind -eq 'pod') {
        $TaskName = "ChaoXingReservePod-$Account"
    } elseif ($isTransfer) {
        $TaskName = 'ChaoXingTransferSeat'
    } elseif ($Account -eq 1) {
        $TaskName = 'ChaoXingReserveSeat'
    } else {
        $TaskName = "ChaoXingReserveSeat-$Account"
    }
}
# Use Task Scheduler directly: the ScheduledTasks CIM provider can deny access
# even when the current user owns the registered task.
try {
    $service = New-Object -ComObject 'Schedule.Service'
    $service.Connect()
    $folder = $service.GetFolder('\')

    function Find-Task([string]$Name) {
        try { return $folder.GetTask($Name) }
        catch {
            # Only a real file-not-found means Missing. Never hide access errors.
            if ($_.Exception.HResult -in @(-2147024894, -2147024893) -or
                $_.Exception.InnerException.HResult -in @(-2147024894, -2147024893)) { return $null }
            throw
        }
    }

    function Get-TaskState([string]$Name) {
        $current = Find-Task $Name
        if (-not $current) {
            return [pscustomobject]@{ state='Missing'; daysOfWeek=0; at=$null
                nextRunTime=$null; lastRunTime=$null; lastTaskResult=$null }
        }
        $trigger = @($current.Definition.Triggers | Where-Object Type -eq 3) | Select-Object -First 1
        $states = @('Unknown', 'Disabled', 'Queued', 'Ready', 'Running')
        return [pscustomobject]@{
            state = $states[[int]$current.State]
            daysOfWeek = if ($trigger) { [int]$trigger.DaysOfWeek } else { 0 }
            at = if ($trigger) { [string]$trigger.StartBoundary } else { $null }
            nextRunTime = if ($current.NextRunTime.Year -gt 2000) { $current.NextRunTime.ToString('yyyy-MM-dd HH:mm:ss') } else { $null }
            lastRunTime = if ($current.LastRunTime.Year -gt 2000) { $current.LastRunTime.ToString('yyyy-MM-dd HH:mm:ss') } else { $null }
            lastTaskResult = $current.LastTaskResult
        }
    }

    $task = Find-Task $TaskName
    if ($Action -eq 'Remove') {
        if ($task) { $folder.DeleteTask($TaskName, 0) }
    } elseif ($Action -eq 'Set') {
        if ($At -notmatch '^([01]\d|2[0-3]):[0-5]\d$') { throw 'Time must be HH:mm.' }
        $selectedDays = @($Days -split ',' | Where-Object { $_ })
        $validDays = @('Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday')
        if ($selectedDays.Count -eq 0 -or @($selectedDays | Where-Object { $_ -notin $validDays }).Count -gt 0) {
            throw 'Select at least one valid weekday.'
        }
        $runTime = [datetime]::ParseExact($At, 'HH:mm', [Globalization.CultureInfo]::InvariantCulture)
        if ($Kind -eq 'pod') {
            $runTime = $runTime.AddMinutes(-1)
            $script = Join-Path $project 'run-pod.ps1'
            $arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $script + '" -Account ' + $Account
            $limitMinutes = 10
        } elseif ($isTransfer) {
            $script = Join-Path $project 'run-transfer.ps1'
            $arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $script + '"'
            $limitMinutes = 20
        } else {
            $runTime = $runTime.AddMinutes(-1)
            $script = Join-Path $project 'run-local.ps1'
            $arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $script + '" -Account ' + $Account
            $limitMinutes = 30
        }
        # Preserve the existing principal and other task properties on update.
        $definition = if ($task) { $task.Definition } else { $service.NewTask(0) }
        if (-not $task) {
            $definition.Principal.UserId = [Security.Principal.WindowsIdentity]::GetCurrent().Name
            $definition.Principal.LogonType = 3 # Interactive token
            $definition.Principal.RunLevel = 0
        }
        $definition.Triggers.Clear()
        $trigger = $definition.Triggers.Create(3) # Weekly
        $trigger.StartBoundary = $runTime.ToString('yyyy-MM-ddTHH:mm:ss')
        $mask = 0
        foreach ($day in $selectedDays) { $mask = $mask -bor (1 -shl [array]::IndexOf($validDays, $day)) }
        $trigger.DaysOfWeek = $mask
        $trigger.WeeksInterval = 1
        $trigger.Enabled = $true
        $definition.Actions.Clear()
        $taskAction = $definition.Actions.Create(0)
        $taskAction.Path = 'powershell.exe'
        $taskAction.Arguments = $arguments
        $taskAction.WorkingDirectory = $project
        $definition.Settings.ExecutionTimeLimit = "PT${limitMinutes}M"
        $definition.Settings.WakeToRun = $true
        $definition.Settings.MultipleInstances = 2 # IgnoreNew
        $folder.RegisterTaskDefinition($TaskName, $definition, 6, $definition.Principal.UserId,
                                      $null, $definition.Principal.LogonType) | Out-Null
    }
    Get-TaskState $TaskName | ConvertTo-Json -Compress
} catch {
    # A single machine-readable error prevents raw PowerShell stack traces in UI.
    [Console]::Error.WriteLine(('TASK_SCHEDULER_ERROR: ' + $_.Exception.Message))
    exit 1
}

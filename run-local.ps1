param(
    [switch]$CheckOnly,
    [ValidateRange(1, 2147483647)][int]$Account = 1,
    [switch]$Today,
    [switch]$IgnoreWeekday,
    [string]$RoomId,
    [string]$SeatId,
    [string]$Start,
    [string]$End
)

$ErrorActionPreference = 'Stop'
$project = $PSScriptRoot
$config = Join-Path $project 'config.local.json'
$python = Join-Path $project '.venv\Scripts\python.exe'
$runner = Join-Path $project 'reserve_segments.py'
$packagedApp = Join-Path $project 'SeatDesk.exe'
$log = Join-Path $project $(if ($Account -eq 1) { 'last-run.log' } else { "last-run-$Account.log" })

function Show-ReservationResult([int]$Code) {
    try {
        $events = @(Get-Content -LiteralPath $log -Encoding UTF8 | ForEach-Object {
            try { $_ | ConvertFrom-Json -ErrorAction Stop } catch { }
        })
        $outcome = @($events | Where-Object { $_.status -in 'complete', 'partial_success', 'failed' -or $null -ne $_.skipped }) | Select-Object -Last 1
        $status = '预约失败'
        $icon = 48
        if ($Code -eq 0 -and $outcome.status -eq 'complete') {
            $status = '预约成功'
            $icon = 64
        } elseif ($outcome.status -eq 'partial_success') {
            $status = '部分预约成功'
        } elseif ($Code -eq 0 -and $outcome.skipped) {
            $status = '本次预约已跳过'
        } elseif ($null -eq $outcome -and @($events | Where-Object { $_.timing -eq 'http' -and $_.path -like '*/seat/submit' -and $_.status -eq 200 }).Count -gt 0) {
            $status = '结果未知'
        }
        $statusNames = @{
            0 = '待履约'; 1 = '学习中'; 2 = '已履约'; 3 = '暂离中'
            5 = '被监督中'; 7 = '已取消'
        }
        $verified = @($events | Where-Object { $_.verify -eq 'account_result' }) | Select-Object -Last 1
        $details = @()
        if ($verified) {
            switch ($verified.verified) {
                'complete' { $status = '预约成功（已核对）'; $icon = 64 }
                'partial' { $status = '部分预约成功（已核对）' }
                'none' { $status = '预约失败（已核对）' }
            }
            if ($verified.verified -eq 'unavailable') {
                $details += '预约信息：查询失败'
                $details += '核对失败：' + (($verified.message, $verified.error) -join ' ').Trim() + '（请在学习通“我的预约”查看）'
            } else {
                $checkedAt = if ($verified.checked_at) { ' · ' + $verified.checked_at } else { '' }
                $details += '预约信息（学习通查询 · ' + [string]$verified.date + $checkedAt + '）：'
                $records = @($verified.reservations)
                if ($records.Count -gt 0) {
                    foreach ($item in $records) {
                        $state = $null
                        if ($null -ne $item.status) {
                            try { $state = $statusNames[[int]$item.status] } catch { }
                        }
                        if (-not $state) { $state = '待履约' }
                        $details += '  ' + ($item.time -join '–') + '  座位 ' + $item.seat + '  阅览室 ' + $item.room + '  记录号 ' + $item.id + '  ' + $state
                    }
                } else {
                    $details += '  没有查到预约记录'
                }
                $gaps = @($verified.missing | ForEach-Object { $_ -join '–' })
                if ($gaps.Count -gt 0) { $details += '  计划内仍空缺：' + ($gaps -join '、') }
                $conclusion = @{ complete = '目标时段已全部约到'; partial = '仍有时段空缺'; none = '该日没有预约记录' }[$verified.verified]
                if ($conclusion) { $details += '结论：' + $conclusion }
            }
        } elseif ($outcome.skipped) {
            $details += '预约信息：本次跳过，未登录查询'
        } else {
            $details += '预约信息：本次运行没有完成预约查询'
        }
        if ($outcome.skipped) { $details += '跳过原因：' + $outcome.skipped }
        if ($outcome.error) { $details += '错误：' + $outcome.error + ' ' + $outcome.detail }
        $attempts = @($events | Where-Object { $null -ne $_.success })
        if ($attempts.Count -gt 0) {
            $okCount = @($attempts | Where-Object { $_.success }).Count
            $details += '提交过程：接口返回成功 ' + $okCount + ' 段 / 共 ' + $attempts.Count + ' 次'
            $failures = @($attempts | Where-Object { -not $_.success })
            if ($failures.Count -gt 0 -and $verified -and $verified.verified -eq 'complete') {
                $details += '提示：提交过程中有超时或异常返回，但学习通查询已确认全部约到。'
            } elseif ($failures.Count -gt 0) {
                $reasons = @($failures | ForEach-Object {
                    if ($_.reason -eq 'seat_taken') { '该时段已被预约' }
                    elseif ($_.reason -eq 'already_held') { '该时段已有预约（可能只是部分重叠）' }
                    else { (([string]$_.message) + ' ' + ([string]$_.error)).Trim() }
                } | Where-Object { $_ } | Group-Object | Sort-Object Count -Descending)
                if ($reasons.Count -gt 0) {
                    $details += '提交异常（按次数）：' + (($reasons | ForEach-Object { '×' + $_.Count + ' ' + $_.Name }) -join '；')
                }
            }
        }
        if ($status -eq '结果未知') { $details += '日志不完整，是否预约成功无法判断。' }
        $message = "账号 $Account：$status`n`n" + ($details -join "`n") + "`n`n详细日志：$log"
        $messageData = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($message))
        $popupScript = "`$text = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String('$messageData')); `$shell = New-Object -ComObject WScript.Shell; `$shell.Popup(`$text, 0, '超星定时预约结果', $(4096 + $icon)) | Out-Null"
        $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($popupScript))
        Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile', '-EncodedCommand', $encoded) -WindowStyle Hidden | Out-Null
    } catch {
        Write-Warning "预约已结束，但结果弹窗显示失败：$_"
    }
}

if (-not (Test-Path -LiteralPath $packagedApp)) {
    $healthy = $false
    if (Test-Path -LiteralPath $python) {
        try {
            & $python -c 'import requests, cryptography, numpy, cv2, tkinter' *> $null
            $healthy = $LASTEXITCODE -eq 0
        } catch { $healthy = $false }
    }
    if (-not $healthy) {
        & (Join-Path $project 'deploy.ps1') -NoLaunch
        if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw 'Python environment repair failed.' }
    }
}

if (Test-Path -LiteralPath $packagedApp) {
    $command = $packagedApp
    $arguments = @('--scheduled-run', '-u', $config, '--account', "$Account")
    $selfLogging = $true
} elseif ((Test-Path -LiteralPath $python) -and (Test-Path -LiteralPath $runner)) {
    $command = $python
    $arguments = @($runner, '-u', $config, '--account', "$Account")
    $selfLogging = $false
} else {
    throw 'Neither packaged runner nor Python environment is available.'
}
$data = Get-Content -LiteralPath $config -Raw -Encoding UTF8 | ConvertFrom-Json
$extraArguments = @()
if ($Today) { $extraArguments += '--today' }
if ($IgnoreWeekday) { $extraArguments += '--ignore-weekday' }
if ($RoomId) { $extraArguments += @('--roomid', $RoomId) }
if ($SeatId) { $extraArguments += @('--seatid', $SeatId) }
if ($Start) { $extraArguments += @('--start', $Start) }
if ($End) { $extraArguments += @('--end', $End) }
$arguments += $extraArguments
if (-not $data.reserve -or $Account -gt $data.reserve.Count) { throw 'Account number is out of range.' }
$placeholder = [string][char]0x8BF7 + [string][char]0x586B + [string][char]0x5199
$ready = @($data.reserve[$Account - 1] | Where-Object { $_.scheduleEnabled -ne $false -and [string]$_.roomid -match '^\d+$' -and @($_.seatid).Count -gt 0 })
if ($ready.Count -eq 0) { throw 'No account has a saved reservation plan.' }
foreach ($item in $ready) {
    foreach ($key in @('username', 'password')) {
        $value = [string]$item.$key
        if (-not $value -or $value.StartsWith($placeholder)) { throw "Fill $key in config.local.json first." }
    }
}
if (@($ready | ForEach-Object { $_.username } | Select-Object -Unique).Count -ne $ready.Count) { throw 'Account usernames must be unique.' }
function Expand-TaskTimeLimit([int]$Account) {
    # The runner waits for the other accounts before it checks the results, so the
    # task limit must leave room for a slow account plus that check. Tasks created
    # before this change still carry 10 minutes, so raise them on the next run.
    $taskName = if ($Account -eq 1) { 'ChaoXingReserveSeat' } else { "ChaoXingReserveSeat-$Account" }
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if (-not $task) { return }
    $limit = [string]$task.Settings.ExecutionTimeLimit
    if ([string]::IsNullOrWhiteSpace($limit) -or $limit -eq 'PT0S') { return }
    try {
        if ([System.Xml.XmlConvert]::ToTimeSpan($limit).TotalMinutes -ge 30) { return }
        $task.Settings.ExecutionTimeLimit = 'PT30M'
        Set-ScheduledTask -TaskName $taskName -Settings $task.Settings | Out-Null
        Write-Host "已将定时任务运行时限从 $limit 调整为 30 分钟，避免等待其他账号时被系统终止。"
    } catch {
        Write-Warning "无法调整定时任务运行时限（当前 $limit），运行时间过长时可能被系统终止：$_"
    }
}

if ($CheckOnly) {
    if ($selfLogging) {
        $process = Start-Process -FilePath $command -ArgumentList (@('--scheduled-run', '-u', ('"' + $config + '"'), '--account', "$Account") + $extraArguments + @('--check-only')) -WorkingDirectory $project -WindowStyle Hidden -Wait -PassThru
        exit $process.ExitCode
    }
    & $command @arguments --check-only
    exit $LASTEXITCODE
}

Set-Location -LiteralPath $project
# Keep a dated copy of the previous run before this one truncates the log.
function Save-PreviousRunLog([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $archive = Join-Path $project 'logs'
    New-Item -ItemType Directory -Force -Path $archive | Out-Null
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $name = [System.IO.Path]::GetFileNameWithoutExtension($Path)
    Copy-Item -LiteralPath $Path -Destination (Join-Path $archive ("$name-$stamp.log"))
    Get-ChildItem -LiteralPath $archive -Filter '*.log' |
        Sort-Object LastWriteTime -Descending |
        Select-Object -Skip 60 |
        Remove-Item -Force -ErrorAction SilentlyContinue
}
if (-not $CheckOnly) { Save-PreviousRunLog $log }
if (-not $CheckOnly) { Expand-TaskTimeLimit $Account }
if ($selfLogging) {
    Write-Host "账号 $Account 定时预约开始；进度同步写入 $log"
    $process = Start-Process -FilePath $command -ArgumentList @('--scheduled-run', '-u', ('"' + $config + '"'), '--account', "$Account") -WorkingDirectory $project -WindowStyle Hidden -PassThru
    $position = 0
    while (-not $process.HasExited) {
        if (Test-Path -LiteralPath $log) {
            $reader = [System.IO.File]::Open($log, 'Open', 'Read', 'ReadWrite')
            try {
                $reader.Seek($position, 'Begin') | Out-Null
                $stream = New-Object System.IO.StreamReader($reader, [System.Text.Encoding]::UTF8)
                while (($line = $stream.ReadLine()) -ne $null) { Write-Host $line }
                $position = $reader.Position
            } finally { $reader.Dispose() }
        }
        Start-Sleep -Milliseconds 500
        $process.Refresh()
    }
    if (Test-Path -LiteralPath $log) {
        $reader = [System.IO.File]::Open($log, 'Open', 'Read', 'ReadWrite')
        try {
            $reader.Seek($position, 'Begin') | Out-Null
            $stream = New-Object System.IO.StreamReader($reader, [System.Text.Encoding]::UTF8)
            while (($line = $stream.ReadLine()) -ne $null) { Write-Host $line }
        } finally { $reader.Dispose() }
    }
    Write-Host "账号 $Account 定时预约结束，退出码：$($process.ExitCode)"
    Show-ReservationResult $process.ExitCode
    exit $process.ExitCode
} else {
    $previousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $writer = [System.IO.StreamWriter]::new($log, $false, [System.Text.UTF8Encoding]::new($false))
    $writer.AutoFlush = $true
    try {
        Write-Host "账号 $Account 定时预约开始；进度同步写入 $log"
        & $command @arguments 2>&1 | ForEach-Object {
            $line = $_.ToString()
            Write-Host $line
            $writer.WriteLine($line)
        }
        $resultCode = $LASTEXITCODE
        Write-Host "账号 $Account 定时预约结束，退出码：$resultCode"
    } finally {
        $writer.Dispose()
        $ErrorActionPreference = $previousErrorAction
    }
    Show-ReservationResult $resultCode
    exit $resultCode
}

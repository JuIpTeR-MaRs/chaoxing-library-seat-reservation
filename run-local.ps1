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
        $last = $events | Select-Object -Last 1
        $status = '预约失败'
        $icon = 48
        if ($Code -eq 0 -and $last.status -eq 'complete') {
            $status = '预约成功'
            $icon = 64
        } elseif ($last.status -eq 'partial_success') {
            $status = '部分预约成功'
        } elseif ($Code -eq 0 -and $last.skipped) {
            $status = '本次预约已跳过'
        }
        $details = @($events | Where-Object { $null -ne $_.success } | ForEach-Object {
            $outcome = if ($_.success) { '成功' } else { '失败：' + ($_.message + ' ' + $_.error).Trim() }
            ($_.time -join '–') + '，座位 ' + ($_.seatid -join '、') + '：' + $outcome
        })
        if ($last.skipped) { $details += '跳过原因：' + $last.skipped }
        if ($last.error) { $details += '错误：' + $last.error + ' ' + $last.detail }
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
if ($CheckOnly) {
    if ($selfLogging) {
        $process = Start-Process -FilePath $command -ArgumentList (@('--scheduled-run', '-u', ('"' + $config + '"'), '--account', "$Account") + $extraArguments + @('--check-only')) -WorkingDirectory $project -WindowStyle Hidden -Wait -PassThru
        exit $process.ExitCode
    }
    & $command @arguments --check-only
    exit $LASTEXITCODE
}

Set-Location -LiteralPath $project
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

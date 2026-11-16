param(
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
$project = $PSScriptRoot
$config = Join-Path $project 'config.local.json'
$python = Join-Path $project '.venv\Scripts\python.exe'
$userPython = Join-Path $env:LOCALAPPDATA 'ChaoXingReserveSeat\venv\Scripts\python.exe'
if (Test-Path -LiteralPath $userPython) { $python = $userPython }
$runner = Join-Path $project 'transfer_seats.py'
$packagedApp = Join-Path $project 'SeatDesk.exe'
$log = Join-Path $project 'last-transfer.log'

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

function Show-TransferResult([int]$Code) {
    try {
        $events = @(Get-Content -LiteralPath $log -Encoding UTF8 | ForEach-Object {
            try { $_ | ConvertFrom-Json -ErrorAction Stop } catch { }
        })
        $outcome = @($events | Where-Object { $_.transfer -eq 'result' }) | Select-Object -Last 1
        $modeNames = @{
            cancel_source = '取消账号2的预约'
            transfer = '账号2转给账号1'
            none = '无操作'
        }
        $resultNames = @{
            complete = '完成'
            partial = '部分完成'
            skipped = '已跳过'
            unavailable = '无法核对'
            error = '失败'
        }
        $verificationNames = @{
            complete = '全部约到'
            partial = '只约到部分'
            none = '没有约到'
            unavailable = '无法核对'
        }
        $mode = [string]$outcome.mode
        $result = [string]$outcome.result
        $status = if ($resultNames.ContainsKey($result)) { $resultNames[$result] } else { '失败' }
        $icon = 48
        if ($Code -eq 0 -and $result -in 'complete', 'skipped') { $icon = 64 }
        if ($result -eq 'error') { $icon = 16 }
        $details = @()
        if ($outcome.date) { $details += '预约日期：' + $outcome.date }
        if ($mode) {
            $modeText = if ($modeNames.ContainsKey($mode)) { $modeNames[$mode] } else { $mode }
            $details += '处理方式：' + $modeText
        }
        if (@($outcome.window).Count -eq 2) { $details += '检查时段：' + ($outcome.window -join '–') }
        if ($outcome.message) { $details += '说明：' + $outcome.message }
        foreach ($item in @($outcome.cancelled)) {
            if (-not $item) { continue }
            $details += '已取消账号' + $item.account + '：' + ($item.time -join '–') + '，座位 ' + $item.seat + '（阅览室 ' + $item.room + '）'
        }
        foreach ($item in @($outcome.booked)) {
            if (-not $item) { continue }
            $line = if ($item.success) { '成功' } else { '失败：' + (($item.message, $item.error) -join ' ').Trim() }
            $details += '账号' + $item.account + '已预约：' + ($item.time -join '–') + '，座位 ' + $item.seat + '：' + $line
        }
        foreach ($item in @($outcome.restored)) {
            if (-not $item) { continue }
            $line = if ($item.success) { '成功' } else { '失败：' + (($item.message, $item.error) -join ' ').Trim() }
            $details += '账号' + $item.account + '已恢复：' + ($item.time -join '–') + '，座位 ' + $item.seat + '：' + $line
        }
        $waitTimeout = @($outcome.wait_timeout | Where-Object { $null -ne $_ })
        if ($waitTimeout.Count -gt 0) {
            $details += '提示：等待账号定时任务结束超时（未完成：' + (($waitTimeout | ForEach-Object { '账号' + $_ }) -join '、') + '）'
        }
        foreach ($node in @($outcome.source, $outcome.target)) {
            if (-not $node) { continue }
            $label = if ($node.index) { '账号' + $node.index } else { '账号' }
            $verified = [string]$node.verified
            $text = $label + '核对：' + $(if ($verificationNames.ContainsKey($verified)) { $verificationNames[$verified] } else { $verified })
            $remaining = @()
            foreach ($slot in @($node.remaining)) {
                if ($null -ne $slot) { $remaining += ($slot -join '–') }
            }
            if ($remaining.Count -gt 0) { $text += '；仍保留 ' + ($remaining -join '、') }
            $missing = @()
            foreach ($slot in @($node.missing)) {
                if ($null -ne $slot) { $missing += ($slot -join '–') }
            }
            if ($missing.Count -gt 0) { $text += '；仍空缺 ' + ($missing -join '、') }
            $details += $text
            $states = @{ 0 = '待履约'; 1 = '学习中'; 2 = '已履约'; 3 = '暂离中'; 5 = '被监督中'; 7 = '已取消' }
            $records = @($node.records)
            foreach ($rec in $records) {
                if (-not $rec) { continue }
                $state = $null
                if ($null -ne $rec.status) { try { $state = $states[[int]$rec.status] } catch { } }
                if (-not $state) { $state = '待履约' }
                $details += $label + '预约信息：' + ($rec.time -join '–') + '，座位 ' + $rec.seat + '，阅览室 ' + $rec.room + '，记录号 ' + $rec.id + '，' + $state
            }
            if ($records.Count -eq 0 -and $verified -eq 'none') {
                $details += $label + '预约信息：没有查到预约记录'
            }
        }
        if ($null -eq $outcome) {
            $details += '日志不完整，是否转预约成功无法判断。'
        } elseif ($result -notin 'complete', 'skipped') {
            $details += '请登录学习通「我的预约」核对该日期与时段，确认最终结果。'
        }
        $message = "超星转预约：$status`n`n" + ($details -join "`n") + "`n`n详细日志：$log"
        $messageData = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($message))
        $popupScript = "`$text = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String('$messageData')); `$shell = New-Object -ComObject WScript.Shell; `$shell.Popup(`$text, 0, '超星转预约结果', $(4096 + $icon)) | Out-Null"
        $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($popupScript))
        Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile', '-EncodedCommand', $encoded) -WindowStyle Hidden | Out-Null
    } catch {
        Write-Warning "转预约已结束，但结果弹窗显示失败：$_"
    }
}

if (-not $CheckOnly) { Save-PreviousRunLog $log }

if (Test-Path -LiteralPath $packagedApp) {
    # The packaged app writes last-transfer.log itself (--transfer-run).
    $arguments = @('--transfer-run', '-u', ('"' + $config + '"'))
    if ($CheckOnly) { $arguments += '--check-only' }
    Write-Host "账号座位转预约开始；进度由 SeatDesk 写入 $log"
    $process = Start-Process -FilePath $packagedApp -ArgumentList $arguments -WorkingDirectory $project -WindowStyle Hidden -Wait -PassThru
    Write-Host "账号座位转预约结束，退出码：$($process.ExitCode)"
    if (-not $CheckOnly) { Show-TransferResult $process.ExitCode }
    exit $process.ExitCode
} elseif ((Test-Path -LiteralPath $python) -and (Test-Path -LiteralPath $runner)) {
    $arguments = @($runner, '-u', $config)
    if ($CheckOnly) { $arguments += '--check-only' }
    if ($CheckOnly) {
        # A read-only check must not truncate or archive the real last-transfer.log.
        & $python @arguments 2>&1 | ForEach-Object { Write-Host $_.ToString() }
        exit $LASTEXITCODE
    }
    $previousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    # transfer_seats.py prints one JSON object per line; mirror it to the console and log.
    $writer = [System.IO.StreamWriter]::new($log, $false, [System.Text.UTF8Encoding]::new($false))
    $writer.AutoFlush = $true
    try {
        Write-Host "账号座位转预约开始；进度同步写入 $log"
        & $python @arguments 2>&1 | ForEach-Object {
            $line = $_.ToString()
            Write-Host $line
            $writer.WriteLine($line)
        }
        $resultCode = $LASTEXITCODE
        Write-Host "账号座位转预约结束，退出码：$resultCode"
    } finally {
        $writer.Dispose()
        $ErrorActionPreference = $previousErrorAction
    }
    Show-TransferResult $resultCode
    exit $resultCode
} else {
    throw 'Neither packaged SeatDesk.exe nor Python environment with transfer_seats.py is available.'
}

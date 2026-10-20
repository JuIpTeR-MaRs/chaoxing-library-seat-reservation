param([switch]$CheckOnly)

$ErrorActionPreference = 'Stop'
$project = $PSScriptRoot
$config = Join-Path $project 'config.local.json'
$python = Join-Path $project '.venv\Scripts\python.exe'
$runner = Join-Path $project 'reserve_segments.py'
$packagedApp = Join-Path $project 'SeatDesk.exe'
$log = Join-Path $project 'last-run.log'

if (Test-Path -LiteralPath $packagedApp) {
    $command = $packagedApp
    $arguments = @('--scheduled-run', '-u', $config)
    $selfLogging = $true
} elseif ((Test-Path -LiteralPath $python) -and (Test-Path -LiteralPath $runner)) {
    $command = $python
    $arguments = @($runner, '-u', $config)
    $selfLogging = $false
} else {
    throw 'Neither packaged runner nor Python environment is available.'
}
$data = Get-Content -LiteralPath $config -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $data.reserve -or $data.reserve.Count -ne 1) { throw 'config.local.json must contain one reserve entry.' }
$item = $data.reserve[0]
$placeholder = [string][char]0x8BF7 + [string][char]0x586B + [string][char]0x5199
foreach ($key in @('username', 'password', 'roomid')) {
    $value = [string]$item.$key
    if (-not $value -or $value.StartsWith($placeholder)) { throw "Fill $key in config.local.json first." }
}
if ($item.roomid -notmatch '^\d+$') { throw 'roomid must be the numeric id from the seat selection link.' }
if ($CheckOnly) {
    if ($selfLogging) {
        $process = Start-Process -FilePath $command -ArgumentList @('--scheduled-run', '-u', ('"' + $config + '"'), '--check-only') -WorkingDirectory $project -WindowStyle Hidden -Wait -PassThru
        exit $process.ExitCode
    }
    & $command @arguments --check-only
    exit $LASTEXITCODE
}

Set-Location -LiteralPath $project
if ($selfLogging) {
    $process = Start-Process -FilePath $command -ArgumentList @('--scheduled-run', '-u', ('"' + $config + '"')) -WorkingDirectory $project -WindowStyle Hidden -Wait -PassThru
    exit $process.ExitCode
} else {
    & $command @arguments 2>&1 | Out-File -LiteralPath $log -Encoding UTF8
    exit $LASTEXITCODE
}

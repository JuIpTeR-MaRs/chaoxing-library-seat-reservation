param([ValidateRange(1,2147483647)][int]$Account = 1, [switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
$env:PYTHONIOENCODING = 'utf-8'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$project = $PSScriptRoot
$python = Join-Path $project '.venv\Scripts\python.exe'
$userPython = Join-Path $env:LOCALAPPDATA 'ChaoXingReserveSeat\venv\Scripts\python.exe'
if (Test-Path -LiteralPath $userPython) { $python = $userPython }
$runnerArgs = @('--config', (Join-Path $project 'config.local.json'), '--account', $Account)
if ($CheckOnly) { $runnerArgs += '--check-only' }
$exe = Join-Path $project 'SeatDesk.exe'
if ((Test-Path -LiteralPath $python) -and (Test-Path -LiteralPath (Join-Path $project 'pod_reservation.py'))) {
    if ($CheckOnly) { & $python (Join-Path $project 'pod_reservation.py') @runnerArgs; exit $LASTEXITCODE }
    & $python (Join-Path $project 'pod_reservation.py') @runnerArgs 2>&1 | Out-File -LiteralPath (Join-Path $project "last-pod-$Account.log") -Encoding UTF8
    exit $LASTEXITCODE
}
if (Test-Path -LiteralPath $exe) {
    & $exe --pod-run @runnerArgs
    exit $LASTEXITCODE
}
throw 'Python environment or SeatDesk.exe not found. Run deploy.cmd first.'

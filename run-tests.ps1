# Use the same runtime as the desktop app and scheduled booking scripts.
$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$userPython = Join-Path $env:LOCALAPPDATA 'ChaoXingReserveSeat\venv\Scripts\python.exe'
if (Test-Path -LiteralPath $userPython) { $python = $userPython }
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Python runtime not found. Run deploy.ps1 first.'
}

Push-Location -LiteralPath $PSScriptRoot
try {
    Write-Host "Testing with $python"
    & $python -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'tests\test_result_popup.ps1')
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
    Pop-Location
}

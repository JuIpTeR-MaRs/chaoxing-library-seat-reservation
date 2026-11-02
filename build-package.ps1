param()

$ErrorActionPreference = 'Stop'
$project = $PSScriptRoot
$python = Join-Path $project '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Run deploy.cmd first to create .venv.' }

& $python -m PyInstaller --version *> $null
if ($LASTEXITCODE -ne 0) {
    & $python -m pip install pyinstaller
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller installation failed.' }
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$package = Join-Path $project "dist\SeatDesk-$stamp"
New-Item -ItemType Directory -Path $package -Force | Out-Null

$holidayData = (Join-Path $project 'holidays.json') + ';.'
& $python -m PyInstaller --noconfirm --clean --onefile --windowed --name SeatDesk --add-data $holidayData --distpath $package --workpath (Join-Path $project 'build\seatdesk-gui') --specpath (Join-Path $project 'build') (Join-Path $project 'desktop_gui.py')
if ($LASTEXITCODE -ne 0) { throw 'GUI packaging failed.' }

Copy-Item -LiteralPath (Join-Path $project 'schedule_task.ps1'), (Join-Path $project 'run-local.ps1'), (Join-Path $project 'package-launch.cmd'), (Join-Path $project 'PACKAGE_README.md'), (Join-Path $project 'holidays.json') -Destination $package
Copy-Item -LiteralPath (Join-Path $project 'package-template.json') -Destination (Join-Path $package 'config.local.json')
$zip = "$package.zip"
Compress-Archive -LiteralPath $package -DestinationPath $zip
Write-Host "Package folder: $package"
Write-Host "ZIP: $zip"

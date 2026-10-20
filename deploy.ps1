param([switch]$NoLaunch)

$ErrorActionPreference = 'Stop'
$project = $PSScriptRoot
$venvPython = Join-Path $project '.venv\Scripts\python.exe'
$venvPythonw = Join-Path $project '.venv\Scripts\pythonw.exe'
$localConfig = Join-Path $project 'config.local.json'
$templateConfig = Join-Path $project 'config.json'

Set-Location -LiteralPath $project

if (-not (Test-Path -LiteralPath $venvPython)) {
    $launcher = if (Get-Command py.exe -ErrorAction SilentlyContinue) { 'py.exe' } else { 'python.exe' }
    Write-Host "Creating Python environment with $launcher ..."
    if ($launcher -eq 'py.exe') {
        & py.exe -3 -m venv '.venv'
    } else {
        & python.exe -m venv '.venv'
    }
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $venvPython)) {
        throw 'Python 3 with venv is required. Install Python, then run deploy.cmd again.'
    }
}

& $venvPython -c 'import requests, cryptography, numpy, cv2, tkinter'
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Installing required Python packages ...'
    & $venvPython -m pip install requests cryptography numpy opencv-python
    if ($LASTEXITCODE -ne 0) { throw 'Package installation failed. Check the network and run deploy.cmd again.' }
}

& $venvPython -c 'import requests, cryptography, numpy, cv2, tkinter'
if ($LASTEXITCODE -ne 0) { throw 'Python dependencies are incomplete.' }

if (-not (Test-Path -LiteralPath $localConfig)) {
    if (-not (Test-Path -LiteralPath $templateConfig)) { throw 'config.json template is missing.' }
    Copy-Item -LiteralPath $templateConfig -Destination $localConfig
    Write-Host 'Created config.local.json from the sample. Fill in your account in the app.'
} else {
    Write-Host 'Keeping the existing config.local.json.'
}

Write-Host 'Deployment checks passed.'
if (-not $NoLaunch) {
    if (-not (Test-Path -LiteralPath $venvPythonw)) { throw 'pythonw.exe is missing from the virtual environment.' }
    Start-Process -FilePath $venvPythonw -ArgumentList 'desktop_gui.py' -WorkingDirectory $project
}

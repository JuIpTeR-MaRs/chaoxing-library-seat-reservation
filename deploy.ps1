param([switch]$NoLaunch)

$ErrorActionPreference = 'Stop'
$project = $PSScriptRoot
$venvPython = Join-Path $project '.venv\Scripts\python.exe'
$venvPythonw = Join-Path $project '.venv\Scripts\pythonw.exe'
$localConfig = Join-Path $project 'config.local.json'
$templateConfig = Join-Path $project 'config.json'

Set-Location -LiteralPath $project

function Test-Python([string]$Exe, [string]$Code) {
    if (-not (Test-Path -LiteralPath $Exe)) { return $false }
    try {
        & $Exe -c $Code *> $null
        return $LASTEXITCODE -eq 0
    } catch { return $false }
}

if (-not (Test-Python $venvPython 'import sys, tkinter; assert sys.version_info >= (3, 10)')) {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python314\python.exe')
    )
    $candidate = $candidates | Where-Object { Test-Python $_ 'import tkinter' } | Select-Object -First 1
    if (-not $candidate) {
        $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
        if ($launcher) {
            try { $candidate = (& $launcher.Source -3 -c 'import sys, tkinter; print(sys.executable)' 2>$null | Select-Object -Last 1) }
            catch { $candidate = $null }
        }
    }
    if (-not $candidate) {
        $launcher = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($launcher -and (Test-Python $launcher.Source 'import tkinter')) { $candidate = $launcher.Source }
    }
    if (-not $candidate) { throw '未找到带 tkinter 的 Python 3.10+。请安装 Python，然后重新运行 deploy.cmd。' }
    $staging = Join-Path $project '.venv-new'
    if (Test-Path -LiteralPath $staging) { throw '发现 .venv-new，请检查上次重建结果后再运行。' }
    & $candidate -m venv $staging
    if ($LASTEXITCODE -ne 0) { throw '创建新 Python 环境失败。' }
    $newPython = Join-Path $staging 'Scripts\python.exe'
    if (-not (Test-Python $newPython 'import tkinter')) { throw '新 Python 环境无法加载 tkinter。' }
    & $newPython -m pip install requests cryptography numpy opencv-python
    if ($LASTEXITCODE -ne 0) { throw '安装 Python 依赖失败；旧环境未替换。' }
    $backup = Join-Path $project ('.venv-backup-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
    if (Test-Path -LiteralPath '.venv') { Move-Item -LiteralPath '.venv' -Destination $backup }
    Move-Item -LiteralPath $staging -Destination '.venv'
}

if (-not (Test-Python $venvPython 'import requests, cryptography, numpy, cv2, tkinter')) {
    & $venvPython -m pip install requests cryptography numpy opencv-python
    if ($LASTEXITCODE -ne 0) { throw '安装 Python 依赖失败，请检查网络。' }
}
if (-not (Test-Python $venvPython 'import requests, cryptography, numpy, cv2, tkinter')) { throw 'Python 依赖仍不完整。' }

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

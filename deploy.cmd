@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy.ps1"
if errorlevel 1 (
  echo.
  echo Deployment failed. See the error above.
  pause
  exit /b 1
)
echo.
echo Deployment complete. The reservation window should be open.
pause

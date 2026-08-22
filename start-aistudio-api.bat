@echo off
setlocal

echo [aistudio-api] Launching startup script from %~dp0

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-aistudio-api.ps1" -Port 8090
set "EXITCODE=%ERRORLEVEL%"
echo.
echo aistudio-api stopped. Exit code: %EXITCODE%
pause
exit /b %EXITCODE%

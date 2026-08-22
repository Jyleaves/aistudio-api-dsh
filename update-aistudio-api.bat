@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0update-aistudio-api.ps1" %*
set "EXITCODE=%ERRORLEVEL%"
echo.
echo aistudio-api update finished. Exit code: %EXITCODE%
pause
exit /b %EXITCODE%

@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment not found:
    echo         %~dp0.venv
    pause
    exit /b 1
)

echo Starting aistudio-api on http://127.0.0.1:8090 ...
echo Press Ctrl+C to stop the service.
echo.

".venv\Scripts\python.exe" "main.py" server --port 8090

echo.
echo aistudio-api stopped. Exit code: %ERRORLEVEL%
pause

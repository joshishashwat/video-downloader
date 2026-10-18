@echo off
rem Launches Video Downloader. First run creates a virtual environment and installs dependencies.
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Setting up for first run...
    python -m venv .venv || goto :error
    ".venv\Scripts\python" -m pip install --upgrade pip >nul
    ".venv\Scripts\python" -m pip install -r requirements.txt || goto :error
)
start "" ".venv\Scripts\pythonw.exe" main.pyw
exit /b 0

:error
echo.
echo Setup failed. Make sure Python 3.10+ is installed and on PATH.
pause

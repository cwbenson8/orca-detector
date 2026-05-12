@echo off
REM ─────────────────────────────────────────────────────────────────────────────
REM run-local.bat — Run OrcaWatch locally on Windows
REM
REM Requirements:
REM   - Python 3.11+ installed and on PATH
REM   - orcAI installed via uv:
REM     uv tool install git+https://github.com/ethz-tb/orcAI.git --python 3.11
REM ─────────────────────────────────────────────────────────────────────────────

cd /d "%~dp0.."

echo.
echo  OrcaWatch - Local Dev Server
echo  ─────────────────────────────
echo.

REM Check orcai is available
where orcai >nul 2>&1
if errorlevel 1 (
    echo  ERROR: orcai not found on PATH.
    echo  Install with: uv tool install git+https://github.com/ethz-tb/orcAI.git --python 3.11
    echo  Then close and reopen this window and try again.
    pause
    exit /b 1
)

REM Install Python deps if not already installed
pip show fastapi >nul 2>&1
if errorlevel 1 (
    echo  Installing Python dependencies...
    pip install -r backend\requirements.txt
)

REM Create temp directory
if not exist "%TEMP%\orca-detector" mkdir "%TEMP%\orca-detector"

echo  Starting server at http://localhost:8080
echo  Press Ctrl+C to stop.
echo.

python -m uvicorn backend.main:app --host 0.0.0.0 --port 8080 --reload

@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto :install

where python >nul 2>nul
if errorlevel 1 (
    where py >nul 2>nul
    if errorlevel 1 (
        echo Python 3.11 or newer is required. Install it from https://www.python.org/downloads/windows/
        exit /b 1
    )
    py -3 -m venv .venv
) else (
    python -m venv .venv
)
if errorlevel 1 (
    echo Failed to create .venv
    exit /b 1
)

:install
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 exit /b 1
echo.
echo Virtual environment ready: %CD%\.venv

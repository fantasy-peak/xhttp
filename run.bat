@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    call setup.bat
    if errorlevel 1 exit /b 1
)

if not exist "config.json" (
    if exist "config.example.json" (
        echo No config.json found; copy config.example.json to config.json and edit it,
        echo or pass flags such as --server / --uuid / --listen.
        echo.
    )
)

".venv\Scripts\python.exe" -m xhttp %*

@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    call setup.bat
    if errorlevel 1 exit /b 1
)

".venv\Scripts\python.exe" -m pip install -r requirements-build.txt
if errorlevel 1 exit /b 1

".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean xhttp-proxy.spec
if errorlevel 1 exit /b 1

echo.
echo Built: %CD%\dist\xhttp-proxy.exe
echo Copy config.json next to the .exe (it is read from the working directory,
echo the directory containing the .exe, or --config).

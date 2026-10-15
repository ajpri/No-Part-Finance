@echo off
setlocal
cd /d "%~dp0"

echo To manage users, run: startWindows.bat manage-users

if exist ".venv\Scripts\python.exe" goto dependencies

where py >nul 2>nul
if %errorlevel% equ 0 (
  py -3 -m venv .venv
) else (
  where python >nul 2>nul
  if errorlevel 1 (
    echo Python 3 is required. Install Python and try again.
    exit /b 1
  )
  python -m venv .venv
)
if errorlevel 1 exit /b 1

:dependencies
".venv\Scripts\python.exe" -m pip --version >nul 2>nul
if errorlevel 1 (
  ".venv\Scripts\python.exe" -m ensurepip --upgrade
  ".venv\Scripts\python.exe" -m pip --version >nul 2>nul
  if errorlevel 1 (
    echo The installed Python lacks virtual-environment or pip support. Repair Python or install its venv component.
    exit /b 1
  )
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" app.py %*
exit /b %errorlevel%

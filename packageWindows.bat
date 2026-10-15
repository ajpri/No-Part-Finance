@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" packageApplication.py %*
  exit /b %errorlevel%
)

where py >nul 2>nul
if %errorlevel% equ 0 (
  py -3 packageApplication.py %*
  exit /b %errorlevel%
)

where python >nul 2>nul
if errorlevel 1 (
  echo Python 3 is required. Install Python and try again.
  exit /b 1
)
python packageApplication.py %*
exit /b %errorlevel%

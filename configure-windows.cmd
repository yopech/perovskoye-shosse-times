@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist ".venv\Scripts\python.exe" (
  echo Run install-windows.cmd first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" gazette.py personalize
if errorlevel 1 goto failed
".venv\Scripts\python.exe" gazette.py setup
if errorlevel 1 goto failed
exit /b 0
:failed
echo Setup failed. Read the error above.
pause
exit /b 1

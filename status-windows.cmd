@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist ".venv\Scripts\python.exe" (
  echo Run install-windows.cmd first.
) else (
  ".venv\Scripts\python.exe" gazette.py status
)
pause

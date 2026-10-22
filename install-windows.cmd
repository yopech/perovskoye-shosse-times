@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
py -3 -c "import sys; raise SystemExit(sys.version_info < (3,11))" >nul 2>&1
if errorlevel 1 (
  echo Install Python 3.11 or newer from https://www.python.org/downloads/windows/
  echo Include the Python launcher, then run this file again.
  pause
  exit /b 1
)
py -3 -m venv .venv
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
if not exist config.json copy /y config.example.json config.json >nul
".venv\Scripts\python.exe" gazette.py demo
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -c "import os,pathlib; os.startfile(str(next(pathlib.Path('output').glob('*.pdf')).resolve()))"
echo.
echo Ready. Demo only: printing is still OFF.
echo Next: read README.md, install SumatraPDF, run configure-windows.cmd.
pause
exit /b 0
:failed
echo Installation failed. Read the error above and README.md.
pause
exit /b 1

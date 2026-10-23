@echo off
REM Bitunix AI Paper Desk -- PAPER TRADING ONLY. Double-click to start.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Creating Python environment...
  python -m venv .venv || py -3 -m venv .venv
)
if not exist .venv\Scripts\python.exe (
  echo Python was not found. Install Python 3.12 from the Microsoft Store or python.org, then run this again.
  pause
  exit /b 1
)
echo Installing/updating packages...
.venv\Scripts\python -m pip install --disable-pip-version-check -q -r requirements.txt
echo.
echo Dashboard: http://127.0.0.1:8765   (keep this window open; close it to stop)
echo.
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8765 --workers 1
pause

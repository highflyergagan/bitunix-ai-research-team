@echo off
REM Bitunix AI Paper Desk -- PAPER TRADING ONLY. Double-click to start.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Creating Python environment...
  py -3 -m venv .venv || python -m venv .venv
)
.venv\Scripts\python -m pip install -q -r requirements.txt
echo.
echo Dashboard: http://127.0.0.1:8765   (keep this window open; close it to stop)
echo.
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8765 --workers 1
pause

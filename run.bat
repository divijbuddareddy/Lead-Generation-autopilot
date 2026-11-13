@echo off
REM GTM Autopilot - one-click local start (Windows)
cd /d "%~dp0backend"
python -m pip install -q -r requirements.txt
echo.
echo  GTM Autopilot is starting on http://localhost:8000
echo  API docs: http://localhost:8000/docs
echo.
start "" http://localhost:8000
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

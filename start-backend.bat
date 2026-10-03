@echo off
echo ========================================================
echo Starting ARIA Backend (FastAPI on http://localhost:8000)
echo ========================================================
cd /d "%~dp0backend"
if exist venv\Scripts\activate.bat (
    call venv\Scripts\activate.bat
)
uvicorn app:app --reload --host 0.0.0.0 --port 8000

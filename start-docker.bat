@echo off
echo ========================================================
echo Starting ARIA Stack with Docker Compose
echo ========================================================
cd /d "%~dp0"
docker compose up --build

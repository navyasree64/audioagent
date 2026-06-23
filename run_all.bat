@echo off
echo ===================================================
echo   AudioAgent: Multi-Tenant AI Outbound System
echo ===================================================
echo.
echo Starting Backend (FastAPI), Localtunnel, and Frontend (Vite)
echo.

:: 1. Launch FastAPI Backend
echo [1/3] Starting Backend Server...
start "AudioAgent Backend" cmd /k "cd backend && venv\Scripts\activate && uvicorn app.main:app --reload --port 8000"

:: 2. Launch Localtunnel
echo [2/3] Starting Localtunnel Webhook Exposer...
start "Localtunnel Webhook" cmd /k "npx localtunnel --port 8000 --subdomain navya --local-host 127.0.0.1"

:: 3. Launch React Frontend
echo [3/3] Starting Frontend Server...
start "AudioAgent Frontend" cmd /k "cd audioagent && npm run dev"

echo.
echo All services launched in separate windows!
echo Please check the Localtunnel terminal window for your public URL.
echo.
pause

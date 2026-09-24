@echo off
setlocal

REM RDC Teamlease Employee Onboarding Portal - one-click startup
REM Double-click this file (or run it from a terminal) to start the app.
REM Leave this window open while people are testing - closing it stops
REM the server. Press Ctrl+C to stop it cleanly.

cd /d "%~dp0"

echo ============================================================
echo  RDC Teamlease Employee Onboarding Portal
echo ============================================================
echo.

if not exist "venv\Scripts\python.exe" (
    echo [ERROR] Could not find venv\Scripts\python.exe
    echo         Run this once first, from this folder:
    echo             python -m venv venv
    echo             venv\Scripts\pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

if not exist ".env" (
    echo [WARNING] No .env file found in this folder.
    echo           The app needs DATABASE_URL and, for the Truein/ZingHR/DVT
    echo           integrations, their credentials set there.
    echo           See .env.example for the required variable names.
    echo.
)

echo Starting the server...
echo Once you see "Running on http://...", open this in a browser:
echo     http://localhost:5000
echo.
echo On another PC on the same network, use this machine's IP address
echo instead of localhost - shown below once the server starts.
echo.
echo Press Ctrl+C to stop the server.
echo ------------------------------------------------------------
echo.

venv\Scripts\python.exe run.py

echo.
echo ------------------------------------------------------------
echo Server stopped.
pause

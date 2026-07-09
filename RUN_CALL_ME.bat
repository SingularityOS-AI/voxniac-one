@echo off
chcp 65001 > nul
title Voxniac ONE — Call Me
cd /d "%~dp0"

:: Find the interpreter in a cascade (absolute paths first, then local venvs) —
:: prioritizes the user's local machine (C:\Python312_Neural) first.
set "PYTHON_EXE=python"
if exist "C:\Python312_Neural\python.exe" (
    set "PYTHON_EXE=C:\Python312_Neural\python.exe"
) else if exist "C:\Users\gabriel\AppData\Local\Programs\Python\Python312\python.exe" (
    set "PYTHON_EXE=C:\Users\gabriel\AppData\Local\Programs\Python\Python312\python.exe"
) else if exist "C:\Python312\python.exe" (
    set "PYTHON_EXE=C:\Python312\python.exe"
) else if exist "%~dp0.venv\Scripts\python.exe" (
    set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
) else if exist "%~dp0venv\Scripts\python.exe" (
    set "PYTHON_EXE=%~dp0venv\Scripts\python.exe"
)

:: Phase 3.5 QA fix: no hardcoded personal number here — if no argument is
:: given, call_launcher.py itself falls back to the CALL_ME_NUMBER
:: environment variable (set it in ..\.env) and fails loud if that's missing
:: too. Never hardcode a real phone number in source (shared hackathon repo).
set "TO_NUMBER=%1"

echo ====================================================
echo [SINGULARITY] Voxniac ONE — Call Me
echo Directory: %~dp0
echo Selected interpreter: %PYTHON_EXE%
if "%TO_NUMBER%"=="" (
    echo Target number: CALL_ME_NUMBER from .env
) else (
    echo Target number: %TO_NUMBER%
)
echo ====================================================

echo [1/2] Starting Voxniac ONE server (uvicorn) in a new window...
start "Voxniac ONE Server" "%PYTHON_EXE%" -m uvicorn server:app --host 127.0.0.1 --port 8080

echo [2/2] Waiting 5s for the server to come up...
timeout /t 5 /nobreak > nul

if "%TO_NUMBER%"=="" (
    "%PYTHON_EXE%" call_launcher.py --port 8080
) else (
    "%PYTHON_EXE%" call_launcher.py %TO_NUMBER% --port 8080
)
if %errorlevel% neq 0 (
    echo [ERROR] call_launcher.py failed with exit code %errorlevel%
    pause
)

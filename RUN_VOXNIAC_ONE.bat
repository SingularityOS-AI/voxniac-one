@echo off
chcp 65001 > nul
title Launching Voxniac ONE (Web)
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

echo ====================================================
echo [SINGULARITY] Starting Voxniac ONE (Web App)
echo Directory: %~dp0
echo Selected interpreter: %PYTHON_EXE%
echo Running: uvicorn server:app --host 127.0.0.1 --port 8080
echo ====================================================

start "" http://127.0.0.1:8080

"%PYTHON_EXE%" -m uvicorn server:app --host 127.0.0.1 --port 8080
if %errorlevel% neq 0 (
    echo [ERROR] Voxniac ONE (uvicorn) failed with exit code %errorlevel%
    pause
)

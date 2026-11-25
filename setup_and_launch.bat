@echo off
title SATB Splitter — Setup
cd /d "%~dp0"

echo ============================================
echo  SATB Splitter — install dependencies
echo ============================================
echo.

where python >nul 2>&1
if errorlevel 1 (
  echo Python was not found on PATH.
  echo Install Python 3.11+ from https://www.python.org/downloads/
  echo Make sure "Add python.exe to PATH" is checked.
  pause
  exit /b 1
)

if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Creating virtual environment...
  python -m venv .venv
  if errorlevel 1 (
    echo Failed to create .venv
    pause
    exit /b 1
  )
)

echo Installing package + GUI + OMR dependencies...
"%~dp0.venv\Scripts\python.exe" -m pip install --upgrade pip
"%~dp0.venv\Scripts\python.exe" -m pip install -e ".[gui,omr,dev]"
if errorlevel 1 (
  echo Install failed.
  pause
  exit /b 1
)

echo.
echo Creating Desktop shortcut...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\create_shortcut.ps1"
echo.
echo Done. Launching app...
call "%~dp0Launch SATB Splitter.bat"

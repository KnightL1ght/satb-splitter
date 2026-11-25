@echo off
title SATB Splitter
cd /d "%~dp0"

if exist "%~dp0.venv\Scripts\pythonw.exe" (
  start "SATB Splitter" "%~dp0.venv\Scripts\pythonw.exe" -m satb_splitter.gui.app
  exit /b 0
)

if exist "%~dp0.venv\Scripts\python.exe" (
  start "SATB Splitter" "%~dp0.venv\Scripts\python.exe" -m satb_splitter.gui.app
  exit /b 0
)

where python >nul 2>&1
if %ERRORLEVEL%==0 (
  start "SATB Splitter" python -m satb_splitter.gui.app
  exit /b 0
)

echo Could not find Python or .venv for SATB Splitter.
echo Run setup first:  setup_and_launch.bat
pause
exit /b 1

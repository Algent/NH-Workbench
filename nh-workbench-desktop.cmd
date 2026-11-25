@echo off
rem Starts the NH Workbench desktop UI. Installs its extra (Qt, about 100 MB) the first time.
title NH Workbench
cd /d "%~dp0"
echo Updating the environment...
uv sync --inexact --extra desktop --quiet 2>nul || echo Could not update the environment, another copy may still be running. Starting anyway.
rem The gui-script uv makes is a windowed program: no console stays behind it, and this one closes at once.
rem (The venv's pythonw.exe would not do: uv makes it a console program, a copy of python.exe.)
if not exist ".venv\Scripts\nh-workbench-desktop.exe" (
    echo The desktop UI is not installed: uv sync --extra desktop failed.
    pause
    exit /b 1
)
start "" ".venv\Scripts\nh-workbench-desktop.exe"

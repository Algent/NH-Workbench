@echo off
rem Starts the NH Workbench desktop UI. Installs its extra (Qt, about 100 MB) the first time.
title NH Workbench
cd /d "%~dp0"
echo Updating the environment...
uv sync --inexact --extra desktop --quiet 2>nul || echo Could not update the environment, another copy may still be running. Starting anyway.
rem pythonw: no console window behind the app, and this one closes once it has started it.
start "" ".venv\Scripts\pythonw.exe" -m nh_workbench.gui

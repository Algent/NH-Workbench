@echo off
rem Starts the NH Workbench desktop UI. Installs its extra (Qt, about 100 MB) the first time.
setlocal
title NH Workbench
cd /d "%~dp0"
echo Updating the environment...
uv sync --inexact --extra desktop --quiet 2>nul || echo Could not update the environment, another copy may still be running. Starting anyway.
rem No console may stay behind the app, or closing it closes the app. The venv's pythonw.exe and the
rem nh-workbench-desktop.exe uv makes are both console programs underneath (uv's pythonw.exe is a copy of
rem python.exe), so run the base Python's pythonw.exe, told which venv it is in as the venv launcher would.
set "PYHOME="
for /f "usebackq tokens=1,* delims==" %%a in (".venv\pyvenv.cfg") do if /i "%%a"=="home " set "PYHOME=%%b"
if defined PYHOME set "PYHOME=%PYHOME:~1%"
if not exist "%PYHOME%\pythonw.exe" (
    echo The environment is not set up: uv sync --extra desktop failed.
    pause
    exit /b 1
)
set "__PYVENV_LAUNCHER__=%~dp0.venv\Scripts\pythonw.exe"
start "" "%PYHOME%\pythonw.exe" -m nh_workbench.gui

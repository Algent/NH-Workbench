@echo off
rem Starts the NH Workbench terminal UI. Arguments are passed through, e.g. nh-workbench.cmd status
title NH Workbench
cd /d "%~dp0"
rem A copy that is still running locks files uv wants to replace: start with what is installed then.
rem --inexact: keeps the desktop and web UIs' extras when they are installed.
uv sync --inexact --quiet 2>nul || echo Could not update the environment, another copy may still be running. Starting anyway.
rem Run as a module: nothing then holds nh-workbench.exe open, so later updates are not blocked.
uv run --no-sync python -m nh_workbench %*
if errorlevel 1 pause

@echo off
rem Starts the NH Workbench web UI: the terminal UI in a browser tab. Installs its extra the first time.
rem Arguments are passed through, e.g. nh-workbench-web.cmd --port 8750 --no-browser
title NH Workbench (web)
cd /d "%~dp0"
uv sync --inexact --extra web --quiet 2>nul || echo Could not update the environment, another copy may still be running. Starting anyway.
uv run --no-sync python -m nh_workbench web %*
if errorlevel 1 pause

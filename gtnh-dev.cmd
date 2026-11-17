@echo off
rem Starts the gtnh-dev terminal UI. Arguments are passed through, e.g. gtnh-dev.cmd status
title GTNH Dev
cd /d "%~dp0"
rem A copy that is still running locks files uv wants to replace: start with what is installed then.
uv sync --quiet 2>nul || echo Could not update the environment, another copy may still be running. Starting anyway.
rem Run as a module: nothing then holds gtnh-dev.exe open, so later updates are not blocked.
uv run --no-sync python -m gtnh_devenv_tools %*
if errorlevel 1 pause

@echo off
rem Starts the gtnh-dev terminal UI. Arguments are passed through, e.g. gtnh-dev.cmd status
cd /d "%~dp0"
uv run gtnh-dev %*
if errorlevel 1 pause

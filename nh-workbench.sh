#!/usr/bin/env sh
# Starts the NH Workbench terminal UI. Arguments are passed through, e.g. ./nh-workbench.sh status
cd "$(dirname "$0")" || exit 1
# --inexact: keeps the desktop UI's extra when it is installed.
uv sync --inexact --quiet 2>/dev/null || echo "Could not update the environment. Starting anyway."
exec uv run --no-sync python -m nh_workbench "$@"

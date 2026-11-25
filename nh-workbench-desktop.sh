#!/usr/bin/env sh
# Starts the NH Workbench desktop UI. Installs its extra (Qt, about 100 MB) the first time.
cd "$(dirname "$0")" || exit 1
uv sync --inexact --extra desktop --quiet 2>/dev/null || echo "Could not update the environment. Starting anyway."
exec uv run --no-sync python -m nh_workbench.gui

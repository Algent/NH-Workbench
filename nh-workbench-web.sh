#!/usr/bin/env sh
# Starts the NH Workbench web UI: the terminal UI in a browser tab. Installs its extra the first time.
# Arguments are passed through, e.g. ./nh-workbench-web.sh --port 8750 --no-browser
cd "$(dirname "$0")" || exit 1
uv sync --inexact --extra web --quiet 2>/dev/null || echo "Could not update the environment. Starting anyway."
exec uv run --no-sync python -m nh_workbench web "$@"

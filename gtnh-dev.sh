#!/usr/bin/env sh
# Starts the gtnh-dev terminal UI. Arguments are passed through, e.g. ./gtnh-dev.sh status
cd "$(dirname "$0")" || exit 1
uv sync --quiet 2>/dev/null || echo "Could not update the environment. Starting anyway."
exec uv run --no-sync python -m gtnh_devenv_tools "$@"

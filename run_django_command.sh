#!/bin/bash
set -eo pipefail

# Local helper script to run Django management commands with proper environment setup.
#
# This is for local checkouts. Production runs Django inside the app container;
# use docker exec there.
#
# Usage: ./run_django_command.sh <manage.py command> [arguments]
# Example: ./run_django_command.sh migrate
# Example: ./run_django_command.sh runserver 0.0.0.0:8000
#
# Python selection order:
#   1. CAPER_PYTHON, if set
#   2. active virtualenv, if VIRTUAL_ENV is set
#   3. active conda env, if CONDA_PREFIX is set
#   4. repo-local ./ampliconenv, if present
#   5. python3, then python from PATH

# Check if at least one argument is provided
if [ $# -eq 0 ]; then
    echo "Error: No command provided"
    echo "Usage: ./run_django_command.sh <manage.py command> [arguments]"
    echo "Example: ./run_django_command.sh migrate"
    echo "Example: ./run_django_command.sh runserver 0.0.0.0:8000"
    exit 1
fi

# Get the directory where this script is located
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Source the config file to set environment variables
echo "Sourcing config.sh to set environment variables..."
if [ -f "$SCRIPT_DIR/caper/config.sh" ]; then
    source "$SCRIPT_DIR/caper/config.sh"
else
    echo "Error: config.sh not found at $SCRIPT_DIR/caper/config.sh"
    exit 1
fi

# Select Python without requiring a repo-local virtualenv.
PYTHON_BIN=""
if [ -n "${CAPER_PYTHON:-}" ]; then
    if ! command -v "$CAPER_PYTHON" >/dev/null 2>&1; then
        echo "Error: CAPER_PYTHON is set but is not executable or on PATH: $CAPER_PYTHON"
        exit 1
    fi
    PYTHON_BIN="$CAPER_PYTHON"
    echo "Using CAPER_PYTHON: $PYTHON_BIN"
elif [ -n "${VIRTUAL_ENV:-}" ] && [ -x "$VIRTUAL_ENV/bin/python" ]; then
    PYTHON_BIN="$VIRTUAL_ENV/bin/python"
    echo "Using active virtualenv Python: $PYTHON_BIN"
elif [ -n "${CONDA_PREFIX:-}" ] && [ -x "$CONDA_PREFIX/bin/python" ]; then
    PYTHON_BIN="$CONDA_PREFIX/bin/python"
    echo "Using active conda Python: $PYTHON_BIN"
elif [ -x "$SCRIPT_DIR/ampliconenv/bin/python" ]; then
    PYTHON_BIN="$SCRIPT_DIR/ampliconenv/bin/python"
    echo "Using repo-local Python: $PYTHON_BIN"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
    echo "Using python3 from PATH: $PYTHON_BIN"
elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python)"
    echo "Using python from PATH: $PYTHON_BIN"
else
    echo "Error: no Python interpreter found. Activate an environment or set CAPER_PYTHON."
    exit 1
fi

# Change to the caper directory
cd "$SCRIPT_DIR/caper" || exit 1

# Run the Django management command with all provided arguments
echo "Running: $PYTHON_BIN manage.py $*"
"$PYTHON_BIN" manage.py "$@"

#!/bin/bash

# Helper script to run Django management commands with proper environment setup
# Usage: ./run_django_command.sh <manage.py command> [arguments]
# Example: ./run_django_command.sh migrate
# Example: ./run_django_command.sh runserver 0.0.0.0:8000

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

# Activate virtual environment if it exists
if [ -d "$SCRIPT_DIR/ampliconenv/bin" ]; then
    echo "Activating virtual environment..."
    source "$SCRIPT_DIR/ampliconenv/bin/activate"
fi

# Change to the caper directory
cd "$SCRIPT_DIR/caper" || exit 1

# Run the Django management command with all provided arguments
echo "Running: python manage.py $@"
python manage.py "$@"

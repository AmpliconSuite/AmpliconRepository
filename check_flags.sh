#!/bin/bash
#
# Wrapper script to check project flags using Django shell
# Works correctly inside Docker containers
#
# Usage:
#   ./check_flags.sh
#   ./check_flags.sh > report.txt
#

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

echo "Checking project flags using Django shell..."
echo ""

python manage.py shell < "${SCRIPT_DIR}/check_project_flags_django.py"

exit_code=$?

if [ $exit_code -ne 0 ]; then
    echo ""
    echo "❌ Script failed with exit code $exit_code"
    echo ""
    echo "Make sure you're running this from the Django app directory:"
    echo "  cd /path/to/caper/caper"
    echo "  ./check_flags.sh"
    exit $exit_code
fi


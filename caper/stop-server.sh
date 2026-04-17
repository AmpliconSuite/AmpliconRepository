#!/bin/bash

MAX_WAIT_MINUTES=30
RETRY_INTERVAL=60  # seconds
STATUS_URL="http://localhost:8000/api/background-task-status/"

echo "Checking for running background tasks before stopping server..."

for ((i=1; i<=MAX_WAIT_MINUTES; i++)); do
    response=$(curl -s --max-time 5 "$STATUS_URL" 2>/dev/null)
    if [ $? -ne 0 ] || [ -z "$response" ]; then
        echo "Could not reach background task status endpoint; proceeding with shutdown."
        break
    fi

    is_busy=$(echo "$response" | python3 -c "import sys, json; d=json.load(sys.stdin); print(d.get('is_busy', False))" 2>/dev/null)
    active_count=$(echo "$response" | python3 -c "import sys, json; d=json.load(sys.stdin); print(d.get('active_count', 0))" 2>/dev/null)

    if [ "$is_busy" = "False" ] && [ "$active_count" = "0" ]; then
        echo "No background tasks running. Proceeding with shutdown."
        break
    fi

    echo "Background tasks still running (is_busy=$is_busy, active_count=$active_count). Waiting... (attempt $i/$MAX_WAIT_MINUTES)"
    if [ "$i" -eq "$MAX_WAIT_MINUTES" ]; then
        echo "Timeout reached after ${MAX_WAIT_MINUTES} minutes. Forcing shutdown anyway."
    else
        sleep "$RETRY_INTERVAL"
    fi
done

# Try Docker path first, then fall back to any manage.py process
PIDS=$(ps aux | grep '[p]ython /srv/caper/manage.py' | awk '{print $2}')
if [ -z "$PIDS" ]; then
    PIDS=$(ps aux | grep '[m]anage.py' | awk '{print $2}')
fi

if [ -z "$PIDS" ]; then
    echo "No Django manage.py process found. Server may already be stopped."
    exit 0
fi

echo "Stopping Django process(es): $PIDS"
kill -9 $PIDS
echo "Done."

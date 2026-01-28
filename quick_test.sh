#!/bin/bash
#
# Quick performance test for currently running server
# Usage: ./quick_test.sh [URL] [REQUESTS] [CONCURRENCY]
#

URL=${1:-http://localhost:8000/}
REQUESTS=${2:-50}
CONCURRENCY=${3:-5}

echo "=================================================="
echo "  Quick Performance Test"
echo "=================================================="
echo "URL:         $URL"
echo "Requests:    $REQUESTS"
echo "Concurrency: $CONCURRENCY"
echo "=================================================="
echo ""

# Check if server is up
echo "Checking server availability..."
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" -L --max-time 10 \
    -H "User-Agent: Mozilla/5.0 (compatible; PerformanceTest/1.0)" \
    "$URL" 2>/dev/null || echo "000")

if ! echo "$HTTP_CODE" | grep -q "200\|302\|301\|403"; then
    echo "Error: Server at $URL is not responding (HTTP code: $HTTP_CODE)"
    echo "Make sure your server is running first."
    echo ""
    echo "Debug: Trying direct connection..."
    curl -v -L --max-time 10 -H "User-Agent: Mozilla/5.0" "$URL" 2>&1 | head -20
    exit 1
fi

echo "✓ Server is responding (HTTP $HTTP_CODE)"
echo ""

# Check if requests library is installed
python3 -c "import requests" 2>/dev/null || {
    echo "Installing requests library..."
    pip3 install requests
}

# Run the test
python3 performance_test.py \
    --url "$URL" \
    --requests $REQUESTS \
    --concurrency $CONCURRENCY


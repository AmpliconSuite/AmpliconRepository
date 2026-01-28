#!/bin/bash
#
# Test from inside the container (bypasses ALL external layers)
# This gives you the TRUE performance of Django/Gunicorn
# Usage: ./test_inside_container.sh [CONTAINER_NAME] [REQUESTS] [CONCURRENCY]
#

CONTAINER_NAME=${1:-amplicon-prod}
REQUESTS=${2:-100}
CONCURRENCY=${3:-10}

echo "=================================================="
echo "  Inside-Container Performance Test"
echo "=================================================="
echo "Container:   $CONTAINER_NAME"
echo "Requests:    $REQUESTS"
echo "Concurrency: $CONCURRENCY"
echo "=================================================="
echo ""

# Check if container is running
if ! docker ps | grep -q "$CONTAINER_NAME"; then
    echo "Error: Container '$CONTAINER_NAME' is not running"
    echo ""
    echo "Available containers:"
    docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
    exit 1
fi

echo "Installing test dependencies inside container..."
docker exec "$CONTAINER_NAME" /bin/bash -c "source /opt/venv/bin/activate && pip install requests 2>/dev/null" || true

echo ""
echo "Copying test script to container..."
docker cp performance_test.py "$CONTAINER_NAME:/tmp/performance_test.py"

echo ""
echo "Running test from INSIDE the container..."
echo "This bypasses load balancers, CloudFront, SSL, etc."
echo "Testing: http://localhost:8000/"
echo ""

docker exec "$CONTAINER_NAME" /bin/bash -c "
source /opt/venv/bin/activate
cd /tmp
python performance_test.py \
    --url http://localhost:8000/ \
    --requests $REQUESTS \
    --concurrency $CONCURRENCY
"

echo ""
echo "=================================================="
echo "Gunicorn Process Information"
echo "=================================================="
docker exec "$CONTAINER_NAME" ps aux | grep -E 'gunicorn|PID' || echo "No gunicorn processes found"

echo ""
echo "=================================================="
echo "Container Resource Usage"
echo "=================================================="
docker stats "$CONTAINER_NAME" --no-stream


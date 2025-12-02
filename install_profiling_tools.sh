#!/bin/bash
# Install memory profiling dependencies for CAPER

echo "=========================================="
echo "Installing Memory Profiling Tools"
echo "=========================================="

# Check if we're in a virtual environment
if [ -z "$VIRTUAL_ENV" ]; then
    echo "⚠️  Warning: No virtual environment detected"
    echo "   Consider activating your virtual environment first"
    read -p "Continue anyway? (y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

echo ""
echo "📦 Installing psutil..."
pip install psutil

echo ""
echo "📦 Installing memory-profiler..."
pip install memory-profiler

echo ""
echo "📦 Installing pympler..."
pip install pympler

echo ""
echo "📦 Installing objgraph..."
pip install objgraph

echo ""
echo "✅ Installation complete!"
echo ""
echo "You can now use:"
echo "  - python memory_monitor.py    (to monitor Django process)"
echo "  - python quick_diagnostic.py  (to check for leak patterns)"
echo ""
echo "Optional: Install graphviz for objgraph visualization"
echo "  macOS: brew install graphviz"
echo "  Linux: apt-get install graphviz"


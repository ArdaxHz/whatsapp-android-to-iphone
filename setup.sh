#!/bin/bash
set -e

echo "Setting up WhatsApp Migration Tool..."

# Check Python
if ! command -v python3 &>/dev/null; then
    echo "ERROR: Python 3 not found. Install from https://python.org"
    exit 1
fi

# Check ADB
if ! command -v adb &>/dev/null; then
    echo "ERROR: ADB not found. Run: brew install android-platform-tools"
    exit 1
fi

# Install Python deps
pip3 install -r requirements.txt --quiet

echo ""
echo "Setup complete. Run the tool with:"
echo "  python3 migrate.py"

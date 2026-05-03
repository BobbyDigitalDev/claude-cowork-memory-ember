#!/bin/bash
# Install the monthly contrary scout launchd daemon
set -e

BASE="$HOME/claude_memory"
PLIST_SRC="$HOME/claude_memory/daemons/com.ember-engine.contrary-scout.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.ember-engine.contrary-scout.plist"

if [ ! -f "$PLIST_SRC" ]; then
    echo "ERROR: plist not found at $PLIST_SRC"
    exit 1
fi

sed "s|__INSTALL_DIR__|$BASE|g" "$PLIST_SRC" > "$PLIST_DST"
launchctl load "$PLIST_DST"

echo "Contrary scout daemon installed."
echo "Runs on the 1st of each month at 2pm."
echo ""
echo "To run immediately (test):"
echo "  python3 ~/claude_memory/scripts/research_scout.py --contrary --no-jitter --dry-run"

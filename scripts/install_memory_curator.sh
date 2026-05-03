#!/bin/bash
# install_memory_curator.sh
# Installs memory_curator.py as a nightly launchd agent.
# Runs at 02:00 daily (after research_scout at noon, after embed_memories in between).
# Re-run to update the plist if the script path changes.

set -e

SCRIPT="$HOME/claude_memory/scripts/memory_curator.py"
PLIST_NAME="com.ember-engine.memory-curator"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_NAME}.plist"
LOG_DIR="$HOME/claude_memory/logs"
LOG_OUT="${LOG_DIR}/memory_curator.log"
LOG_ERR="${LOG_DIR}/memory_curator_error.log"
PYTHON=$(which python3)

echo ""
echo "============================================================"
echo "Memory Curator Agent -- launchd installer"
echo "============================================================"

# Step 1: verify script exists
echo ""
echo "[1/4] Checking memory_curator.py..."
if [ ! -f "$SCRIPT" ]; then
  echo "  ERROR: not found at $SCRIPT"
  exit 1
fi
echo "  OK: $SCRIPT"

# Step 2: ensure log directory exists
echo ""
echo "[2/4] Log directory..."
mkdir -p "$LOG_DIR"
echo "  OK: $LOG_DIR"

# Step 3: write plist
echo ""
echo "[3/4] Writing plist to $PLIST_PATH..."
cat > "$PLIST_PATH" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_NAME}</string>

    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON}</string>
        <string>${SCRIPT}</string>
    </array>

    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>2</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>

    <key>StandardOutPath</key>
    <string>${LOG_OUT}</string>

    <key>StandardErrorPath</key>
    <string>${LOG_ERR}</string>

    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
EOF
echo "  OK: plist written"

# Step 4: load agent
echo ""
echo "[4/4] Loading launchd agent..."
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"
STATUS=$(launchctl list | grep "$PLIST_NAME" | awk '{print $1}')
echo "  Status PID/last-exit: $STATUS"

echo ""
echo "============================================================"
echo "Memory Curator Agent installed."
echo "Schedule: daily at 02:00"
echo "Log:      $LOG_OUT"
echo "============================================================"
echo ""

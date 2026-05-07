#!/bin/bash
# install_context_agent.sh
# Installs the Context Snapshot Agent as a launchd job on macOS.
# Run once from your Mac terminal:
#   bash ~/claude_memory/scripts/install_context_agent.sh
#
# What it does:
#   1. Creates ~/claude_memory/logs/ if it doesn't exist
#   2. Copies the plist to ~/Library/LaunchAgents/
#   3. Loads the job with launchctl (activates the schedule)
#
# To uninstall:
#   launchctl unload ~/Library/LaunchAgents/com.ember-engine.context-agent.plist
#   rm ~/Library/LaunchAgents/com.ember-engine.context-agent.plist
#
# To run manually at any time (e.g. at session start):
#   python3 ~/claude_memory/scripts/context_snapshot_agent.py
#
# To check job status:
#   launchctl list | grep claude-memory
#
# To view last launchd output:
#   cat ~/claude_memory/logs/context_agent_launchd.out
#   cat ~/claude_memory/logs/context_agent_launchd.err

set -e

BASE="$HOME/claude_memory"
PLIST_SRC="$BASE/daemons/com.ember-engine.context-agent.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.ember-engine.context-agent.plist"
LOGS_DIR="$BASE/logs"
AGENT_SCRIPT="$BASE/scripts/context_snapshot_agent.py"

echo "============================================================"
echo "Context Snapshot Agent installer"
echo "============================================================"

# Check prerequisites
if [ ! -f "$PLIST_SRC" ]; then
  echo "ERROR: plist not found at $PLIST_SRC"
  echo "Make sure you are running this from the correct machine."
  exit 1
fi

if [ ! -f "$AGENT_SCRIPT" ]; then
  echo "ERROR: agent script not found at $AGENT_SCRIPT"
  exit 1
fi

# Detect python3 path (Homebrew on Apple Silicon is /opt/homebrew/bin/python3)
PYTHON3_PATH=$(which python3 2>/dev/null || echo "/usr/bin/python3")
echo "Detected python3: $PYTHON3_PATH"

# Create logs directory
echo "Creating logs directory: $LOGS_DIR"
mkdir -p "$LOGS_DIR"

# Copy plist to LaunchAgents, patching python3 path and home directory
echo "Copying plist to ~/Library/LaunchAgents/ (python3: $PYTHON3_PATH, home: $HOME)"
sed \
    -e "s|__PYTHON3__|$PYTHON3_PATH|g" \
    -e "s|__INSTALL_DIR__|$BASE|g" \
    "$PLIST_SRC" > "$PLIST_DST"


# Unload current label if already loaded (handles re-install)
if launchctl list | grep -q "com.ember-engine.context-agent"; then
  echo "Unloading existing job..."
  launchctl unload "$PLIST_DST" 2>/dev/null || true
fi

# Load the job
echo "Loading job with launchctl..."
launchctl load "$PLIST_DST"

echo ""
echo "============================================================"
echo "Installation complete."
echo ""
echo "Schedule: daily at 10:00 AM local time"
echo "Agent:    $AGENT_SCRIPT"
echo "Logs:     $LOGS_DIR/"
echo ""
echo "To verify the job is registered:"
echo "  launchctl list | grep claude-memory"
echo ""
echo "To run immediately (test or session start):"
echo "  python3 ~/claude_memory/scripts/context_snapshot_agent.py"
echo ""
echo "To uninstall:"
echo "  launchctl unload ~/Library/LaunchAgents/com.ember-engine.context-agent.plist"
echo "  rm ~/Library/LaunchAgents/com.ember-engine.context-agent.plist"
echo "============================================================"

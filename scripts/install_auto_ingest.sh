#!/bin/bash
# install_auto_ingest.sh
# Installs auto_ingest.py as a launchd WatchPaths agent.
# Fires automatically whenever the conversations/ directory changes.
# Debounce window (default 15 min) prevents mid-session false triggers.

set -e

SCRIPT="$HOME/claude_memory/scripts/auto_ingest.py"
CONV_DIR="$HOME/claude_memory/conversations"
PLIST_NAME="com.ember-engine.auto-ingest"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_NAME}.plist"
LOG_DIR="$HOME/claude_memory/logs"
LOG_OUT="${LOG_DIR}/auto_ingest.log"
LOG_ERR="${LOG_DIR}/auto_ingest_error.log"
PYTHON=$(which python3)
TOTAL_STEPS=4

# ── Progress bar ──────────────────────────────────────────────────────────────
progress_bar() {
  local step=$1
  local total=$2
  local label=$3
  local width=30
  local filled=$(( step * width / total ))
  local empty=$(( width - filled ))
  local bar=""
  for ((i=0; i<filled; i++)); do bar+="█"; done
  for ((i=0; i<empty;  i++)); do bar+="░"; done
  printf "\n[%s] %d/%d  %s\n" "$bar" "$step" "$total" "$label"
}

echo ""
echo "============================================================"
echo " Auto-Ingest Agent — launchd installer"
echo "============================================================"
echo ""
echo " Watches ~/claude_memory/conversations/ for changes."
echo " Runs ingest.py --scan after 15 min of session inactivity."
echo ""

# Step 1: verify scripts exist
progress_bar 1 $TOTAL_STEPS "Checking scripts and directories"
if [ ! -f "$SCRIPT" ]; then
  echo "  ERROR: auto_ingest.py not found at $SCRIPT"
  exit 1
fi
if [ ! -d "$CONV_DIR" ]; then
  echo "  ERROR: conversations directory not found at $CONV_DIR"
  exit 1
fi
echo "  OK: $SCRIPT"
echo "  OK: $CONV_DIR"

# Step 2: ensure log directory exists
progress_bar 2 $TOTAL_STEPS "Log directory"
mkdir -p "$LOG_DIR"
echo "  OK: $LOG_DIR"

# Step 3: write plist
progress_bar 3 $TOTAL_STEPS "Writing plist"
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

    <key>WatchPaths</key>
    <array>
        <string>${CONV_DIR}</string>
    </array>

    <key>StandardOutPath</key>
    <string>${LOG_OUT}</string>

    <key>StandardErrorPath</key>
    <string>${LOG_ERR}</string>

    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
EOF
echo "  OK: $PLIST_PATH"

# Step 4: load agent
progress_bar 4 $TOTAL_STEPS "Loading launchd agent"
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"
STATUS=$(launchctl list | grep "$PLIST_NAME" | awk '{print $1}')
echo "  Status PID/last-exit: $STATUS"

echo ""
echo "============================================================"
echo " Auto-Ingest Agent installed."
echo " Trigger:  WatchPaths on ~/claude_memory/conversations/"
echo " Debounce: 15 minutes of inactivity"
echo " Log:      $LOG_OUT"
echo ""
echo " Force run:  python3 ~/claude_memory/scripts/auto_ingest.py --force"
echo " Dry run:    python3 ~/claude_memory/scripts/auto_ingest.py --dry-run --force"
echo " Requires:   Ollama running with qwen2.5:14b and nomic-embed-text"
echo "============================================================"
echo ""

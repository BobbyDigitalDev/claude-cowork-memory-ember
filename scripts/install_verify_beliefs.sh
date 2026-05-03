#!/bin/bash
# install_verify_beliefs.sh
# Installs verify_beliefs.py as a nightly launchd agent.
# Runs at 03:00 daily — after Memory Curator (02:00), before session prompt (03:30).
# Re-run to update the plist if the script path changes.

set -e

SCRIPT="$HOME/claude_memory/scripts/verify_beliefs.py"
PLIST_NAME="com.ember-engine.verify-beliefs"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_NAME}.plist"
LOG_DIR="$HOME/claude_memory/logs"
LOG_OUT="${LOG_DIR}/verify_beliefs.log"
LOG_ERR="${LOG_DIR}/verify_beliefs_error.log"
PYTHON=$(which python3)
TOTAL_STEPS=5

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
echo " Belief Verification Agent — launchd installer"
echo "============================================================"

# Step 1: verify script exists
progress_bar 1 $TOTAL_STEPS "Checking verify_beliefs.py"
if [ ! -f "$SCRIPT" ]; then
  echo "  ERROR: not found at $SCRIPT"
  exit 1
fi
echo "  OK: $SCRIPT"

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
        <string>--limit</string>
        <string>30</string>
        <string>--check-contradictions</string>
    </array>

    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>3</integer>
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
echo "  OK: $PLIST_PATH"

# Step 4: dry run to confirm the script is functional
progress_bar 4 $TOTAL_STEPS "Dry run (no DB writes)"
$PYTHON "$SCRIPT" --dry-run --limit 3
echo "  OK: dry run passed"

# Step 5: load agent
progress_bar 5 $TOTAL_STEPS "Loading launchd agent"
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"
STATUS=$(launchctl list | grep "$PLIST_NAME" | awk '{print $1}')
echo "  Status PID/last-exit: $STATUS"

echo ""
echo "============================================================"
echo " Belief Verification Agent installed."
echo " Schedule: daily at 03:00"
echo " Log:      $LOG_OUT"
echo ""
echo " Manual run:  python3 ~/claude_memory/scripts/verify_beliefs.py --no-jitter"
echo " Dry run:     python3 ~/claude_memory/scripts/verify_beliefs.py --dry-run"
echo " Requires:    Ollama running with deepseek-r1:14b"
echo "============================================================"
echo ""

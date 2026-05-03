#!/bin/bash
# install_session_prompt.sh
# Installs generate_session_prompt.py as a nightly launchd agent.
# Runs at 03:30 AM — after Memory Curator (02:00) and Belief Verification (03:00),
# so START_HERE.md is freshly generated with verified beliefs each morning.

set -e

SCRIPT="$HOME/claude_memory/scripts/generate_session_prompt.py"
PLIST_NAME="com.ember-engine.session-prompt"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_NAME}.plist"
LOG_DIR="$HOME/claude_memory/logs"
LOG_OUT="${LOG_DIR}/session_prompt.log"
LOG_ERR="${LOG_DIR}/session_prompt_error.log"
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
echo " Session Prompt Agent — launchd installer"
echo "============================================================"
echo ""
echo " Generates START_HERE.md and ember_engine_context.md nightly."
echo " Runs at 03:30 AM after Curator (02:00) and Verification (03:00)."
echo ""

# Step 1: verify script exists
progress_bar 1 $TOTAL_STEPS "Checking generate_session_prompt.py"
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
launchctl unload "$PLIST_PATH" 2>/dev/null || true
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
        <integer>3</integer>
        <key>Minute</key>
        <integer>30</integer>
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

# Step 4: load agent
progress_bar 4 $TOTAL_STEPS "Loading launchd agent"
launchctl load "$PLIST_PATH"
STATUS=$(launchctl list | grep "$PLIST_NAME" | awk '{print $1}')
echo "  Status PID/last-exit: $STATUS"

echo ""
echo "============================================================"
echo " Session Prompt Agent installed."
echo " Schedule: nightly at 03:30"
echo " Output:   ~/claude_memory/START_HERE.md"
echo "           ~/claude_memory/ember_engine_context.md"
echo " Log:      $LOG_OUT"
echo ""
echo " Manual:   python3 ~/claude_memory/scripts/generate_session_prompt.py"
echo "============================================================"
echo ""

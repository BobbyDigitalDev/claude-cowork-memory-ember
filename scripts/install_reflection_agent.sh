#!/bin/bash
# install_reflection_agent.sh
# Installs reflection_agent.py as a weekly launchd agent.
# Runs Sundays at 04:00 — after the nightly pipeline (Curator 02:00,
# Verify 03:00, session prompt 03:30), giving a quiet window for
# the weekly synthesis pass.
# Skips automatically if a reflection was written in the last 7 days.

set -e

SCRIPT="$HOME/claude_memory/scripts/reflection_agent.py"
PLIST_NAME="com.ember-engine.reflection-agent"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_NAME}.plist"
LOG_DIR="$HOME/claude_memory/logs"
LOG_OUT="${LOG_DIR}/reflection_agent.log"
LOG_ERR="${LOG_DIR}/reflection_agent_error.log"
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
echo " Reflection Agent — launchd installer"
echo "============================================================"
echo ""
echo " Synthesizes the last 7 days of sessions into a higher-order"
echo " reflection. Writes to the reflections table. Runs weekly."
echo ""

# Step 1: verify script exists
progress_bar 1 $TOTAL_STEPS "Checking reflection_agent.py"
if [ ! -f "$SCRIPT" ]; then
  echo "  ERROR: not found at $SCRIPT"
  exit 1
fi
echo "  OK: $SCRIPT"

# Step 2: ensure log directory exists
progress_bar 2 $TOTAL_STEPS "Log directory"
mkdir -p "$LOG_DIR"
echo "  OK: $LOG_DIR"

# Step 3: write plist (weekly: Sunday at 04:00)
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

    <key>StartCalendarInterval</key>
    <dict>
        <key>Weekday</key>
        <integer>0</integer>
        <key>Hour</key>
        <integer>4</integer>
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

# Step 4: load agent
progress_bar 4 $TOTAL_STEPS "Loading launchd agent"
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"
STATUS=$(launchctl list | grep "$PLIST_NAME" | awk '{print $1}')
echo "  Status PID/last-exit: $STATUS"

echo ""
echo "============================================================"
echo " Reflection Agent installed."
echo " Schedule: Sundays at 04:00 (weekly)"
echo " Interval: skips if reflection written in last 7 days"
echo " Log:      $LOG_OUT"
echo ""
echo " Manual:   python3 ~/claude_memory/scripts/reflection_agent.py --no-jitter --force"
echo " Dry run:  python3 ~/claude_memory/scripts/reflection_agent.py --dry-run --force"
echo " Requires: Ollama running with qwen2.5:14b"
echo "============================================================"
echo ""

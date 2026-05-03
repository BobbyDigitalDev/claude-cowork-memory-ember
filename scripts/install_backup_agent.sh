#!/bin/bash
# install_backup_agent.sh
# -----------------------
# Installs com.ember-engine.backup-agent as a launchd user agent.
# Backs up memory.db every 6 hours. Keeps the 10 most recent copies.

set -e

SCRIPT="$HOME/claude_memory/scripts/backup_memory.py"
PLIST_SRC="$HOME/claude_memory/daemons/com.ember-engine.backup-agent.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/com.ember-engine.backup-agent.plist"
LOG_DIR="$HOME/claude_memory/logs"
LABEL="com.ember-engine.backup-agent"

BAR="============================================================"

echo ""
echo "$BAR"
echo " Memory Backup Agent — launchd installer"
echo "$BAR"
echo " Backs up memory.db every 6 hours."
echo " Retains the 10 most recent copies in ~/claude_memory/backups/"

progress() { printf "\r[%-30s] %d/4  %s" "$(printf '#%.0s' $(seq 1 $((${1}*7))))" "$1" "$2"; echo; }

# 1. Check script
progress 1 "Checking backup_memory.py"
if [ ! -f "$SCRIPT" ]; then
    echo "  ERROR: $SCRIPT not found."
    exit 1
fi
echo "  OK: $SCRIPT"

# 2. Log dir and backups dir
progress 2 "Log and backups directories"
mkdir -p "$LOG_DIR"
mkdir -p "$HOME/claude_memory/backups"
echo "  OK: $LOG_DIR"
echo "  OK: $HOME/claude_memory/backups"

# 3. Write plist
progress 3 "Writing plist"
if [ ! -f "$PLIST_SRC" ]; then
    echo "  ERROR: $PLIST_SRC not found."
    exit 1
fi
sed "s|__INSTALL_DIR__|$HOME/claude_memory|g" "$PLIST_SRC" > "$PLIST_DEST"
echo "  OK: $PLIST_DEST"

# 4. Load agent
progress 4 "Loading launchd agent"
launchctl unload "$PLIST_DEST" 2>/dev/null || true
launchctl load "$PLIST_DEST"
STATUS=$(launchctl list | grep "$LABEL" | awk '{print $1"/"$2}' || echo "loaded")
echo "  Status PID/last-exit: $STATUS"

echo "$BAR"
echo " Backup Agent installed."
echo " Schedule: every 6 hours"
echo " Backups:  ~/claude_memory/backups/ (10 most recent)"
echo " Log:      ~/claude_memory/logs/backup_agent_stdout.log"
echo " Manual:   python3 ~/claude_memory/scripts/backup_memory.py"
echo " Dry run:  python3 ~/claude_memory/scripts/backup_memory.py --dry-run"
echo "$BAR"
echo ""

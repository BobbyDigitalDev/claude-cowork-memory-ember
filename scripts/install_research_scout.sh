#!/bin/bash
# install_research_scout.sh
# -------------------------
# One-command installer for the Research Scout Agent.
#
# USAGE
#   chmod +x ~/claude_memory/scripts/install_research_scout.sh
#   ~/claude_memory/scripts/install_research_scout.sh

set -e

PLIST_NAME="com.ember-engine.research-scout"
PLIST_SRC="$HOME/claude_memory/daemons/$PLIST_NAME.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_NAME.plist"
DB="$HOME/claude_memory/memory.db"
SCOUT="$HOME/claude_memory/scripts/research_scout.py"

echo ""
echo "============================================================"
echo "  Research Scout Agent -- Installer"
echo "============================================================"
echo ""

# ── Step 1: Verify database ──────────────────────────────────────────────────
echo "[1/4] Checking database..."
if [ ! -f "$DB" ]; then
    echo "  ERROR: Database not found at $DB"
    echo "  Run setup_db.py first."
    exit 1
fi
echo "  OK: $DB"
echo ""

# ── Step 2: Verify scout script ──────────────────────────────────────────────
echo "[2/4] Verifying research_scout.py..."
if [ ! -f "$SCOUT" ]; then
    echo "  ERROR: $SCOUT not found."
    exit 1
fi
python3 "$SCOUT" --list-topics
echo ""

# ── Step 3: Install plist ────────────────────────────────────────────────────
echo "[3/4] Installing launchd agent..."
if [ ! -f "$PLIST_SRC" ]; then
    echo "  ERROR: $PLIST_SRC not found."
    exit 1
fi


# Unload current label if already registered (handles re-install)
if launchctl list | grep -q "$PLIST_NAME" 2>/dev/null; then
    echo "  Unloading existing agent..."
    launchctl unload "$PLIST_DST" 2>/dev/null || true
fi

# Patch home directory into plist at install time (plist ships with placeholder path)
PYTHON3=$(which python3 2>/dev/null || echo "/opt/homebrew/bin/python3")
sed \
    -e "s|__INSTALL_DIR__|$HOME/claude_memory|g" \
    -e "s|/opt/homebrew/bin/python3|$PYTHON3|g" \
    "$PLIST_SRC" > "$PLIST_DST"
launchctl load "$PLIST_DST"
echo "  Agent registered: $PLIST_NAME"
echo ""

# ── Step 4: Confirm ──────────────────────────────────────────────────────────
echo "[4/4] Verifying registration..."
if launchctl list | grep -q "$PLIST_NAME"; then
    echo "  Status: ACTIVE"
else
    echo "  WARNING: Agent not found in launchctl list. Check plist syntax."
fi
echo ""

echo "============================================================"
echo "  Installation complete."
echo ""
echo "  Schedule: daily at noon (fires on next wake if asleep)"
echo "  Logs:     ~/claude_memory/logs/scout_YYYY-MM-DD.log"
echo ""
echo "  Manual run:"
echo "    python3 ~/claude_memory/scripts/research_scout.py"
echo ""
echo "  Dry run (no database writes):"
echo "    python3 ~/claude_memory/scripts/research_scout.py --dry-run"
echo "============================================================"
echo ""

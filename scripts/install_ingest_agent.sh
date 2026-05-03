#!/bin/bash
# install_ingest_agent.sh
# -----------------------
# One-command installer for the Ingest Agent.
#
# The Ingest Agent runs daily at 11am, finds all scout_results marked
# "interesting", extracts concepts via process_research.py, and indexes
# them via embed_memories.py. Requires Ollama to be running at job time.
#
# USAGE
#   chmod +x ~/claude_memory/scripts/install_ingest_agent.sh
#   ~/claude_memory/scripts/install_ingest_agent.sh

set -e

PLIST_NAME="com.ember-engine.ingest-agent"
PLIST_SRC="$HOME/claude_memory/daemons/$PLIST_NAME.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_NAME.plist"
DB="$HOME/claude_memory/memory.db"
AGENT="$HOME/claude_memory/scripts/ingest_agent.py"
PROCESS="$HOME/claude_memory/scripts/process_research.py"
EMBED="$HOME/claude_memory/scripts/embed_memories.py"

echo ""
echo "============================================================"
echo "  Ingest Agent -- Installer"
echo "============================================================"
echo ""

# ── Step 1: Verify prerequisites ─────────────────────────────────────────────
echo "[1/4] Verifying prerequisites..."

if [ ! -f "$DB" ]; then
    echo "  ERROR: Database not found at $DB"
    echo "  Run setup_db.py first."
    exit 1
fi
echo "  database:           OK"

if [ ! -f "$AGENT" ]; then
    echo "  ERROR: ingest_agent.py not found at $AGENT"
    exit 1
fi
echo "  ingest_agent.py:    OK"

if [ ! -f "$PROCESS" ]; then
    echo "  WARNING: process_research.py not found at $PROCESS"
    echo "  Ingest will fail at runtime. Continuing install anyway."
else
    echo "  process_research.py: OK"
fi

if [ ! -f "$EMBED" ]; then
    echo "  WARNING: embed_memories.py not found at $EMBED"
    echo "  Embedding step will be skipped at runtime. Continuing install anyway."
else
    echo "  embed_memories.py:  OK"
fi
echo ""

# ── Step 2: Dry run ──────────────────────────────────────────────────────────
echo "[2/4] Running dry-run check..."
python3 "$AGENT" --dry-run --no-jitter
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
echo "  Schedule: daily at 11am (fires on next wake if asleep)"
echo "  Note:     If machine is OFF at 11am, the window is missed."
echo "            Use the manual command to process immediately."
echo "  Logs:     ~/claude_memory/logs/ingest_agent_YYYY-MM-DD.log"
echo ""
echo "  Manual run (immediate, no delay):"
echo "    python3 ~/claude_memory/scripts/ingest_agent.py --no-jitter"
echo ""
echo "  Dry run (no database writes, no Ollama calls):"
echo "    python3 ~/claude_memory/scripts/ingest_agent.py --dry-run --no-jitter"
echo ""
echo "  Requires: Ollama running with qwen2.5:14b and nomic-embed-text"
echo "============================================================"
echo ""

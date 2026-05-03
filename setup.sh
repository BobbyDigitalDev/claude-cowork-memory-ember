#!/bin/bash
# setup.sh
# --------
# One-command installer for ember-engine.
#
# Run this once from inside the cloned repo directory:
#   chmod +x setup.sh
#   ./setup.sh
#
# What this does:
#   1. Checks prerequisites (Python 3.10+, Ollama)
#   2. Asks for your name (used for transcript filenames)
#   3. Creates all required directories
#   4. Installs Python dependencies
#   5. Initializes the database
#   6. Pulls required Ollama models (nomic-embed-text, qwen2.5:14b)
#   7. Writes your config
#   8. Generates your first START_HERE.md session prompt
#
# After this runs, open CoWork and paste START_HERE.md to begin.

set -e

INSTALL_DIR="$HOME/claude_memory"
SCRIPTS_DIR="$INSTALL_DIR/scripts"

echo ""
echo "============================================================"
echo "  ember-engine — Setup"
echo "============================================================"
echo ""

# ── Step 1: Check Python ─────────────────────────────────────────────────────
echo "[1/8] Checking Python..."
if ! command -v python3 &>/dev/null; then
    echo "  ERROR: python3 not found."
    echo "  Install Python 3.10+ from https://python.org or via Homebrew:"
    echo "    brew install python"
    exit 1
fi

PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    echo "  ERROR: Python 3.10+ required. Found: $PY_VERSION"
    exit 1
fi
echo "  Python $PY_VERSION — OK"
echo ""

# ── Step 2: Check Ollama ─────────────────────────────────────────────────────
echo "[2/8] Checking Ollama..."
if ! command -v ollama &>/dev/null; then
    echo "  ERROR: Ollama not found."
    echo "  Install from https://ollama.com — then re-run this script."
    exit 1
fi

if ! curl -sf http://localhost:11434/api/tags &>/dev/null; then
    echo "  WARNING: Ollama is installed but not running."
    echo "  Start it with: ollama serve"
    echo "  Model pulls (steps 6-7) will be skipped — run them manually later:"
    echo "    ollama pull nomic-embed-text"
    echo "    ollama pull qwen2.5:14b"
    OLLAMA_RUNNING=false
else
    echo "  Ollama is running — OK"
    OLLAMA_RUNNING=true
fi
echo ""

# ── Step 3: Get username ─────────────────────────────────────────────────────
echo "[3/8] Setting up your identity..."
echo "  Your name is used for transcript filenames (e.g. alice_2026_05_01_001.md)."
echo ""
read -rp "  Enter your first name (lowercase, no spaces): " RAW_USERNAME
USERNAME=$(echo "$RAW_USERNAME" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_')

if [ -z "$USERNAME" ]; then
    echo "  ERROR: Username cannot be empty."
    exit 1
fi
echo "  Username: $USERNAME"
echo ""

# ── Step 4: Create directories ───────────────────────────────────────────────
echo "[4/8] Creating directory structure..."

mkdir -p "$INSTALL_DIR/conversations"
mkdir -p "$INSTALL_DIR/logs"
mkdir -p "$INSTALL_DIR/cache"
mkdir -p "$INSTALL_DIR/research/transcripts"
mkdir -p "$INSTALL_DIR/personal"
mkdir -p "$INSTALL_DIR/docs"
mkdir -p "$INSTALL_DIR/debug"

echo "  $INSTALL_DIR/ — OK"
echo ""

# ── Step 5: Install Python dependencies ──────────────────────────────────────
echo "[5/8] Installing Python dependencies..."
REQUIREMENTS="$INSTALL_DIR/requirements.txt"

if [ ! -f "$REQUIREMENTS" ]; then
    echo "  ERROR: requirements.txt not found at $REQUIREMENTS"
    echo "  Make sure you're running this from inside the cloned repo."
    exit 1
fi

python3 -m pip install -r "$REQUIREMENTS" --break-system-packages -q
echo "  Dependencies installed — OK"
echo ""

# ── Step 6: Initialize database ──────────────────────────────────────────────
echo "[6/8] Initializing database..."
DB="$INSTALL_DIR/memory.db"

if [ -f "$DB" ]; then
    echo "  Database already exists at $DB — skipping."
    echo "  (To rebuild from scratch: rm $DB && ./setup.sh)"
else
    python3 "$SCRIPTS_DIR/setup_db.py"
    echo "  Database created — OK"
fi
echo ""

# ── Step 7: Pull Ollama models ────────────────────────────────────────────────
echo "[7/8] Pulling Ollama models..."

if [ "$OLLAMA_RUNNING" = true ]; then
    echo "  Pulling nomic-embed-text (semantic embeddings — fast, ~274MB)..."
    ollama pull nomic-embed-text

    echo ""
    echo "  Pulling qwen2.5:14b (extraction and reasoning — ~9GB)..."
    echo "  This may take several minutes on first install."
    ollama pull qwen2.5:14b

    echo ""
    echo "  Pulling deepseek-r1:14b (belief verification and reasoning — ~9GB)..."
    echo "  Required for verify_beliefs.py validator pass."
    ollama pull deepseek-r1:14b

    echo "  Models ready — OK"
else
    echo "  SKIPPED — Ollama not running."
    echo "  Pull models manually when Ollama is running:"
    echo "    ollama pull nomic-embed-text"
    echo "    ollama pull qwen2.5:14b"
    echo "    ollama pull deepseek-r1:14b"
fi
echo ""

# ── Step 8: Write config and generate session prompt ────────────────────────
echo "[8/8] Writing config and generating session prompt..."

# Write config file
CONFIG="$INSTALL_DIR/.ember_config"
cat > "$CONFIG" << CONF
# ember-engine configuration
# Generated by setup.sh — edit to customize

USERNAME=$USERNAME
INSTALL_DIR=$INSTALL_DIR

# Optional: used as a courtesy identifier in OpenAlex API requests.
# Set this to your email address to follow OpenAlex polite-pool guidelines.
# EMAIL=you@example.com
CONF
echo "  Config written to $CONFIG"

# Generate START_HERE.md and ember_engine_context.md
python3 "$SCRIPTS_DIR/generate_session_prompt.py" --no-jitter 2>/dev/null \
    || python3 "$SCRIPTS_DIR/generate_session_prompt.py" 2>/dev/null \
    || echo "  Note: Could not generate session prompt (Ollama may be offline). Run manually: python3 ~/claude_memory/scripts/generate_session_prompt.py"

echo ""
echo "============================================================"
echo "  Setup complete."
echo ""
echo "  Next steps:"
echo ""
echo "  1. Install background agents (run each in order):"
echo "       chmod +x ~/claude_memory/scripts/install_*.sh"
echo ""
echo "       # Session context (runs on schedule, builds memory snapshot)"
echo "       ~/claude_memory/scripts/install_context_agent.sh"
echo ""
echo "       # Research Scout (nightly — finds relevant external content)"
echo "       ~/claude_memory/scripts/install_research_scout.sh"
echo ""
echo "       # Ingest Agent (daily — ingests scout results into memory)"
echo "       ~/claude_memory/scripts/install_ingest_agent.sh"
echo ""
echo "       # Belief Verification (nightly — challenges beliefs against evidence)"
echo "       ~/claude_memory/scripts/install_verify_beliefs.sh"
echo ""
echo "       # Reflection Agent (weekly — synthesizes sessions into insights)"
echo "       ~/claude_memory/scripts/install_reflection_agent.sh"
echo ""
echo "       # Memory Curator (weekly — deduplicates and prunes the memory graph)"
echo "       ~/claude_memory/scripts/install_memory_curator.sh"
echo ""
echo "       # Auto-Ingest (watches conversations/ and triggers ingest on session end)"
echo "       ~/claude_memory/scripts/install_auto_ingest.sh"
echo ""
echo "       # Backup Agent (every 6 hours — backs up memory.db)"
echo "       ~/claude_memory/scripts/install_backup_agent.sh"
echo ""
echo "  2. Open Claude CoWork and start a new chat."
echo ""
echo "  3. Open ~/claude_memory/START_HERE.md and paste the"
echo "     contents into your CoWork session to begin."
echo ""
echo "  4. At the end of each session, run:"
echo "       python3 ~/claude_memory/scripts/ingest.py"
echo ""
echo "  Need help? See README.md"
echo "============================================================"
echo ""

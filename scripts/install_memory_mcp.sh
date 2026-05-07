#!/usr/bin/env bash
# install_memory_mcp.sh
# ---------------------
# Registers the EMBER memory MCP server with Claude Code / Cowork.
#
# Run once from any directory:
#   bash ~/claude_memory/scripts/install_memory_mcp.sh
#
# Then restart Cowork / Claude Code to activate. The query_memory tool
# will appear in Claude's tool list automatically on next session open.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MCP_SCRIPT="$SCRIPT_DIR/memory_mcp_server.py"
CLAUDE_CONFIG="$HOME/.claude.json"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " EMBER Memory MCP Server — Install"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ── 1. Verify the server script exists ────────────────────────────────────────
if [[ ! -f "$MCP_SCRIPT" ]]; then
    echo "✗ Server script not found: $MCP_SCRIPT"
    echo "  Make sure you're running this from inside ~/claude_memory/"
    exit 1
fi
echo "✓ Server script found: $MCP_SCRIPT"

# ── 2. Install the mcp package if needed ──────────────────────────────────────
echo "→ Checking mcp package..."
if python3 -c "import mcp.server.fastmcp" 2>/dev/null; then
    echo "  ✓ mcp (FastMCP) already installed"
else
    echo "  Installing mcp>=1.0..."
    python3 -m pip install "mcp>=1.0" --break-system-packages --quiet 2>/dev/null \
        || {
            echo "  ✗ pip install failed."
            echo "    Try manually: python3 -m pip install 'mcp>=1.0' --break-system-packages"
            exit 1
        }
    # Verify install succeeded
    python3 -c "import mcp.server.fastmcp" 2>/dev/null || {
        echo "  ✗ mcp installed but FastMCP not importable — check mcp version"
        exit 1
    }
    echo "  ✓ mcp installed"
fi

# ── 3. Verify retrieve.py is importable (sanity check) ────────────────────────
echo "→ Checking retrieve.py..."
python3 -c "
import sys
sys.path.insert(0, '${SCRIPT_DIR}')
import retrieve
print('  ✓ retrieve.py importable')
" || {
    echo "  ✗ retrieve.py not importable — check scripts/ directory"
    exit 1
}

# ── 4. Register with Claude config ────────────────────────────────────────────
echo "→ Registering with Claude config ($CLAUDE_CONFIG)..."

python3 - <<PYEOF
import json
import os
import sys

config_path = os.path.expanduser("~/.claude.json")
script_path = "$MCP_SCRIPT"

# Load existing config or start fresh
if os.path.exists(config_path):
    try:
        with open(config_path) as f:
            config = json.load(f)
    except json.JSONDecodeError as e:
        print(f"  ✗ ~/.claude.json exists but contains invalid JSON: {e}")
        print("    Fix the file manually and re-run, or delete it to start fresh.")
        print("    Aborting — no changes made to avoid destroying existing MCP registrations.")
        sys.exit(1)
    except IOError as e:
        print(f"  ✗ Could not read ~/.claude.json: {e}")
        sys.exit(1)
else:
    config = {}

# Add or overwrite the ember-memory entry
config.setdefault("mcpServers", {})
already = "ember-memory" in config["mcpServers"]
config["mcpServers"]["ember-memory"] = {
    "command": "python3",
    "args": [script_path],
}

with open(config_path, "w") as f:
    json.dump(config, f, indent=2)

action = "updated" if already else "registered"
print(f"  ✓ ember-memory {action} in {config_path}")
print(f"    command: python3 {script_path}")
PYEOF

# ── 5. Quick smoke test ────────────────────────────────────────────────────────
echo "→ Running smoke test (import only, no DB required)..."
python3 -c "
import sys
sys.path.insert(0, '${SCRIPT_DIR}')
import memory_mcp_server
print('  ✓ memory_mcp_server imports cleanly')
" || {
    echo "  ✗ Smoke test failed — check memory_mcp_server.py"
    exit 1
}

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " Install complete."
echo ""
echo " Next step: restart Cowork / Claude Code."
echo ""
echo " After restart, Claude will call query_memory()"
echo " automatically during sessions when recall is"
echo " needed — no manual terminal steps required."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

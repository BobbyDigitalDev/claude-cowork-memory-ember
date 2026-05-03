#!/usr/bin/env python3
"""
memory_mcp_server.py
--------------------
MCP server for the EMBER Engine. Exposes the persistent memory database
to Claude as a callable tool during live Cowork sessions, eliminating
the need to manually paste query results.

Claude calls query_memory() directly whenever it needs to recall something
from prior sessions that isn't in the current context window — beliefs,
epiphanies, goals, open questions, concepts, patterns, entities.

Install (run once):
    bash ~/claude_memory/scripts/install_memory_mcp.sh

Then restart Cowork / Claude Code to activate. The tool will appear in
Claude's tool list automatically on next session open.

Usage during sessions:
    Claude will call this tool autonomously when retrieval is needed.
    No manual steps required.
"""

import sys
import os

# Add scripts/ directory to path so retrieve.py is importable
# regardless of where the MCP host spawns this process from.
_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from mcp.server.fastmcp import FastMCP
from retrieve import retrieve as _retrieve

# ── Server definition ──────────────────────────────────────────────────────────

mcp = FastMCP(
    name="EMBER Memory",
    instructions=(
        "Use query_memory when the user references something from prior sessions, "
        "when you notice a topic that likely has stored beliefs or context, "
        "when you need to verify what was previously decided, or when the user "
        "asks 'what do we think about X', 'what are our goals for Y', "
        "'do we have anything on Z'. Don't wait to be asked — retrieve proactively "
        "when the conversation touches a topic that memory might enrich."
    ),
)


# ── Tools ──────────────────────────────────────────────────────────────────────

@mcp.tool()
def query_memory(
    query: str,
    top: int = 10,
    strategies: str = "semantic,structural,temporal",
    threshold: float = 0.45,
    days: int = 30,
) -> str:
    """
    Retrieve stored memories from the EMBER Engine database.

    Call this during a session to recall beliefs, epiphanies, goals, open
    questions, concepts, entities, or patterns from prior sessions. Results
    are semantically ranked and filtered by epistemic state — deprecated or
    disputed beliefs are suppressed automatically.

    Args:
        query:      Natural language description of what to recall.
                    Good examples:
                      "what do we believe about substrate independence"
                      "current goals for the ember project"
                      "open questions about mid-session retrieval"
                      "epiphanies about the push vs pull model"
                      "who is Bobby and what does he work on"
        top:        Max results to return (default 10).
        strategies: Comma-separated retrieval strategies to use.
                    Options: semantic, structural, temporal
                    Default: all three. Use "semantic" alone for pure
                    concept matching; "temporal" alone for recent items.
        threshold:  Min cosine similarity for semantic results (default 0.45).
                    Lower to surface more results; raise for stricter matching.
        days:       Temporal lookback window in days (default 30).

    Returns:
        Formatted markdown block with matching memories, scores, status badges,
        and retrieval provenance. Ready for direct use in session context.
    """
    strat_list = [s.strip() for s in strategies.split(",") if s.strip()]

    # Validate strategies
    valid = {"semantic", "structural", "temporal"}
    strat_list = [s for s in strat_list if s in valid] or ["semantic", "structural", "temporal"]

    try:
        result = _retrieve(
            query=query,
            strategies=strat_list,
            top=top,
            threshold=threshold,
            days=days,
        )
        return result["context_block"]
    except Exception as exc:
        return (
            f"Memory retrieval error: {exc}\n\n"
            "If Ollama is offline, try: strategies='structural,temporal' "
            "to retrieve without semantic embedding."
        )


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()

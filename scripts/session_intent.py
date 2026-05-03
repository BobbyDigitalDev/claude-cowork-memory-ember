#!/usr/bin/env python3
"""
session_intent.py — Goal 81
----------------------------
Session intent declaration + knowledge gap detection + preflight research pipeline.

The problem this solves:
    Without explicit intent, the session bootstrap (refresh_deep_memory.py) derives
    seeds from recent questions, pending goals, and top beliefs. Those seeds reflect
    project momentum, not what you actually want to work on today. Every session
    retrieves from the same domain unless you redirect it.

The solution:
    1. Declare intent: say what you want to work on this session (one sentence).
    2. Extract topics: parse the intent into 2-5 seed phrases.
    3. Density check: run retrieve() against each topic. Count results above
       threshold to measure how well the memory system covers this area.
    4. Gap report: classify each topic as DENSE / PARTIAL / SPARSE.
    5. Research suggestions: for SPARSE topics, suggest specific search queries
       and transcript fetch commands.
    6. Write current_intent.txt so refresh_deep_memory.py --intent-file can use
       the intent topics as bootstrap seeds for this session.
    7. (optional) --refresh: immediately regenerates deep_memory.md with intent seeds.

Usage (CLI):
    python3 ~/claude_memory/scripts/session_intent.py "Build setup.sh and write README"
    python3 ~/claude_memory/scripts/session_intent.py "Goal 81 implementation" --refresh
    python3 ~/claude_memory/scripts/session_intent.py "OpenClaw integration" --no-semantic
    python3 ~/claude_memory/scripts/session_intent.py "anything" --top 15 --threshold 0.60

Usage (library):
    from session_intent import declare_intent
    result = declare_intent("Build the installer")
    print(result["gaps"])
    print(result["suggestions"])
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta

# ── Configuration ──────────────────────────────────────────────────────────────

_BASE           = os.path.expanduser("~/claude_memory")
_SCRIPTS        = os.path.join(_BASE, "scripts")
INTENT_FILE     = os.path.join(_BASE, "current_intent.txt")
REFRESH_SCRIPT  = os.path.join(_SCRIPTS, "refresh_deep_memory.py")

# Density thresholds (number of results, not cosine score)
DENSE_MIN_RESULTS   = 5   # 5+ results → DENSE, well-covered
PARTIAL_MIN_RESULTS = 2   # 2-4 results → PARTIAL, some coverage
# 0-1 results → SPARSE, knowledge gap

# Retrieval score thresholds passed to retrieve()
DENSE_THRESHOLD   = 0.60
PARTIAL_THRESHOLD = 0.50
SPARSE_THRESHOLD  = 0.40

# How many retrieve() results to request per topic
_TOP_PER_TOPIC = 10

# Staleness cutoff for current_intent.txt
_DEFAULT_MAX_AGE_HOURS = 24

# Words stripped from the start of topic fragments (action verbs in imperatives)
_LEADING_VERBS = {
    "build", "write", "create", "implement", "design", "fix", "add", "make",
    "develop", "update", "refactor", "run", "test", "check", "review",
    "generate", "produce", "set", "define", "establish", "improve", "migrate",
    "deploy", "install", "configure", "document", "plan", "draft", "finish",
    "complete", "ship", "release",
}

# Patterns to split intent on when extracting topics
_SPLIT_PATTERNS = [" and ", " or ", " also ", " + ", "; ", ", "]


# ── Topic extraction ──────────────────────────────────────────────────────────

def extract_topics(intent_text: str, max_topics: int = 5) -> list:
    """Extract 2-5 topic seed phrases from a natural language intent statement.

    Strategy:
      1. Split on common conjunctions and punctuation.
      2. Strip leading action verbs from each fragment.
      3. Drop fragments that are too short to be meaningful.
      4. If splitting produced only one fragment (or nothing useful), return the
         original intent text as the single seed.
      5. Cap at max_topics.

    Parameters
    ----------
    intent_text : str  — natural language intent (e.g. "Build setup.sh and write README")
    max_topics  : int  — maximum number of topics to return

    Returns
    -------
    list of str — topic seed phrases, each stripped of whitespace
    """
    if not intent_text or not intent_text.strip():
        return []

    text = intent_text.strip()

    # Split on conjunction/separator patterns (case-insensitive)
    fragments = [text]
    for pattern in _SPLIT_PATTERNS:
        new_fragments = []
        for frag in fragments:
            parts = re.split(re.escape(pattern), frag, flags=re.IGNORECASE)
            new_fragments.extend(parts)
        fragments = new_fragments

    # Clean each fragment
    cleaned = []
    for frag in fragments:
        frag = frag.strip()
        if not frag:
            continue
        # Strip leading action verb
        words = frag.split()
        if words and words[0].lower() in _LEADING_VERBS:
            frag = " ".join(words[1:]).strip()
        if len(frag) >= 3:
            cleaned.append(frag)

    # If splitting gave us multiple useful fragments, use them
    if len(cleaned) >= 2:
        return cleaned[:max_topics]

    # Single fragment — return the cleaned version (verb-stripped) if we got one,
    # otherwise fall back to the raw intent text
    if cleaned:
        return [cleaned[0]]

    return [text[:200]]  # cap length for safety


# ── Density scoring ────────────────────────────────────────────────────────────

def classify_density(results: list) -> str:
    """Classify retrieval density as DENSE, PARTIAL, or SPARSE.

    Parameters
    ----------
    results : list of result dicts (each has at minimum a "score" key)

    Returns
    -------
    str: "DENSE", "PARTIAL", or "SPARSE"
    """
    n = len(results)
    if n >= DENSE_MIN_RESULTS:
        return "DENSE"
    if n >= PARTIAL_MIN_RESULTS:
        return "PARTIAL"
    return "SPARSE"


def _retrieve_for_topic(topic: str, top: int, threshold: float,
                        no_semantic: bool, db_path=None) -> dict:
    """Call retrieve() for a single topic. Returns the full bundle."""
    # Import here to allow mocking in tests
    strategies = ["structural"]
    if not no_semantic:
        strategies = ["semantic", "structural"]  # temporal adds noise for intent checking

    return retrieve(
        topic,
        strategies=strategies,
        top=top,
        threshold=threshold,
        db_path=db_path,
    )


# ── Gap detection ──────────────────────────────────────────────────────────────

def detect_gaps(topics: list, top: int = _TOP_PER_TOPIC,
                threshold: float = PARTIAL_THRESHOLD,
                no_semantic: bool = False,
                db_path: str = None) -> list:
    """Run density analysis for each topic.

    Parameters
    ----------
    topics      : list of str  — topic seed phrases (from extract_topics)
    top         : int          — max results to request from retrieve()
    threshold   : float        — minimum score for retrieve() (default: PARTIAL_THRESHOLD)
    no_semantic : bool         — skip semantic strategy (use structural only)
    db_path     : str          — override database path

    Returns
    -------
    list of dicts, one per topic:
        topic     : str
        density   : "DENSE" | "PARTIAL" | "SPARSE"
        n_results : int
        top_score : float
        results   : list (top 3 results for display)
    """
    gaps = []
    for topic in topics:
        bundle = _retrieve_for_topic(topic, top, threshold, no_semantic, db_path)
        results = bundle.get("results", [])
        top_score = max((r.get("score", 0) for r in results), default=0.0)
        gaps.append({
            "topic":     topic,
            "density":   classify_density(results),
            "n_results": len(results),
            "top_score": top_score,
            "results":   results[:3],
        })
    return gaps


# ── Research suggestions ───────────────────────────────────────────────────────

def suggest_research(sparse_topics: list) -> list:
    """Generate research action suggestions for sparse topics.

    Returns a list of human-readable suggestion strings including YouTube search
    queries and fetch_youtube_transcript.py usage hints.

    Parameters
    ----------
    sparse_topics : list of str — topic names that returned SPARSE density

    Returns
    -------
    list of str — suggestion strings
    """
    if not sparse_topics:
        return []

    suggestions = []
    for topic in sparse_topics:
        suggestions.append(
            f'YouTube search: "{topic} tutorial OR overview OR explained"'
        )
        suggestions.append(
            f"Fetch transcript: python3 ~/claude_memory/scripts/fetch_youtube_transcript.py <URL>"
            f"  # then: process_research.py <file> && embed_memories.py"
        )

    # Generic web research hint
    if sparse_topics:
        combined = " + ".join(sparse_topics[:3])
        suggestions.append(
            f'Web search for prior art: "{combined} best practices"'
        )

    return suggestions


# ── Intent file ────────────────────────────────────────────────────────────────

def write_intent_file(intent_text: str, topics: list,
                      path: str = None) -> str:
    """Write intent declaration to current_intent.txt.

    Parameters
    ----------
    intent_text : str  — raw intent string from user
    topics      : list — extracted topic seeds
    path        : str  — override file path (default: INTENT_FILE)

    Returns
    -------
    str — the path where the file was written
    """
    dest = path or INTENT_FILE
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    data = {
        "intent":     intent_text,
        "topics":     topics,
        "written_at": datetime.utcnow().isoformat(),
    }
    with open(dest, "w") as f:
        json.dump(data, f, indent=2)
    return dest


def read_intent_file(path: str = None) -> dict:
    """Read current_intent.txt.

    Returns
    -------
    dict with keys intent, topics, written_at — or None if file does not exist.
    """
    src = path or INTENT_FILE
    if not os.path.exists(src):
        return None
    try:
        with open(src) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


def is_intent_file_stale(path: str = None, max_age_hours: int = _DEFAULT_MAX_AGE_HOURS) -> bool:
    """Return True if the intent file is missing or older than max_age_hours."""
    data = read_intent_file(path=path)
    if data is None:
        return True
    try:
        written_at = datetime.fromisoformat(data["written_at"])
        age = datetime.utcnow() - written_at
        return age > timedelta(hours=max_age_hours)
    except (KeyError, ValueError):
        return True


# ── Report formatting ──────────────────────────────────────────────────────────

_DENSITY_ICON = {
    "DENSE":   "●●●",
    "PARTIAL": "●●○",
    "SPARSE":  "●○○",
}

_DENSITY_LABEL = {
    "DENSE":   "well-covered",
    "PARTIAL": "partial coverage",
    "SPARSE":  "knowledge gap",
}


def format_gap_report(intent_text: str, topics: list, gaps: list,
                      suggestions: list) -> str:
    """Format the gap analysis as a human-readable console report.

    Parameters
    ----------
    intent_text : str
    topics      : list of str
    gaps        : list of gap dicts (from detect_gaps)
    suggestions : list of str (from suggest_research)

    Returns
    -------
    str — formatted report
    """
    lines = []
    lines.append("=" * 62)
    lines.append("  Session Intent Declaration")
    lines.append("=" * 62)
    lines.append(f"  Intent : {intent_text}")
    lines.append(f"  Topics : {', '.join(topics)}")
    lines.append("")

    lines.append("  Knowledge Density Check:")
    lines.append("  " + "-" * 58)

    sparse_topics = []
    for gap in gaps:
        icon  = _DENSITY_ICON[gap["density"]]
        label = _DENSITY_LABEL[gap["density"]]
        score_str = f"{gap['top_score']:.2f}" if gap["top_score"] > 0 else " n/a"
        lines.append(
            f"  {icon}  {gap['topic']:<30}  {gap['n_results']:>3} results  "
            f"top {score_str}  [{label}]"
        )
        if gap["density"] == "SPARSE":
            sparse_topics.append(gap["topic"])

    lines.append("")

    if not sparse_topics:
        lines.append("  All topics covered. No preflight research needed.")
    else:
        lines.append(f"  SPARSE topics ({len(sparse_topics)}): {', '.join(sparse_topics)}")
        lines.append("")
        lines.append("  Preflight Research Suggestions:")
        lines.append("  " + "-" * 58)
        for s in suggestions:
            lines.append(f"  {s}")

    lines.append("=" * 62)
    return "\n".join(lines)


# ── Main entry point ───────────────────────────────────────────────────────────

def declare_intent(intent_text: str, refresh: bool = False,
                   no_semantic: bool = False, top: int = _TOP_PER_TOPIC,
                   threshold: float = PARTIAL_THRESHOLD,
                   db_path: str = None,
                   intent_file_path: str = None) -> dict:
    """Declare session intent, detect knowledge gaps, suggest preflight research.

    Parameters
    ----------
    intent_text      : str  — natural language description of session goal
    refresh          : bool — if True, regenerate deep_memory.md with intent seeds
    no_semantic      : bool — skip semantic strategy (structural only)
    top              : int  — max results per topic from retrieve()
    threshold        : float — minimum retrieval score
    db_path          : str  — override database path
    intent_file_path : str  — override current_intent.txt path

    Returns
    -------
    dict:
        intent       : str
        topics       : list[str]
        gaps         : list[dict]
        sparse_count : int
        suggestions  : list[str]
        report       : str (formatted printable report)
        intent_file  : str (path written)
    """
    topics = extract_topics(intent_text)
    gaps   = detect_gaps(topics, top=top, threshold=threshold,
                         no_semantic=no_semantic, db_path=db_path)

    sparse_topics = [g["topic"] for g in gaps if g["density"] == "SPARSE"]
    suggestions   = suggest_research(sparse_topics)
    report        = format_gap_report(intent_text, topics, gaps, suggestions)

    # Write intent file so refresh_deep_memory.py --intent-file can use it
    intent_path = write_intent_file(intent_text, topics, path=intent_file_path)

    # Optionally regenerate bootstrap context with intent seeds
    if refresh:
        _refresh_bootstrap(topics)

    return {
        "intent":       intent_text,
        "topics":       topics,
        "gaps":         gaps,
        "sparse_count": len(sparse_topics),
        "suggestions":  suggestions,
        "report":       report,
        "intent_file":  intent_path,
    }


def _refresh_bootstrap(topics: list):
    """Run refresh_deep_memory.py --seeds <topics> as a subprocess."""
    python = sys.executable
    cmd = [python, REFRESH_SCRIPT, "--seeds"] + topics
    print(f"\n  Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=False)
    if result.returncode != 0:
        print(f"  WARNING: refresh_deep_memory.py exited with code {result.returncode}")


# ── Lazy import of retrieve (allows mocking in tests) ─────────────────────────

def retrieve(query, strategies=None, top=10, threshold=0.45,
             days=30, db_path=None):
    """Thin wrapper around retrieve.retrieve() supporting the same call signature."""
    # Import lazily so tests can mock this function at the module level
    sys.path.insert(0, _SCRIPTS)
    import retrieve as _retrieve_module
    return _retrieve_module.retrieve(
        query,
        strategies=strategies,
        top=top,
        threshold=threshold,
        days=days,
        db_path=db_path,
    )


# ── CLI ────────────────────────────────────────────────────────────────────────

def parse_args(argv=None):
    """Parse CLI arguments. Separated from _main() for testability."""
    parser = argparse.ArgumentParser(
        description=(
            "Session intent declaration + knowledge gap detection.\n"
            "Declare what you want to work on this session. The script checks\n"
            "how well the memory system covers each topic and suggests preflight\n"
            "research for sparse areas."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "intent",
        help='Natural language description of session goal. '
             'Example: "Build setup.sh and write README"',
    )
    parser.add_argument(
        "--refresh", action="store_true", default=False,
        help="Regenerate deep_memory.md using intent topics as seeds "
             "(calls refresh_deep_memory.py --seeds ...)",
    )
    parser.add_argument(
        "--no-semantic", action="store_true", default=False,
        help="Skip semantic (embedding) strategy. Use structural search only. "
             "Faster, no Ollama required.",
    )
    parser.add_argument(
        "--top", type=int, default=_TOP_PER_TOPIC, metavar="N",
        help=f"Max results per topic from retrieve() (default: {_TOP_PER_TOPIC})",
    )
    parser.add_argument(
        "--threshold", type=float, default=PARTIAL_THRESHOLD, metavar="F",
        help=f"Minimum retrieval score (default: {PARTIAL_THRESHOLD})",
    )
    parser.add_argument(
        "--db", metavar="PATH", default=None,
        help="Override database path (default: ~/claude_memory/memory.db)",
    )
    return parser.parse_args(argv)


def _main():
    args = parse_args()

    result = declare_intent(
        intent_text=args.intent,
        refresh=args.refresh,
        no_semantic=args.no_semantic,
        top=args.top,
        threshold=args.threshold,
        db_path=args.db,
    )

    print(result["report"])

    if result["intent_file"]:
        print(f"\n  Intent written to: {result['intent_file']}")
        print(f"  Run: python3 {REFRESH_SCRIPT} --intent-file")
        print(f"       to regenerate deep_memory.md with intent seeds.\n")


if __name__ == "__main__":
    _main()

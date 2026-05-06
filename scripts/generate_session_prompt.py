#!/usr/bin/env python3
"""
generate_session_prompt.py
--------------------------
Generates two files every night:

  START_HERE.md       — Human-facing session briefing. Bobby opens this,
                        reads the pre-session digest, and copies the paste block.

  ember_engine_context.md   — Machine-facing combined context for Claude. Contains
                        ember_engine_instructions.md + recent_memory.md + deep_memory.md
                        concatenated in load order. DO NOT EDIT MANUALLY — this
                        file is regenerated nightly and any edits will be lost.
                        Edit ember_engine_instructions.md to change standing instructions.
                        Listed in .gitignore (build artifact, not source).

WHY THIS EXISTS
---------------
The paste-to-open instruction block is the same every session. What changes
is the date, the transcript filename, and the memory state. This script
pre-generates that context nightly so the session opener is a single file read.

WHAT IT GENERATES
-----------------
  START_HERE.md:
    1. A ready-to-paste instruction block (one line: read ember_engine_context.md)
    2. A pre-session briefing (Bobby's eyes only):
         - Pending immediate goals
         - Memory state snapshot (chunks, beliefs, questions, goals)
         - Interesting scout results awaiting follow-up
         - Last curator run and what it found
         - Last bootstrap run info

  ember_engine_context.md:
    1. DO NOT EDIT header
    2. ember_engine_instructions.md content  (standing instructions, architecture, tone)
    3. recent_memory.md content        (current cognitive state)
    4. deep_memory.md content          (semantic scaffold)
    Includes staleness warning if any source file is older than 48 hours.

NO OLLAMA REQUIRED. Pure DB reads + date math + file reads. Runs in <1 second.

USAGE
-----
    python3 ~/claude_memory/scripts/generate_session_prompt.py
    python3 ~/claude_memory/scripts/generate_session_prompt.py --date 2026-05-01
    python3 ~/claude_memory/scripts/generate_session_prompt.py --stdout
    python3 ~/claude_memory/scripts/generate_session_prompt.py --output /path/to/START_HERE.md
    python3 ~/claude_memory/scripts/generate_session_prompt.py --skip-context   (START_HERE.md only)
"""

import sqlite3
import argparse
import sys
import os
from datetime import datetime, date, timedelta
from pathlib import Path

_BASE           = Path.home() / "claude_memory"
DB_PATH         = _BASE / "memory.db"
OUTPUT_PATH     = _BASE / "START_HERE.md"
CONTEXT_PATH    = _BASE / "ember_engine_context.md"
CONV_DIR        = _BASE / "conversations"

# Read USERNAME from .ember_config (written by setup.sh).
# Falls back to "user" if config is missing — run setup.sh to configure.
def _read_username() -> str:
    config = _BASE / ".ember_config"
    if config.exists():
        for line in config.read_text().splitlines():
            line = line.strip()
            if line.startswith("USERNAME=") and not line.startswith("#"):
                val = line.split("=", 1)[1].strip()
                if val:
                    return val
    return "user"

USERNAME = _read_username()

# Source files for ember_engine_context.md, in load order
CONTEXT_SOURCES = [
    (_BASE / "ember_engine_instructions.md",  "STANDING INSTRUCTIONS"),
    (_BASE / "recent_memory.md",        "RECENT MEMORY (current cognitive state)"),
    (_BASE / "deep_memory.md",          "DEEP MEMORY (semantic scaffold)"),
]
STALENESS_HOURS = 48

NOW = datetime.now()


# ── DB helpers ────────────────────────────────────────────────────────────────

def fetch(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


def fetchone(conn, sql, params=()):
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


# ── Data gathering ────────────────────────────────────────────────────────────

def gather_state(conn: sqlite3.Connection, session_date: date) -> dict:
    state = {}

    # Memory counts
    state["n_chunks"]   = fetchone(conn, "SELECT COUNT(*) FROM memory_chunks WHERE embedding_vector IS NOT NULL")
    state["n_beliefs"]  = fetchone(conn, "SELECT COUNT(*) FROM beliefs WHERE is_active=1")
    state["n_questions"]= fetchone(conn, "SELECT COUNT(*) FROM questions WHERE status='open'")
    state["n_goals_pending"] = fetchone(conn, "SELECT COUNT(*) FROM goals WHERE status='pending'")

    # Session count
    state["n_sessions"] = fetchone(conn, "SELECT COUNT(*) FROM sessions")

    # Last session date
    last_session = fetchone(conn, "SELECT date FROM sessions ORDER BY id DESC LIMIT 1")
    state["last_session_date"] = last_session or "unknown"

    # Pending immediate goals
    rows = fetch(conn, """
        SELECT id, description, category
        FROM goals
        WHERE status='pending' AND priority='immediate'
        ORDER BY id ASC
    """)
    state["immediate_goals"] = [dict(id=r[0], description=r[1], category=r[2]) for r in rows]

    # Pending near-term goals (top 5)
    rows = fetch(conn, """
        SELECT id, description, category
        FROM goals
        WHERE status='pending' AND priority='near-term'
        ORDER BY id ASC
        LIMIT 5
    """)
    state["near_term_goals"] = [dict(id=r[0], description=r[1], category=r[2]) for r in rows]

    # Interesting scout results not yet actioned
    try:
        rows = fetch(conn, """
            SELECT id, title, source_name, relevance_score, date_fetched
            FROM scout_results
            WHERE status='interesting'
            ORDER BY relevance_score DESC
            LIMIT 5
        """)
        state["interesting_scout"] = [
            dict(id=r[0], title=r[1], source=r[2], score=r[3], fetched=r[4])
            for r in rows
        ]
    except Exception:
        state["interesting_scout"] = []

    # Active high-severity tensions (top 5 by importance_score)
    try:
        rows = fetch(conn, """
            SELECT t.id, t.topic, t.description,
                   t.importance_score,
                   ba.topic AS topic_a, ba.position AS pos_a,
                   bb.topic AS topic_b, bb.position AS pos_b
            FROM tensions t
            LEFT JOIN beliefs ba ON ba.id = t.belief_a_id
            LEFT JOIN beliefs bb ON bb.id = t.belief_b_id
            WHERE t.is_active = 1
            ORDER BY t.importance_score DESC
            LIMIT 5
        """)
        state["active_tensions"] = [
            dict(
                id=r[0], topic=r[1], description=r[2],
                severity=r[3],
                topic_a=r[4], pos_a=r[5],
                topic_b=r[6], pos_b=r[7],
            )
            for r in rows
        ]
    except Exception:
        state["active_tensions"] = []

    # Last curator report summary (from curator_report.md if exists)
    curator_report = _BASE / "curator_report.md"
    if curator_report.exists():
        lines = curator_report.read_text().split("\n")
        first_line = lines[1] if len(lines) > 1 else (lines[0] if lines else "")
        state["curator_report_line"] = first_line.strip()
    else:
        state["curator_report_line"] = "No curator report found."

    # Last bootstrap run (from deep_memory.md header)
    bootstrap = _BASE / "deep_memory.md"
    if bootstrap.exists():
        first_lines = bootstrap.read_text().split("\n")[:3]
        state["bootstrap_info"] = " | ".join(l.strip() for l in first_lines if l.strip())
    else:
        state["bootstrap_info"] = "deep_memory.md not found."

    # Most recent mood (last session)
    try:
        mood_rows = fetch(conn, """
            SELECT tone, energy, notable_moments, bobby_state, claude_state, date
            FROM moods
            ORDER BY id DESC
            LIMIT 1
        """)
        if mood_rows:
            r = mood_rows[0]
            state["last_mood"] = dict(
                tone=r[0], energy=r[1], notable_moments=r[2],
                bobby_state=r[3], claude_state=r[4], date=r[5]
            )
        else:
            state["last_mood"] = None
    except Exception:
        state["last_mood"] = None

    # Mood-based session hint: suggest context emphasis based on tone/energy
    mood = state["last_mood"]
    if mood:
        tone   = (mood.get("tone")   or "").lower()
        energy = (mood.get("energy") or "").lower()
        if any(w in tone for w in ("exploratory", "curious", "philosophical", "open")):
            hint = "Philosophical session likely — load more open questions in bootstrap."
        elif any(w in tone for w in ("focused", "productive", "build", "execution")):
            hint = "Build session likely — prioritize immediate goals in bootstrap."
        elif any(w in tone for w in ("tired", "heavy", "low", "frustrated", "stuck")):
            hint = "Recovery session likely — surface wins and near-term momentum items."
        elif any(w in energy for w in ("high", "energized", "motivated")):
            hint = "High-energy session — good time for deep conceptual work."
        elif any(w in energy for w in ("low", "tired", "slow")):
            hint = "Low-energy session — favor structured tasks over open exploration."
        else:
            hint = "Neutral session tone — standard context allocation."
        state["mood_hint"] = hint
    else:
        state["mood_hint"] = "No mood data yet — standard context allocation."

    # Next transcript filename
    next_date_str = session_date.strftime("%Y_%m_%d")
    # Check for existing files from that date to determine the counter
    counter = 1
    if CONV_DIR.exists():
        existing = sorted(CONV_DIR.glob(f"{USERNAME}_{next_date_str}_*.md"))
        if existing:
            last = existing[-1].stem  # e.g. bobby_2026_04_25_001
            try:
                counter = int(last.split("_")[-1]) + 1
            except ValueError:
                counter = len(existing) + 1
    state["next_filename"] = f"{USERNAME}_{next_date_str}_{counter:03d}.md"
    state["session_date"]  = session_date

    return state


# ── Rendering ─────────────────────────────────────────────────────────────────

def render(state: dict, generated_at: datetime) -> str:
    session_date = state["session_date"]
    next_filename = state["next_filename"]

    lines = []
    lines.append(f"# Session Starter — {session_date.strftime('%Y-%m-%d')}")
    lines.append(f"Generated: {generated_at.strftime('%Y-%m-%d %H:%M')}  |  "
                 f"Session {(state['n_sessions'] or 0) + 1}  |  "
                 f"Last session: {state['last_session_date']}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Paste this to open your session")
    lines.append("")
    lines.append("```")
    lines.append("Please read ~/claude_memory/ember_engine_context.md before responding to anything.")
    lines.append("")
    lines.append("Follow the standing instructions in it exactly, "
                 f"starting with creating today's transcript file: {next_filename}")
    lines.append("")
    lines.append("TRANSCRIPT RULE: Every Bobby message and every Claude response must be written "
                 "into the transcript VERBATIM — exact words, full length, no summaries, no "
                 "paraphrasing. The extraction pipeline depends on full fidelity. Summaries "
                 "defeat the purpose of the system. Append after EVERY exchange — no batching, "
                 "no deferring. Bobby speaks + Claude responds = append immediately, before "
                 "doing anything else.")
    lines.append("```")
    lines.append("")
    lines.append("_Fallback if ember_engine_context.md is missing or stale: read ember_engine_instructions.md, "
                 "recent_memory.md, and deep_memory.md in that order instead._")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Pre-session briefing")
    lines.append("*(For your eyes — not part of the paste above)*")
    lines.append("")

    # Immediate goals
    lines.append("### Immediate goals")
    if state["immediate_goals"]:
        for g in state["immediate_goals"]:
            lines.append(f"- **id={g['id']}** [{g['category']}] {g['description']}")
    else:
        lines.append("_None pending._")
    lines.append("")

    # Near-term goals preview
    lines.append("### Near-term (top 5)")
    if state["near_term_goals"]:
        for g in state["near_term_goals"]:
            lines.append(f"- id={g['id']} {g['description'][:90]}")
    else:
        lines.append("_None pending._")
    lines.append("")

    # Memory state
    lines.append("### Memory state")
    lines.append(f"- Embedded chunks:  {state['n_chunks']}")
    lines.append(f"- Active beliefs:   {state['n_beliefs']}")
    lines.append(f"- Open questions:   {state['n_questions']}")
    lines.append(f"- Pending goals:    {state['n_goals_pending']}")
    lines.append("")

    # Active tensions
    lines.append("### Active belief tensions")
    tensions = state.get("active_tensions", [])
    if tensions:
        for t in tensions:
            severity = t.get("severity") or 0.0
            sev_str  = f"{severity:.2f}" if severity else "?"
            topic    = t.get("topic") or "Unnamed tension"
            desc     = (t.get("description") or "")[:120]
            ta       = (t.get("topic_a") or "belief A")
            tb       = (t.get("topic_b") or "belief B")
            lines.append(f"- **[sev={sev_str}]** {topic}")
            lines.append(f"  _{ta}_ vs. _{tb}_")
            if desc:
                lines.append(f"  {desc}")
        lines.append("")
        lines.append("  Resolve with: `python3 ~/claude_memory/scripts/verify_beliefs.py "
                     "--check-contradictions --no-jitter`")
    else:
        lines.append("_None detected._")
    lines.append("")

    # Scout results
    lines.append("### Interesting scout results awaiting follow-up")
    if state["interesting_scout"]:
        for r in state["interesting_scout"]:
            lines.append(f"- id={r['id']} ({r['score']:.3f}) [{r['source']}] {r['title'][:80]}")
        lines.append("")
        lines.append("  Promote with: `python3 ~/claude_memory/scripts/review_scout.py --promote ID`")
    else:
        lines.append("_None._")
    lines.append("")

    # Mood
    lines.append("### Last session mood")
    mood = state.get("last_mood")
    if mood:
        lines.append(f"  Date: {mood.get('date', 'unknown')}")
        if mood.get("tone"):
            lines.append(f"  Tone: {mood['tone']}")
        if mood.get("energy"):
            lines.append(f"  Energy: {mood['energy']}")
        if mood.get("bobby_state"):
            lines.append(f"  Bobby: {mood['bobby_state']}")
        if mood.get("notable_moments"):
            lines.append(f"  Notable: {str(mood['notable_moments'])[:120]}")
    else:
        lines.append("  _No mood data yet._")
    lines.append(f"  **Session hint:** {state.get('mood_hint', 'Standard context allocation.')}")
    lines.append("")

    # Curator
    lines.append("### Last curator run")
    lines.append(f"  {state['curator_report_line']}")
    lines.append("")

    # Bootstrap
    lines.append("### Last bootstrap context")
    lines.append(f"  {state['bootstrap_info'][:160]}")
    lines.append("")

    lines.append("---")
    lines.append(f"_Regenerate: `python3 ~/claude_memory/scripts/generate_session_prompt.py` (writes START_HERE.md + ember_engine_context.md)_")

    return "\n".join(lines) + "\n"


# ── Live schema helpers ───────────────────────────────────────────────────────

# Tables referenced directly by the SESSION OPEN PROTOCOL.
# Schema is injected into ember_engine_context.md so Claude always reads
# the real column list rather than relying on instructions prose.
SCHEMA_TABLES = [
    "processing_jobs",
    "goals",
    "scout_results",
    "beliefs",
    "questions",
    "sessions",
    "memory_chunks",
    "tensions",
]


def gather_live_schema(conn: sqlite3.Connection) -> str:
    """
    Query PRAGMA table_info for each table in SCHEMA_TABLES and return
    a formatted markdown block for injection into ember_engine_context.md.
    Tables that do not exist are noted as '(not yet created)'.
    """
    lines = []
    lines.append("## Live Schema — SESSION OPEN PROTOCOL reference tables")
    lines.append("Auto-generated from memory.db at context build time.")
    lines.append("Use these column lists for any DB query. Do not rely on column names")
    lines.append("from instructions prose — instructions may lag behind schema changes.")
    lines.append("")

    for table in SCHEMA_TABLES:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if rows:
            col_names = ", ".join(r[1] for r in rows)
            lines.append(f"**{table}:** {col_names}")
        else:
            lines.append(f"**{table}:** (not yet created — agent not built)")

    lines.append("")
    lines.append("_Verify any column at runtime: `PRAGMA table_info(<table>)`_")
    return "\n".join(lines)


# ── ember_engine_context.md builder ─────────────────────────────────────────────────

def build_ember_context(generated_at: datetime, schema_block: str = "") -> str:
    """
    Concatenate ember_engine_instructions.md + recent_memory.md + deep_memory.md
    into a single file for Claude to read at session open.

    Includes:
      - DO NOT EDIT header with regeneration instructions
      - Staleness warnings for any source file older than STALENESS_HOURS
      - Live schema block (from memory.db) so Claude has correct column names
      - Clear section dividers so Claude knows which section it is reading
    """
    now_ts = generated_at.timestamp()
    lines = []

    lines.append("<!-- ================================================================ -->")
    lines.append("<!-- ember_engine_context.md — MACHINE GENERATED. DO NOT EDIT THIS FILE.   -->")
    lines.append("<!-- Edit ember_engine_instructions.md to change standing instructions.     -->")
    lines.append("<!-- Regenerated nightly by generate_session_prompt.py               -->")
    lines.append(f"<!-- Generated: {generated_at.strftime('%Y-%m-%d %H:%M')}           -->")
    lines.append("<!-- ================================================================ -->")
    lines.append("")

    stale_warnings = []
    for source_path, section_label in CONTEXT_SOURCES:
        if not source_path.exists():
            stale_warnings.append(f"MISSING: {source_path.name}")
        else:
            age_hours = (now_ts - source_path.stat().st_mtime) / 3600
            if age_hours > STALENESS_HOURS:
                stale_warnings.append(
                    f"STALE ({age_hours:.0f}h old): {source_path.name} — "
                    f"run the appropriate refresh script before this session"
                )

    if stale_warnings:
        lines.append("<!-- STALENESS WARNINGS:")
        for w in stale_warnings:
            lines.append(f"     {w}")
        lines.append("-->")
        lines.append("")

    # Inject live schema before standing instructions so Claude reads real
    # column names before encountering any DB query examples in the instructions.
    if schema_block:
        lines.append("<!-- ===== SECTION: LIVE SCHEMA (auto-generated from memory.db) ===== -->")
        lines.append("")
        lines.append(schema_block)
        lines.append("")
        lines.append("")

    for source_path, section_label in CONTEXT_SOURCES:
        lines.append(f"<!-- ===== SECTION: {section_label} ===== -->")
        lines.append("")
        if source_path.exists():
            lines.append(source_path.read_text().rstrip())
        else:
            lines.append(f"_[{source_path.name} not found — run the appropriate refresh script]_")
        lines.append("")
        lines.append("")

    return "\n".join(lines) + "\n"


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Generate START_HERE.md and ember_engine_context.md for the next session"
    )
    parser.add_argument("--date",         help="Session date to generate for (YYYY-MM-DD, default: tomorrow)")
    parser.add_argument("--stdout",       action="store_true", help="Print START_HERE.md to stdout instead of writing files")
    parser.add_argument("--output",       default=str(OUTPUT_PATH), help=f"START_HERE.md output path (default: {OUTPUT_PATH})")
    parser.add_argument("--context-out",  default=str(CONTEXT_PATH), help=f"ember_engine_context.md output path (default: {CONTEXT_PATH})")
    parser.add_argument("--skip-context", action="store_true", help="Skip writing ember_engine_context.md (START_HERE.md only)")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"ERROR: database not found at {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    if args.date:
        try:
            session_date = date.fromisoformat(args.date)
        except ValueError:
            print(f"ERROR: invalid date format '{args.date}', expected YYYY-MM-DD", file=sys.stderr)
            sys.exit(1)
    else:
        session_date = date.today() + timedelta(days=1)

    conn = sqlite3.connect(DB_PATH)
    state = gather_state(conn, session_date)
    schema_block = gather_live_schema(conn)
    conn.close()

    # ── Write START_HERE.md ──
    content = render(state, NOW)

    if args.stdout:
        print(content)
        # Still write ember_engine_context.md unless --skip-context
        if not args.skip_context:
            ctx_path = Path(args.context_out)
            ctx_path.parent.mkdir(parents=True, exist_ok=True)
            ctx_path.write_text(build_ember_context(NOW, schema_block))
            print(f"[ember_engine_context.md written to {ctx_path}]", file=sys.stderr)
        return

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content)
    print(f"START_HERE.md written to {out_path}")
    print(f"  Next filename:           {state['next_filename']}")
    print(f"  Immediate goals:         {len(state['immediate_goals'])}")
    print(f"  Interesting scout items: {len(state['interesting_scout'])}")

    # ── Write ember_engine_context.md ──
    if not args.skip_context:
        ctx_path = Path(args.context_out)
        ctx_path.parent.mkdir(parents=True, exist_ok=True)
        ctx_context = build_ember_context(NOW, schema_block)
        ctx_path.write_text(ctx_context)
        missing = [s.name for s, _ in CONTEXT_SOURCES if not s.exists()]
        stale   = [s.name for s, _ in CONTEXT_SOURCES
                   if s.exists() and (NOW.timestamp() - s.stat().st_mtime) / 3600 > STALENESS_HOURS]
        status_parts = []
        if missing: status_parts.append(f"MISSING: {', '.join(missing)}")
        if stale:   status_parts.append(f"STALE: {', '.join(stale)}")
        status = " | ".join(status_parts) if status_parts else "all sources current"
        print(f"ember_engine_context.md written to {ctx_path}")
        print(f"  Sources: {status}")


if __name__ == "__main__":
    main()

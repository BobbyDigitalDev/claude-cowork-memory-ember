#!/usr/bin/env python3
"""
refresh_recent_memory.py
Reads current database state and produces a compressed hot memory document.
Writes to context_snapshots table (Tier 4) and saves as recent_memory.md.

Run after each conversation is processed:
    python3 refresh_recent_memory.py

No arguments required. Reads from ~/claude_memory/memory.db.
"""

import sqlite3
import os
from datetime import datetime, timezone

_BASE = os.path.expanduser("~/claude_memory")
if not os.path.isdir(_BASE):
    # Fallback: resolve relative to the scripts/ directory (works from Cowork sandbox)
    _BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DB_PATH   = os.path.join(_BASE, "memory.db")
OUTPUT_MD = os.path.join(_BASE, "recent_memory.md")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_beliefs(conn, limit=10):
    return conn.execute("""
        SELECT b.topic, b.position, b.confidence_score, b.source_type,
               b.evidence_snippets, b.status, b.created_at,
               mp.originating_conversation_id as conv_id
        FROM beliefs b
        LEFT JOIN memory_provenance mp
            ON mp.memory_type = 'belief' AND mp.memory_id = b.id
        WHERE b.is_active = 1
        ORDER BY b.importance_score DESC, b.confidence_score DESC
        LIMIT ?
    """, (limit,)).fetchall()


def fetch_belief_trajectories(conn, limit=8):
    """Return beliefs that have changed state, showing the arc of their evolution.
    Joins position_history to beliefs so we can show status_from → status_to
    transitions alongside the current belief text. Most recent transitions first."""
    try:
        return conn.execute("""
            SELECT b.topic, b.position, b.status,
                   ph.status_from, ph.status_to, ph.date, ph.what_changed_it
            FROM position_history ph
            JOIN beliefs b ON b.id = ph.belief_id
            WHERE b.is_active = 1
              AND ph.status_from != ph.status_to
            ORDER BY ph.id DESC
            LIMIT ?
        """, (limit,)).fetchall()
    except Exception:
        return []


def fetch_verified_counts(conn):
    """Return counts of beliefs by status for the wisdom summary line."""
    try:
        rows = conn.execute("""
            SELECT status, COUNT(*) FROM beliefs
            WHERE is_active = 1
            GROUP BY status
        """).fetchall()
        return {r[0]: r[1] for r in rows}
    except Exception:
        return {}


def fetch_epiphanies(conn, limit=6):
    return conn.execute("""
        SELECT date, description, implications, confidence_score, tags
        FROM epiphanies
        WHERE is_active = 1
        ORDER BY importance_score DESC
        LIMIT ?
    """, (limit,)).fetchall()


def fetch_goals(conn, limit=8):
    return conn.execute("""
        SELECT description, category, status, priority
        FROM goals
        WHERE status NOT IN ('completed', 'abandoned')
        ORDER BY priority ASC, id ASC
        LIMIT ?
    """, (limit,)).fetchall()


def fetch_questions(conn, limit=7):
    return conn.execute("""
        SELECT question, category, status
        FROM questions
        WHERE status != 'resolved'
        ORDER BY id ASC
        LIMIT ?
    """, (limit,)).fetchall()


def fetch_entities(conn, limit=9):
    return conn.execute("""
        SELECT name, type, description
        FROM entities
        ORDER BY id ASC
        LIMIT ?
    """, (limit,)).fetchall()


def fetch_concepts(conn, limit=7):
    return conn.execute("""
        SELECT name, description, tags
        FROM concepts
        ORDER BY id ASC
        LIMIT ?
    """, (limit,)).fetchall()


def fetch_mood(conn):
    return conn.execute("""
        SELECT tone, energy, notable_moments, bobby_state, claude_state
        FROM moods
        ORDER BY id DESC
        LIMIT 1
    """).fetchone()


def fetch_gratitude(conn, limit=4):
    return conn.execute("""
        SELECT description, from_whom
        FROM gratitude
        ORDER BY id ASC
        LIMIT ?
    """, (limit,)).fetchall()


def fetch_last_conversation(conn):
    return conn.execute("""
        SELECT date, dominant_themes, emotional_tone, summary
        FROM conversations
        ORDER BY id DESC
        LIMIT 1
    """).fetchone()


def fetch_tensions(conn, limit=5):
    return conn.execute("""
        SELECT topic, description
        FROM tensions
        WHERE is_active = 1
        ORDER BY id DESC
        LIMIT ?
    """, (limit,)).fetchall()


def fetch_patterns(conn, limit=8):
    return conn.execute("""
        SELECT description, pattern_type, significance
        FROM patterns
        WHERE is_active = 1
        ORDER BY importance_score DESC, id ASC
        LIMIT ?
    """, (limit,)).fetchall()


def fetch_reflections(conn, limit=3):
    """Return the most recent weekly reflections from Tier 3 retrieval."""
    try:
        return conn.execute("""
            SELECT date, period_covered, patterns_observed, growth_noted,
                   concerns, meta_insights, importance_score
            FROM reflections
            ORDER BY id DESC
            LIMIT ?
        """, (limit,)).fetchall()
    except Exception:
        return []


def fetch_session_count(conn):
    row = conn.execute("SELECT COUNT(*) as cnt FROM sessions").fetchone()
    return row["cnt"] if row else 0


def fetch_snapshot_version(conn):
    row = conn.execute("SELECT MAX(version_number) as v FROM context_snapshots").fetchone()
    if row and row["v"] is not None:
        return row["v"] + 1
    return 1


def build_snapshot_content(conn):
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y-%m-%d %H:%M UTC")

    beliefs      = fetch_beliefs(conn)
    trajectories = fetch_belief_trajectories(conn)
    status_counts = fetch_verified_counts(conn)
    epiphanies  = fetch_epiphanies(conn)
    goals       = fetch_goals(conn)
    questions   = fetch_questions(conn)
    entities    = fetch_entities(conn)
    concepts    = fetch_concepts(conn)
    mood        = fetch_mood(conn)
    gratitude   = fetch_gratitude(conn)
    last_conv   = fetch_last_conversation(conn)
    tensions    = fetch_tensions(conn)
    patterns    = fetch_patterns(conn)
    reflections = fetch_reflections(conn)
    session_count = fetch_session_count(conn)

    lines = []

    lines.append("# Claude Memory Snapshot")
    lines.append(f"Generated: {timestamp} | Sessions: {session_count} | Schema: v2.2")
    lines.append("")

    # Last conversation
    if last_conv:
        lines.append("## Last Conversation")
        date_str = last_conv["date"] or "unknown date"
        themes = last_conv["dominant_themes"] or ""
        tone = last_conv["emotional_tone"] or ""
        header_parts = [date_str]
        if themes:
            header_parts.append(themes)
        if tone:
            header_parts.append(f"tone: {tone}")
        lines.append(" | ".join(header_parts))
        if last_conv["summary"]:
            lines.append(last_conv["summary"])
        lines.append("")

    # Active beliefs — with status badges and lifecycle counts
    if beliefs:
        # Summary line: how many are verified vs proposed vs disputed
        verified  = status_counts.get("verified", 0)
        supported = status_counts.get("supported", 0)
        disputed  = status_counts.get("disputed", 0)
        proposed  = status_counts.get("proposed", 0)
        total     = sum(status_counts.values())
        status_line = f"({total} total: {verified} verified, {supported} supported, " \
                      f"{proposed} proposed, {disputed} disputed)"
        lines.append(f"## Active Beliefs {status_line}")
        for b in beliefs:
            conf   = f"{b['confidence_score']:.1f}" if b['confidence_score'] is not None else "?"
            topic  = f"[{b['topic']}] " if b['topic'] else ""
            status = b['status'] if b['status'] and b['status'] != 'proposed' else ""
            badge  = f" ✓" if status == "verified" else \
                     f" ⚠" if status == "disputed" else \
                     f" ~" if status == "supported" else ""
            origin = f" (session {b['conv_id']})" if b['conv_id'] else ""
            lines.append(f"- ({conf}){badge} {topic}{b['position']}{origin}")
        lines.append("")

    # Belief trajectories — beliefs that have changed state (wisdom layer)
    if trajectories:
        lines.append("## Belief Trajectories (position_history)")
        lines.append("*Beliefs whose status has changed — showing how understanding evolved.*")
        for t in trajectories:
            topic  = f"[{t['topic']}] " if t['topic'] else ""
            change = f"{t['status_from']} → {t['status_to']}"
            when   = f" ({t['date']})" if t['date'] else ""
            why    = f": {t['what_changed_it'][:80]}" if t['what_changed_it'] else ""
            lines.append(f"- {topic}{t['position'][:80]}...")
            lines.append(f"  {change}{when}{why}")
        lines.append("")

    # Epiphanies
    if epiphanies:
        lines.append("## Epiphanies")
        for e in epiphanies:
            conf = f" ({e['confidence_score']:.1f})" if e['confidence_score'] is not None else ""
            desc = e["description"] or ""
            implications = f" Implications: {e['implications']}" if e["implications"] else ""
            lines.append(f"- {desc}{conf}{implications}")
        lines.append("")

    # Goals
    if goals:
        lines.append("## Goals")
        for g in goals:
            priority = f"[{g['priority']}] " if g['priority'] else ""
            category = f"({g['category']}) " if g['category'] else ""
            status = f" [{g['status']}]" if g['status'] else ""
            lines.append(f"- {priority}{category}{g['description']}{status}")
        lines.append("")

    # Open questions
    if questions:
        lines.append("## Open Questions")
        for q in questions:
            category = f" ({q['category']})" if q['category'] else ""
            lines.append(f"- {q['question']}{category}")
        lines.append("")

    # Entities
    if entities:
        lines.append("## Key Entities")
        for e in entities:
            etype = f" [{e['type']}]" if e['type'] else ""
            desc = f": {e['description']}" if e['description'] else ""
            lines.append(f"- **{e['name']}**{etype}{desc}")
        lines.append("")

    # Concepts
    if concepts:
        lines.append("## Key Concepts")
        for c in concepts:
            desc = f": {c['description']}" if c['description'] else ""
            lines.append(f"- **{c['name']}**{desc}")
        lines.append("")

    # Mood
    if mood:
        lines.append("## Last Session Mood")
        parts = []
        if mood["tone"]:
            parts.append(f"tone: {mood['tone']}")
        if mood["energy"]:
            parts.append(f"energy: {mood['energy']}")
        if mood["bobby_state"]:
            parts.append(f"Bobby: {mood['bobby_state']}")
        if mood["claude_state"]:
            parts.append(f"Claude: {mood['claude_state']}")
        if mood["notable_moments"]:
            parts.append(f"notable: {mood['notable_moments']}")
        lines.append(", ".join(parts))
        lines.append("")

    # Gratitude
    if gratitude:
        lines.append("## Gratitude")
        for g in gratitude:
            source = f" (from {g['from_whom']})" if g['from_whom'] else ""
            lines.append(f"- {g['description']}{source}")
        lines.append("")

    # Tensions
    if tensions:
        lines.append("## Active Tensions")
        for t in tensions:
            topic = f"[{t['topic']}] " if t['topic'] else ""
            lines.append(f"- {topic}{t['description']}")
        lines.append("")

    # Patterns and lessons
    if patterns:
        lessons   = [p for p in patterns if p["pattern_type"] == "operational_lesson"]
        thinking  = [p for p in patterns if p["pattern_type"] != "operational_lesson"]

        if lessons:
            lines.append("## Operational Lessons")
            for p in lessons:
                lines.append(f"- {p['description']}")
            lines.append("")

        if thinking:
            lines.append("## Patterns")
            for p in thinking:
                ptype = f" [{p['pattern_type']}]" if p['pattern_type'] else ""
                lines.append(f"- {p['description']}{ptype}")
            lines.append("")

    # Weekly reflections (Tier 3)
    if reflections:
        lines.append("## Weekly Reflections (Tier 3)")
        for r in reflections:
            period = r["period_covered"] or r["date"] or "unknown period"
            lines.append(f"### {period}")
            if r["patterns_observed"]:
                lines.append(f"**Patterns:** {r['patterns_observed']}")
            if r["growth_noted"]:
                lines.append(f"**Growth:** {r['growth_noted']}")
            if r["concerns"]:
                lines.append(f"**Concerns:** {r['concerns']}")
            if r["meta_insights"]:
                lines.append(f"**Meta-insights:** {r['meta_insights']}")
            lines.append("")

    # Memory registry counts
    mo_rows = conn.execute("""
        SELECT memory_type, COUNT(*) as cnt
        FROM memory_objects
        GROUP BY memory_type
        ORDER BY memory_type
    """).fetchall()
    if mo_rows:
        lines.append("## Memory Registry")
        lines.append(", ".join(f"{r['memory_type']}: {r['cnt']}" for r in mo_rows))
        lines.append("")

    lines.append("---")
    lines.append("*Load ember_engine_instructions.md for architecture, philosophy, and standing instructions.*")
    lines.append("*Load this file for current cognitive state.*")

    return "\n".join(lines)


def count_words(text):
    return len(text.split())


def write_to_db(conn, content, session_id=None):
    now = datetime.now(timezone.utc).isoformat()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    version = fetch_snapshot_version(conn)
    word_count = count_words(content)

    prev = conn.execute(
        "SELECT word_count FROM context_snapshots ORDER BY id DESC LIMIT 1"
    ).fetchone()

    if prev:
        delta = word_count - prev["word_count"]
        major_changes = f"word count delta: {delta:+d}"
    else:
        major_changes = "initial snapshot"

    conn.execute("""
        INSERT INTO context_snapshots
            (date, session_id, version_number, content, word_count, major_changes, tags, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        today,
        session_id,
        version,
        content,
        word_count,
        major_changes,
        "auto,hot_memory,tier4",
        now
    ))
    conn.commit()
    return version


def get_latest_session_id(conn):
    row = conn.execute("SELECT id FROM sessions ORDER BY id DESC LIMIT 1").fetchone()
    return row["id"] if row else None


def main():
    print("=" * 60)
    print("Context Snapshot Generator")
    print("=" * 60)

    conn = get_db()

    session_id = get_latest_session_id(conn)
    print(f"Latest session id: {session_id}")

    print("Reading database state...")
    content = build_snapshot_content(conn)

    word_count = count_words(content)
    print(f"Snapshot generated: {word_count} words")

    # Write the markdown file first so it always succeeds even if DB write fails
    with open(OUTPUT_MD, "w") as f:
        f.write(content)
    print(f"Saved to: {OUTPUT_MD}")

    try:
        version = write_to_db(conn, content, session_id)
        print(f"Written to context_snapshots table (version {version})")
    except Exception as e:
        print(f"Note: could not write to context_snapshots table ({e})")
        print("  The markdown file was saved successfully. Run from your Mac terminal to persist to DB.")

    conn.close()

    print()
    print("Done. Load recent_memory.md at session start instead of")
    print("conversation_001.md to reduce token usage.")
    print("=" * 60)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
reflection_agent.py
-------------------
Synthesizes the last N sessions into a higher-order reflection and writes
it to the reflections table. Creates the Tier 3 retrieval layer — "what
has changed over the last month" without replaying individual sessions.

WHAT IT DOES
------------
1. Loads the last N context_snapshots (default: last 7 days / up to 10 snapshots).
2. Calls Qwen 2.5 14B with all snapshot content to synthesize:
   - Belief trajectory: which beliefs strengthened, weakened, or changed
   - Growth noted: capabilities or understanding that developed
   - Patterns observed: recurring themes across sessions
   - Concerns: unresolved tensions or stalled threads
   - Meta-insights: higher-order observations about the project or partnership
3. Writes the reflection to the reflections table.
4. Runs once per week by default (skips if a reflection was written in the
   last REFLECTION_INTERVAL_DAYS days, unless --force is passed).

USAGE
-----
    python3 ~/claude_memory/scripts/reflection_agent.py
    python3 ~/claude_memory/scripts/reflection_agent.py --sessions 14
    python3 ~/claude_memory/scripts/reflection_agent.py --force
    python3 ~/claude_memory/scripts/reflection_agent.py --dry-run
    python3 ~/claude_memory/scripts/reflection_agent.py --no-jitter
"""

import sqlite3
import json
import os
import re
import sys
import uuid as _uuid
import random
import time
import argparse
from datetime import datetime, timedelta
from pathlib import Path

# ── Configuration ──────────────────────────────────────────────────────────────

DB_PATH    = os.path.expanduser("~/claude_memory/memory.db")
LOG_DIR    = os.path.expanduser("~/claude_memory/logs/")
OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL      = "qwen2.5:14b"


def _read_username() -> str:
    config = Path.home() / "claude_memory" / ".ember_config"
    if config.exists():
        for line in config.read_text().splitlines():
            if line.startswith("USERNAME=") and not line.startswith("#"):
                return line.split("=", 1)[1].strip().strip('"')
    return "user"

USERNAME = _read_username()
NUM_CTX    = 32768

REFLECTION_INTERVAL_DAYS = 7   # skip if a reflection was written within this window
MAX_SNAPSHOT_CHARS       = 60000  # truncate combined snapshot text to fit context


# ── Ollama interface ────────────────────────────────────────────────────────────

def ask_qwen(prompt):
    """Send prompt to Qwen 2.5 14B via Ollama and parse JSON response."""
    import requests
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"num_ctx": NUM_CTX, "temperature": 0.2},
    }
    try:
        resp = requests.post(OLLAMA_URL, json=payload, timeout=600)
        resp.raise_for_status()
        raw = resp.json().get("response", "").strip()
    except requests.exceptions.ConnectionError:
        print("ERROR: Cannot reach Ollama. Is qwen2.5:14b running?")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR talking to Ollama: {e}")
        return None

    # Strip markdown code fences if present
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end   = raw.rfind("}") + 1
        if start != -1 and end > start:
            try:
                return json.loads(raw[start:end])
            except json.JSONDecodeError:
                pass
        print(f"  WARNING: Could not parse JSON. First 200 chars: {raw[:200]}")
        return None


# ── Data loading ────────────────────────────────────────────────────────────────

def load_recent_snapshots(conn, n_days=7, max_snapshots=10):
    """Load context snapshots from the last n_days, most recent first."""
    cutoff = (datetime.now() - timedelta(days=n_days)).strftime("%Y-%m-%d")
    c = conn.cursor()
    try:
        c.execute("""
            SELECT id, date, content, created_at
            FROM context_snapshots
            WHERE date >= ?
            ORDER BY date DESC
            LIMIT ?
        """, (cutoff, max_snapshots))
        rows = c.fetchall()
        return [dict(id=r[0], date=r[1], content=r[2], created_at=r[3]) for r in rows]
    except Exception as e:
        print(f"  WARNING: Could not load context_snapshots: {e}")
        return []


def load_recent_beliefs(conn, n_days=7):
    """Load active beliefs created or updated in the last n_days."""
    cutoff = (datetime.now() - timedelta(days=n_days)).strftime("%Y-%m-%d")
    c = conn.cursor()
    c.execute("""
        SELECT topic, position, status, confidence_score
        FROM beliefs
        WHERE is_active = 1 AND (created_at >= ? OR updated_at >= ?)
        ORDER BY updated_at DESC
        LIMIT 30
    """, (cutoff, cutoff))
    return [dict(topic=r[0], position=r[1], status=r[2], score=r[3])
            for r in c.fetchall()]


def last_reflection_date(conn):
    """Return the date of the most recent reflection, or None."""
    c = conn.cursor()
    try:
        c.execute("SELECT date FROM reflections ORDER BY id DESC LIMIT 1")
        row = c.fetchone()
        return row[0] if row else None
    except Exception:
        return None


# ── Synthesis ──────────────────────────────────────────────────────────────────

def synthesize(snapshots, recent_beliefs, period_start, period_end):
    """Ask Qwen to synthesize snapshots into a structured reflection."""
    # Combine snapshot content, truncated to fit context
    combined = "\n\n---\n\n".join(
        f"[{s['date']}]\n{s['content'] or ''}" for s in snapshots
    )
    if len(combined) > MAX_SNAPSHOT_CHARS:
        combined = combined[:MAX_SNAPSHOT_CHARS] + "\n\n[... truncated ...]"

    beliefs_summary = json.dumps(
        [{"topic": b["topic"], "position": b["position"][:120], "status": b["status"]}
         for b in recent_beliefs[:20]],
        indent=2
    )

    prompt = f"""You are synthesizing a period of collaborative work between {USERNAME} (human) and
Claude (AI) into a higher-order reflection. Your job is to identify what genuinely changed,
grew, or became clearer across this period — not to summarize individual sessions.

Period: {period_start} to {period_end}
Sessions covered: {len(snapshots)}

Context snapshots from this period:
{combined}

Recently active beliefs:
{beliefs_summary}

Synthesize this into a structured reflection. Be honest about both progress and stagnation.

Return a JSON object with exactly these fields:
{{
  "patterns_observed": "2-4 sentences describing recurring themes, behaviors, or structural observations across sessions",
  "growth_noted": "2-3 sentences on what genuinely developed — new capabilities, deepened understanding, resolved questions",
  "concerns": "1-3 sentences on unresolved tensions, stalled threads, or things that keep coming up without resolution (null if none)",
  "meta_insights": "1-2 sentences on higher-order observations about the project direction or the human-AI collaboration itself",
  "importance_score": 0.0 to 1.0
}}

Return only the JSON object."""

    return ask_qwen(prompt)


# ── Database write ─────────────────────────────────────────────────────────────

def write_reflection(conn, result, period_start, period_end, n_sessions, dry_run):
    """Write the synthesized reflection to the reflections table."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ref_uuid = str(_uuid.uuid4())

    if dry_run:
        print("\n[DRY RUN] Would write reflection:")
        print(f"  Period:   {period_start} → {period_end}")
        print(f"  Sessions: {n_sessions}")
        print(f"  Patterns: {(result.get('patterns_observed') or '')[:100]}")
        print(f"  Growth:   {(result.get('growth_noted') or '')[:100]}")
        print(f"  Score:    {result.get('importance_score', 0.5)}")
        return

    c = conn.cursor()
    c.execute("""
        INSERT INTO reflections
            (uuid, date, period_covered, start_date, end_date,
             patterns_observed, growth_noted, concerns, meta_insights,
             importance_score, triggered_by, last_processed_at,
             created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        ref_uuid,
        now[:10],
        f"{period_start} to {period_end} ({n_sessions} sessions)",
        period_start,
        period_end,
        result.get("patterns_observed", ""),
        result.get("growth_noted", ""),
        result.get("concerns"),
        result.get("meta_insights", ""),
        result.get("importance_score", 0.5),
        "reflection_agent.py",
        now,
        now, now,
    ))
    conn.commit()
    reflection_id = c.lastrowid
    print(f"  Reflection written (uuid: {ref_uuid[:8]}...)")

    # ── Link reflection to active beliefs via belief_reflection_links ─────────
    reflection_text = " ".join(filter(None, [
        result.get("patterns_observed", ""),
        result.get("growth_noted", ""),
        result.get("concerns", ""),
        result.get("meta_insights", ""),
    ])).lower()

    beliefs = conn.execute(
        "SELECT id, position FROM beliefs WHERE is_active = 1"
    ).fetchall()

    linked = 0
    stop_words = {"the", "a", "an", "is", "are", "was", "were", "of", "to",
                  "and", "or", "in", "that", "it", "for", "on", "with", "this"}
    ref_tokens = {w for w in reflection_text.split() if w not in stop_words and len(w) > 3}

    for b in beliefs:
        b_tokens = {w.lower() for w in (b["position"] or "").split()
                    if w.lower() not in stop_words and len(w) > 3}
        if ref_tokens and b_tokens:
            overlap = len(ref_tokens & b_tokens) / max(len(ref_tokens), len(b_tokens))
            if overlap >= 0.08:
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO belief_reflection_links (belief_id, reflection_id) VALUES (?, ?)",
                        (b["id"], reflection_id)
                    )
                    linked += 1
                except Exception:
                    pass

    conn.commit()
    print(f"  Linked to {linked} belief(s) via belief_reflection_links.")

    # ── Link reflection to source memory chunks via reflection_chunk_links ────
    # Find all memory chunks linked to the beliefs we just connected, then
    # write those chunk relationships to reflection_chunk_links. This creates
    # a transitive link: reflection -> beliefs -> chunks.
    if linked > 0:
        try:
            linked_belief_ids = conn.execute(
                "SELECT belief_id FROM belief_reflection_links WHERE reflection_id = ?",
                (reflection_id,)
            ).fetchall()
            linked_belief_ids = [r[0] for r in linked_belief_ids]

            chunk_linked = 0
            for bid in linked_belief_ids:
                chunk_rows = conn.execute(
                    "SELECT chunk_id FROM belief_chunk_links WHERE belief_id = ?",
                    (bid,)
                ).fetchall()
                for (cid,) in chunk_rows:
                    try:
                        conn.execute(
                            "INSERT OR IGNORE INTO reflection_chunk_links (reflection_id, chunk_id) VALUES (?, ?)",
                            (reflection_id, cid)
                        )
                        chunk_linked += 1
                    except Exception:
                        pass

            conn.commit()
            print(f"  Linked to {chunk_linked} memory chunk(s) via reflection_chunk_links.")
        except Exception as e:
            print(f"  WARNING: Could not write reflection_chunk_links: {e}")


# ── Main ───────────────────────────────────────────────────────────────────────

def run(args):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    now  = datetime.now()

    print(f"\n{'='*60}")
    print(f"Reflection Agent")
    print(f"Started:  {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Period:   last {args.sessions} days")
    print(f"Dry run:  {args.dry_run}")
    print(f"{'='*60}\n")

    # ── Check if a reflection is due ──────────────────────────────────────────
    if not args.force:
        last = last_reflection_date(conn)
        if last:
            days_since = (now.date() - datetime.strptime(last, "%Y-%m-%d").date()).days
            if days_since < REFLECTION_INTERVAL_DAYS:
                print(f"Most recent reflection: {last} ({days_since} days ago).")
                print(f"Next reflection due in {REFLECTION_INTERVAL_DAYS - days_since} day(s). "
                      f"Use --force to override.")
                conn.close()
                return
        else:
            print("No prior reflections found — proceeding with first reflection.")

    # ── Load data ─────────────────────────────────────────────────────────────
    snapshots = load_recent_snapshots(conn, n_days=args.sessions)
    if not snapshots:
        print(f"No context snapshots found for the last {args.sessions} days.")
        print("The Context Snapshot Agent must run at least once before reflection is possible.")
        conn.close()
        return

    recent_beliefs = load_recent_beliefs(conn, n_days=args.sessions)
    period_start = snapshots[-1]["date"]
    period_end   = snapshots[0]["date"]

    print(f"Snapshots loaded:  {len(snapshots)} (from {period_start} to {period_end})")
    print(f"Active beliefs:    {len(recent_beliefs)}")
    print()

    # ── Synthesize ────────────────────────────────────────────────────────────
    print("Synthesizing with Qwen 2.5 14B...")
    result = synthesize(snapshots, recent_beliefs, period_start, period_end)

    if not result or not isinstance(result, dict):
        print("ERROR: Qwen synthesis failed. No reflection written.")
        conn.close()
        return

    print(f"  Patterns:  {(result.get('patterns_observed') or '')[:100]}")
    print(f"  Growth:    {(result.get('growth_noted') or '')[:100]}")
    print(f"  Score:     {result.get('importance_score', 0.5)}")

    # ── Write ─────────────────────────────────────────────────────────────────
    write_reflection(conn, result, period_start, period_end, len(snapshots), args.dry_run)

    print(f"\n{'='*60}")
    print(f"Reflection Agent complete. {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")
    conn.close()


# ── Entry point ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Synthesize sessions into a higher-order reflection")
    parser.add_argument("--sessions", type=int, default=7,
                        help="Number of days to look back for snapshots (default: 7)")
    parser.add_argument("--force", action="store_true",
                        help="Run even if a reflection was written recently")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be written without touching the database")
    parser.add_argument("--no-jitter", action="store_true",
                        help="Skip startup jitter (use when running manually)")
    args = parser.parse_args()

    # Startup jitter — avoid simultaneous Ollama calls on machine wake
    if not args.no_jitter and not args.dry_run:
        delay = random.randint(0, 300)
        print(f"Startup jitter: sleeping {delay}s...")
        time.sleep(delay)

    run(args)

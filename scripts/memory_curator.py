#!/usr/bin/env python3
"""
memory_curator.py
-----------------
Nightly hygiene agent for memory.db.

WHY THIS EXISTS
---------------
Qwen extraction runs on every conversation ingest, creating duplicate goals
and beliefs over time. Manual hygiene passes work but don't scale. The Curator
automates the three highest-value cleanup tasks so the DB stays signal-dense.

PASSES
------
  1. GOAL DEDUP    -- embed pending goals, find near-duplicates by cosine
                      similarity. Above DEDUP_AUTO: auto-deprecate the newer
                      duplicate (higher id), keeping the older canonical goal.
                      In DEDUP_REVIEW band: flag in report, no DB change.

  2. STALE GOALS   -- pending goals untouched for > STALE_DAYS with no mention
                      in recent session text. Flagged in report only (human call).

  3. BELIEF DEDUP  -- same embedding approach on active beliefs. Canonical =
                      higher (importance + confidence) score; lower id breaks ties.

  4. QUESTION AUDIT -- open questions unreferenced in recent sessions for >
                      STALE_DAYS. Flagged in report only.

WHAT IT DOES NOT DO
-------------------
- Does not delete any rows (only status field + notes changes)
- Does not resolve open questions automatically
- Does not touch memory_chunks

OUTPUT
------
- Prints a summary to stdout
- Writes ~/claude_memory/curator_report.md (overwrites each run)
- Commits DB changes unless --dry-run is passed

USAGE
-----
    python3 ~/claude_memory/scripts/memory_curator.py
    python3 ~/claude_memory/scripts/memory_curator.py --dry-run
    python3 ~/claude_memory/scripts/memory_curator.py --stale-days 30
    python3 ~/claude_memory/scripts/memory_curator.py --dedup-threshold 0.80
"""

import sqlite3
import struct
import math
import sys
import os
import argparse
import requests
from datetime import datetime, date, timedelta
from pathlib import Path

# ── Paths and constants ───────────────────────────────────────────────────────

_BASE        = Path.home() / "claude_memory"
DB_PATH      = _BASE / "memory.db"
REPORT_PATH  = _BASE / "curator_report.md"
OLLAMA_URL   = "http://localhost:11434/api/embeddings"
EMBED_MODEL  = "nomic-embed-text"

DEDUP_AUTO_THRESHOLD   = 0.85   # cosine >= this -> auto-deprecate duplicate
DEDUP_REVIEW_THRESHOLD = 0.78   # cosine in [0.78, 0.85) -> flag for review
STALE_DAYS             = 45     # days without update -> stale
RECENT_SESSIONS        = 5      # sessions to scan for question references

NOW   = datetime.now().isoformat()
TODAY = date.today().isoformat()


# ── Embedding ─────────────────────────────────────────────────────────────────

def embed_text(text: str, model: str = EMBED_MODEL) -> list[float] | None:
    try:
        resp = requests.post(
            OLLAMA_URL,
            json={"model": model, "prompt": text},
            timeout=60,
        )
        resp.raise_for_status()
        vec = resp.json().get("embedding", [])
        if not vec:
            print(f"  WARNING: empty embedding for: {text[:60]}", file=sys.stderr)
            return None
        return vec
    except requests.exceptions.ConnectionError:
        print("ERROR: Cannot reach Ollama. Is it running?", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"  WARNING: embedding failed ({e}): {text[:60]}", file=sys.stderr)
        return None


def cosine(a: list[float], b: list[float]) -> float:
    dot   = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


# ── Pass 1: Goal dedup ────────────────────────────────────────────────────────

def run_goal_dedup(conn: sqlite3.Connection, auto_thresh: float,
                   review_thresh: float, dry_run: bool) -> dict:
    """
    Embed all pending goals, find near-duplicate pairs by cosine similarity.
    Auto-deprecates the higher-id goal in pairs above auto_thresh.
    Returns a results dict for the report.
    """
    rows = conn.execute("""
        SELECT id, description, priority, category, updated_at
        FROM goals
        WHERE status = 'pending'
          AND description IS NOT NULL AND description != ''
        ORDER BY id ASC
    """).fetchall()

    if not rows:
        return {"goals_checked": 0, "auto_deprecated": [], "review_flagged": []}

    print(f"  Embedding {len(rows)} pending goals...")
    items = []
    for r in rows:
        vec = embed_text(r["description"])
        items.append({
            "id":          r["id"],
            "description": r["description"],
            "priority":    r["priority"],
            "category":    r["category"],
            "updated_at":  r["updated_at"],
            "vec":         vec,
        })

    if all(item["vec"] is None for item in items):
        print("  WARNING: all embeddings failed — skipping goal dedup. Is Ollama running?",
              file=sys.stderr)
        return {"goals_checked": len(items), "auto_deprecated": [], "review_flagged": []}

    # Pairwise comparison — O(n²) but goals table is small
    auto_pairs   = []   # (score, canonical_id, duplicate_id)
    review_pairs = []

    deprecated_ids = set()  # track so a goal isn't processed twice

    for i in range(len(items)):
        if items[i]["vec"] is None:
            continue
        for j in range(i + 1, len(items)):
            if items[j]["vec"] is None:
                continue
            # Skip if either already queued for deprecation in this pass
            if items[i]["id"] in deprecated_ids or items[j]["id"] in deprecated_ids:
                continue
            score = cosine(items[i]["vec"], items[j]["vec"])
            if score >= auto_thresh:
                # Lower id is canonical (older = original)
                canonical = items[i]  # lower id (sorted ASC)
                duplicate = items[j]  # higher id
                auto_pairs.append({
                    "score":     round(score, 3),
                    "canonical": canonical,
                    "duplicate": duplicate,
                })
                deprecated_ids.add(duplicate["id"])
            elif score >= review_thresh:
                review_pairs.append({
                    "score": round(score, 3),
                    "a":     items[i],
                    "b":     items[j],
                })

    # Apply auto-deprecations
    for pair in auto_pairs:
        dup = pair["duplicate"]
        can = pair["canonical"]
        note = (
            f"[curator {TODAY}] Auto-deprecated: cosine={pair['score']:.3f} "
            f"with canonical goal id={can['id']}. "
            f"Canonical: \"{can['description'][:80]}\""
        )
        if not dry_run:
            conn.execute("""
                UPDATE goals
                SET status='deprecated',
                    notes=COALESCE(notes||' | ','') || ?,
                    updated_at=?
                WHERE id=?
            """, (note, NOW, dup["id"]))

    return {
        "goals_checked":    len(rows),
        "auto_deprecated":  auto_pairs,
        "review_flagged":   review_pairs,
    }


# ── Pass 2: Stale goals ───────────────────────────────────────────────────────

def run_stale_goals(conn: sqlite3.Connection, stale_days: int,
                    recent_sessions: int) -> dict:
    """
    Find pending goals not updated in stale_days with no mention in recent
    session transcripts. Flagged only — no DB changes.
    """
    cutoff = (date.today() - timedelta(days=stale_days)).isoformat()

    rows = conn.execute("""
        SELECT id, description, priority, category, updated_at
        FROM goals
        WHERE status = 'pending'
          AND (updated_at IS NULL OR updated_at < ?)
        ORDER BY updated_at ASC
    """, (cutoff,)).fetchall()

    if not rows:
        return {"stale_cutoff_days": stale_days, "stale_goals": []}

    # Grab recent session conversation content for reference check
    recent_text = ""
    session_rows = conn.execute("""
        SELECT raw_export, summary, key_insights
        FROM conversations
        ORDER BY id DESC
        LIMIT ?
    """, (recent_sessions,)).fetchall()
    for sr in session_rows:
        for col in ("raw_export", "summary", "key_insights"):
            val = sr[col]
            if val:
                recent_text += val.lower() + " "

    stale = []
    for r in rows:
        # Simple substring check: is a meaningful fragment of the description
        # mentioned in recent sessions?
        desc_lower = (r["description"] or "").lower()
        # Use the first 6 meaningful words as the fingerprint
        words = [w for w in desc_lower.split() if len(w) > 4][:6]
        referenced = any(w in recent_text for w in words) if words else False
        if not referenced:
            stale.append(dict(r))

    return {
        "stale_cutoff_days": stale_days,
        "stale_goals":       stale,
    }


# ── Pass 3: Belief dedup ──────────────────────────────────────────────────────

def run_belief_dedup(conn: sqlite3.Connection, auto_thresh: float,
                     review_thresh: float, dry_run: bool) -> dict:
    """
    Embed active beliefs, find near-duplicate pairs.
    Canonical = higher (importance + confidence) score; lower id breaks ties.
    """
    rows = conn.execute("""
        SELECT id, topic, position,
               COALESCE(importance_score, 0.5) AS imp,
               COALESCE(confidence_score, 0.5) AS conf,
               updated_at
        FROM beliefs
        WHERE is_active = 1
          AND topic IS NOT NULL AND topic != ''
        ORDER BY id ASC
    """).fetchall()

    if not rows:
        return {"beliefs_checked": 0, "auto_deprecated": [], "review_flagged": []}

    print(f"  Embedding {len(rows)} active beliefs...")
    items = []
    for r in rows:
        topic    = (r["topic"] or "").strip()
        position = (r["position"] or "").strip()
        text     = f"{topic}: {position}" if position and len(topic) < 60 else topic
        vec = embed_text(text)
        items.append({
            "id":       r["id"],
            "topic":    topic,
            "position": position,
            "text":     text,
            "score":    r["imp"] + r["conf"],
            "vec":      vec,
        })

    if all(item["vec"] is None for item in items):
        print("  WARNING: all embeddings failed — skipping belief dedup. Is Ollama running?",
              file=sys.stderr)
        return {"beliefs_checked": len(items), "auto_deprecated": [], "review_flagged": []}

    auto_pairs   = []
    review_pairs = []
    deprecated_ids = set()

    for i in range(len(items)):
        if items[i]["vec"] is None:
            continue
        for j in range(i + 1, len(items)):
            if items[j]["vec"] is None:
                continue
            if items[i]["id"] in deprecated_ids or items[j]["id"] in deprecated_ids:
                continue
            sim = cosine(items[i]["vec"], items[j]["vec"])
            if sim >= auto_thresh:
                # Canonical = higher combined score; lower id breaks ties
                if items[i]["score"] >= items[j]["score"]:
                    canonical, duplicate = items[i], items[j]
                else:
                    canonical, duplicate = items[j], items[i]
                auto_pairs.append({
                    "score":     round(sim, 3),
                    "canonical": canonical,
                    "duplicate": duplicate,
                })
                deprecated_ids.add(duplicate["id"])
            elif sim >= review_thresh:
                review_pairs.append({
                    "score": round(sim, 3),
                    "a":     items[i],
                    "b":     items[j],
                })

    for pair in auto_pairs:
        dup = pair["duplicate"]
        can = pair["canonical"]
        note = (
            f"[curator {TODAY}] Auto-deprecated: cosine={pair['score']:.3f} "
            f"with canonical belief id={can['id']}. "
            f"Canonical topic: \"{can['topic'][:80]}\""
        )
        if not dry_run:
            conn.execute("""
                UPDATE beliefs
                SET is_active=0,
                    origin=COALESCE(origin||' | ','') || ?,
                    updated_at=?
                WHERE id=?
            """, (note, NOW, dup["id"]))

    return {
        "beliefs_checked":  len(rows),
        "auto_deprecated":  auto_pairs,
        "review_flagged":   review_pairs,
    }


# ── Pass 4: Open question audit ───────────────────────────────────────────────

def run_question_audit(conn: sqlite3.Connection, stale_days: int,
                       recent_sessions: int) -> dict:
    """
    Find open questions older than stale_days that appear unreferenced in
    recent sessions. Flagged in report only.
    """
    cutoff = (date.today() - timedelta(days=stale_days)).isoformat()

    rows = conn.execute("""
        SELECT id, question, created_at, updated_at
        FROM questions
        WHERE status = 'open'
          AND (created_at IS NULL OR created_at < ?)
        ORDER BY created_at ASC
    """, (cutoff,)).fetchall()

    if not rows:
        return {"stale_cutoff_days": stale_days, "stale_questions": []}

    recent_text = ""
    session_rows = conn.execute("""
        SELECT raw_export, summary, key_insights FROM conversations
        ORDER BY id DESC LIMIT ?
    """, (recent_sessions,)).fetchall()
    for sr in session_rows:
        for col in ("raw_export", "summary", "key_insights"):
            val = sr[col]
            if val:
                recent_text += val.lower() + " "

    stale = []
    for r in rows:
        q_lower = (r["question"] or "").lower()
        words = [w for w in q_lower.split() if len(w) > 4][:6]
        referenced = any(w in recent_text for w in words) if words else False
        if not referenced:
            stale.append(dict(r))

    return {
        "stale_cutoff_days": stale_days,
        "stale_questions":   stale,
    }


# ── Report ─────────────────────────────────────────────────────────────────────

def write_report(results: dict, dry_run: bool, elapsed: float) -> str:
    g  = results["goals"]
    sg = results["stale_goals"]
    b  = results["beliefs"]
    q  = results["questions"]

    lines = []
    lines.append("# Memory Curator Report")
    lines.append(f"Generated: {NOW[:19]}  |  Dry run: {dry_run}  |  Elapsed: {elapsed:.1f}s")
    lines.append("")

    # ── Goal dedup ──
    lines.append("## Goal Dedup")
    lines.append(f"Checked: {g['goals_checked']} pending goals")
    lines.append("")

    if g["auto_deprecated"]:
        lines.append(f"### Auto-deprecated ({len(g['auto_deprecated'])})")
        for p in g["auto_deprecated"]:
            lines.append(
                f"- **id={p['duplicate']['id']}** deprecated → canonical **id={p['canonical']['id']}**  "
                f"(cosine={p['score']})"
            )
            lines.append(f"  - Kept:    {p['canonical']['description'][:100]}")
            lines.append(f"  - Removed: {p['duplicate']['description'][:100]}")
    else:
        lines.append("_No auto-deprecations._")

    lines.append("")

    if g["review_flagged"]:
        lines.append(f"### Review flagged ({len(g['review_flagged'])})")
        for p in g["review_flagged"]:
            lines.append(
                f"- cosine={p['score']}  id={p['a']['id']} vs id={p['b']['id']}"
            )
            lines.append(f"  - A: {p['a']['description'][:100]}")
            lines.append(f"  - B: {p['b']['description'][:100]}")
    else:
        lines.append("_No pairs in review band._")

    lines.append("")

    # ── Stale goals ──
    lines.append("## Stale Goals")
    lines.append(f"(Pending, unreferenced in recent sessions, not updated in {sg['stale_cutoff_days']}+ days)")
    lines.append("")
    if sg["stale_goals"]:
        for r in sg["stale_goals"]:
            lines.append(
                f"- id={r['id']}  [{r.get('priority','?')}]  "
                f"last updated: {(r.get('updated_at') or 'unknown')[:10]}"
            )
            lines.append(f"  {r['description'][:120]}")
    else:
        lines.append("_No stale goals found._")

    lines.append("")

    # ── Belief dedup ──
    lines.append("## Belief Dedup")
    lines.append(f"Checked: {b['beliefs_checked']} active beliefs")
    lines.append("")

    if b["auto_deprecated"]:
        lines.append(f"### Auto-deprecated ({len(b['auto_deprecated'])})")
        for p in b["auto_deprecated"]:
            lines.append(
                f"- **id={p['duplicate']['id']}** deprecated → canonical **id={p['canonical']['id']}**  "
                f"(cosine={p['score']})"
            )
            lines.append(f"  - Kept:    {p['canonical']['topic'][:100]}")
            lines.append(f"  - Removed: {p['duplicate']['topic'][:100]}")
    else:
        lines.append("_No auto-deprecations._")

    lines.append("")

    if b["review_flagged"]:
        lines.append(f"### Review flagged ({len(b['review_flagged'])})")
        for p in b["review_flagged"]:
            lines.append(
                f"- cosine={p['score']}  id={p['a']['id']} vs id={p['b']['id']}"
            )
            lines.append(f"  - A: {p['a']['topic'][:100]}")
            lines.append(f"  - B: {p['b']['topic'][:100]}")
    else:
        lines.append("_No pairs in review band._")

    lines.append("")

    # ── Question audit ──
    lines.append("## Open Question Audit")
    lines.append(
        f"(Open questions unreferenced in last {RECENT_SESSIONS} sessions, "
        f"older than {q['stale_cutoff_days']} days)"
    )
    lines.append("")
    if q["stale_questions"]:
        for r in q["stale_questions"]:
            lines.append(
                f"- id={r['id']}  opened: {(r.get('created_at') or 'unknown')[:10]}"
            )
            lines.append(f"  {r['question'][:140]}")
    else:
        lines.append("_No stale open questions._")

    lines.append("")
    lines.append("---")
    lines.append(
        f"_Curator pass complete. Committed: {not dry_run}. "
        f"Goal dedup: {len(g['auto_deprecated'])} deprecated. "
        f"Belief dedup: {len(b['auto_deprecated'])} deprecated._"
    )

    return "\n".join(lines) + "\n"


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Memory Curator Agent -- nightly DB hygiene"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview changes without writing to DB")
    parser.add_argument("--stale-days", type=int, default=STALE_DAYS,
                        help=f"Days without update -> stale (default: {STALE_DAYS})")
    parser.add_argument("--dedup-threshold", type=float, default=DEDUP_AUTO_THRESHOLD,
                        help=f"Auto-deprecate cosine threshold (default: {DEDUP_AUTO_THRESHOLD})")
    parser.add_argument("--review-threshold", type=float, default=DEDUP_REVIEW_THRESHOLD,
                        help=f"Review-flag cosine threshold (default: {DEDUP_REVIEW_THRESHOLD})")
    parser.add_argument("--report", default=str(REPORT_PATH),
                        help=f"Report output path (default: {REPORT_PATH})")
    parser.add_argument("--stdout", action="store_true",
                        help="Print report to stdout instead of writing file")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"ERROR: database not found at {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    start = datetime.now()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    print()
    print("=" * 60)
    print("Memory Curator Agent")
    print(f"DB:          {DB_PATH}")
    print(f"Dry run:     {args.dry_run}")
    print(f"Auto thresh: {args.dedup_threshold}  Review thresh: {args.review_threshold}")
    print(f"Stale days:  {args.stale_days}")
    print("=" * 60)

    print("\n[Pass 1] Goal dedup")
    goal_results = run_goal_dedup(
        conn,
        auto_thresh=args.dedup_threshold,
        review_thresh=args.review_threshold,
        dry_run=args.dry_run,
    )
    print(f"  Checked: {goal_results['goals_checked']} goals")
    print(f"  Auto-deprecated: {len(goal_results['auto_deprecated'])}")
    print(f"  Review flagged:  {len(goal_results['review_flagged'])}")

    print("\n[Pass 2] Stale goal detection")
    stale_results = run_stale_goals(conn, args.stale_days, RECENT_SESSIONS)
    print(f"  Stale (>{args.stale_days}d, unreferenced): {len(stale_results['stale_goals'])}")

    print("\n[Pass 3] Belief dedup")
    belief_results = run_belief_dedup(
        conn,
        auto_thresh=args.dedup_threshold,
        review_thresh=args.review_threshold,
        dry_run=args.dry_run,
    )
    print(f"  Checked: {belief_results['beliefs_checked']} beliefs")
    print(f"  Auto-deprecated: {len(belief_results['auto_deprecated'])}")
    print(f"  Review flagged:  {len(belief_results['review_flagged'])}")

    print("\n[Pass 4] Open question audit")
    question_results = run_question_audit(conn, args.stale_days, RECENT_SESSIONS)
    print(f"  Stale open questions: {len(question_results['stale_questions'])}")

    if not args.dry_run:
        try:
            conn.commit()
            print("\nChanges committed.")
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        conn.close()

    elapsed = (datetime.now() - start).total_seconds()

    results = {
        "goals":        goal_results,
        "stale_goals":  stale_results,
        "beliefs":      belief_results,
        "questions":    question_results,
    }

    report_text = write_report(results, args.dry_run, elapsed)

    if args.stdout:
        print("\n" + report_text)
    else:
        with open(args.report, "w") as f:
            f.write(report_text)
        print(f"\nReport written to {args.report}")

    print()
    print("=" * 60)
    print(f"Curator complete in {elapsed:.1f}s")
    n_deprecated = (
        len(goal_results["auto_deprecated"]) +
        len(belief_results["auto_deprecated"])
    )
    print(f"  {n_deprecated} items auto-deprecated")
    print(f"  {len(stale_results['stale_goals'])} stale goals flagged")
    print(f"  {len(question_results['stale_questions'])} stale questions flagged")
    print("=" * 60)
    print()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
belief_checksum.py
------------------
Implements the checksum mechanism from the founding conversation:

  "Before a belief hardens into memory it gets stress-tested against
   external sources."

Three trigger conditions (any one qualifies a belief for checksum):
  1. HIGH CONFIDENCE  — confidence_score >= CONFIDENCE_THRESHOLD
  2. EPIPHANY-LINKED  — belief is linked to an epiphany record
  3. TENSION TOPIC    — belief's topic already has a tension record

For each triggered belief, this script:
  - Searches scout_results for semantically relevant external content
    (keyword overlap; embedding-based search used if Ollama is available)
  - If coverage found (score >= COVERAGE_THRESHOLD):
      Writes a research_task as "fulfilled" with the supporting evidence.
      Updates sources table to record the external grounding.
  - If no coverage found:
      Writes a research_task as "pending" so the next scout run
      knows to search for this topic specifically.

This is the missing link between belief extraction and external verification.
The research_tasks table was built for this purpose but never populated.

USAGE
-----
    python3 ~/claude_memory/scripts/belief_checksum.py
    python3 ~/claude_memory/scripts/belief_checksum.py --limit 30
    python3 ~/claude_memory/scripts/belief_checksum.py --conversation bobby_2026_04_30_001.md
    python3 ~/claude_memory/scripts/belief_checksum.py --dry-run
    python3 ~/claude_memory/scripts/belief_checksum.py --threshold 0.75

WIRED INTO
----------
ingest.py Step 5.5 — runs after verify_beliefs.py, before embed_memories snapshot.
Can also be run standalone after any ingest pass.
"""

import sqlite3
import os
import sys
import argparse
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
_BASE      = Path.home() / "claude_memory"
DB_PATH    = _BASE / "memory.db"
SCRIPTS    = _BASE / "scripts"

# ── Thresholds ────────────────────────────────────────────────────────────────
CONFIDENCE_THRESHOLD  = 0.80   # beliefs at or above this confidence are triggered
COVERAGE_THRESHOLD    = 0.55   # keyword/semantic score to consider "genuinely covered"
                                # 0.35 was too permissive — loose word overlap isn't
                                # meaningful external grounding. 0.55 requires the
                                # scout result to share substantive vocabulary with
                                # the belief, not just incidental overlap.
DEFAULT_LIMIT         = 40     # max beliefs to process per run
RECENCY_DAYS          = 7      # only process beliefs from the last N days (0 = all)

# ── Keyword extraction ────────────────────────────────────────────────────────
STOP_WORDS = {
    "the","a","an","is","are","was","were","be","been","being","have","has",
    "had","do","does","did","will","would","could","should","may","might",
    "must","can","to","of","in","on","at","by","for","with","about","as",
    "it","its","this","that","these","those","and","or","but","not","no",
    "from","into","through","during","before","after","above","below","up",
    "down","out","off","over","under","again","further","then","once","i",
    "we","you","he","she","they","them","their","our","my","your","all",
    "both","each","few","more","most","other","some","such","own","same",
    "so","if","while","now","just","also","very","than","too","when","there",
}

def extract_keywords(text, min_len=4):
    """Extract meaningful keywords from a text string."""
    words = re.findall(r'\b[a-zA-Z][a-zA-Z0-9_-]*\b', text.lower())
    return [w for w in words if len(w) >= min_len and w not in STOP_WORDS]


def keyword_overlap_score(belief_keywords, title, abstract):
    """
    Score a scout result against belief keywords.
    Returns float in [0, 1]. Higher = more relevant.
    """
    if not belief_keywords:
        return 0.0
    haystack = (title + " " + (abstract or "")).lower()
    matches = sum(1 for kw in belief_keywords if kw in haystack)
    return matches / len(belief_keywords)


# ── Ollama embedding (optional, graceful fallback) ────────────────────────────
def _try_embed(text):
    """Attempt to get an embedding via Ollama. Returns list or None."""
    try:
        import requests
        r = requests.post(
            "http://localhost:11434/api/embeddings",
            json={"model": "nomic-embed-text", "prompt": text},
            timeout=10,
        )
        if r.status_code == 200:
            return r.json().get("embedding")
    except Exception:
        pass
    return None


def _cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na  = sum(x * x for x in a) ** 0.5
    nb  = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ── DB helpers ────────────────────────────────────────────────────────────────
def get_triggered_beliefs(conn, limit, recency_days, conv_filter=None):
    """
    Return beliefs meeting at least one trigger condition.
    Returns list of dicts.
    """
    date_cutoff = ""
    params = []

    if recency_days > 0:
        cutoff = (datetime.now() - timedelta(days=recency_days)).strftime("%Y-%m-%d")
        date_cutoff = "AND b.created_at >= ?"
        params.append(cutoff)

    conv_clause = ""
    if conv_filter:
        # beliefs extracted from a specific conversation
        conv_clause = """
        AND b.id IN (
            SELECT DISTINCT target_id FROM processing_jobs
            WHERE target_type = 'belief' AND source_file = ?
        )"""
        params.append(conv_filter)

    # Trigger 1: high confidence
    # Trigger 2: epiphany-linked (via memory_relationships or epiphanies table)
    # Trigger 3: topic has a tension record
    query = f"""
    SELECT DISTINCT b.id, b.topic, b.position, b.confidence_score,
                    b.status, b.memory_origin, b.created_at
    FROM beliefs b
    WHERE b.is_active = 1
      AND b.status NOT IN ('deprecated','archived')
      {date_cutoff}
      {conv_clause}
      AND (
          b.confidence_score >= {CONFIDENCE_THRESHOLD}
          OR b.id IN (
              SELECT DISTINCT mr.source_id FROM memory_relationships mr
              WHERE mr.source_type = 'epiphany' AND mr.target_type = 'belief'
              UNION
              SELECT DISTINCT mr.target_id FROM memory_relationships mr
              WHERE mr.target_type = 'epiphany' AND mr.source_type = 'belief'
          )
          OR LOWER(b.topic) IN (
              SELECT LOWER(t.topic) FROM tensions t WHERE t.is_active = 1
          )
      )
      AND b.id NOT IN (
          SELECT CAST(REPLACE(triggered_by, 'belief:', '') AS INTEGER)
          FROM research_tasks
          WHERE triggered_by LIKE 'belief:%'
      )
    ORDER BY b.confidence_score DESC, b.created_at DESC
    LIMIT ?
    """
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    cols = ["id","topic","position","confidence_score","status","memory_origin","created_at"]
    return [dict(zip(cols, r)) for r in rows]


def search_scout_results(conn, belief, use_embeddings=True):
    """
    Find scout_results relevant to this belief.
    Returns list of (score, row_dict) sorted descending.
    """
    rows = conn.execute("""
        SELECT id, title, abstract, source_name, source_url, relevance_score
        FROM scout_results
        WHERE status IN ('pending','interesting','ingested')
        ORDER BY relevance_score DESC
        LIMIT 200
    """).fetchall()

    if not rows:
        return []

    keywords = extract_keywords(belief["position"] + " " + (belief["topic"] or ""))
    belief_embed = _try_embed(belief["position"]) if use_embeddings else None

    scored = []
    for row in rows:
        rid, title, abstract, src_name, src_url, rel_score = row
        kw_score = keyword_overlap_score(keywords, title or "", abstract or "")

        if belief_embed:
            text_for_embed = (title or "") + " " + (abstract or "")[:500]
            row_embed = _try_embed(text_for_embed)
            sem_score = _cosine(belief_embed, row_embed) if row_embed else 0.0
            combined = 0.4 * kw_score + 0.6 * sem_score
        else:
            combined = kw_score

        if combined >= COVERAGE_THRESHOLD * 0.5:   # pre-filter: half threshold
            scored.append((combined, {
                "id": rid, "title": title, "abstract": abstract,
                "source_name": src_name, "source_url": src_url,
                "combined_score": combined,
            }))

    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:5]


def write_research_task(conn, belief, matches, dry_run):
    """Write a row to research_tasks for this belief."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    today = datetime.now().strftime("%Y-%m-%d")

    if matches:
        status    = "fulfilled"
        sources   = ", ".join(m["source_name"] for _, m in matches[:3])
        findings  = "Checksum found {} relevant result(s): {}".format(
            len(matches),
            "; ".join(m["title"][:80] for _, m in matches[:3])
        )
        belief_impact = "supporting evidence exists in scout archive"
    else:
        status    = "pending"
        sources   = ""
        findings  = "No external coverage found. Scout should search for this topic."
        belief_impact = "unverified externally — scout run needed"

    query = (belief["position"] or "")[:400]

    if dry_run:
        print(f"  [DRY RUN] research_task for belief {belief['id']}: {status}")
        print(f"    query: {query[:80]}...")
        return

    conn.execute("""
        INSERT INTO research_tasks
            (date, query, triggered_by, sources_consulted,
             findings, belief_impact, status, confidence_score,
             source_type, tags, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        today,
        query,
        f"belief:{belief['id']}",
        sources,
        findings,
        belief_impact,
        status,
        belief["confidence_score"],
        "checksum",
        f"topic:{belief['topic']}" if belief["topic"] else "",
        now,
    ))
    conn.commit()


def write_source_record(conn, belief, matches, dry_run):
    """Record external grounding in sources table."""
    if not matches or dry_run:
        return
    now   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    today = datetime.now().strftime("%Y-%m-%d")
    for score, m in matches[:2]:
        conn.execute("""
            INSERT OR IGNORE INTO sources
                (url, title, date_fetched, summary, relevance_tags,
                 challenged_belief_id, confidence_score, source_type, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            m["source_url"] or f"scout:{m['id']}",
            (m["title"] or "")[:200],
            today,
            (m["abstract"] or "")[:400],
            "checksum_match",
            belief["id"],
            round(score, 3),
            "external_research",
            now,
        ))
    conn.commit()


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    global CONFIDENCE_THRESHOLD, COVERAGE_THRESHOLD

    ap = argparse.ArgumentParser(description="Belief checksum — queue research tasks for high-confidence beliefs")
    ap.add_argument("--limit",        type=int,   default=DEFAULT_LIMIT)
    ap.add_argument("--threshold",    type=float, default=CONFIDENCE_THRESHOLD,
                    help="Min belief confidence score to trigger checksum (default 0.80). "
                         "Controls WHICH beliefs are selected, not match quality.")
    ap.add_argument("--coverage",     type=float, default=COVERAGE_THRESHOLD,
                    help="Min keyword/semantic score to count as 'covered' (default 0.55). "
                         "Controls how tight the external match must be.")
    ap.add_argument("--recency-days", type=int,   default=RECENCY_DAYS)
    ap.add_argument("--conversation", type=str,   default=None,
                    help="Only process beliefs from this conversation file")
    ap.add_argument("--dry-run",      action="store_true")
    ap.add_argument("--no-embed",     action="store_true",
                    help="Disable Ollama embedding (keyword-only matching)")
    args = ap.parse_args()

    CONFIDENCE_THRESHOLD = args.threshold
    COVERAGE_THRESHOLD   = args.coverage

    if not DB_PATH.exists():
        print(f"ERROR: database not found at {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    print("=" * 60)
    print("Belief Checksum Pass")
    print(f"Confidence threshold: {CONFIDENCE_THRESHOLD}  |  Coverage threshold: {COVERAGE_THRESHOLD}  |  Recency: {args.recency_days}d  |  Limit: {args.limit}")
    if args.dry_run:
        print("[DRY RUN — no DB writes]")
    print("=" * 60)

    beliefs = get_triggered_beliefs(
        conn,
        limit       = args.limit,
        recency_days= args.recency_days,
        conv_filter = args.conversation,
    )

    if not beliefs:
        print("No triggered beliefs found (all already queued or none meet threshold).")
        conn.close()
        return

    print(f"\n{len(beliefs)} belief(s) triggered for checksum:\n")

    fulfilled = 0
    pending   = 0

    for b in beliefs:
        print(f"  [{b['id']}] {b['topic']} | conf={b['confidence_score']:.2f}")
        print(f"       \"{b['position'][:80]}...\"")

        matches = search_scout_results(conn, b, use_embeddings=not args.no_embed)
        top_matches = [(s, m) for s, m in matches if s >= COVERAGE_THRESHOLD]

        if top_matches:
            print(f"       COVERED — {len(top_matches)} match(es) found")
            for score, m in top_matches[:2]:
                print(f"         [{score:.2f}] {m['title'][:70]}")
            fulfilled += 1
        else:
            print(f"       PENDING — no coverage, queuing for scout")
            pending += 1

        write_research_task(conn, b, top_matches, args.dry_run)
        write_source_record(conn, b, top_matches, args.dry_run)
        print()

    print("=" * 60)
    print(f"Summary: {fulfilled} fulfilled, {pending} queued for scout")
    if pending > 0:
        print(f"Run research_scout.py to fulfil {pending} pending research task(s).")
    print("=" * 60)

    conn.close()


if __name__ == "__main__":
    main()

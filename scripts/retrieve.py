#!/usr/bin/env python3
"""
retrieve.py
-----------
Retrieval orchestration engine for the Claude memory system.

Combines three strategies and returns a ranked, deduplicated context bundle:

  semantic    — embed query with nomic-embed-text, cosine similarity over
                memory_chunks, resolve back to source record via link tables
  structural  — SQL keyword match across beliefs, goals, questions, entities,
                concepts (works without Ollama)
  temporal    — most recent high-value items regardless of semantic match

Results are deduplicated by (source_type, source_id). If the same record is
found by multiple strategies, the scores are merged (max) and all strategies
are noted. The final list is sorted by combined score.

Can be used as a library (imported by refresh_deep_memory.py, agents, etc.)
or as a standalone CLI tool.

Usage (CLI):
    python3 ~/claude_memory/scripts/retrieve.py "substrate independence"
    python3 ~/claude_memory/scripts/retrieve.py "authentication architecture"
    python3 ~/claude_memory/scripts/retrieve.py "memory curator" --top 10
    python3 ~/claude_memory/scripts/retrieve.py "recent goals" --strategies temporal
    python3 ~/claude_memory/scripts/retrieve.py "beliefs about AI" --format json
    python3 ~/claude_memory/scripts/retrieve.py "schema" --no-semantic

Usage (library):
    from retrieve import retrieve
    bundle = retrieve("substrate independence")
    print(bundle["context_block"])

    results = retrieve("database schema", strategies=["semantic", "structural"], top=5)
    for r in results["results"]:
        print(r["source_type"], r["score"], r["content"][:80])
"""

import sqlite3
import struct
import json
import math
import os
import re
import sys
import argparse
import requests
from datetime import datetime, timedelta

# ── Configuration ──────────────────────────────────────────────────────────────

_BASE       = os.path.expanduser("~/claude_memory")
DB_PATH     = os.path.join(_BASE, "memory.db")
OLLAMA_URL  = "http://localhost:11434/api/embeddings"
EMBED_MODEL = "nomic-embed-text"

ALL_STRATEGIES = ["semantic", "structural", "temporal"]

# Stop words excluded from keyword tokenisation
_STOP_WORDS = {
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "up", "is", "are", "was", "were", "be",
    "been", "being", "have", "has", "had", "do", "does", "did", "will",
    "would", "could", "should", "may", "might", "shall", "can", "not",
    "this", "that", "these", "those", "it", "its", "how", "what", "why",
    "when", "where", "who", "which", "about", "as",
}


# ── Vector utilities ────────────────────────────────────────────────────────────

def _unpack_vector(blob):
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def _cosine(a, b):
    dot   = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


def _recency_bonus(timestamp_str, max_bonus=0.10, half_life_days=14):
    """Small additive bonus for recently active memories. Decays exponentially.

    Full bonus (+0.10) at 0 days old, half bonus (+0.05) at 14 days,
    negligible (~0.003) at 60+ days. Applied after epistemic damping so it
    acts as a tiebreaker between equally relevant items — not a rerank of
    semantically distant ones.

    For beliefs: uses updated_at (reflects latest verification/transition).
    For other chunks: uses memory_chunks.created_at.
    """
    try:
        dt       = datetime.strptime(timestamp_str[:10], "%Y-%m-%d")
        age_days = (datetime.now() - dt).days
        return max_bonus * (2 ** (-age_days / half_life_days))
    except Exception:
        return 0.0


# ── Embedding ───────────────────────────────────────────────────────────────────

def _embed(text, model=EMBED_MODEL):
    """Return embedding vector or None if Ollama is unreachable / unavailable."""
    try:
        resp = requests.post(
            OLLAMA_URL,
            json={"model": model, "prompt": text},
            timeout=60
        )
        resp.raise_for_status()
        vec = resp.json().get("embedding", [])
        return vec if vec else None
    except Exception:
        return None


# ── Keyword tokeniser ───────────────────────────────────────────────────────────

def _tokenise(query):
    """Return a list of meaningful, lowercased tokens from a query string."""
    raw = re.findall(r"[a-z0-9_]+", query.lower())
    return [t for t in raw if t not in _STOP_WORDS and len(t) > 1]


# ── Source record loader ────────────────────────────────────────────────────────

def _load_source_record(conn, source_type, source_id):
    """Load the primary display fields from the original structured table."""
    c = conn.cursor()
    try:
        if source_type == "belief":
            c.execute("""
                SELECT id, 'belief' as source_type,
                       topic || ': ' || COALESCE(position,'') as content,
                       confidence_score as score_hint,
                       status, tags, confidence, created_at
                FROM beliefs WHERE id = ?
            """, (source_id,))
        elif source_type == "epiphany":
            c.execute("""
                SELECT id, 'epiphany' as source_type,
                       'Epiphany: ' || COALESCE(description,'') as content,
                       confidence_score as score_hint,
                       checksum_status as status, tags, NULL as confidence, created_at
                FROM epiphanies WHERE id = ?
            """, (source_id,))
        elif source_type == "concept":
            c.execute("""
                SELECT id, 'concept' as source_type,
                       name || ': ' || COALESCE(description,'') as content,
                       0.7 as score_hint,
                       'active' as status, tags, NULL as confidence, created_at
                FROM concepts WHERE id = ?
            """, (source_id,))
        elif source_type == "pattern":
            c.execute("""
                SELECT id, 'pattern' as source_type,
                       COALESCE(description,'') as content,
                       COALESCE(importance_score, 0.5) as score_hint,
                       'active' as status, tags, NULL as confidence, created_at
                FROM patterns WHERE id = ?
            """, (source_id,))
        elif source_type == "goal":
            c.execute("""
                SELECT id, 'goal' as source_type,
                       COALESCE(description,'') as content,
                       CASE priority WHEN 'immediate' THEN 0.9
                                     WHEN 'near-term'  THEN 0.7
                                     ELSE 0.5 END as score_hint,
                       status, tags, priority as confidence, created_at
                FROM goals WHERE id = ?
            """, (source_id,))
        elif source_type == "question":
            c.execute("""
                SELECT id, 'question' as source_type,
                       COALESCE(question,'') as content,
                       0.6 as score_hint,
                       status, tags, category as confidence, created_at
                FROM questions WHERE id = ?
            """, (source_id,))
        elif source_type == "entity":
            c.execute("""
                SELECT id, 'entity' as source_type,
                       name || ' (' || COALESCE(type,'?') || '): ' || COALESCE(description,'') as content,
                       CASE importance WHEN 'high'   THEN 0.8
                                       WHEN 'medium' THEN 0.6
                                       ELSE 0.4 END as score_hint,
                       'active' as status, tags, type as confidence, created_at
                FROM entities WHERE id = ?
            """, (source_id,))
        else:
            return None
        row = c.fetchone()
        if not row:
            return None
        return {
            "source_id":   row[0],
            "source_type": row[1],
            "content":     (row[2] or "").strip(),
            "score_hint":  row[3] or 0.5,
            "status":      row[4] or "",
            "tags":        row[5] or "",
            "meta":        row[6] or "",
            "created_at":  row[7] or "",
        }
    except Exception:
        return None


# ── Strategy 1: Semantic search ─────────────────────────────────────────────────

def _semantic_search(query, top, threshold, conn):
    """Embed query, cosine-rank memory_chunks, resolve to source records.

    Returns list of result dicts. Silently returns [] if Ollama unreachable.
    """
    q_vec = _embed(query)
    if not q_vec:
        return []          # nomic-embed-text not running — degrade gracefully

    c = conn.cursor()
    c.execute("""
        SELECT id, content, embedding_vector, topic_tags, importance_score, conversation_id,
               COALESCE(created_at, '') as created_at
        FROM memory_chunks
        WHERE embedding_status = 'embedded' AND embedding_vector IS NOT NULL
    """)
    rows = c.fetchall()
    if not rows:
        return []

    # Epistemic damping multipliers (Concern 2 fix).
    # Applied to belief chunks only — non-belief chunks get 1.0.
    # Deprecated/disputed beliefs are pushed below threshold so they stop
    # polluting retrieval context even when semantically close to the query.
    _BELIEF_DAMPING = {
        "verified":   1.0,
        "supported":  0.85,
        "proposed":   0.75,
        "disputed":   0.35,
        "deprecated": 0.10,
        "archived":   0.05,
    }

    # Maximum additive recency bonus — must match _recency_bonus(max_bonus=...) default.
    # Used to set the absolute floor: the minimum raw cosine score a chunk needs
    # before it's worth doing a DB lookup. A chunk can only survive the final
    # threshold check if: (score * best_multiplier) + max_bonus >= threshold.
    # For non-belief chunks, best_multiplier=1.0, so floor = threshold - max_bonus.
    # This avoids DB round-trips for clearly irrelevant chunks at scale.
    _MAX_RECENCY_BONUS = 0.10
    _FLOOR = max(0.0, threshold - _MAX_RECENCY_BONUS)

    scored = []
    for row in rows:
        chunk_id, content, blob, topic_tags, importance, conv_id, chunk_created_at = row
        try:
            chunk_vec = _unpack_vector(blob)
            score     = _cosine(q_vec, chunk_vec)
        except Exception:
            continue

        # Absolute floor — skip chunks that cannot possibly survive the final
        # threshold check even with maximum recency bonus and no epistemic damping.
        # At default settings: floor = 0.45 - 0.10 = 0.35 (vs. the previous 0.0225).
        if score < _FLOOR:
            continue

        # Determine source type from topic_tags prefix ("belief,tag1,tag2")
        parts       = (topic_tags or "").split(",", 1)
        source_type = parts[0].strip() if parts else "unknown"

        # Look up source record ID + belief status (for epistemic damping)
        # and updated_at (for recency bonus).
        source_id        = None
        belief_status    = ""
        belief_updated_at = ""
        if source_type == "belief":
            r = c.execute(
                """SELECT bcl.belief_id, b.status, COALESCE(b.updated_at, b.created_at, '') as ts
                   FROM belief_chunk_links bcl
                   JOIN beliefs b ON b.id = bcl.belief_id
                   WHERE bcl.chunk_id = ? LIMIT 1""",
                (chunk_id,)
            ).fetchone()
            if r:
                source_id         = r[0]
                belief_status     = r[1] or ""
                belief_updated_at = r[2] or ""
        else:
            r = c.execute("""
                SELECT source_id FROM memory_relationships
                WHERE target_type = 'memory_chunk' AND target_id = ?
                  AND relationship_type = 'embedded_as'
                LIMIT 1
            """, (chunk_id,)).fetchone()
            if r:
                source_id = r[0]

        # Apply epistemic damping
        multiplier   = _BELIEF_DAMPING.get(belief_status, 1.0) if source_type == "belief" else 1.0
        damped_score = score * multiplier

        # Apply recency bonus — additive tiebreaker for recently active items.
        # Beliefs use updated_at (reflects latest verification pass).
        # Other chunks use the chunk's own created_at.
        recency_ts = belief_updated_at if source_type == "belief" and belief_updated_at else chunk_created_at
        bonus      = _recency_bonus(recency_ts)
        final_score = damped_score + bonus

        if final_score < threshold:
            continue

        # Build meta string for debugging
        meta_parts = []
        if source_type == "belief" and multiplier < 1.0:
            meta_parts.append(f"epistemic_multiplier={multiplier:.2f}")
        if bonus > 0.005:
            meta_parts.append(f"recency_bonus={bonus:.3f}")

        scored.append({
            "source_type": source_type,
            "source_id":   source_id,
            "chunk_id":    chunk_id,
            "content":     content,
            "score":       final_score,
            "strategy":    "semantic",
            "tags":        parts[1].strip() if len(parts) > 1 else "",
            "meta":        " | ".join(meta_parts),
            "status":      belief_status,
        })

    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored[:top]


# ── Strategy 2: Structural keyword search ──────────────────────────────────────

def _structural_search(query, top, conn):
    """Keyword match across structured tables. Works without Ollama.

    Score = (matching_tokens / total_tokens) * importance_hint.
    """
    tokens = _tokenise(query)
    if not tokens:
        return []

    c   = conn.cursor()
    out = []

    def _keyword_score(text, tok_list):
        t = (text or "").lower()
        hits = sum(1 for tok in tok_list if tok in t)
        return hits / len(tok_list) if tok_list else 0.0

    # Build a single OR LIKE clause
    like_parts  = " OR ".join(["? LIKE ?" for _ in tokens])
    like_values = []   # filled per-column below

    def _like_args(col):
        """Returns (placeholders_str, [col, %tok%, col, %tok%, ...])"""
        placeholders = " OR ".join([f"{col} LIKE ?" for _ in tokens])
        values = [f"%{t}%" for t in tokens]
        return placeholders, values

    # ── beliefs ──────────────────────────────────────────────────────────────
    ph, vals = _like_args("topic")
    ph2, vals2 = _like_args("position")
    rows = c.execute(f"""
        SELECT id, topic || ': ' || COALESCE(position,'') as content,
               confidence_score, status, tags
        FROM beliefs
        WHERE is_active = 1 AND ({ph} OR {ph2})
        LIMIT ?
    """, vals + vals2 + [top * 2]).fetchall()
    for row in rows:
        score = _keyword_score(row[1], tokens) * (0.5 + 0.5 * (row[2] or 0.5))
        out.append({
            "source_type": "belief",
            "source_id":   row[0],
            "content":     (row[1] or "").strip(),
            "score":       min(score, 0.95),
            "strategy":    "structural",
            "tags":        row[4] or "",
            "status":      row[3] or "",
            "meta":        "",
        })

    # ── goals ─────────────────────────────────────────────────────────────────
    ph, vals = _like_args("description")
    rows = c.execute(f"""
        SELECT id, description, priority, status, tags
        FROM goals
        WHERE status != 'completed' AND ({ph})
        LIMIT ?
    """, vals + [top * 2]).fetchall()
    for row in rows:
        pri_bonus = {"immediate": 0.3, "near-term": 0.15}.get(row[2] or "", 0.0)
        score     = _keyword_score(row[1], tokens) * 0.8 + pri_bonus
        out.append({
            "source_type": "goal",
            "source_id":   row[0],
            "content":     (row[1] or "").strip(),
            "score":       min(score, 0.95),
            "strategy":    "structural",
            "tags":        row[4] or "",
            "status":      row[3] or "",
            "meta":        row[2] or "",
        })

    # ── questions ─────────────────────────────────────────────────────────────
    ph, vals = _like_args("question")
    rows = c.execute(f"""
        SELECT id, question, category, status, tags
        FROM questions
        WHERE status = 'open' AND ({ph})
        LIMIT ?
    """, vals + [top * 2]).fetchall()
    for row in rows:
        score = _keyword_score(row[1], tokens) * 0.75
        out.append({
            "source_type": "question",
            "source_id":   row[0],
            "content":     (row[1] or "").strip(),
            "score":       min(score, 0.95),
            "strategy":    "structural",
            "tags":        row[4] or "",
            "status":      row[3] or "",
            "meta":        row[2] or "",
        })

    # ── entities ──────────────────────────────────────────────────────────────
    ph, vals = _like_args("name")
    ph2, vals2 = _like_args("description")
    rows = c.execute(f"""
        SELECT id, name || ' (' || COALESCE(type,'?') || '): ' || COALESCE(description,'') as content,
               importance, tags, type
        FROM entities
        WHERE ({ph} OR {ph2})
        LIMIT ?
    """, vals + vals2 + [top]).fetchall()
    for row in rows:
        imp_bonus = {"high": 0.2, "medium": 0.1}.get(row[2] or "", 0.0)
        score     = _keyword_score(row[1], tokens) * 0.7 + imp_bonus
        out.append({
            "source_type": "entity",
            "source_id":   row[0],
            "content":     (row[1] or "").strip(),
            "score":       min(score, 0.95),
            "strategy":    "structural",
            "tags":        row[3] or "",
            "status":      "active",
            "meta":        row[4] or "",
        })

    # ── concepts ──────────────────────────────────────────────────────────────
    ph, vals = _like_args("name")
    ph2, vals2 = _like_args("description")
    rows = c.execute(f"""
        SELECT id, name || ': ' || COALESCE(description,'') as content,
               tags
        FROM concepts
        WHERE ({ph} OR {ph2})
        LIMIT ?
    """, vals + vals2 + [top]).fetchall()
    for row in rows:
        score = _keyword_score(row[1], tokens) * 0.72
        out.append({
            "source_type": "concept",
            "source_id":   row[0],
            "content":     (row[1] or "").strip(),
            "score":       min(score, 0.95),
            "strategy":    "structural",
            "tags":        row[2] or "",
            "status":      "active",
            "meta":        "",
        })

    out.sort(key=lambda r: r["score"], reverse=True)
    return out[:top]


# ── Strategy 3: Temporal search ─────────────────────────────────────────────────

def _temporal_search(days, top, conn):
    """Return recently active high-value items: open goals, open questions,
    verified/supported beliefs, recent concepts.

    Score decays with age (1.0 at 0 days, 0.5 at `days` days).
    """
    c      = conn.cursor()
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d 00:00:00")
    out    = []

    def _age_score(created_at_str, max_days):
        try:
            dt     = datetime.strptime(created_at_str[:10], "%Y-%m-%d")
            age    = (datetime.now() - dt).days
            return max(0.3, 1.0 - 0.7 * (age / max_days))
        except Exception:
            return 0.5

    # Recent open goals
    rows = c.execute("""
        SELECT id, description, priority, status, tags, created_at
        FROM goals
        WHERE status != 'completed' AND created_at >= ?
        ORDER BY CASE priority WHEN 'immediate' THEN 0 WHEN 'near-term' THEN 1 ELSE 2 END,
                 created_at DESC
        LIMIT ?
    """, (cutoff, top)).fetchall()
    for row in rows:
        pri_bonus = {"immediate": 0.3, "near-term": 0.15}.get(row[2] or "", 0.0)
        base      = _age_score(row[5], days)
        out.append({
            "source_type": "goal",
            "source_id":   row[0],
            "content":     (row[1] or "").strip(),
            "score":       min(base + pri_bonus, 0.95),
            "strategy":    "temporal",
            "tags":        row[4] or "",
            "status":      row[3] or "",
            "meta":        row[2] or "",
        })

    # Recent open questions
    rows = c.execute("""
        SELECT id, question, category, status, tags, created_at
        FROM questions
        WHERE status = 'open' AND created_at >= ?
        ORDER BY created_at DESC
        LIMIT ?
    """, (cutoff, top)).fetchall()
    for row in rows:
        out.append({
            "source_type": "question",
            "source_id":   row[0],
            "content":     (row[1] or "").strip(),
            "score":       _age_score(row[5], days),
            "strategy":    "temporal",
            "tags":        row[4] or "",
            "status":      row[3] or "",
            "meta":        row[2] or "",
        })

    # Recent high-confidence beliefs
    rows = c.execute("""
        SELECT id, topic || ': ' || COALESCE(position,'') as content,
               confidence_score, status, tags, created_at
        FROM beliefs
        WHERE is_active = 1 AND status IN ('verified','supported')
          AND created_at >= ?
        ORDER BY confidence_score DESC, created_at DESC
        LIMIT ?
    """, (cutoff, top)).fetchall()
    for row in rows:
        base = _age_score(row[5], days)
        out.append({
            "source_type": "belief",
            "source_id":   row[0],
            "content":     (row[1] or "").strip(),
            "score":       min(base * (0.5 + 0.5 * (row[2] or 0.5)), 0.95),
            "strategy":    "temporal",
            "tags":        row[4] or "",
            "status":      row[3] or "",
            "meta":        "",
        })

    out.sort(key=lambda r: r["score"], reverse=True)
    return out[:top]


# ── Deduplication and merge ─────────────────────────────────────────────────────

def _merge(strategy_results):
    """Merge results from multiple strategies.

    Deduplication key: (source_type, source_id).
    If the same record appears in multiple strategies, take max score and
    collect all strategy names.

    Items with source_id = None are kept as-is (chunk-only fallback) and
    deduplicated by content hash.
    """
    seen    = {}   # (source_type, source_id) → index into merged
    no_id   = []   # items without a source_id (chunk-only)
    merged  = []

    for result in strategy_results:
        st = result.get("source_type", "unknown")
        si = result.get("source_id")

        if si is None:
            # chunk-only result — keep unless exact content duplicate
            content_key = (st, result.get("content", "")[:80])
            if content_key not in {(x["source_type"], x["content"][:80]) for x in no_id}:
                no_id.append(result)
            continue

        key = (st, si)
        if key in seen:
            idx = seen[key]
            existing = merged[idx]
            # Merge: max score, union strategies
            if result["score"] > existing["score"]:
                existing["score"] = result["score"]
            strategies_set = set(existing.get("strategies", [existing["strategy"]]))
            strategies_set.add(result["strategy"])
            existing["strategies"] = sorted(strategies_set)
        else:
            result_copy = dict(result)
            result_copy["strategies"] = [result["strategy"]]
            seen[key]  = len(merged)
            merged.append(result_copy)

    combined = merged + no_id
    combined.sort(key=lambda r: r["score"], reverse=True)
    return combined


# ── Context block formatter ─────────────────────────────────────────────────────

def _format_context_block(results, query, strategies_used, elapsed_s):
    """Format results as a markdown context block suitable for session injection."""
    if not results:
        return f"*No relevant memories found for: {query}*\n"

    lines = []
    lines.append(f"## Retrieved Memory Context")
    lines.append(
        f"*Query: \"{query}\"  |  {datetime.now().strftime('%Y-%m-%d')}  |  "
        f"strategies: {', '.join(strategies_used)}  |  "
        f"{len(results)} results in {elapsed_s:.1f}s*"
    )
    lines.append("")

    # Group by source_type
    by_type = {}
    for r in results:
        by_type.setdefault(r["source_type"], []).append(r)

    type_order = ["belief", "goal", "question", "epiphany", "concept", "entity", "pattern"]
    other_types = [t for t in by_type if t not in type_order]

    for stype in type_order + other_types:
        if stype not in by_type:
            continue
        items = by_type[stype]
        label = stype.upper() + ("S" if not stype.endswith("s") else "")
        lines.append(f"### {label} ({len(items)})")
        for item in items:
            strats  = "/".join(item.get("strategies", [item.get("strategy", "?")]))
            score   = item["score"]
            content = item["content"]
            status  = item.get("status", "")
            meta    = item.get("meta", "")
            tags    = item.get("tags", "")

            status_str = f"[{status}] " if status else ""
            meta_str   = f" | {meta}" if meta else ""
            tags_str   = f" | tags: {tags[:60]}" if tags else ""

            lines.append(f"- {status_str}{content[:200]}")
            lines.append(f"  *score: {score:.3f} | {strats}{meta_str}{tags_str}*")
        lines.append("")

    lines.append("---")
    return "\n".join(lines)


# ── Public API ──────────────────────────────────────────────────────────────────

def retrieve(
    query,
    strategies=None,
    top=10,
    threshold=0.45,
    days=30,
    db_path=None,
):
    """Retrieve the most relevant memories for a given query.

    Parameters
    ----------
    query      : str   — natural language search query
    strategies : list  — subset of ["semantic", "structural", "temporal"]
                         default: all three
    top        : int   — max results to return after merging (default: 10)
    threshold  : float — minimum cosine similarity for semantic results (default: 0.45)
    days       : int   — lookback window in days for temporal strategy (default: 30)
    db_path    : str   — override database path

    Returns
    -------
    dict with keys:
        query          : str
        results        : list of result dicts (sorted by score)
        context_block  : str (markdown, ready for session injection)
        stats          : dict (counts per strategy, timing)
    """
    import time
    _t0 = time.time()

    if strategies is None:
        strategies = ALL_STRATEGIES

    _db = db_path or DB_PATH
    conn = sqlite3.connect(_db)
    conn.row_factory = None   # raw tuples for performance

    all_raw = []

    if "semantic" in strategies:
        sem = _semantic_search(query, top * 2, threshold, conn)
        all_raw.extend(sem)

    if "structural" in strategies:
        struct_ = _structural_search(query, top * 2, conn)
        all_raw.extend(struct_)

    if "temporal" in strategies:
        temp = _temporal_search(days, top, conn)
        all_raw.extend(temp)

    conn.close()

    merged  = _merge(all_raw)[:top]
    elapsed = time.time() - _t0

    # Collect per-strategy counts before merge
    strat_counts = {}
    for r in all_raw:
        s = r.get("strategy", "?")
        strat_counts[s] = strat_counts.get(s, 0) + 1

    ctx_block = _format_context_block(merged, query, strategies, elapsed)

    # ── Log to retrieval_events ───────────────────────────────────────────────
    _log_retrieval_event(
        db_path      = _db,
        query        = query,
        tiers_used   = ",".join(strategies),
        chunks_returned = len(merged),
        latency_ms   = int(elapsed * 1000),
    )

    return {
        "query":         query,
        "results":       merged,
        "context_block": ctx_block,
        "stats": {
            "total":          len(merged),
            "elapsed_s":      round(elapsed, 2),
            "per_strategy":   strat_counts,
            "strategies_used": strategies,
        },
    }


def _log_retrieval_event(db_path, query, tiers_used, chunks_returned, latency_ms):
    """Write one row to retrieval_events for observability.

    Opens its own connection rather than reusing the caller's (which is
    already closed by this point). Silently skips on any error so a logging
    failure never breaks a retrieval call.
    """
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("""
            INSERT INTO retrieval_events
                (date, query, tiers_used, chunks_returned,
                 retrieval_latency_ms, created_at)
            VALUES (date('now'), ?, ?, ?, ?, datetime('now'))
        """, (
            query[:300] if query else "",
            tiers_used,
            chunks_returned,
            latency_ms,
        ))
        conn.commit()
        conn.close()
    except Exception:
        pass


# ── CLI ─────────────────────────────────────────────────────────────────────────

def _main():
    parser = argparse.ArgumentParser(
        description="Retrieval orchestration engine — combines semantic, structural, and temporal search"
    )
    parser.add_argument("query", help="Natural language query string")
    parser.add_argument("--top",       type=int,   default=10, metavar="N",
                        help="Max results to return (default: 10)")
    parser.add_argument("--threshold", type=float, default=0.45, metavar="F",
                        help="Min cosine similarity for semantic results (default: 0.45)")
    parser.add_argument("--days",      type=int,   default=30,
                        help="Temporal lookback window in days (default: 30)")
    parser.add_argument("--strategies", nargs="+",
                        choices=ALL_STRATEGIES, default=ALL_STRATEGIES,
                        metavar="STRATEGY",
                        help=f"Strategies to use (default: all). Options: {ALL_STRATEGIES}")
    parser.add_argument("--no-semantic",    action="store_true",
                        help="Skip semantic strategy (useful if nomic-embed-text not running)")
    parser.add_argument("--format", choices=["markdown", "json", "plain"], default="plain",
                        help="Output format (default: plain)")
    parser.add_argument("--db", default=None, metavar="PATH",
                        help="Override database path")
    args = parser.parse_args()

    strategies = list(args.strategies)
    if args.no_semantic and "semantic" in strategies:
        strategies.remove("semantic")

    bundle = retrieve(
        query=args.query,
        strategies=strategies,
        top=args.top,
        threshold=args.threshold,
        days=args.days,
        db_path=args.db,
    )

    stats = bundle["stats"]
    print(f'\nQuery: "{args.query}"')
    print(f"Strategies: {', '.join(strategies)}  |  "
          f"Results: {stats['total']}  |  Time: {stats['elapsed_s']}s")
    per = stats["per_strategy"]
    for s in strategies:
        print(f"  {s}: {per.get(s, 0)} candidates")
    print()

    if args.format == "markdown":
        print(bundle["context_block"])

    elif args.format == "json":
        print(json.dumps(bundle, indent=2, default=str))

    else:   # plain
        results = bundle["results"]
        if not results:
            print("No results found.")
            return

        bar_width = 20
        for i, r in enumerate(results, 1):
            score  = r["score"]
            bar    = "█" * int(score * bar_width)
            strats = "/".join(r.get("strategies", [r.get("strategy", "?")]))
            stype  = r["source_type"].upper()
            status = f"[{r['status']}] " if r.get("status") else ""
            sid    = r.get("source_id", "?")

            print(f"[{i:2d}] {stype:<10}  id:{sid:<5}  score:{score:.3f} {bar:<{bar_width}}  ({strats})")
            content_preview = (r["content"] or "").replace("\n", " ")[:120]
            print(f"     {status}{content_preview}")
            if r.get("tags"):
                print(f"     tags: {r['tags'][:80]}")
            print()

        print(f"{'─'*60}")
        print(f"Showing {len(results)} results")


if __name__ == "__main__":
    _main()

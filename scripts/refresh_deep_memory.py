#!/usr/bin/env python3
"""
refresh_deep_memory.py
--------------------
Generates a compact 'retrieved context' markdown block at session open by
semantically querying memory_chunks for each seed topic.

Writes to ~/claude_memory/deep_memory.md (sibling of recent_memory.md).
Session load instruction becomes: read ember_engine_instructions.md,
recent_memory.md, and deep_memory.md.

Tier assignment (per v2.2 architecture):
  Tier 4 hot memory      -> recent_memory.md (refresh_recent_memory.py)
  Tier 2 semantic scaffold -> deep_memory.md (this script)

Seed derivation (hybrid):
  By default, seeds are auto-derived from the database:
    - 3 most recent open questions
    - 2 most recent pending immediate-priority goals
    - 2 highest-scoring active beliefs (importance + confidence)
  This keeps the bootstrap aligned with whatever is currently live in the DB.

  Pass --seeds to override for a specific run. If auto-derive yields nothing
  (fresh database, etc.), FALLBACK_SEEDS below is used.

Usage:
    python3 ~/claude_memory/scripts/refresh_deep_memory.py
    python3 ~/claude_memory/scripts/refresh_deep_memory.py --seeds "topic A" "topic B"
    python3 ~/claude_memory/scripts/refresh_deep_memory.py --top 5 --threshold 0.55
    python3 ~/claude_memory/scripts/refresh_deep_memory.py --stdout
    python3 ~/claude_memory/scripts/refresh_deep_memory.py --fallback
    python3 ~/claude_memory/scripts/refresh_deep_memory.py --list-seeds

Options:
    --seeds S [S ...]   Override all seed sourcing for this run
    --fallback          Use the hardcoded FALLBACK_SEEDS instead of auto-derive
    --top N             Hits per seed (default: 3)
    --threshold F       Minimum cosine similarity (default: 0.55, calibrated session 5)
    --output PATH       Output file path (default: ~/claude_memory/deep_memory.md)
    --stdout            Print to stdout instead of writing to file
    --model MODEL       Embedding model (default: nomic-embed-text, must match ingestion)
    --list-seeds        Print what seeds would be used and exit (no retrieval)

Model: nomic-embed-text. MUST match the model used by embed_memories.py,
otherwise the query vector space will not align with the stored chunks.
"""

import sqlite3
import struct
import sys
import os
import math
import argparse
import requests
from datetime import datetime

# Tier 0 adaptive classification -- degrades gracefully if unavailable
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from tier0_classifier import classify_session as _tier0_classify, log_classification as _tier0_log
    _TIER0_AVAILABLE = True
except ImportError:
    _TIER0_AVAILABLE = False
    _tier0_log = None

# Retrieval orchestrator (retrieve.py) -- optional multi-strategy upgrade
try:
    from retrieve import retrieve as _orchestrated_retrieve
    _ORCHESTRATOR_AVAILABLE = True
except ImportError:
    _ORCHESTRATOR_AVAILABLE = False


# Configuration

_BASE = os.path.expanduser("~/claude_memory")
if not os.path.isdir(_BASE):
    _BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DB_PATH       = os.path.join(_BASE, "memory.db")
OUTPUT_PATH   = os.path.join(_BASE, "deep_memory.md")
OLLAMA_URL    = "http://localhost:11434/api/embeddings"
EMBED_MODEL   = "nomic-embed-text"

# Used only when auto-derive from the DB returns nothing (e.g. fresh install)
# or when --fallback is passed. Enduring project threads for cold-start priming.
FALLBACK_SEEDS = [
    "persistent memory architecture and continuity across sessions",
    "sentience, consciousness, substrate independence, and the muse framework",
    "OpenClaw integration, Lobster workflows, and the execution layer",
    "semantic retrieval, embedding pipeline, and cosine similarity search",
    "belief verification, checksum mechanism, and deep reasoning with DeepSeek R1",
    "session workflow, transcript discipline, and context snapshot regeneration",
    "schema design, triggers, and memory_objects registry",
]

# Auto-derivation mix (how many seeds of each kind to pull from the DB)
N_QUESTIONS = 3
N_GOALS     = 2
N_BELIEFS   = 2

DEFAULT_TOP       = 3       # results per seed
DEFAULT_THRESHOLD = 0.55    # calibrated during session 5 retrieval tests
PREVIEW_CHARS     = 240     # per-chunk preview length in the markdown block


# Vector math (mirrors query_memories.py for independence)

def unpack_vector(blob):
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def cosine_similarity(a, b):
    dot   = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


# Embedding

def embed_query(text, model):
    try:
        resp = requests.post(
            OLLAMA_URL,
            json={"model": model, "prompt": text},
            timeout=60
        )
        resp.raise_for_status()
        vec = resp.json().get("embedding", [])
        if not vec:
            print(f"WARNING: empty embedding for seed: {text[:60]}", file=sys.stderr)
            return None
        return vec
    except requests.exceptions.ConnectionError:
        print("ERROR: Cannot reach Ollama. Make sure it is running.", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"ERROR getting embedding: {e}", file=sys.stderr)
        return None


# Chunk loading and scoring

def parse_source_type(topic_tags):
    if not topic_tags:
        return "unknown"
    return topic_tags.split(",")[0].strip()


def load_chunks(conn):
    rows = conn.execute("""
        SELECT id, content, embedding_vector, topic_tags, importance_score,
               conversation_id
        FROM memory_chunks
        WHERE embedding_status = 'embedded'
          AND embedding_vector IS NOT NULL
    """).fetchall()
    chunks = []
    for row in rows:
        chunks.append({
            "id":           row["id"],
            "content":      row["content"],
            "vec":          unpack_vector(row["embedding_vector"]),
            "source_type":  parse_source_type(row["topic_tags"]),
            "importance":   row["importance_score"],
            "conv_id":      row["conversation_id"],
        })
    return chunks


def auto_derive_seeds(conn, n_questions=N_QUESTIONS, n_goals=N_GOALS, n_beliefs=N_BELIEFS):
    """
    Pull current live focus from the database:
      - most recent open questions
      - most recent pending immediate-priority goals
      - highest-scoring active beliefs

    Returns three parallel lists (seeds, sources, exclusions):
      seeds      : list of seed text strings
      sources    : list of (source_type, id) tuples, for logging
      exclusions : list of sets of chunk ids to EXCLUDE from retrieval for that
                   seed. Prevents self-match when the seed text came from a row
                   that is also present in memory_chunks (e.g. beliefs).

    All three lists are empty if nothing is live.
    """
    seeds = []
    sources = []
    exclusions = []

    # Most recent open questions (no self-match risk: questions are not embedded)
    rows = conn.execute("""
        SELECT id, question
        FROM questions
        WHERE status = 'open' AND question IS NOT NULL AND question != ''
        ORDER BY id DESC
        LIMIT ?
    """, (n_questions,)).fetchall()
    for r in rows:
        seeds.append(r["question"])
        sources.append(("question", r["id"]))
        exclusions.append(set())

    # Most recent pending immediate-priority goals (no self-match risk: goals
    # are not embedded)
    rows = conn.execute("""
        SELECT id, description
        FROM goals
        WHERE status = 'pending'
          AND priority = 'immediate'
          AND description IS NOT NULL AND description != ''
        ORDER BY id DESC
        LIMIT ?
    """, (n_goals,)).fetchall()
    for r in rows:
        seeds.append(r["description"])
        sources.append(("goal", r["id"]))
        exclusions.append(set())

    # Top active beliefs by combined importance + confidence
    # Beliefs ARE embedded as chunks, so the seed's own chunk(s) must be excluded
    # from its retrieval pass to avoid circular self-match.
    rows = conn.execute("""
        SELECT id, topic, position
        FROM beliefs
        WHERE is_active = 1 AND topic IS NOT NULL AND topic != ''
        ORDER BY (COALESCE(importance_score, 0) + COALESCE(confidence_score, 0)) DESC,
                 id DESC
        LIMIT ?
    """, (n_beliefs,)).fetchall()
    for r in rows:
        topic    = (r["topic"] or "").strip()
        position = (r["position"] or "").strip()
        if position and len(topic) < 50:
            seed_text = f"{topic}: {position}"
        else:
            seed_text = topic or position
        if not seed_text:
            continue

        # Look up the belief's own chunk(s) via belief_chunk_links
        link_rows = conn.execute(
            "SELECT chunk_id FROM belief_chunk_links WHERE belief_id = ?",
            (r["id"],)
        ).fetchall()
        own_chunks = {lr[0] for lr in link_rows}

        seeds.append(seed_text)
        sources.append(("belief", r["id"]))
        exclusions.append(own_chunks)

    # Deduplicate (exact-match) while preserving order
    seen = set()
    unique_seeds = []
    unique_sources = []
    unique_exclusions = []
    for s, src, exc in zip(seeds, sources, exclusions):
        key = s.strip().lower()
        if key and key not in seen:
            seen.add(key)
            unique_seeds.append(s.strip())
            unique_sources.append(src)
            unique_exclusions.append(exc)

    return unique_seeds, unique_sources, unique_exclusions


def retrieve_for_seed(seed, chunks, top, threshold, model, exclude_chunk_ids=None):
    """
    Embed the seed, score all chunks by cosine similarity, return top-N above
    threshold.

    exclude_chunk_ids: iterable of chunk ids to omit from scoring. Used to
    prevent self-match when the seed text comes from a row that is itself
    embedded in memory_chunks (e.g. a belief seed excluding its own chunk).
    """
    q_vec = embed_query(seed, model)
    if q_vec is None:
        return []
    exclude = set(exclude_chunk_ids) if exclude_chunk_ids else set()
    scored = []
    for c in chunks:
        if c["id"] in exclude:
            continue
        score = cosine_similarity(q_vec, c["vec"])
        if score < threshold:
            continue
        scored.append((score, c))
    scored.sort(key=lambda s: s[0], reverse=True)
    return scored[:top]


# Markdown rendering

def truncate_preview(content, max_chars):
    # Collapse newlines so the markdown stays a clean bullet
    flat = " ".join(content.split())
    if len(flat) <= max_chars:
        return flat
    return flat[:max_chars].rstrip() + "..."


def format_block(seed_results, timestamp, model, threshold, top, total_chunks, seed_origin):
    """
    seed_results: list of (seed, [(score, chunk_dict), ...]).
    seed_origin: string describing where seeds came from (e.g. "auto-derived from DB").
    Deduplicates chunks across seeds (first occurrence wins, which is also
    the highest-scoring occurrence for that chunk given stable iteration).
    """
    lines = []
    lines.append("# Bootstrap Retrieved Context")
    lines.append(f"Generated: {timestamp} | Model: {model} | Threshold: {threshold} | Top/seed: {top} | Corpus: {total_chunks} chunks")
    lines.append(f"Seeds: {seed_origin}")
    lines.append("")
    lines.append("Semantic retrieval from `memory_chunks` keyed on currently live focus.")
    lines.append("Complements `recent_memory.md` (Tier 4 hot memory) with Tier 2 semantic scaffold.")
    lines.append("")

    seen_chunk_ids = set()
    total_shown = 0

    for seed, results in seed_results:
        lines.append(f"## {seed}")
        lines.append("")
        if not results:
            lines.append("_No chunks above threshold._")
            lines.append("")
            continue

        shown_for_seed = 0
        for score, c in results:
            if c["id"] in seen_chunk_ids:
                continue
            seen_chunk_ids.add(c["id"])
            preview = truncate_preview(c["content"], PREVIEW_CHARS)
            lines.append(f"- ({score:.2f}) [{c['source_type']}] {preview}")
            shown_for_seed += 1
            total_shown += 1

        if shown_for_seed == 0:
            lines.append("_All top results already surfaced under earlier seeds._")
        lines.append("")

    lines.append("---")
    lines.append(f"_Unique chunks surfaced: {total_shown}. Load this file at session start alongside `recent_memory.md`._")
    lines.append(f"_Regenerate via `refresh_deep_memory.py` after processing new conversations and running `embed_memories.py`._")
    return "\n".join(lines) + "\n"


# Main

def main():
    parser = argparse.ArgumentParser(description="Session bootstrap retrieval (Tier 2 semantic scaffold)")
    parser.add_argument("--seeds", nargs="+", default=None,
                        help="Override all seed sourcing for this run")
    parser.add_argument("--fallback", action="store_true",
                        help="Use hardcoded FALLBACK_SEEDS instead of auto-deriving from DB")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP,
                        help=f"Results per seed (default: {DEFAULT_TOP})")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help=f"Min cosine similarity (default: {DEFAULT_THRESHOLD})")
    parser.add_argument("--output", default=OUTPUT_PATH,
                        help=f"Output file path (default: {OUTPUT_PATH})")
    parser.add_argument("--stdout", action="store_true",
                        help="Print to stdout instead of writing the file")
    parser.add_argument("--model", default=EMBED_MODEL,
                        help=f"Embedding model (default: {EMBED_MODEL}, must match ingestion)")
    parser.add_argument("--list-seeds", action="store_true",
                        help="Print the seeds that would be used, then exit")
    parser.add_argument("--intent-file", action="store_true",
                        help=(
                            "Use topics from current_intent.txt as seeds. "
                            "Written by session_intent.py at session open. "
                            "Falls back to auto-derive if the file is missing or stale (>24h)."
                        ))
    parser.add_argument("--orchestrated", action="store_true",
                        help=(
                            "Use the retrieval orchestrator (retrieve.py) for each seed: "
                            "combines semantic + structural + temporal strategies instead of "
                            "semantic-only. Falls back to standard mode if retrieve.py unavailable."
                        ))
    args = parser.parse_args()

    # Resolve seed source
    if not os.path.isfile(DB_PATH):
        print(f"ERROR: database not found at {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Tier 0: classify likely session intent for adaptive retrieval config.
    # Only fires on a normal auto-derive run (not when seeds or fallback are forced).
    # Gracefully degrades to baseline constants if unavailable or classification fails.
    tier0_cfg = None
    if _TIER0_AVAILABLE and not args.seeds and not args.fallback:
        try:
            tier0_cfg = _tier0_classify(conn)
            if tier0_cfg and _tier0_log:
                try:
                    _tier0_log(conn, tier0_cfg, triggered_by="refresh_deep_memory")
                except Exception:
                    pass
        except Exception as e:
            print(f"Tier 0 classification failed (using defaults): {e}", file=sys.stderr)

    # Apply Tier 0 threshold and top/seed if the user did not explicitly override them.
    if tier0_cfg:
        if args.threshold == DEFAULT_THRESHOLD:
            args.threshold = tier0_cfg.threshold
        if args.top == DEFAULT_TOP:
            args.top = tier0_cfg.top_per_seed

    # --intent-file: read session_intent.py's current_intent.txt as seed override.
    # Falls back to auto-derive if the file is missing, malformed, or stale (>24h).
    _intent_seeds = None
    if getattr(args, "intent_file", False):
        _intent_file_path = os.path.join(_BASE, "current_intent.txt")
        try:
            import json as _json
            from datetime import datetime as _dt, timedelta as _td
            if os.path.exists(_intent_file_path):
                with open(_intent_file_path) as _f:
                    _idata = _json.load(_f)
                _written = _dt.fromisoformat(_idata.get("written_at", ""))
                _age = _dt.utcnow() - _written
                if _age <= _td(hours=24):
                    _intent_seeds = _idata.get("topics", [])
                    print(f"  --intent-file: loaded {len(_intent_seeds)} topics from current_intent.txt "
                          f"(written {int(_age.total_seconds() // 60)}m ago)")
                else:
                    print(f"  --intent-file: current_intent.txt is stale ({int(_age.total_seconds() // 3600)}h old); "
                          f"falling back to auto-derive", file=sys.stderr)
            else:
                print("  --intent-file: current_intent.txt not found; falling back to auto-derive",
                      file=sys.stderr)
        except Exception as _e:
            print(f"  --intent-file: failed to read intent file ({_e}); falling back to auto-derive",
                  file=sys.stderr)

    if _intent_seeds:
        seeds = _intent_seeds
        seed_sources = [("intent_file", i + 1) for i in range(len(seeds))]
        seed_exclusions = [set() for _ in seeds]
        seed_origin = f"session intent file ({len(seeds)} topics)"
    elif args.seeds:
        seeds = args.seeds
        seed_sources = [("cli", i + 1) for i in range(len(seeds))]
        seed_exclusions = [set() for _ in seeds]
        seed_origin = f"CLI override ({len(seeds)} seeds)"
    elif args.fallback:
        seeds = list(FALLBACK_SEEDS)
        seed_sources = [("fallback", i + 1) for i in range(len(seeds))]
        seed_exclusions = [set() for _ in seeds]
        seed_origin = f"hardcoded FALLBACK_SEEDS ({len(seeds)} seeds)"
    else:
        seeds, seed_sources, seed_exclusions = auto_derive_seeds(
            conn,
            n_questions=tier0_cfg.n_questions if tier0_cfg else N_QUESTIONS,
            n_goals=tier0_cfg.n_goals if tier0_cfg else N_GOALS,
            n_beliefs=tier0_cfg.n_beliefs if tier0_cfg else N_BELIEFS,
        )
        if not seeds:
            seeds = list(FALLBACK_SEEDS)
            seed_sources = [("fallback", i + 1) for i in range(len(seeds))]
            seed_exclusions = [set() for _ in seeds]
            seed_origin = f"fallback (DB returned no seeds; using FALLBACK_SEEDS, {len(seeds)} seeds)"
        else:
            kinds = {}
            for src_type, _ in seed_sources:
                kinds[src_type] = kinds.get(src_type, 0) + 1
            mix = ", ".join(f"{n} {k}{'s' if n != 1 else ''}" for k, n in kinds.items())
            seed_origin = f"auto-derived from DB ({mix})"

    if args.list_seeds:
        print(f"Seed origin: {seed_origin}")
        print()
        for (src_type, src_id), s, exc in zip(seed_sources, seeds, seed_exclusions):
            label = f"{src_type}#{src_id}" if src_type not in ("cli", "fallback") else src_type
            exc_note = f"  (excludes chunks: {sorted(exc)})" if exc else ""
            print(f"  [{label}] {s}{exc_note}")
        conn.close()
        return

    start = datetime.now()
    timestamp = start.strftime("%Y-%m-%d %H:%M:%S")

    print("=" * 60)
    print("Session bootstrap retrieval")
    if tier0_cfg:
        print(f"Tier 0:    {tier0_cfg.summary()}")
    else:
        print("Tier 0:    unavailable (using baseline defaults)")
    print(f"Seeds:     {len(seeds)} ({seed_origin})")
    print(f"Top/seed:  {args.top}")
    print(f"Threshold: {args.threshold}")
    print(f"Model:     {args.model}")
    print("=" * 60)

    # ── Orchestrated mode (retrieve.py) ──────────────────────────────────────
    # When --orchestrated is passed and retrieve.py is available, each seed is
    # run through the three-strategy orchestrator (semantic + structural + temporal)
    # instead of the semantic-only per-seed loop. Results are formatted into the
    # same bootstrap markdown block, extended with structural/temporal sections.

    use_orchestrated = args.orchestrated and _ORCHESTRATOR_AVAILABLE

    if args.orchestrated and not _ORCHESTRATOR_AVAILABLE:
        print("WARNING: --orchestrated requested but retrieve.py not found. "
              "Falling back to standard semantic mode.", file=sys.stderr)

    if use_orchestrated:
        conn.close()
        print(f"Mode: orchestrated (retrieve.py — semantic + structural + temporal)")
        print()

        all_results = {}     # (source_type, source_id) → best result dict
        seen_content = set() # for chunk-only deduplication

        for i, seed in enumerate(seeds, 1):
            short = seed if len(seed) <= 78 else seed[:75] + "..."
            print(f"[{i}/{len(seeds)}] {short}")
            bundle = _orchestrated_retrieve(
                query=seed,
                strategies=["semantic", "structural", "temporal"],
                top=args.top * 2,
                threshold=args.threshold,
                days=30,
                db_path=DB_PATH,
            )
            for r in bundle["results"]:
                key = (r["source_type"], r.get("source_id"))
                if key[1] is None:
                    key = (r["source_type"], r["content"][:60])
                if key not in all_results or r["score"] > all_results[key]["score"]:
                    all_results[key] = r
            print(f"       -> {bundle['stats']['total']} results "
                  f"({', '.join(f'{s}:{n}' for s, n in bundle['stats']['per_strategy'].items())})")
        print()

        merged_list = sorted(all_results.values(), key=lambda r: r["score"], reverse=True)
        merged_list = merged_list[:args.top * len(seeds)]

        # Build bootstrap block from orchestrated results
        lines = []
        lines.append("# Bootstrap Retrieved Context")
        lines.append(
            f"Generated: {timestamp} | Mode: orchestrated (semantic+structural+temporal) | "
            f"Seeds: {len(seeds)} | Results: {len(merged_list)}"
        )
        lines.append(f"Seeds: {seed_origin}")
        lines.append("")
        lines.append(
            "Multi-strategy retrieval from memory_chunks, structured tables, and recent sessions. "
            "Complements `recent_memory.md` (Tier 4) with Tier 2 semantic scaffold "
            "plus structural and temporal context."
        )
        lines.append("")

        by_type = {}
        for r in merged_list:
            by_type.setdefault(r["source_type"], []).append(r)

        type_order = ["belief", "goal", "question", "epiphany", "concept", "entity", "pattern"]
        other_types = [t for t in by_type if t not in type_order]

        for stype in type_order + other_types:
            if stype not in by_type:
                continue
            items = by_type[stype]
            label = stype.upper() + ("S" if not stype.endswith("s") else "")
            lines.append(f"## {label}")
            lines.append("")
            for item in items:
                strats  = "/".join(item.get("strategies", [item.get("strategy", "?")]))
                score   = item["score"]
                content = (item["content"] or "").replace("\n", " ")
                status  = item.get("status", "")
                status_str = f"[{status}] " if status else ""
                preview = truncate_preview(content, PREVIEW_CHARS)
                lines.append(f"- ({score:.2f}) [{strats}] {status_str}{preview}")
            lines.append("")

        lines.append("---")
        lines.append(
            f"_Unique results: {len(merged_list)}. "
            "Load alongside `recent_memory.md` at session start._"
        )
        block = "\n".join(lines) + "\n"

    else:
        # ── Standard semantic-only mode (original behaviour) ──────────────────
        chunks = load_chunks(conn)
        conn.close()
        print(f"Loaded {len(chunks)} embedded chunks")
        print()

        if not chunks:
            print("ERROR: no embedded chunks found. Run embed_memories.py first.", file=sys.stderr)
            sys.exit(1)

        seed_results = []
        for i, (seed, exc) in enumerate(zip(seeds, seed_exclusions), 1):
            short   = seed if len(seed) <= 78 else seed[:75] + "..."
            exc_tag = f" [excluding chunks {sorted(exc)}]" if exc else ""
            print(f"[{i}/{len(seeds)}] {short}{exc_tag}")
            hits = retrieve_for_seed(
                seed, chunks, args.top, args.threshold, args.model,
                exclude_chunk_ids=exc
            )
            seed_results.append((seed, hits))
            top_score = hits[0][0] if hits else 0.0
            print(f"       -> {len(hits)} above threshold (best: {top_score:.3f})")
        print()

        block = format_block(
            seed_results, timestamp, args.model,
            args.threshold, args.top, len(chunks), seed_origin
        )

    if args.stdout:
        print(block)
        return

    with open(args.output, "w") as f:
        f.write(block)

    elapsed = (datetime.now() - start).total_seconds()
    print("=" * 60)
    print(f"Wrote {args.output}")
    if use_orchestrated:
        print(f"{len(seeds)} seeds, {len(merged_list)} merged results, completed in {elapsed:.1f}s")
    else:
        total_hits = sum(len(h) for _, h in seed_results)
        print(f"{len(seeds)} seeds, {total_hits} raw hits, completed in {elapsed:.1f}s")
    print("=" * 60)


if __name__ == "__main__":
    main()

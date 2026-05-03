#!/usr/bin/env python3
"""
query_memories.py
-----------------
Semantic search over memory_chunks using cosine similarity.

Embeds your query with nomic-embed-text (same model used during ingestion),
then ranks all stored chunks by similarity and prints the top results.

Usage:
    python3 ~/claude_memory/scripts/query_memories.py "substrate independence"
    python3 ~/claude_memory/scripts/query_memories.py "how does memory work" --top 10
    python3 ~/claude_memory/scripts/query_memories.py "sentience" --type belief
    python3 ~/claude_memory/scripts/query_memories.py "sentience" --threshold 0.7
    python3 ~/claude_memory/scripts/query_memories.py "parallel universes" --full

Options:
    --top N          Return top N results (default: 5)
    --type TYPE      Filter by source type: belief | epiphany | concept | pattern
    --threshold F    Minimum cosine similarity score (0.0–1.0, default: 0.0)
    --full           Print full content instead of 120-char preview
    --model MODEL    Override embedding model (default: nomic-embed-text)
"""

import sqlite3
import struct
import sys
import os
import math
import argparse
import requests

# ── Configuration ──────────────────────────────────────────────────────────────

_BASE = os.path.expanduser("~/claude_memory")
if not os.path.isdir(_BASE):
    _BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DB_PATH     = os.path.join(_BASE, "memory.db")
OLLAMA_URL  = "http://localhost:11434/api/embeddings"
EMBED_MODEL = "nomic-embed-text"


# ── Vector math ─────────────────────────────────────────────────────────────────

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


# ── Embedding ───────────────────────────────────────────────────────────────────

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
            print("ERROR: empty embedding returned")
            sys.exit(1)
        return vec
    except requests.exceptions.ConnectionError:
        print("ERROR: Cannot reach Ollama. Make sure it is running.")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR getting embedding: {e}")
        sys.exit(1)


# ── Source type extraction ──────────────────────────────────────────────────────

def parse_source_type(topic_tags):
    """Extract source type from topic_tags field (format: 'belief,tag1,tag2')."""
    if not topic_tags:
        return "unknown"
    return topic_tags.split(",")[0].strip()


def parse_display_tags(topic_tags):
    """Return the tags portion after the source type prefix."""
    if not topic_tags:
        return ""
    parts = topic_tags.split(",", 1)
    return parts[1].strip() if len(parts) > 1 else ""


# ── Main ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Semantic search over memory_chunks"
    )
    parser.add_argument("query", help="Natural language query string")
    parser.add_argument("--top",       type=int,   default=5,    metavar="N",
                        help="Number of results to return (default: 5)")
    parser.add_argument("--type",      choices=["belief", "epiphany", "concept", "pattern"],
                        dest="filter_type", default=None,
                        help="Filter results by source type")
    parser.add_argument("--threshold", type=float, default=0.0,  metavar="F",
                        help="Minimum cosine similarity (default: 0.0)")
    parser.add_argument("--full",      action="store_true",
                        help="Print full content instead of preview")
    parser.add_argument("--model",     default=EMBED_MODEL,
                        help=f"Embedding model (default: {EMBED_MODEL})")
    args = parser.parse_args()

    print(f'\nQuery: "{args.query}"')
    print(f"Model: {args.model}  |  Top: {args.top}  |  Threshold: {args.threshold}")
    if args.filter_type:
        print(f"Filter: {args.filter_type} only")
    print()

    # Embed the query
    print("Embedding query...", end=" ", flush=True)
    q_vec = embed_query(args.query, args.model)
    print(f"done ({len(q_vec)}d)")

    # Load all chunks
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT id, content, embedding_vector, topic_tags, importance_score,
               conversation_id, created_at
        FROM memory_chunks
        WHERE embedding_status = 'embedded'
          AND embedding_vector IS NOT NULL
    """).fetchall()
    conn.close()

    print(f"Loaded {len(rows)} embedded chunks")

    # Score each chunk
    results = []
    for row in rows:
        source_type = parse_source_type(row["topic_tags"])

        # Apply type filter
        if args.filter_type and source_type != args.filter_type:
            continue

        chunk_vec = unpack_vector(row["embedding_vector"])
        score     = cosine_similarity(q_vec, chunk_vec)

        if score < args.threshold:
            continue

        results.append({
            "id":           row["id"],
            "score":        score,
            "source_type":  source_type,
            "tags":         parse_display_tags(row["topic_tags"]),
            "content":      row["content"],
            "importance":   row["importance_score"],
            "conv_id":      row["conversation_id"],
        })

    # Sort by score descending
    results.sort(key=lambda r: r["score"], reverse=True)
    top = results[:args.top]

    if not top:
        print("\nNo results found.")
        return

    print(f"\n{'='*60}")
    print(f"Top {len(top)} results")
    print(f"{'='*60}\n")

    for i, r in enumerate(top, 1):
        score_bar = "█" * int(r["score"] * 20)
        print(f"[{i}] {r['source_type'].upper()}  |  score: {r['score']:.4f}  {score_bar}")
        if r["tags"]:
            print(f"    tags: {r['tags']}")
        if r["conv_id"]:
            print(f"    conversation_id: {r['conv_id']}")

        content = r["content"]
        if not args.full:
            preview = content.replace("\n", " ")[:120]
            if len(content) > 120:
                preview += "..."
            print(f"    {preview}")
        else:
            for line in content.split("\n"):
                print(f"    {line}")

        print()

    print(f"{'='*60}")
    print(f"Showing {len(top)} of {len(results)} results above threshold {args.threshold}")


if __name__ == "__main__":
    main()

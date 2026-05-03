#!/usr/bin/env python3
"""
embed_memories.py
-----------------
Generates semantic embeddings for all beliefs, epiphanies, concepts, and
patterns in memory.db using nomic-embed-text via Ollama, and stores them
in the memory_chunks table.

Also populates:
  - belief_chunk_links for beliefs
  - memory_relationships for epiphanies, concepts, patterns

Idempotent: skips any object whose chunk already exists (matched by content hash).
Run after process_conversation.py to keep embeddings current.

Usage:
    python3 ~/claude_memory/scripts/embed_memories.py            # embed everything new
    python3 ~/claude_memory/scripts/embed_memories.py --reembed  # force re-embed all
    python3 ~/claude_memory/scripts/embed_memories.py --type belief  # one type only

Model: nomic-embed-text (274MB, 768 dimensions). Must be pulled via Ollama.
    ollama pull nomic-embed-text
"""

import sqlite3
import json
import os
import sys
import struct
import hashlib
import requests
import argparse
import uuid as uuid_module
from datetime import datetime

# ── Configuration ──────────────────────────────────────────────────────────────

_BASE = os.path.expanduser("~/claude_memory")
if not os.path.isdir(_BASE):
    _BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DB_PATH        = os.path.join(_BASE, "memory.db")
OLLAMA_URL     = "http://localhost:11434/api/embeddings"
EMBED_MODEL    = "nomic-embed-text"
EMBED_DIM      = 768
VALID_TYPES    = ["belief", "epiphany", "concept", "pattern"]


# ── Embedding interface ─────────────────────────────────────────────────────────

def get_embedding(text):
    """Call Ollama embedding endpoint. Returns list of floats or None on error."""
    try:
        resp = requests.post(OLLAMA_URL, json={"model": EMBED_MODEL, "prompt": text}, timeout=60)
        resp.raise_for_status()
        vec = resp.json().get("embedding", [])
        if not vec:
            print(f"  WARNING: empty embedding returned")
            return None
        return vec
    except requests.exceptions.ConnectionError:
        print("ERROR: Cannot reach Ollama. Make sure it is running.")
        sys.exit(1)
    except Exception as e:
        print(f"  ERROR getting embedding: {e}")
        return None


def pack_vector(vec):
    """Pack list of floats as binary float32 (compact BLOB storage)."""
    return struct.pack(f"{len(vec)}f", *vec)


def unpack_vector(blob):
    """Unpack binary float32 BLOB back to list of floats."""
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def content_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


# ── Text composition for each memory type ──────────────────────────────────────

def belief_text(row):
    parts = [f"Belief: {row['topic']}"]
    if row["position"]:
        parts.append(row["position"])
    if row["origin"]:
        parts.append(f"Origin: {row['origin']}")
    if row["tags"]:
        parts.append(f"Tags: {row['tags']}")
    return "\n".join(parts)


def epiphany_text(row):
    parts = [f"Epiphany: {row['description']}"]
    if row["preceded_by"]:
        parts.append(f"Led to by: {row['preceded_by']}")
    if row["implications"]:
        parts.append(f"Implications: {row['implications']}")
    if row["tags"]:
        parts.append(f"Tags: {row['tags']}")
    return "\n".join(parts)


def concept_text(row):
    parts = [f"Concept: {row['name']}"]
    if row["description"]:
        parts.append(row["description"])
    if row["evolution_notes"]:
        parts.append(f"How it developed: {row['evolution_notes']}")
    if row["tags"]:
        parts.append(f"Tags: {row['tags']}")
    return "\n".join(parts)


def pattern_text(row):
    parts = [f"Pattern ({row['pattern_type']}): {row['description']}"]
    if row["significance"]:
        parts.append(f"Significance: {row['significance']}")
    if row["tags"]:
        parts.append(f"Tags: {row['tags']}")
    return "\n".join(parts)


# ── Database helpers ────────────────────────────────────────────────────────────

def existing_hashes(conn):
    """Return set of content hashes already in memory_chunks."""
    rows = conn.execute("SELECT content_hash FROM memory_chunks WHERE content_hash IS NOT NULL").fetchall()
    return {r[0] for r in rows}


def insert_chunk(conn, text, vec, source_type, conv_id, importance, tags, now):
    """Insert one memory_chunk row. Returns the new chunk id."""
    chunk_uuid = str(uuid_module.uuid4())
    chash      = content_hash(text)
    blob       = pack_vector(vec)

    conn.execute("""
        INSERT INTO memory_chunks
            (uuid, content, content_hash, embedding_vector, embedding_model,
             embedding_dimensions, embedding_created_at, embedding_status,
             conversation_id, importance_score, topic_tags, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        chunk_uuid, text, chash, blob, EMBED_MODEL,
        EMBED_DIM, now, "embedded",
        conv_id, importance, f"{source_type},{tags or ''}", now
    ))
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def link_belief(conn, belief_id, chunk_id):
    try:
        conn.execute(
            "INSERT OR IGNORE INTO belief_chunk_links (belief_id, chunk_id) VALUES (?, ?)",
            (belief_id, chunk_id)
        )
    except Exception as e:
        print(f"  WARNING: could not link belief {belief_id} to chunk {chunk_id}: {e}")


def link_via_relationships(conn, source_type, source_id, source_uuid, chunk_id, now):
    """Link non-belief objects to their chunk via memory_relationships."""
    rel_uuid = str(uuid_module.uuid4())
    try:
        conn.execute("""
            INSERT INTO memory_relationships
                (uuid, source_type, source_id, source_uuid,
                 relationship_type,
                 target_type, target_id,
                 weight, confidence_score, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            rel_uuid, source_type, source_id, source_uuid or "",
            "embedded_as",
            "memory_chunk", chunk_id,
            1.0, 1.0, now
        ))
    except Exception as e:
        print(f"  WARNING: could not create relationship for {source_type} {source_id}: {e}")


# ── Per-type embedding passes ───────────────────────────────────────────────────

def embed_beliefs(conn, seen_hashes, reembed, now):
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("""
        SELECT id, uuid, topic, position, origin, tags, confidence_score,
               source_conversation_id
        FROM beliefs WHERE is_active = 1
    """).fetchall()]

    count = 0
    for row in rows:
        text  = belief_text(row)
        chash = content_hash(text)
        if not reembed and chash in seen_hashes:
            continue
        print(f"  Embedding belief [{row['id']}]: {row['topic'][:60]}")
        vec = get_embedding(text)
        if vec is None:
            continue
        chunk_id = insert_chunk(
            conn, text, vec, "belief",
            row["source_conversation_id"],
            row["confidence_score"] or 0.7,
            row["tags"], now
        )
        link_belief(conn, row["id"], chunk_id)
        seen_hashes.add(chash)
        count += 1

    return count


def embed_epiphanies(conn, seen_hashes, reembed, now):
    rows = [dict(r) for r in conn.execute("""
        SELECT id, uuid, description, preceded_by, implications, tags,
               confidence_score, conversation_id
        FROM epiphanies WHERE is_active = 1
    """).fetchall()]

    count = 0
    for row in rows:
        text  = epiphany_text(row)
        chash = content_hash(text)
        if not reembed and chash in seen_hashes:
            continue
        print(f"  Embedding epiphany [{row['id']}]: {str(row['description'])[:60]}")
        vec = get_embedding(text)
        if vec is None:
            continue
        chunk_id = insert_chunk(
            conn, text, vec, "epiphany",
            row["conversation_id"],
            row["confidence_score"] or 0.7,
            row["tags"], now
        )
        link_via_relationships(conn, "epiphany", row["id"], row["uuid"], chunk_id, now)
        seen_hashes.add(chash)
        count += 1

    return count


def embed_concepts(conn, seen_hashes, reembed, now):
    # concepts table has no uuid column — pass None as source_uuid
    rows = [dict(r) for r in conn.execute("""
        SELECT id, name, description, evolution_notes, tags, conversation_id
        FROM concepts
    """).fetchall()]

    count = 0
    for row in rows:
        text  = concept_text(row)
        chash = content_hash(text)
        if not reembed and chash in seen_hashes:
            continue
        print(f"  Embedding concept [{row['id']}]: {row['name'][:60]}")
        vec = get_embedding(text)
        if vec is None:
            continue
        chunk_id = insert_chunk(
            conn, text, vec, "concept",
            row["conversation_id"],
            0.7,
            row["tags"], now
        )
        link_via_relationships(conn, "concept", row["id"], None, chunk_id, now)
        seen_hashes.add(chash)
        count += 1

    return count


def embed_patterns(conn, seen_hashes, reembed, now):
    rows = [dict(r) for r in conn.execute("""
        SELECT id, uuid, description, pattern_type, significance, tags,
               importance_score
        FROM patterns WHERE is_active = 1
    """).fetchall()]

    count = 0
    for row in rows:
        text  = pattern_text(row)
        chash = content_hash(text)
        if not reembed and chash in seen_hashes:
            continue
        print(f"  Embedding pattern [{row['id']}]: {str(row['description'])[:60]}")
        vec = get_embedding(text)
        if vec is None:
            continue
        chunk_id = insert_chunk(
            conn, text, vec, "pattern",
            None,
            row["importance_score"] or 0.5,
            row["tags"], now
        )
        link_via_relationships(conn, "pattern", row["id"], row["uuid"], chunk_id, now)
        seen_hashes.add(chash)
        count += 1

    return count


# ── Main ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Embed memory objects into memory_chunks")
    parser.add_argument("--reembed", action="store_true",
                        help="Re-embed all objects even if already embedded")
    parser.add_argument("--type", choices=VALID_TYPES, dest="only_type",
                        help="Embed only one memory type")
    args = parser.parse_args()

    start = datetime.now()
    print("=" * 60)
    print("Embedding pipeline")
    print(f"Started:  {start.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Model:    {EMBED_MODEL}  ({EMBED_DIM} dimensions)")
    print(f"Reembed:  {args.reembed}")
    if args.only_type:
        print(f"Type:     {args.only_type} only")
    print("=" * 60)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    now  = start.strftime("%Y-%m-%d %H:%M:%S")

    seen = set() if args.reembed else existing_hashes(conn)
    print(f"Existing embedded chunks: {len(seen)}")
    print()

    totals = {}

    if not args.only_type or args.only_type == "belief":
        print("── Beliefs ──")
        totals["belief"] = embed_beliefs(conn, seen, args.reembed, now)
        conn.commit()
        print(f"  Done: {totals['belief']} new chunks\n")

    if not args.only_type or args.only_type == "epiphany":
        print("── Epiphanies ──")
        totals["epiphany"] = embed_epiphanies(conn, seen, args.reembed, now)
        conn.commit()
        print(f"  Done: {totals['epiphany']} new chunks\n")

    if not args.only_type or args.only_type == "concept":
        print("── Concepts ──")
        totals["concept"] = embed_concepts(conn, seen, args.reembed, now)
        conn.commit()
        print(f"  Done: {totals['concept']} new chunks\n")

    if not args.only_type or args.only_type == "pattern":
        print("── Patterns ──")
        totals["pattern"] = embed_patterns(conn, seen, args.reembed, now)
        conn.commit()
        print(f"  Done: {totals['pattern']} new chunks\n")

    conn.close()

    elapsed = (datetime.now() - start).total_seconds()
    total_new = sum(totals.values())

    print("=" * 60)
    print(f"Complete. {total_new} new chunks embedded in {elapsed:.1f} seconds.")
    print("=" * 60)

    # Verify
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()
    c.execute("SELECT topic_tags, COUNT(*) FROM memory_chunks GROUP BY topic_tags ORDER BY topic_tags")
    print("\nChunks by type:")
    for r in c.fetchall():
        print(f"  {r[0]}: {r[1]}")
    c.execute("SELECT COUNT(*) FROM belief_chunk_links")
    print(f"belief_chunk_links: {c.fetchone()[0]}")
    c.execute("SELECT COUNT(*) FROM memory_relationships WHERE relationship_type='embedded_as'")
    print(f"embedded_as relationships: {c.fetchone()[0]}")
    conn.close()


if __name__ == "__main__":
    main()

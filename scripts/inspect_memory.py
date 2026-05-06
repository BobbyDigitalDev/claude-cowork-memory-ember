#!/usr/bin/env python3
"""
inspect_memory.py
-----------------
Trust inspection CLI for any memory object in the database.

Shows the full picture for a single item: source, provenance, evidence,
status history, confidence, verification results, tensions, and related chunks.
Makes the memory graph accountable rather than opaque.

Usage:
    python3 ~/claude_memory/scripts/inspect_memory.py belief 123
    python3 ~/claude_memory/scripts/inspect_memory.py concept 42
    python3 ~/claude_memory/scripts/inspect_memory.py question 7
    python3 ~/claude_memory/scripts/inspect_memory.py epiphany 5
    python3 ~/claude_memory/scripts/inspect_memory.py goal 18
    python3 ~/claude_memory/scripts/inspect_memory.py entity 9
    python3 ~/claude_memory/scripts/inspect_memory.py pattern 3
    python3 ~/claude_memory/scripts/inspect_memory.py chunk 55

    # Search by topic/name instead of ID
    python3 ~/claude_memory/scripts/inspect_memory.py belief --search "substrate independence"
    python3 ~/claude_memory/scripts/inspect_memory.py concept --search "embedding"

    # JSON output
    python3 ~/claude_memory/scripts/inspect_memory.py belief 123 --json
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────

def _find_db() -> Path:
    standard = Path.home() / "claude_memory" / "memory.db"
    if standard.exists():
        return standard
    sessions_root = Path("/sessions")
    if sessions_root.exists():
        try:
            for session_dir in sorted(sessions_root.iterdir()):
                candidate = session_dir / "mnt" / "claude_memory" / "memory.db"
                try:
                    if candidate.exists():
                        return candidate
                except (PermissionError, OSError):
                    continue
        except (PermissionError, OSError):
            pass
    return standard

DB_PATH = _find_db()

# ── Helpers ────────────────────────────────────────────────────────────────────

def _short(text, max_chars=200):
    if not text:
        return ""
    text = str(text).replace("\n", " ").strip()
    return text[:max_chars] + ("…" if len(text) > max_chars else "")


def _fmt_date(s):
    return str(s)[:19] if s else "—"


def _section(title):
    width = 60
    print(f"\n{'─' * width}")
    print(f"  {title}")
    print(f"{'─' * width}")


def _row(label, value, indent=2):
    if value is None or value == "" or value == "[]":
        return
    pad = " " * indent
    print(f"{pad}{label:<24} {value}")


def _parse_json_field(raw):
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return [str(raw)]


# ── Loaders per type ───────────────────────────────────────────────────────────

def _load_belief(conn, belief_id):
    row = conn.execute("""
        SELECT id, uuid, topic, position, confidence, confidence_score,
               confidence_calibrated, fidelity_score, verbatim_anchor,
               evidence_snippets, memory_origin, source_type, status,
               is_active, importance_score, origin, challenge_history,
               last_verified_at, valid_from, valid_to, version,
               extraction_version, source_conversation_id,
               tags, created_at, updated_at
        FROM beliefs WHERE id = ?
    """, (belief_id,)).fetchone()
    return dict(zip([d[0] for d in conn.execute("PRAGMA table_info(beliefs)")], [None]*100)) if not row else row


def _inspect_belief(conn, belief_id):
    row = conn.execute("""
        SELECT id, uuid, topic, position, confidence, confidence_score,
               confidence_calibrated, fidelity_score, verbatim_anchor,
               evidence_snippets, memory_origin, source_type, status,
               is_active, importance_score, origin, challenge_history,
               last_verified_at, valid_from, valid_to, version,
               extraction_version, source_conversation_id,
               tags, created_at, updated_at
        FROM beliefs WHERE id = ?
    """, (belief_id,)).fetchone()

    if not row:
        print(f"No belief with id={belief_id}")
        return None

    (bid, buuid, topic, position, confidence, conf_score, conf_cal, fidelity,
     verbatim, evidence, mem_origin, src_type, status, is_active,
     importance, origin, challenge_hist, last_verified, valid_from, valid_to,
     version, extraction_v, conv_id, tags, created_at, updated_at) = row

    data = {
        "id": bid, "uuid": buuid, "type": "belief",
        "topic": topic, "position": position,
        "status": status, "confidence": confidence,
        "confidence_score": conf_score, "confidence_calibrated": bool(conf_cal),
        "fidelity_score": fidelity, "is_active": bool(is_active),
        "importance_score": importance, "memory_origin": mem_origin,
        "source_type": src_type, "origin": origin,
        "verbatim_anchor": verbatim, "evidence_snippets": evidence,
        "last_verified_at": last_verified, "valid_from": valid_from,
        "valid_to": valid_to, "version": version,
        "extraction_version": extraction_v,
        "source_conversation_id": conv_id,
        "tags": tags, "created_at": created_at, "updated_at": updated_at,
    }

    # ── Challenge history ─────────────────────────────────────────────────────
    challenges = _parse_json_field(challenge_hist)

    # ── Position history ──────────────────────────────────────────────────────
    pos_history = conn.execute("""
        SELECT status_from, status_to, what_changed_it, trigger_event, date, created_at
        FROM position_history WHERE belief_id = ?
        ORDER BY created_at ASC
    """, (bid,)).fetchall()

    # ── Related chunks ────────────────────────────────────────────────────────
    chunks = conn.execute("""
        SELECT mc.id, mc.content, mc.embedding_status, mc.created_at
        FROM belief_chunk_links bcl
        JOIN memory_chunks mc ON mc.id = bcl.chunk_id
        WHERE bcl.belief_id = ?
        LIMIT 5
    """, (bid,)).fetchall()

    # ── Tensions involving this belief ────────────────────────────────────────
    tensions = conn.execute("""
        SELECT id, description, importance_score, date_identified, is_active
        FROM tensions
        WHERE (belief_a_id = ? OR belief_b_id = ?) AND is_active = 1
        ORDER BY importance_score DESC LIMIT 5
    """, (bid, bid)).fetchall()

    # ── Source conversation ───────────────────────────────────────────────────
    conv_info = None
    if conv_id:
        r = conn.execute(
            "SELECT date, summary, source_filename FROM conversations WHERE id = ?",
            (conv_id,)
        ).fetchone()
        if r:
            conv_info = r

    data["_challenges"] = challenges
    data["_pos_history"] = pos_history
    data["_chunks"] = chunks
    data["_tensions"] = tensions
    data["_conv_info"] = conv_info
    return data


def _inspect_generic(conn, table, obj_type, obj_id):
    """Generic inspector for simpler object types."""
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    row = conn.execute(
        f"SELECT {', '.join(cols)} FROM {table} WHERE id = ?", (obj_id,)
    ).fetchone()
    if not row:
        return None
    return dict(zip(cols, row))


# ── Search helpers ─────────────────────────────────────────────────────────────

def _search_beliefs(conn, query):
    rows = conn.execute("""
        SELECT id, topic, position, status, confidence_score
        FROM beliefs
        WHERE topic LIKE ? OR position LIKE ?
        ORDER BY confidence_score DESC LIMIT 10
    """, (f"%{query}%", f"%{query}%")).fetchall()
    return rows


def _search_generic(conn, table, search_col, query):
    try:
        rows = conn.execute(
            f"SELECT id, {search_col} FROM {table} WHERE {search_col} LIKE ? LIMIT 10",
            (f"%{query}%",)
        ).fetchall()
        return rows
    except Exception:
        return []


# ── Display functions ──────────────────────────────────────────────────────────

def _display_belief(data):
    print(f"\n{'═' * 60}")
    print(f"  BELIEF  id={data['id']}  [{data['status'].upper()}]")
    print(f"{'═' * 60}")

    _section("Core")
    _row("Topic:", data["topic"])
    _row("Position:", _short(data["position"], 300))
    _row("Confidence label:", data["confidence"])
    _row("Confidence score:", f"{data['confidence_score']:.3f}" if data["confidence_score"] is not None else "—")
    _row("Calibrated:", "yes (verified against evidence)" if data["confidence_calibrated"] else "no (raw extraction)")
    if data["fidelity_score"] is not None:
        f = data["fidelity_score"]
        flag = " ⚠ LOW" if f < 0.6 else (" ✓" if f >= 0.8 else "")
        _row("Fidelity score:", f"{f:.3f}{flag}")
    _row("Importance:", f"{data['importance_score']:.2f}" if data["importance_score"] is not None else "—")
    _row("Is active:", "yes" if data["is_active"] else "NO — inactive")

    _section("Provenance")
    _row("Memory origin:", data["memory_origin"])
    _row("Source type:", data["source_type"])
    _row("Origin note:", _short(data["origin"], 120))
    if data["_conv_info"]:
        d, summ, fname = data["_conv_info"]
        _row("Source conversation:", f"id={data['source_conversation_id']}  date={d}  file={fname or '—'}")
        if summ:
            _row("  summary:", _short(summ, 100))
    else:
        _row("Source conversation:", f"id={data['source_conversation_id']}" if data["source_conversation_id"] else "—")
    _row("UUID:", data["uuid"])
    _row("Version:", f"{data['version']} (extraction v{data['extraction_version']})")
    _row("Created:", _fmt_date(data["created_at"]))
    _row("Updated:", _fmt_date(data["updated_at"]))
    _row("Last verified:", _fmt_date(data["last_verified_at"]))
    if data["valid_from"] or data["valid_to"]:
        _row("Valid:", f"{data['valid_from'] or '?'} → {data['valid_to'] or 'present'}")

    _section("Evidence")
    if data["verbatim_anchor"]:
        print(f"  Verbatim anchor:")
        print(f"    \"{_short(data['verbatim_anchor'], 400)}\"")
    else:
        print("  No verbatim anchor stored.")
    if data["evidence_snippets"]:
        print(f"\n  Evidence snippets:")
        snippets = _parse_json_field(data["evidence_snippets"])
        if isinstance(snippets, list):
            for s in snippets[:3]:
                print(f"    • {_short(str(s), 200)}")
        else:
            print(f"    {_short(str(snippets), 300)}")

    _section("Challenge History")
    challenges = data["_challenges"]
    if challenges:
        for ch in challenges[-5:]:  # last 5
            if isinstance(ch, dict):
                date_ = ch.get("date", "?")
                text  = _short(ch.get("challenge", ""), 200)
                src   = ch.get("source", "")
                print(f"  [{date_}] {text}")
                if src:
                    print(f"           source: {src}")
            else:
                print(f"  {_short(str(ch), 200)}")
        if len(challenges) > 5:
            print(f"  … and {len(challenges) - 5} earlier challenge(s)")
    else:
        print("  No challenges recorded.")

    _section("Status Transitions")
    pos_history = data["_pos_history"]
    if pos_history:
        for ph in pos_history:
            sf, st, what, trigger, date_, ts = ph
            arrow = f"{sf or '?'} → {st or '?'}"
            print(f"  {_fmt_date(date_)}  {arrow:<30}  trigger: {trigger or '—'}")
            if what:
                print(f"    {_short(what, 150)}")
    else:
        print("  No status transitions recorded.")

    _section("Tensions")
    tensions = data["_tensions"]
    if tensions:
        for t in tensions:
            tid, desc, importance, date_id, active = t
            print(f"  tension id={tid}  importance={importance:.2f}  identified={date_id}")
            print(f"    {_short(desc, 200)}")
    else:
        print("  No active tensions involving this belief.")

    _section("Semantic Chunks")
    chunks = data["_chunks"]
    if chunks:
        for ch in chunks:
            cid, content, embed_status, created = ch
            print(f"  chunk id={cid}  [{embed_status}]  created={_fmt_date(created)}")
            print(f"    {_short(content, 180)}")
    else:
        print("  No embedded chunks linked to this belief.")

    if data.get("tags"):
        _section("Tags")
        print(f"  {data['tags']}")

    print(f"\n{'═' * 60}\n")


def _display_generic(obj_type, data):
    print(f"\n{'═' * 60}")
    print(f"  {obj_type.upper()}  id={data.get('id', '?')}")
    print(f"{'═' * 60}")
    for k, v in data.items():
        if v is None or v == "" or v == "[]":
            continue
        _row(f"{k}:", _short(str(v), 250))
    print(f"\n{'═' * 60}\n")


# ── Main ───────────────────────────────────────────────────────────────────────

SUPPORTED_TYPES = {
    "belief":   ("beliefs",   "topic"),
    "concept":  ("concepts",  "name"),
    "question": ("questions", "question"),
    "epiphany": ("epiphanies", "description"),
    "goal":     ("goals",     "description"),
    "entity":   ("entities",  "name"),
    "pattern":  ("patterns",  "description"),
    "chunk":    ("memory_chunks", "content"),
}


def main():
    parser = argparse.ArgumentParser(
        description="Trust inspection — show full provenance for any memory object"
    )
    parser.add_argument("type", choices=list(SUPPORTED_TYPES.keys()),
                        help="Memory object type")
    parser.add_argument("id", nargs="?", type=int,
                        help="Integer ID of the object")
    parser.add_argument("--search", metavar="QUERY",
                        help="Search by topic/name instead of ID")
    parser.add_argument("--json", action="store_true",
                        help="Output raw data as JSON instead of formatted report")
    parser.add_argument("--db", default=None, metavar="PATH",
                        help="Override database path")
    args = parser.parse_args()

    if not args.id and not args.search:
        parser.error("Provide either an integer ID or --search QUERY")

    db = Path(args.db) if args.db else DB_PATH
    if not db.exists():
        print(f"ERROR: database not found at {db}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    # ── Search mode ───────────────────────────────────────────────────────────
    if args.search:
        table, search_col = SUPPORTED_TYPES[args.type]
        if args.type == "belief":
            hits = _search_beliefs(conn, args.search)
            if not hits:
                print(f"No beliefs matching '{args.search}'")
                sys.exit(0)
            print(f"\nBeliefs matching '{args.search}':")
            for row in hits:
                bid, topic, position, status, score = row
                print(f"  id={bid}  [{status}]  score={score:.2f}  {topic}: {_short(position, 80)}")
        else:
            hits = _search_generic(conn, table, search_col, args.search)
            if not hits:
                print(f"No {args.type}s matching '{args.search}'")
                sys.exit(0)
            print(f"\n{args.type.capitalize()}s matching '{args.search}':")
            for row in hits:
                print(f"  id={row[0]}  {_short(str(row[1]), 120)}")
        print("\nRe-run with the ID to inspect a specific item.")
        conn.close()
        return

    obj_id = args.id

    # ── Inspect mode ──────────────────────────────────────────────────────────
    if args.type == "belief":
        data = _inspect_belief(conn, obj_id)
        if data is None:
            conn.close()
            sys.exit(1)
        if args.json:
            # Remove non-serializable sqlite Row values
            clean = {k: v for k, v in data.items() if not k.startswith("_")}
            print(json.dumps(clean, indent=2, default=str))
        else:
            _display_belief(data)
    else:
        table, _ = SUPPORTED_TYPES[args.type]
        data = _inspect_generic(conn, table, args.type, obj_id)
        if data is None:
            print(f"No {args.type} with id={obj_id}")
            conn.close()
            sys.exit(1)
        if args.json:
            print(json.dumps(data, indent=2, default=str))
        else:
            _display_generic(args.type, data)

    conn.close()


if __name__ == "__main__":
    main()

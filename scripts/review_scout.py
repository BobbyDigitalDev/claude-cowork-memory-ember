#!/usr/bin/env python3
"""
review_scout.py
---------------
Review interface for research_scout.py results stored in scout_results.

WHY THIS EXISTS
---------------
research_scout.py writes up to 25 results per day into scout_results with
status='pending'. Without a review tool, those results are only accessible
via raw SQL. This script surfaces them as a readable digest and lets you
mark results as interesting, dismissed, or reviewed so the table stays
signal-dense over time.

WORKFLOW
--------
  1. Run with no args to see today's pending results
  2. Read the digest, note IDs of interesting / dismissable results
  3. Run --mark to update their status
  4. Use --ingest ID to send an approved result through process_research.py
     so it gets extracted and embedded into the knowledge base

COMMANDS
--------
  (no args)             Pending results, newest fetch date first
  --all                 All results regardless of status
  --today               Only results fetched today
  --ring 1|2            Filter by search ring (1=belief/question seeds, 2=Qwen expansions)
  --status STATUS       Filter by status (pending/interesting/dismissed/reviewed/ingested)
  --summary             Status counts and fetch-date breakdown
  --mark IDS --status S Mark comma-separated IDs with STATUS
                        STATUS: interesting | dismissed | reviewed | ingested
  --notes "TEXT"        Attach curator notes when marking (used with --mark)
  --promote ID          Mark as interesting AND create an open question in the DB
  --ingest ID           Run result through process_research.py and embed into memory.
                        Formats title + abstract as a temp file, calls process_research.py,
                        marks row as ingested. Requires Ollama running.
  --ingest-dry-run ID   Preview the temp file that would be sent to process_research.py
  --output FILE         Write digest to FILE instead of stdout
  --limit N             Max results to show (default: 50)

EXAMPLES
--------
  python3 ~/claude_memory/scripts/review_scout.py
  python3 ~/claude_memory/scripts/review_scout.py --today
  python3 ~/claude_memory/scripts/review_scout.py --mark 1,2,4 --status dismissed
  python3 ~/claude_memory/scripts/review_scout.py --mark 3 --status interesting --notes "strong RAG paper"
  python3 ~/claude_memory/scripts/review_scout.py --promote 7
  python3 ~/claude_memory/scripts/review_scout.py --ingest 12
  python3 ~/claude_memory/scripts/review_scout.py --ingest-dry-run 12
  python3 ~/claude_memory/scripts/review_scout.py --summary
"""

import sqlite3
import argparse
import sys
import os
import json
import subprocess
import tempfile
from datetime import datetime, date
from pathlib import Path

_BASE   = Path.home() / "claude_memory"
DB_PATH = _BASE / "memory.db"

NOW   = datetime.now().isoformat()
TODAY = date.today().isoformat()

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

VALID_STATUSES = {"pending", "interesting", "dismissed", "reviewed", "ingested"}

STATUS_ICONS = {
    "pending":     "·",
    "interesting": "★",
    "dismissed":   "✗",
    "reviewed":    "✓",
    "ingested":    "⬆",
}


# ── Formatting ────────────────────────────────────────────────────────────────

def fmt_authors(authors_json: str, max_authors: int = 2) -> str:
    try:
        authors = json.loads(authors_json or "[]")
        if not authors:
            return "Unknown"
        shown = authors[:max_authors]
        suffix = f" +{len(authors) - max_authors}" if len(authors) > max_authors else ""
        return ", ".join(shown) + suffix
    except Exception:
        return (authors_json or "")[:60]


def fmt_triggered_by(triggered: str) -> str:
    """Shorten triggered_by to a readable label."""
    if not triggered:
        return "unknown"
    # "belief: topic_name: long description..." -> "belief: topic_name"
    parts = triggered.split(":")
    if len(parts) >= 2:
        kind  = parts[0].strip()
        label = parts[1].strip()[:50]
        return f"{kind}: {label}"
    return triggered[:60]


def fmt_abstract(abstract: str, max_chars: int = 220) -> str:
    if not abstract:
        return "(no abstract)"
    flat = " ".join(abstract.split())
    if len(flat) <= max_chars:
        return flat
    return flat[:max_chars].rstrip() + "..."


def fmt_score(score: float) -> str:
    bar_len = int((score - 0.60) / 0.40 * 10)  # 0.60-1.00 mapped to 0-10
    bar_len = max(0, min(10, bar_len))
    return f"{score:.3f} [{'█' * bar_len}{'░' * (10 - bar_len)}]"


def render_result(row: dict, index: int = None) -> list[str]:
    icon   = STATUS_ICONS.get(row["status"], "?")
    prefix = f"[{index}] " if index is not None else ""
    lines  = []
    lines.append(
        f"{prefix}{icon} id={row['id']}  score={fmt_score(row['relevance_score'])}  "
        f"ring={row['search_ring']}  {row['source_name']}  {(row['publication_date'] or '')[:10]}"
    )
    lines.append(f"   Title:  {row['title'] or '(no title)'}")
    lines.append(f"   By:     {fmt_authors(row['authors'])}")
    lines.append(f"   From:   {fmt_triggered_by(row['triggered_by'])}")
    lines.append(f"   URL:    {row['source_url'] or '(no url)'}")
    lines.append(f"   ↳ {fmt_abstract(row['abstract'])}")
    if row.get("curator_notes"):
        lines.append(f"   Notes:  {row['curator_notes']}")
    return lines


# ── DB helpers ────────────────────────────────────────────────────────────────

def load_results(conn: sqlite3.Connection, where_clauses: list[str],
                 params: list, limit: int) -> list[dict]:
    where = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    sql = f"""
        SELECT id, title, authors, abstract, source_url, source_name, source_type,
               publication_date, search_ring, triggered_by, relevance_score,
               status, curator_notes, promoted_to, reviewed_at, date_fetched
        FROM scout_results
        {where}
        ORDER BY date_fetched DESC, relevance_score DESC
        LIMIT ?
    """
    rows = conn.execute(sql, params + [limit]).fetchall()
    return [dict(r) for r in rows]


def mark_results(conn: sqlite3.Connection, ids: list[int], status: str,
                 notes: str | None, dry_run: bool = False) -> int:
    if status not in VALID_STATUSES:
        print(f"ERROR: invalid status '{status}'. Valid: {', '.join(sorted(VALID_STATUSES))}")
        sys.exit(1)

    updated = 0
    for rid in ids:
        row = conn.execute("SELECT id, title, status FROM scout_results WHERE id=?",
                           (rid,)).fetchone()
        if not row:
            print(f"  SKIP id={rid}: not found")
            continue
        print(f"  id={rid}: {row['status']} → {status}  \"{(row['title'] or '')[:60]}\"")
        if not dry_run:
            conn.execute("""
                UPDATE scout_results
                SET status=?, reviewed_at=?,
                    curator_notes=CASE
                        WHEN ? IS NOT NULL THEN COALESCE(curator_notes||' | ','') || ?
                        ELSE curator_notes
                    END,
                    updated_at=?
                WHERE id=?
            """, (status, NOW, notes, notes, NOW, rid))
        updated += 1
    return updated


def promote_result(conn: sqlite3.Connection, rid: int) -> bool:
    """
    Mark result as interesting and create an open question in the questions
    table derived from the paper title and abstract.
    """
    row = conn.execute("""
        SELECT id, title, abstract, source_url, authors, relevance_score
        FROM scout_results WHERE id=?
    """, (rid,)).fetchone()

    if not row:
        print(f"ERROR: id={rid} not found")
        return False

    title    = row["title"] or ""
    abstract = (row["abstract"] or "")[:300]
    url      = row["source_url"] or ""
    score    = row["relevance_score"]

    question_text = (
        f"[Scout result, score={score:.3f}] {title} -- "
        f"What relevance does this have to our memory architecture? "
        f"Key excerpt: {abstract[:160]}"
    )

    conn.execute("""
        INSERT INTO questions
            (question, status, category, current_best_thinking,
             created_at, updated_at, user_id, agent_id)
        VALUES (?, 'open', 'research', ?, ?, ?, ?, 'claude')
    """, (
        question_text,
        f"Promoted from scout_results id={rid}. URL: {url}",
        NOW, NOW, USERNAME,
    ))

    conn.execute("""
        UPDATE scout_results
        SET status='ingested', promoted_to='questions', reviewed_at=?, updated_at=?
        WHERE id=?
    """, (NOW, NOW, rid))

    print(f"  Promoted id={rid} → new open question created")
    print(f"  Title: {title[:80]}")
    return True


# ── Ingest ────────────────────────────────────────────────────────────────────

def _format_as_research_text(row: dict) -> str:
    """
    Format a scout_results row as a plain-text research document suitable
    for process_research.py. Mirrors the format of YouTube transcript files
    so process_research.py prompts work without modification.
    """
    title       = row["title"]        or "Untitled"
    abstract    = row["abstract"]     or "(no abstract available)"
    source_name = row["source_name"]  or "unknown"
    source_url  = row["source_url"]   or ""
    pub_date    = row["publication_date"] or "unknown"
    score       = row["relevance_score"]
    doi         = row["doi"]          or ""
    tags        = row["tags"]         or ""

    try:
        authors_list = json.loads(row["authors"] or "[]")
        authors_str  = ", ".join(authors_list) if authors_list else "Unknown"
    except (json.JSONDecodeError, TypeError):
        authors_str = row["authors"] or "Unknown"

    lines = [
        f"# Research Paper: {title}",
        f"**Source:** {source_name}",
        f"**URL:** {source_url}",
        f"**Authors:** {authors_str}",
        f"**Publication date:** {pub_date}",
        f"**DOI:** {doi}",
        f"**Relevance score:** {score:.3f}",
        f"**Tags:** {tags}",
        f"**Fetched:** {row['date_fetched']}",
        "",
        "## Abstract",
        "",
        abstract,
    ]
    return "\n".join(lines) + "\n"


def ingest_result(conn: sqlite3.Connection, rid: int, dry_run: bool = False) -> bool:
    """
    Send a scout result through process_research.py so its abstract gets
    extracted into concepts/beliefs/patterns and embedded into memory_chunks.

    Steps:
      1. Load the row from scout_results.
      2. Format title + abstract as a temp .txt file (matching process_research.py input format).
      3. Call process_research.py on the temp file via subprocess.
      4. On success, mark row as ingested.
      5. Clean up the temp file.

    Requires Ollama running (same dependency as process_research.py).
    """
    row = conn.execute("""
        SELECT id, title, abstract, source_url, source_name, authors,
               relevance_score, doi, tags, date_fetched, publication_date, status
        FROM scout_results WHERE id=?
    """, (rid,)).fetchone()

    if not row:
        print(f"ERROR: id={rid} not found in scout_results")
        return False

    if row["status"] == "ingested":
        print(f"NOTE: id={rid} is already marked ingested. Use --force to re-ingest.")
        return False

    research_text = _format_as_research_text(row)

    if dry_run:
        print(f"--- Dry run for scout result id={rid} ---")
        print(research_text)
        print("--- (no files written, no process_research.py call) ---")
        return True

    # Write to temp file with a descriptive name process_research.py can use
    title_slug = "".join(
        c if c.isalnum() or c in "-_" else "_"
        for c in (row["title"] or "untitled")[:40]
    ).strip("_").lower()
    tmp_filename = f"scout_{rid}_{title_slug}.txt"
    tmp_dir = _BASE / "cache"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / tmp_filename

    tmp_path.write_text(research_text)
    print(f"  Temp file written: {tmp_path.name}")

    # Locate process_research.py
    script_path = _BASE / "scripts" / "process_research.py"
    if not script_path.exists():
        print(f"ERROR: process_research.py not found at {script_path}")
        tmp_path.unlink(missing_ok=True)
        return False

    print(f"  Running process_research.py on scout result id={rid}...")
    result = subprocess.run(
        [sys.executable, str(script_path), str(tmp_path)],
        capture_output=False,   # let output stream to terminal so user can see progress
        text=True,
    )

    if result.returncode != 0:
        print(f"\nERROR: process_research.py exited with code {result.returncode}")
        print(f"  Temp file kept for inspection: {tmp_path}")
        return False

    # Mark as ingested
    conn.execute("""
        UPDATE scout_results
        SET status='ingested', promoted_to='memory_chunks', reviewed_at=?, updated_at=?,
            curator_notes=COALESCE(curator_notes || ' | ', '') || 'Ingested via review_scout.py --ingest'
        WHERE id=?
    """, (NOW, NOW, rid))
    conn.commit()

    # ── Write research_tasks record ───────────────────────────────────────────
    # Records what was researched, from which source, and what the findings were.
    # belief_impact is left NULL here — populate manually or via a future curator
    # pass once beliefs extracted from this content have been identified.
    _write_research_task(conn, row)

    tmp_path.unlink(missing_ok=True)
    print(f"\n  Ingested id={rid} → extracted and embedded into memory.")
    print(f"  Run embed_memories.py to index any new chunks that were not auto-embedded.")
    return True


def _write_research_task(conn: sqlite3.Connection, scout_row: dict):
    """Insert one row into research_tasks when a scout result is ingested.

    Silently skips on any error so a logging failure never blocks an ingest.
    """
    try:
        conn.execute("""
            INSERT INTO research_tasks
                (date, query, triggered_by, sources_consulted, findings,
                 belief_impact, status, confidence_score, source_type,
                 tags, created_at)
            VALUES (date('now'), ?, ?, ?, ?, NULL, 'completed', ?, ?, ?, datetime('now'))
        """, (
            (scout_row["search_query"] or "")[:300],
            (scout_row["triggered_by"] or "review_scout --ingest")[:200],
            (scout_row["source_name"] or "")[:200],
            (scout_row["abstract"] or "")[:1000],
            scout_row["relevance_score"] or 0.0,
            scout_row["source_type"] or "unknown",
            scout_row["tags"] or "",
        ))
        conn.commit()
    except Exception:
        pass


# ── Summary ───────────────────────────────────────────────────────────────────

def print_summary(conn: sqlite3.Connection):
    print()
    print("Scout Results Summary")
    print("=" * 50)

    print("\nBy status:")
    for r in conn.execute("""
        SELECT status, COUNT(*) as n FROM scout_results GROUP BY status ORDER BY n DESC
    """).fetchall():
        icon = STATUS_ICONS.get(r["status"], "?")
        print(f"  {icon} {r['status']:<12} {r['n']}")

    print("\nBy fetch date (pending):")
    for r in conn.execute("""
        SELECT date_fetched, COUNT(*) as n, ROUND(AVG(relevance_score), 3) as avg_score
        FROM scout_results
        WHERE status='pending'
        GROUP BY date_fetched ORDER BY date_fetched DESC LIMIT 10
    """).fetchall():
        print(f"  {r['date_fetched']}  {r['n']} results  avg_score={r['avg_score']}")

    print("\nBy ring (pending):")
    for r in conn.execute("""
        SELECT search_ring, COUNT(*) as n, ROUND(AVG(relevance_score), 3) as avg_score
        FROM scout_results WHERE status='pending'
        GROUP BY search_ring ORDER BY search_ring
    """).fetchall():
        print(f"  Ring {r['search_ring']}  {r['n']} results  avg_score={r['avg_score']}")

    total = conn.execute("SELECT COUNT(*) FROM scout_results").fetchone()[0]
    print(f"\nTotal rows: {total}")
    print()


# ── Batch ingest helpers ──────────────────────────────────────────────────────

def _parse_ids(raw: str) -> list[int]:
    """Parse a comma-separated string of IDs into a list of ints."""
    ids = []
    for part in str(raw).split(","):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
        else:
            print(f"ERROR: '{part}' is not a valid ID")
            sys.exit(1)
    return ids


def _run_batch_ingest(conn: sqlite3.Connection, ids: list[int],
                      force: bool = False, no_embed: bool = False) -> None:
    """
    Ingest a list of scout result IDs in sequence.
    After all ingests, auto-run embed_memories.py unless --no-embed is set.
    """
    succeeded = []
    failed    = []

    for rid in ids:
        print(f"\n[{ids.index(rid)+1}/{len(ids)}] Ingesting id={rid}...")
        if force:
            conn.execute(
                "UPDATE scout_results SET status='interesting' WHERE id=? AND status='ingested'",
                (rid,)
            )
            conn.commit()
        ok = ingest_result(conn, rid, dry_run=False)
        (succeeded if ok else failed).append(rid)

    print(f"\n{'='*50}")
    print(f"Ingest complete: {len(succeeded)} succeeded, {len(failed)} failed.")
    if succeeded:
        print(f"  Ingested: {succeeded}")
    if failed:
        print(f"  Failed:   {failed}")

    if no_embed:
        print("\n--no-embed set: skipping embed_memories.py.")
        print("Run manually: python3 ~/claude_memory/scripts/embed_memories.py")
        return

    if not succeeded:
        print("\nNothing successfully ingested — skipping embed step.")
        return

    embed_script = _BASE / "scripts" / "embed_memories.py"
    if not embed_script.exists():
        print(f"\nWARNING: embed_memories.py not found at {embed_script}. Run manually.")
        return

    print("\nRunning embed_memories.py to index new chunks...")
    result = subprocess.run(
        [sys.executable, str(embed_script)],
        capture_output=False,
        text=True,
    )
    if result.returncode == 0:
        print("Embedding complete. New chunks are now searchable in semantic retrieval.")
    else:
        print(f"WARNING: embed_memories.py exited with code {result.returncode}. "
              f"Run manually to complete indexing.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Review research scout results")
    parser.add_argument("--all",      action="store_true",  help="Show all results regardless of status")
    parser.add_argument("--today",    action="store_true",  help="Only today's results")
    parser.add_argument("--ring",     type=int, choices=[1, 2], help="Filter by search ring")
    parser.add_argument("--status",   help="Filter by status (pending/interesting/dismissed/reviewed/ingested)")
    parser.add_argument("--summary",        action="store_true",  help="Show status counts and date breakdown")
    parser.add_argument("--mark",           help="Comma-separated IDs to mark, e.g. '1,3,5'")
    parser.add_argument("--notes",          help="Curator notes to attach when marking")
    parser.add_argument("--promote",        type=int, help="Promote result ID to an open question")
    parser.add_argument("--ingest",         metavar="IDS",
                        help="Ingest one or more results: --ingest 51 or --ingest 51,52,53. "
                             "Runs process_research.py on each, then auto-runs embed_memories.py.")
    parser.add_argument("--ingest-queued",  action="store_true", dest="ingest_queued",
                        help="Ingest all results currently marked 'interesting', then embed.")
    parser.add_argument("--ingest-dry-run", metavar="IDS", dest="ingest_dry_run",
                        help="Preview temp file(s) without running anything. Accepts comma-separated IDs.")
    parser.add_argument("--no-embed",       action="store_true", dest="no_embed",
                        help="Skip auto-running embed_memories.py after ingest.")
    parser.add_argument("--force",          action="store_true",
                        help="Re-ingest even if already marked ingested")
    parser.add_argument("--output",         help="Write digest to this file instead of stdout")
    parser.add_argument("--limit",          type=int, default=50, help="Max results to show (default: 50)")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"ERROR: database not found at {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # ── Summary mode ──
    if args.summary:
        print_summary(conn)
        conn.close()
        return

    # ── Mark mode ──
    if args.mark:
        if not args.status:
            print("ERROR: --mark requires --status, e.g. --mark 1,3,5 --status dismissed")
            sys.exit(1)

        ids = []
        for part in args.mark.split(","):
            part = part.strip()
            if part.isdigit():
                ids.append(int(part))
            else:
                print(f"ERROR: '{part}' is not a valid ID")
                sys.exit(1)

        print()
        print(f"Marking {len(ids)} result(s) as '{args.status}':")
        n = mark_results(conn, ids, args.status, args.notes)
        conn.commit()
        print(f"\n{n} result(s) updated.")
        conn.close()
        return

    # ── Promote mode ──
    if args.promote:
        # Check questions table has expected columns
        cols = [r[1] for r in conn.execute("PRAGMA table_info(questions)").fetchall()]
        if "source" not in cols or "context" not in cols:
            # Gracefully adapt to available columns
            print(f"NOTE: questions table columns: {cols}")

        print()
        print(f"Promoting scout result id={args.promote}...")
        ok = promote_result(conn, args.promote)
        if ok:
            conn.commit()
        conn.close()
        return

    # ── Ingest dry-run mode ──
    if args.ingest_dry_run:
        ids = _parse_ids(args.ingest_dry_run)
        conn2 = sqlite3.connect(DB_PATH)
        conn2.row_factory = sqlite3.Row
        print()
        for rid in ids:
            ingest_result(conn2, rid, dry_run=True)
            print()
        conn2.close()
        conn.close()
        return

    # ── Ingest-queued mode ──
    if args.ingest_queued:
        conn2 = sqlite3.connect(DB_PATH)
        conn2.row_factory = sqlite3.Row
        rows = conn2.execute(
            "SELECT id FROM scout_results WHERE status='interesting' ORDER BY relevance_score DESC"
        ).fetchall()
        ids = [r["id"] for r in rows]
        if not ids:
            print("No results marked 'interesting'. Nothing to ingest.")
            conn2.close()
            conn.close()
            return
        print(f"\nIngesting {len(ids)} queued result(s): {ids}")
        _run_batch_ingest(conn2, ids, args.force, args.no_embed)
        conn2.close()
        conn.close()
        return

    # ── Ingest mode (one or more IDs) ──
    if args.ingest:
        ids = _parse_ids(args.ingest)
        conn2 = sqlite3.connect(DB_PATH)
        conn2.row_factory = sqlite3.Row
        print(f"\nIngesting {len(ids)} result(s): {ids}")
        _run_batch_ingest(conn2, ids, args.force, args.no_embed)
        conn2.close()
        conn.close()
        return

    # ── Digest mode (default) ──
    where_clauses = []
    params = []

    if args.all:
        pass  # no status filter
    elif args.status:
        where_clauses.append("status = ?")
        params.append(args.status)
    else:
        where_clauses.append("status = 'pending'")

    if args.today:
        where_clauses.append("date_fetched = ?")
        params.append(TODAY)

    if args.ring:
        where_clauses.append("search_ring = ?")
        params.append(args.ring)

    rows = load_results(conn, where_clauses, params, args.limit)
    conn.close()

    if not rows:
        print("No results found matching filters.")
        return

    # Group by fetch date
    by_date: dict[str, list[dict]] = {}
    for r in rows:
        d = r["date_fetched"] or "unknown"
        by_date.setdefault(d, []).append(r)

    output_lines = []
    output_lines.append("# Scout Results Digest")
    output_lines.append(
        f"Generated: {NOW[:19]}  |  "
        f"Showing: {'all' if args.all else (args.status or 'pending')}  |  "
        f"Results: {len(rows)}"
    )
    output_lines.append("")
    output_lines.append(
        "Mark commands:  --mark IDS interesting | dismissed | reviewed | ingested"
    )
    output_lines.append("Promote to question:  --promote ID")
    output_lines.append("")

    for fetch_date in sorted(by_date.keys(), reverse=True):
        date_rows = by_date[fetch_date]
        output_lines.append(f"## {fetch_date}  ({len(date_rows)} results)")
        output_lines.append("")

        # Sub-group by ring within date
        ring1 = [r for r in date_rows if r["search_ring"] == 1]
        ring2 = [r for r in date_rows if r["search_ring"] == 2]

        for ring_label, ring_rows in [("Ring 1 (belief/question seeds)", ring1),
                                       ("Ring 2 (Qwen expansions)", ring2)]:
            if not ring_rows:
                continue
            output_lines.append(f"### {ring_label}")
            output_lines.append("")
            for i, row in enumerate(ring_rows, 1):
                for line in render_result(row, index=row["id"]):
                    output_lines.append(line)
                output_lines.append("")

    digest = "\n".join(output_lines)

    if args.output:
        with open(args.output, "w") as f:
            f.write(digest)
        print(f"Digest written to {args.output}")
    else:
        print(digest)


if __name__ == "__main__":
    main()

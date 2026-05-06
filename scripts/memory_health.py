#!/usr/bin/env python3
"""
memory_health.py
----------------
Daily memory health report for ember-engine.

Generates a markdown summary of what happened to the memory graph — new
memories, belief state changes, failed jobs, embedding gaps, stale open
questions, and Scout items awaiting review. Makes the background agents
visible and the database legible.

Usage:
    python3 ~/claude_memory/scripts/memory_health.py             # last 24h
    python3 ~/claude_memory/scripts/memory_health.py --days 7    # last 7 days
    python3 ~/claude_memory/scripts/memory_health.py --print     # print to stdout only
    python3 ~/claude_memory/scripts/memory_health.py --days 1 --print

Output:
    ~/claude_memory/reports/memory_health_YYYY_MM_DD.md  (dated file)
    ~/claude_memory/reports/memory_health_latest.md      (always overwritten)

Both are gitignored. Open either file or paste into a CoWork session.
"""

import argparse
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────

_BASE       = Path.home() / "claude_memory"
REPORTS_DIR = _BASE / "reports"

def _find_db() -> Path:
    standard = _BASE / "memory.db"
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


# ── Data gathering ─────────────────────────────────────────────────────────────

def _cutoff(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def gather(conn: sqlite3.Connection, days: int) -> dict:
    cut = _cutoff(days)
    now = datetime.now()
    data = {}

    # ── New memories ──────────────────────────────────────────────────────────
    new_counts = {}
    for tbl in ("beliefs", "epiphanies", "concepts", "entities",
                "patterns", "questions", "memory_chunks"):
        try:
            n = conn.execute(
                f"SELECT COUNT(*) FROM {tbl} WHERE created_at >= ?", (cut,)
            ).fetchone()[0]
            if n > 0:
                new_counts[tbl] = n
        except Exception:
            pass
    data["new_counts"] = new_counts

    # ── Belief state distribution ──────────────────────────────────────────────
    belief_states = {}
    for row in conn.execute(
        "SELECT status, COUNT(*) FROM beliefs WHERE is_active=1 GROUP BY status"
    ).fetchall():
        belief_states[row[0]] = row[1]
    data["belief_states"] = belief_states

    # ── Belief transitions (position_history) ─────────────────────────────────
    try:
        transitions = conn.execute("""
            SELECT status_from, status_to, COUNT(*) as n
            FROM position_history
            WHERE created_at >= ?
            GROUP BY status_from, status_to
            ORDER BY n DESC
        """, (cut,)).fetchall()
        data["belief_transitions"] = [(r[0], r[1], r[2]) for r in transitions]
    except Exception:
        data["belief_transitions"] = []

    # ── Recently disputed/deprecated beliefs ──────────────────────────────────
    try:
        flagged = conn.execute("""
            SELECT id, topic, position, status, confidence_score, updated_at
            FROM beliefs
            WHERE status IN ('disputed', 'deprecated')
              AND is_active = 1
              AND updated_at >= ?
            ORDER BY updated_at DESC LIMIT 10
        """, (cut,)).fetchall()
        data["flagged_beliefs"] = [(r[0], r[1], r[2], r[3], r[4], r[5]) for r in flagged]
    except Exception:
        data["flagged_beliefs"] = []

    # ── Processing jobs ───────────────────────────────────────────────────────
    try:
        job_stats = {}
        for row in conn.execute(
            "SELECT status, COUNT(*) FROM processing_jobs WHERE created_at >= ? GROUP BY status",
            (cut,)
        ).fetchall():
            job_stats[row[0]] = row[1]
        data["job_stats"] = job_stats

        failed_jobs = conn.execute("""
            SELECT id, job_type, source_file, error_log, created_at
            FROM processing_jobs
            WHERE status = 'failed' AND created_at >= ?
            ORDER BY created_at DESC LIMIT 10
        """, (cut,)).fetchall()
        data["failed_jobs"] = [(r[0], r[1], r[2], r[3], r[4]) for r in failed_jobs]
    except Exception:
        data["job_stats"] = {}
        data["failed_jobs"] = []

    # ── Embedding gaps ────────────────────────────────────────────────────────
    try:
        total    = conn.execute("SELECT COUNT(*) FROM memory_chunks").fetchone()[0]
        embedded = conn.execute(
            "SELECT COUNT(*) FROM memory_chunks WHERE embedding_status = 'embedded'"
        ).fetchone()[0]
        pending  = conn.execute(
            "SELECT COUNT(*) FROM memory_chunks WHERE embedding_status = 'pending'"
        ).fetchone()[0]
        data["embedding_total"]    = total
        data["embedding_embedded"] = embedded
        data["embedding_pending"]  = pending
    except Exception:
        data["embedding_total"] = data["embedding_embedded"] = data["embedding_pending"] = 0

    # ── Stale open questions ───────────────────────────────────────────────────
    stale_cutoff = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    try:
        stale_q = conn.execute("""
            SELECT id, question, category, created_at
            FROM questions
            WHERE status = 'open' AND created_at < ?
            ORDER BY created_at ASC LIMIT 10
        """, (stale_cutoff,)).fetchall()
        data["stale_questions"] = [(r[0], r[1], r[2], r[3]) for r in stale_q]
        data["open_question_count"] = conn.execute(
            "SELECT COUNT(*) FROM questions WHERE status = 'open'"
        ).fetchone()[0]
    except Exception:
        data["stale_questions"] = []
        data["open_question_count"] = 0

    # ── Scout results awaiting review ─────────────────────────────────────────
    try:
        pending_scout = conn.execute(
            "SELECT COUNT(*) FROM scout_results WHERE status = 'pending'"
        ).fetchone()[0]
        interesting_scout = conn.execute(
            "SELECT COUNT(*) FROM scout_results WHERE status = 'interesting'"
        ).fetchone()[0]
        top_pending = conn.execute("""
            SELECT id, title, source_name, relevance_score, challenge_score, date_fetched
            FROM scout_results
            WHERE status = 'pending'
            ORDER BY relevance_score DESC LIMIT 5
        """).fetchall()
        data["scout_pending"]     = pending_scout
        data["scout_interesting"] = interesting_scout
        data["scout_top"]         = [(r[0], r[1], r[2], r[3], r[4], r[5]) for r in top_pending]
    except Exception:
        data["scout_pending"] = data["scout_interesting"] = 0
        data["scout_top"] = []

    # ── Low-fidelity beliefs ──────────────────────────────────────────────────
    try:
        low_fidelity = conn.execute("""
            SELECT id, topic, position, fidelity_score
            FROM beliefs
            WHERE fidelity_score IS NOT NULL AND fidelity_score < 0.6
              AND is_active = 1
            ORDER BY fidelity_score ASC LIMIT 10
        """).fetchall()
        data["low_fidelity"] = [(r[0], r[1], r[2], r[3]) for r in low_fidelity]
    except Exception:
        data["low_fidelity"] = []

    # ── Retrieval events ──────────────────────────────────────────────────────
    try:
        retrieval_count = conn.execute(
            "SELECT COUNT(*) FROM retrieval_events WHERE created_at >= ?", (cut,)
        ).fetchone()[0]
        data["retrieval_count"] = retrieval_count
    except Exception:
        data["retrieval_count"] = 0

    return data


# ── Report rendering ───────────────────────────────────────────────────────────

def _short(text, n=120):
    if not text:
        return ""
    text = str(text).replace("\n", " ").strip()
    return text[:n] + ("…" if len(text) > n else "")


def render(data: dict, generated_at: datetime, days: int) -> str:
    lines = []
    cutoff_str = (generated_at - timedelta(days=days)).strftime("%Y-%m-%d")

    lines.append(f"# Memory Health Report — {generated_at.strftime('%Y-%m-%d')}")
    lines.append(
        f"*Generated: {generated_at.strftime('%Y-%m-%d %H:%M')} | "
        f"Lookback: {days} day(s) (since {cutoff_str})*"
    )
    lines.append("")

    # ── New memories ──────────────────────────────────────────────────────────
    lines.append("## New Memories")
    new = data.get("new_counts", {})
    if new:
        for tbl, n in sorted(new.items(), key=lambda x: -x[1]):
            lines.append(f"- **{tbl}**: +{n}")
    else:
        lines.append("- No new memories in this window.")
    lines.append("")

    # ── Belief health ─────────────────────────────────────────────────────────
    lines.append("## Belief Health")
    states = data.get("belief_states", {})
    if states:
        for status in ["verified", "supported", "proposed", "disputed", "deprecated", "archived"]:
            n = states.get(status, 0)
            if n > 0:
                lines.append(f"- {status}: {n}")
    else:
        lines.append("- No active beliefs.")
    lines.append("")

    transitions = data.get("belief_transitions", [])
    if transitions:
        lines.append("**Status transitions this period:**")
        for sf, st, n in transitions:
            lines.append(f"- {sf or '?'} → {st or '?'}  ×{n}")
        lines.append("")

    flagged = data.get("flagged_beliefs", [])
    if flagged:
        lines.append("**Recently disputed or deprecated:**")
        for bid, topic, position, status, score, updated in flagged:
            score_str = f"{score:.2f}" if score is not None else "?"
            lines.append(
                f"- id={bid}  [{status}]  score={score_str}  "
                f"*{topic}: {_short(position, 80)}*"
            )
        lines.append("")

    # ── Fidelity warnings ──────────────────────────────────────────────────────
    low_fid = data.get("low_fidelity", [])
    if low_fid:
        lines.append("## Low-Fidelity Beliefs")
        lines.append(
            "*These beliefs scored below 0.6 on extraction fidelity — "
            "the stored position may not faithfully represent the source quote.*"
        )
        for bid, topic, position, score in low_fid:
            lines.append(f"- id={bid}  fidelity={score:.2f}  *{topic}: {_short(position, 80)}*")
            lines.append(
                f"  → Review: `python3 ~/claude_memory/scripts/inspect_memory.py belief {bid}`"
            )
        lines.append("")

    # ── Processing jobs ───────────────────────────────────────────────────────
    lines.append("## Processing Jobs")
    job_stats = data.get("job_stats", {})
    if job_stats:
        for status, n in sorted(job_stats.items()):
            lines.append(f"- {status}: {n}")
    else:
        lines.append("- No jobs in this window.")

    failed_jobs = data.get("failed_jobs", [])
    if failed_jobs:
        lines.append("")
        lines.append("**Failed jobs:**")
        for jid, jtype, src, err, created in failed_jobs:
            lines.append(f"- id={jid}  type={jtype}  file={src or '—'}  ({str(created)[:10]})")
            if err:
                lines.append(f"  error: {_short(err, 120)}")
    lines.append("")

    # ── Embedding coverage ────────────────────────────────────────────────────
    lines.append("## Embedding Coverage")
    total    = data.get("embedding_total", 0)
    embedded = data.get("embedding_embedded", 0)
    pending  = data.get("embedding_pending", 0)
    if total > 0:
        pct = embedded / total * 100
        bar_filled = int(pct / 5)
        bar = "█" * bar_filled + "░" * (20 - bar_filled)
        lines.append(f"- {embedded}/{total} chunks embedded ({pct:.0f}%)  {bar}")
        if pending > 0:
            lines.append(
                f"- {pending} chunk(s) pending — run: "
                f"`python3 ~/claude_memory/scripts/embed_memories.py`"
            )
    else:
        lines.append("- No memory chunks yet.")
    lines.append("")

    # ── Open questions ────────────────────────────────────────────────────────
    lines.append("## Open Questions")
    total_open = data.get("open_question_count", 0)
    stale_q    = data.get("stale_questions", [])
    lines.append(f"- Total open: {total_open}")
    if stale_q:
        lines.append(f"- Stale (30+ days unanswered): {len(stale_q)}")
        for qid, question, category, created in stale_q[:5]:
            lines.append(f"  - id={qid}  [{category}]  since {str(created)[:10]}  *{_short(question, 100)}*")
        if len(stale_q) > 5:
            lines.append(f"  - … and {len(stale_q) - 5} more")
    lines.append("")

    # ── Scout queue ───────────────────────────────────────────────────────────
    lines.append("## Scout Queue")
    pending_scout     = data.get("scout_pending", 0)
    interesting_scout = data.get("scout_interesting", 0)
    scout_top         = data.get("scout_top", [])

    lines.append(f"- Pending review: {pending_scout}")
    lines.append(f"- Flagged interesting: {interesting_scout}")

    if scout_top:
        lines.append("")
        lines.append("**Top pending by relevance:**")
        for sid, title, source, rel, chall, fetched in scout_top:
            chall_str = f"  challenge={chall:.2f}" if chall is not None else ""
            lines.append(
                f"- id={sid}  [{source}]  score={rel:.2f}{chall_str}  "
                f"{_short(title, 80)}"
            )
        lines.append(
            "\nReview: `python3 ~/claude_memory/scripts/generate_scout_digest.py`"
        )
    lines.append("")

    # ── Retrieval activity ────────────────────────────────────────────────────
    lines.append("## Retrieval Activity")
    lines.append(f"- Retrieval calls this period: {data.get('retrieval_count', 0)}")
    lines.append("")

    # ── Footer ────────────────────────────────────────────────────────────────
    lines.append("---")
    lines.append(
        "*Generated by memory_health.py — E.M.B.E.R Engine for Claude CoWork*"
    )

    return "\n".join(lines)


# ── Output ─────────────────────────────────────────────────────────────────────

def write_report(content: str, generated_at: datetime, print_only: bool) -> tuple:
    dated_name = f"memory_health_{generated_at.strftime('%Y-%m-%d')}.md"
    dated_path = REPORTS_DIR / dated_name
    latest_path = REPORTS_DIR / "memory_health_latest.md"

    if print_only:
        print(content)
        return str(latest_path), str(dated_path)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    latest_path.write_text(content, encoding="utf-8")
    dated_path.write_text(content, encoding="utf-8")
    return str(latest_path), str(dated_path)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Generate a daily memory health report"
    )
    parser.add_argument("--days", type=int, default=1,
                        help="Lookback window in days (default: 1)")
    parser.add_argument("--print", action="store_true",
                        help="Print to stdout without writing files")
    parser.add_argument("--db", default=None, metavar="PATH",
                        help="Override database path")
    args = parser.parse_args()

    db = Path(args.db) if args.db else DB_PATH
    if not db.exists():
        print(f"ERROR: database not found at {db}", flush=True)
        raise SystemExit(1)

    conn = sqlite3.connect(db)
    generated_at = datetime.now()
    data = gather(conn, args.days)
    conn.close()

    content = render(data, generated_at, args.days)
    latest, dated = write_report(content, generated_at, print_only=args.print)

    if not args.print:
        print(f"Memory health report generated.")
        print(f"  Latest:  {latest}")
        print(f"  Archive: {dated}")
        print(f"  Open:    open {latest}")


if __name__ == "__main__":
    main()

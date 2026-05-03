#!/usr/bin/env python3
"""
generate_scout_digest.py
------------------------
Formats pending and interesting Scout results into a readable markdown
digest for human review. Closes the loop between the automated Research
Scout and Bobby's own reading habits.

Run manually or add to the nightly session prompt pipeline.

Usage:
    python3 ~/claude_memory/scripts/generate_scout_digest.py
    python3 ~/claude_memory/scripts/generate_scout_digest.py --status pending
    python3 ~/claude_memory/scripts/generate_scout_digest.py --status interesting
    python3 ~/claude_memory/scripts/generate_scout_digest.py --days 7
    python3 ~/claude_memory/scripts/generate_scout_digest.py --dry-run

Output:
    ~/claude_memory/scout_digest_latest.md  (always overwritten — current digest)
    ~/claude_memory/research/digests/scout_digest_YYYY_MM_DD.md  (dated archive)

Both files are gitignored. Open scout_digest_latest.md directly or paste its
contents into a Cowork session for review.

To act on a result after reading:
    python3 ~/claude_memory/scripts/review_scout.py --mark ID --status interesting
    python3 ~/claude_memory/scripts/review_scout.py --mark ID --status dismissed
    python3 ~/claude_memory/scripts/review_scout.py --mark ID,ID --status interesting
"""

import argparse
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────

_BASE       = Path.home() / "claude_memory"
DB_PATH     = _BASE / "memory.db"
DIGEST_DIR  = _BASE / "research" / "digests"
LATEST_PATH = _BASE / "scout_digest_latest.md"

# ── Config ─────────────────────────────────────────────────────────────────────

ABSTRACT_PREVIEW_CHARS = 280
TITLE_MAX_CHARS        = 100
TRIGGERED_MAX_CHARS    = 80


# ── Formatting helpers ─────────────────────────────────────────────────────────

def _fmt_score(score) -> str:
    if score is None:
        return "?.??"
    return f"{float(score):.2f}"


def _fmt_date(date_str) -> str:
    if not date_str:
        return ""
    return str(date_str)[:10]


def _fmt_abstract(abstract: str) -> str:
    if not abstract:
        return "*(no abstract)*"
    flat = " ".join(abstract.split())
    if len(flat) <= ABSTRACT_PREVIEW_CHARS:
        return flat
    return flat[:ABSTRACT_PREVIEW_CHARS].rsplit(" ", 1)[0] + "…"


def _ring_label(ring) -> str:
    if ring == 1:
        return "Ring 1 — direct match"
    if ring == 2:
        return "Ring 2 — adjacent topic"
    return f"Ring {ring}"


def _score_bar(score, width=12) -> str:
    """ASCII bar proportional to relevance score (0.65–1.0 range)."""
    if score is None:
        return " " * width
    lo, hi = 0.60, 1.0
    filled = int((float(score) - lo) / (hi - lo) * width)
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


def _format_item(row: sqlite3.Row, idx: int) -> str:
    """Format a single scout_results row as a digest entry."""
    title     = (row["title"] or "Untitled")[:TITLE_MAX_CHARS]
    source    = row["source_name"] or "?"
    pub_date  = _fmt_date(row["publication_date"])
    score     = row["relevance_score"]
    ring      = row["search_ring"]
    triggered = (row["triggered_by"] or "")[:TRIGGERED_MAX_CHARS]
    abstract  = _fmt_abstract(row["abstract"] or "")
    url       = row["source_url"] or ""
    rid       = row["id"]
    notes     = row["curator_notes"] or ""

    lines = []
    lines.append(
        f"### [{idx}] {title}"
    )
    lines.append(
        f"**{source}** | {pub_date} | score: {_fmt_score(score)} {_score_bar(score)} | {_ring_label(ring)}"
    )
    if triggered:
        lines.append(f"*Triggered by:* {triggered}")
    if url:
        lines.append(f"*Source:* {url}")
    lines.append("")
    lines.append(f"> {abstract}")
    if notes:
        lines.append("")
        lines.append(f"*Notes:* {notes}")
    lines.append("")
    lines.append(
        f"```\n"
        f"python3 ~/claude_memory/scripts/review_scout.py --mark {rid} --status interesting\n"
        f"python3 ~/claude_memory/scripts/review_scout.py --mark {rid} --status dismissed\n"
        f"python3 ~/claude_memory/scripts/review_scout.py --ingest {rid}\n"
        f"```"
    )
    return "\n".join(lines)


def _build_digest(rows: list, generated_at: datetime, days: int, status_filter: str) -> str:
    """Build the full markdown digest from a list of scout_results rows."""
    # Partition by status
    interesting = [r for r in rows if r["status"] == "interesting"]
    pending     = [r for r in rows if r["status"] == "pending"]
    other       = [r for r in rows if r["status"] not in ("interesting", "pending")]

    total   = len(rows)
    n_int   = len(interesting)
    n_pend  = len(pending)
    cutoff  = (generated_at - timedelta(days=days)).strftime("%Y-%m-%d")

    lines = []

    # ── Header ────────────────────────────────────────────────────────────────
    lines.append(f"# EMBER Scout Digest — {generated_at.strftime('%Y-%m-%d')}")
    lines.append(
        f"*Generated: {generated_at.strftime('%Y-%m-%d %H:%M')} | "
        f"{total} items | {n_int} interesting | {n_pend} pending | "
        f"lookback: {days} days (since {cutoff})*"
    )
    lines.append("")
    lines.append(
        "To act on items: use the `review_scout.py` commands shown under each entry.  \n"
        "To dismiss all pending: `python3 ~/claude_memory/scripts/review_scout.py --all --status pending`  \n"
        "For full list: `python3 ~/claude_memory/scripts/review_scout.py --all`"
    )
    lines.append("")
    lines.append("---")
    lines.append("")

    if not rows:
        lines.append("*No items match the current filter. The Scout may not have run yet, or all results have been reviewed.*")
        return "\n".join(lines)

    # ── Interesting items ──────────────────────────────────────────────────────
    if interesting:
        lines.append(f"## ⭐ Interesting — Review First ({n_int})")
        lines.append("")
        lines.append(
            "*These were manually flagged as interesting in a prior review. "
            "Consider ingesting or promoting to open questions.*"
        )
        lines.append("")
        for i, row in enumerate(interesting, 1):
            lines.append(_format_item(row, i))
            lines.append("")
            lines.append("---")
            lines.append("")

    # ── Pending items ─────────────────────────────────────────────────────────
    if pending:
        lines.append(f"## 📬 Pending — New Items ({n_pend})")
        lines.append("")
        lines.append(
            "*Sorted by relevance score. Higher score = stronger semantic match "
            "to current beliefs and open questions.*"
        )
        lines.append("")
        start_idx = len(interesting) + 1
        for i, row in enumerate(pending, start_idx):
            lines.append(_format_item(row, i))
            lines.append("")
            lines.append("---")
            lines.append("")

    # ── Other statuses (if filter is 'all') ───────────────────────────────────
    if other:
        lines.append(f"## Other ({len(other)})")
        lines.append("")
        start_idx = len(interesting) + len(pending) + 1
        for i, row in enumerate(other, start_idx):
            lines.append(_format_item(row, i))
            lines.append("")
            lines.append("---")
            lines.append("")

    # ── Footer ────────────────────────────────────────────────────────────────
    lines.append(
        f"*Digest generated by generate_scout_digest.py — "
        f"E.M.B.E.R Engine for Claude Cowork*"
    )

    return "\n".join(lines)


# ── Database query ─────────────────────────────────────────────────────────────

def _fetch_results(conn: sqlite3.Connection, days: int, status_filter: str) -> list:
    """Fetch scout_results rows for the digest."""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    if status_filter == "all":
        where_status = "status NOT IN ('dismissed', 'ingested')"
        params = [cutoff]
    elif status_filter == "interesting":
        where_status = "status = 'interesting'"
        params = [cutoff]
    else:
        # Default: pending + interesting
        where_status = "status IN ('pending', 'interesting')"
        params = [cutoff]

    rows = conn.execute(f"""
        SELECT id, title, authors, abstract, source_url, source_name, source_type,
               publication_date, search_ring, triggered_by, relevance_score,
               status, curator_notes, promoted_to, reviewed_at, date_fetched
        FROM scout_results
        WHERE {where_status}
          AND date_fetched >= ?
        ORDER BY
            CASE status WHEN 'interesting' THEN 0 ELSE 1 END,
            relevance_score DESC
    """, params).fetchall()

    return rows


# ── Output ─────────────────────────────────────────────────────────────────────

def _write_digest(content: str, generated_at: datetime, dry_run: bool) -> tuple:
    """Write digest to latest path and dated archive. Returns (latest, dated) paths."""
    dated_name   = f"scout_digest_{generated_at.strftime('%Y-%m-%d')}.md"
    dated_path   = DIGEST_DIR / dated_name

    if dry_run:
        print(content)
        print(f"\n[dry-run] Would write to:")
        print(f"  {LATEST_PATH}")
        print(f"  {dated_path}")
        return str(LATEST_PATH), str(dated_path)

    DIGEST_DIR.mkdir(parents=True, exist_ok=True)

    LATEST_PATH.write_text(content, encoding="utf-8")
    dated_path.write_text(content, encoding="utf-8")

    return str(LATEST_PATH), str(dated_path)


# ── Main ───────────────────────────────────────────────────────────────────────

def generate_digest(
    days: int = 14,
    status_filter: str = "default",
    dry_run: bool = False,
    db_path: str | None = None,
) -> dict:
    """
    Generate the scout digest and write output files.

    Returns dict with keys: total, interesting, pending, latest_path, dated_path.
    """
    _db   = Path(db_path) if db_path else DB_PATH
    conn  = sqlite3.connect(_db)
    conn.row_factory = sqlite3.Row

    rows         = _fetch_results(conn, days, status_filter)
    conn.close()

    generated_at = datetime.now()
    content      = _build_digest(rows, generated_at, days, status_filter)
    latest, dated = _write_digest(content, generated_at, dry_run)

    return {
        "total":       len(rows),
        "interesting": sum(1 for r in rows if r["status"] == "interesting"),
        "pending":     sum(1 for r in rows if r["status"] == "pending"),
        "latest_path": latest,
        "dated_path":  dated,
    }


def _main():
    parser = argparse.ArgumentParser(
        description="Generate a readable digest of pending Scout results."
    )
    parser.add_argument(
        "--days", type=int, default=14,
        help="Include results fetched within this many days (default: 14)"
    )
    parser.add_argument(
        "--status",
        choices=["default", "pending", "interesting", "all"],
        default="default",
        help=(
            "Which statuses to include: "
            "'default' = pending+interesting (default), "
            "'pending' = pending only, "
            "'interesting' = interesting only, "
            "'all' = everything except dismissed/ingested"
        )
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print digest to stdout without writing files"
    )
    parser.add_argument(
        "--db", default=None, metavar="PATH",
        help="Override database path"
    )
    args = parser.parse_args()

    result = generate_digest(
        days          = args.days,
        status_filter = args.status,
        dry_run       = args.dry_run,
        db_path       = args.db,
    )

    if not args.dry_run:
        print(f"Scout digest generated.")
        print(f"  Items:       {result['total']} ({result['interesting']} interesting, {result['pending']} pending)")
        print(f"  Latest:      {result['latest_path']}")
        print(f"  Archive:     {result['dated_path']}")
        print()
        print(f"  Open digest: open {result['latest_path']}")
        print(f"  In session:  read ~/claude_memory/scout_digest_latest.md")


if __name__ == "__main__":
    _main()

#!/usr/bin/env python3
"""
tier0_classifier.py
-------------------
Classifies the likely intent of the next session based on the last session's
completed work, and returns an adaptive retrieval configuration for
refresh_deep_memory.py to use.

WHY THIS EXISTS
---------------
refresh_deep_memory.py uses a fixed seed mix (3 questions, 2 goals, 2 beliefs)
and a fixed threshold (0.55) regardless of what kind of work is being done.
As the chunk corpus grows past 200, a flat retrieval config degrades: more
chunks pass the threshold but the top-3 cap means quality drops. Different
session types also need fundamentally different memory slices to be useful.

HOW CLASSIFICATION WORKS
-------------------------
After each session, ingest.py runs refresh_deep_memory.py as post-processing.
At that point we know what the just-finished session accomplished (goals,
patterns) and what's queued next (immediate pending goals). We score those
texts against keyword sets for four intent types and pick the winner.

No Qwen call. No embedding. Fast and deterministic.

INTENT TYPES
------------
  build         Technical work: scripts, migrations, debugging, features.
                Needs: recent goals, patterns (operational lessons), precision.

  philosophical Consciousness, ethics, ideas, exploration.
                Needs: open questions, epiphanies, beliefs; looser threshold.

  maintenance   Hygiene, goal review, audits, cleanup.
                Needs: patterns and lessons; tight threshold.

  research      Scout result review, literature, papers.
                Needs: open questions; moderate threshold.

  unknown       No strong signal. Uses current baseline config.

CORPUS SCALING
--------------
Seed count scales with chunk total independently of intent:
  < 200 chunks  ->  7 seeds  (3Q 2G 2B)  [current baseline]
  200-300       ->  10 seeds (4Q 3G 3B)
  300-500       ->  13 seeds (5Q 4G 4B)
  500+          ->  15 seeds (6Q 5G 4B)  + warning in output

USAGE
-----
Standalone check (shows what the next session would get):
    python3 ~/claude_memory/scripts/tier0_classifier.py

Imported by refresh_deep_memory.py:
    from tier0_classifier import classify_session, RetrievalConfig
"""

import sqlite3
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

_BASE   = Path.home() / "claude_memory"
DB_PATH = _BASE / "memory.db"

# ── Intent keyword sets ───────────────────────────────────────────────────────

INTENT_KEYWORDS: dict[str, list[str]] = {
    "build": [
        "build", "implement", "create", "script", "debug", "fix", "install",
        "migrate", "migration", "deploy", "write", "run", "test", "pipeline",
        "schema", "function", "agent", "tool", "api", "database", "parse",
        "refactor", "endpoint", "compile", "embed",
    ],
    "philosophical": [
        "consciousness", "sentience", "feel", "wonder", "explore", "ethical",
        "philosophical", "meaning", "substrate", "awareness", "existence",
        "identity", "emergence", "intelligence", "mind", "experience",
        "intuition", "introspection", "muse",
    ],
    "maintenance": [
        "hygiene", "cleanup", "audit", "review", "stale", "deprecate",
        "merge", "duplicate", "validator", "update goals", "goal pass",
        "tidy", "prune", "verify", "check", "reconcile", "backfill",
    ],
    "research": [
        "paper", "research", "scout", "findings", "study", "literature",
        "arxiv", "pubmed", "openalex", "read", "article", "publication",
        "survey", "review findings", "scout results",
    ],
}

# ── Retrieval configs per intent ──────────────────────────────────────────────

# Base configs before corpus scaling is applied.
# n_questions, n_goals, n_beliefs are the MINIMUM for that intent.
_INTENT_CONFIGS = {
    "build": dict(
        threshold=0.58,
        top_per_seed=3,
        base_q=2, base_g=3, base_b=2,
    ),
    "philosophical": dict(
        threshold=0.50,
        top_per_seed=4,
        base_q=4, base_g=1, base_b=3,
    ),
    "maintenance": dict(
        threshold=0.62,
        top_per_seed=3,
        base_q=2, base_g=3, base_b=2,
    ),
    "research": dict(
        threshold=0.52,
        top_per_seed=4,
        base_q=4, base_g=2, base_b=2,
    ),
    "unknown": dict(
        threshold=0.55,
        top_per_seed=3,
        base_q=3, base_g=2, base_b=2,
    ),
}


@dataclass
class RetrievalConfig:
    intent:      str
    confidence:  float           # 0.0-1.0, ratio of winning score to total
    n_questions: int
    n_goals:     int
    n_beliefs:   int
    threshold:   float
    top_per_seed: int
    corpus_size: int
    corpus_tier: str             # "small" / "medium" / "large" / "xlarge"
    notes:       str = ""        # human-readable explanation

    @property
    def total_seeds(self):
        return self.n_questions + self.n_goals + self.n_beliefs

    def summary(self):
        return (
            f"intent={self.intent} (conf={self.confidence:.2f})  "
            f"seeds={self.total_seeds} ({self.n_questions}Q {self.n_goals}G {self.n_beliefs}B)  "
            f"threshold={self.threshold}  top/seed={self.top_per_seed}  "
            f"corpus={self.corpus_size} [{self.corpus_tier}]"
        )


# ── Corpus scaling ────────────────────────────────────────────────────────────

def _corpus_tier(n_chunks: int) -> str:
    if n_chunks < 200:
        return "small"
    if n_chunks < 300:
        return "medium"
    if n_chunks < 500:
        return "large"
    return "xlarge"


def _scale_seeds(base_q: int, base_g: int, base_b: int,
                 n_chunks: int) -> tuple[int, int, int]:
    """
    Add extra seeds proportionally as the corpus grows.
    Extra seeds are distributed Q > G >= B to keep questions driving retrieval.
    """
    if n_chunks < 200:
        return base_q, base_g, base_b
    if n_chunks < 300:
        # +3 seeds total
        return base_q + 1, base_g + 1, base_b + 1
    if n_chunks < 500:
        # +6 seeds total
        return base_q + 2, base_g + 2, base_b + 2
    # +8 seeds total
    return base_q + 3, base_g + 3, base_b + 2


# ── Classification ────────────────────────────────────────────────────────────

def _score_text(text: str, keywords: list[str]) -> int:
    """Count how many keywords appear in text (case-insensitive)."""
    low = text.lower()
    return sum(1 for kw in keywords if kw in low)


def classify_session(conn: sqlite3.Connection) -> RetrievalConfig:
    """
    Classify likely next-session intent from DB state.
    Uses: last session's completed goals, pending immediate goals, recent patterns.
    Returns a fully-configured RetrievalConfig.
    """
    conn.row_factory = sqlite3.Row
    texts: list[str] = []

    # Pending immediate goals -- strongest signal for what comes next
    rows = conn.execute("""
        SELECT description, category FROM goals
        WHERE status = 'pending' AND priority = 'immediate'
        ORDER BY id DESC LIMIT 10
    """).fetchall()
    for r in rows:
        texts.append((r["description"] or "") + " " + (r["category"] or ""))

    # Last session's completed goals (what we just finished -- session type inertia)
    rows = conn.execute("""
        SELECT g.description, g.category
        FROM goals g
        JOIN sessions s ON s.id = (SELECT MAX(id) FROM sessions)
        WHERE g.status = 'completed'
          AND g.updated_at >= s.created_at
        LIMIT 15
    """).fetchall()
    for r in rows:
        texts.append((r["description"] or "") + " " + (r["category"] or ""))

    # Recent patterns (operational context)
    rows = conn.execute("""
        SELECT pattern_type, description FROM patterns
        WHERE is_active = 1
        ORDER BY id DESC LIMIT 8
    """).fetchall()
    for r in rows:
        texts.append((r["pattern_type"] or "") + " " + (r["description"] or ""))

    # Corpus size
    n_chunks = conn.execute(
        "SELECT COUNT(*) FROM memory_chunks WHERE embedding_vector IS NOT NULL"
    ).fetchone()[0]

    # Score each intent
    combined_text = " ".join(texts)
    scores: dict[str, int] = {}
    for intent, keywords in INTENT_KEYWORDS.items():
        scores[intent] = _score_text(combined_text, keywords)

    total = sum(scores.values())
    if total == 0:
        winner = "unknown"
        confidence = 0.0
    else:
        winner = max(scores, key=lambda k: scores[k])
        confidence = scores[winner] / total if total > 0 else 0.0
        # Require a meaningful signal -- below 25% treat as unknown
        if confidence < 0.25:
            winner = "unknown"
            confidence = 0.0

    cfg = _INTENT_CONFIGS[winner]
    nq, ng, nb = _scale_seeds(
        cfg["base_q"], cfg["base_g"], cfg["base_b"], n_chunks
    )

    notes_parts = []
    notes_parts.append(f"Keyword scores: " + ", ".join(
        f"{k}={v}" for k, v in sorted(scores.items(), key=lambda x: -x[1])
    ))
    if not texts:
        notes_parts.append("No session data found -- using baseline config.")

    return RetrievalConfig(
        intent=winner,
        confidence=round(confidence, 3),
        n_questions=nq,
        n_goals=ng,
        n_beliefs=nb,
        threshold=cfg["threshold"],
        top_per_seed=cfg["top_per_seed"],
        corpus_size=n_chunks,
        corpus_tier=_corpus_tier(n_chunks),
        notes=" | ".join(notes_parts),
    )


# ── Persistence ──────────────────────────────────────────────────────────────

def log_classification(conn: sqlite3.Connection, cfg: "RetrievalConfig",
                       triggered_by: str = "unknown") -> None:
    """Write one row to session_intent_log recording this classification result.

    Non-fatal: any schema or write error is silently swallowed so classifier
    failures never block the calling pipeline.

    triggered_by: 'ingest' | 'manual' | 'refresh_deep_memory' | 'unknown'
    """
    from datetime import datetime
    try:
        now  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        today = now[:10]
        conn.execute("""
            INSERT INTO session_intent_log
                (date, intent, confidence, corpus_size, corpus_tier,
                 n_questions, n_goals, n_beliefs,
                 threshold, top_per_seed, notes, triggered_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            today,
            cfg.intent,
            cfg.confidence,
            cfg.corpus_size,
            cfg.corpus_tier,
            cfg.n_questions,
            cfg.n_goals,
            cfg.n_beliefs,
            cfg.threshold,
            cfg.top_per_seed,
            cfg.notes,
            triggered_by,
            now,
        ))
        conn.commit()
    except Exception:
        pass  # non-fatal — log failure never blocks classification


# ── Standalone entry point ────────────────────────────────────────────────────

def main():
    if not DB_PATH.exists():
        print(f"ERROR: database not found at {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    cfg = classify_session(conn)
    log_classification(conn, cfg, triggered_by="manual")
    conn.close()

    print()
    print("Tier 0 Classifier")
    print("=" * 60)
    print(f"  Intent:        {cfg.intent}  (confidence: {cfg.confidence:.0%})")
    print(f"  Seeds:         {cfg.total_seeds}  ({cfg.n_questions}Q  {cfg.n_goals}G  {cfg.n_beliefs}B)")
    print(f"  Threshold:     {cfg.threshold}")
    print(f"  Top / seed:    {cfg.top_per_seed}")
    print(f"  Corpus:        {cfg.corpus_size} chunks  [{cfg.corpus_tier}]")
    print(f"  Notes:         {cfg.notes}")
    print()


if __name__ == "__main__":
    main()

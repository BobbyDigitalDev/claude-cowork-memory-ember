"""
verify_beliefs.py
-----------------
Runs a DeepSeek R1 verification pass over beliefs in memory.db.

For each belief in scope:
  1. Loads the source conversation text from ~/claude_memory/conversations/
  2. Asks R1 whether the belief is actually supported by that conversation
  3. Updates status, confidence_calibrated, last_verified_at, and challenge_history

Status transitions after verification:
  proposed  →  verified        (R1 confirms, confidence_score >= 0.8)
  proposed  →  supported       (R1 confirms with caveats, score 0.5-0.79)
  proposed  →  disputed        (R1 finds contradicting or missing evidence)
  any       →  disputed        (cross-topic contradiction detected)

Also runs an optional cross-belief contradiction check:
  Groups all active beliefs by topic, asks R1 if any beliefs within the same
  topic cluster contradict each other, and marks conflicting pairs as disputed.

Usage:
    python3 ~/claude_memory/scripts/verify_beliefs.py
    python3 ~/claude_memory/scripts/verify_beliefs.py --limit 20
    python3 ~/claude_memory/scripts/verify_beliefs.py --status proposed
    python3 ~/claude_memory/scripts/verify_beliefs.py --all-statuses
    python3 ~/claude_memory/scripts/verify_beliefs.py --check-contradictions
    python3 ~/claude_memory/scripts/verify_beliefs.py --dry-run
"""

import sqlite3
import json
import os
import struct
from pathlib import Path
import re
import sys
import requests
import uuid as _uuid
from datetime import datetime

# ── Configuration ──────────────────────────────────────────────────────────────

DB_PATH      = os.path.expanduser("~/claude_memory/memory.db")
CONV_DIR     = os.path.expanduser("~/claude_memory/conversations/")
DEBUG_DIR    = os.path.expanduser("~/claude_memory/debug/")
OLLAMA_URL   = "http://localhost:11434/api/generate"
EMBED_URL    = "http://localhost:11434/api/embeddings"
EMBED_MODEL  = "nomic-embed-text"

MODEL_REASONING = "deepseek-r1:14b"
MODEL           = MODEL_REASONING


def _read_username() -> str:
    config = Path.home() / "claude_memory" / ".ember_config"
    if config.exists():
        for line in config.read_text().splitlines():
            if line.startswith("USERNAME=") and not line.startswith("#"):
                return line.split("=", 1)[1].strip().strip('"')
    return "user"

USERNAME = _read_username()
NUM_CTX         = 65536

# ── Verification tuning ─────────────────────────────────────────────────────────
STALE_DAYS            = 90    # beliefs unverified longer than this get confidence decay
STALE_DECAY_PER_MONTH = 0.05  # confidence nudge per 30 days past threshold
STALE_MAX_DECAY       = 0.25  # cap on total decay applied in one pass
SCOUT_CONTEXT_LIMIT   = 3     # max external scout results to include as context

# ── Ollama interface ────────────────────────────────────────────────────────────

def ask_r1_for_json(prompt):
    """Send prompt to DeepSeek R1 via Ollama and parse JSON.
    R1 wraps its chain-of-thought in <think>...</think> — strip it before parsing."""
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {
            "num_ctx": NUM_CTX,
            "temperature": 0.1,
        }
    }
    try:
        response = requests.post(OLLAMA_URL, json=payload, timeout=1200)
        response.raise_for_status()
        raw = response.json().get("response", "").strip()
    except requests.exceptions.ConnectionError:
        print("ERROR: Cannot reach Ollama. Is deepseek-r1:14b running?")
        sys.exit(1)
    except requests.exceptions.Timeout:
        print("ERROR: R1 timed out.")
        return None
    except Exception as e:
        print(f"ERROR talking to Ollama: {e}")
        return None

    # Strip <think>...</think> block
    cleaned = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()

    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        cleaned = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end   = cleaned.rfind("}") + 1
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start:end])
            except json.JSONDecodeError:
                pass
        start = cleaned.find("[")
        end   = cleaned.rfind("]") + 1
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start:end])
            except json.JSONDecodeError:
                pass
        print(f"  WARNING (R1): Could not parse JSON. First 200 chars: {cleaned[:200]}")
        return None


# ── Conversation loader ─────────────────────────────────────────────────────────

def load_conversation_text(conn, conv_id):
    """Load the source conversation .md file for a given conversation ID.

    Lookup order:
      1. Parse filename from raw_export field: "[stored in USERNAME_YYYY_MM_DD_NNN.md]"
      2. List all .md files in CONV_DIR matching the conversation date
      3. Fall back to None (caller uses evidence_snippets instead)
    """
    c = conn.cursor()
    c.execute("SELECT date, raw_export FROM conversations WHERE id = ?", (conv_id,))
    row = c.fetchone()
    if not row:
        return None

    conv_date, raw_export = row

    # Strategy 1: parse filename from raw_export
    if raw_export:
        m = re.search(r'\[stored in ([^\]]+)\]', raw_export)
        if m:
            filename = m.group(1).strip()
            filepath = os.path.join(CONV_DIR, filename)
            if os.path.exists(filepath):
                with open(filepath, "r") as f:
                    return f.read()

    # Strategy 2: find any .md file in CONV_DIR whose name contains the date
    if conv_date:
        date_slug = conv_date.replace("-", "_")  # "2026_04_25"
        candidates = [
            fn for fn in os.listdir(CONV_DIR)
            if fn.endswith(".md") and date_slug in fn
        ]
        if candidates:
            # Use the first match (usually there's only one per date)
            filepath = os.path.join(CONV_DIR, candidates[0])
            with open(filepath, "r") as f:
                return f.read()

    return None


# ── External context loader ─────────────────────────────────────────────────────

def _load_scout_context(conn, topic, position):
    """Find relevant ingested scout results to use as external grounding context.

    Queries scout_results for ingested or high-relevance entries, then keyword-
    filters to the SCOUT_CONTEXT_LIMIT most topically related ones.
    Returns a formatted string block ready for insertion into a prompt, or "" if
    no relevant results are found.
    """
    try:
        rows = conn.execute("""
            SELECT title, abstract, source_name, relevance_score, url
            FROM scout_results
            WHERE status = 'ingested' OR relevance_score >= 0.65
            ORDER BY relevance_score DESC
            LIMIT 150
        """).fetchall()
    except Exception:
        return ""

    if not rows:
        return ""

    stop = {"the","a","an","is","in","of","to","and","or","that","it","this",
            "for","with","was","are","be","as","at","by","we","i","my","our",
            "not","but","so","if","on","from","have","has","had","they","its",
            "which","when","their","been","were","than","what","more","also"}

    def _tokens(t):
        return {w.lower().strip(".,;:\"'()") for w in t.split()
                if len(w) > 3 and w.lower().strip(".,;:\"'()") not in stop}

    belief_tokens = _tokens(f"{topic or ''} {position or ''}")
    if not belief_tokens:
        return ""

    scored = []
    for row in rows:
        r_tokens = _tokens(f"{row[0] or ''} {row[1] or ''}")
        if r_tokens:
            overlap = len(belief_tokens & r_tokens) / max(len(belief_tokens), len(r_tokens))
            if overlap >= 0.10:
                scored.append((overlap, row))

    if not scored:
        return ""

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:SCOUT_CONTEXT_LIMIT]

    lines = ["\nExternal research relevant to this belief (from Scout archive):"]
    for _, row in top:
        title    = (row[0] or "")[:120]
        abstract = (row[1] or "")[:300]
        source   = row[2] or "unknown"
        lines.append(f"  [{source}] {title}")
        if abstract:
            lines.append(f"    {abstract}...")

    return "\n".join(lines)


# ── Per-belief verification ─────────────────────────────────────────────────────

def verify_belief(belief, conv_text, conn=None):
    """Ask R1 to evaluate whether this belief is supported by the conversation.

    Uses an adversarial two-phase prompt: Phase 1 seeks supporting evidence,
    Phase 2 actively seeks challenges. This reduces confirmation bias compared
    to a single neutral prompt. External scout results are included when
    available as additional grounding beyond the source conversation.

    belief:    dict with keys topic, position, confidence, confidence_score,
               evidence_snippets, origin
    conv_text: full conversation text, or None (uses evidence_snippets as fallback)
    conn:      optional sqlite3 connection for loading scout context

    Returns dict: {
        verdict:          "verified" | "supported" | "disputed" | "insufficient_evidence",
        confidence_score: float,
        reasoning:        str,
        evidence_found:   str or None,
        challenge:        str or None
    }
    """
    # ── Build primary evidence section ────────────────────────────────────────
    if conv_text:
        excerpt = conv_text[:60000]
        evidence_section = f"Conversation text (primary source):\n{excerpt}"
    else:
        snippets = belief.get("evidence_snippets", "[]")
        if isinstance(snippets, str):
            try:
                snippets = json.loads(snippets)
            except Exception:
                snippets = [snippets]
        evidence_section = (
            "(Full conversation not available — using extracted evidence snippets only)\n"
            f"Evidence snippets:\n{json.dumps(snippets, indent=2)}"
        )

    # ── Optionally append external scout context ──────────────────────────────
    scout_section = ""
    if conn is not None:
        scout_section = _load_scout_context(
            conn, belief.get("topic", ""), belief.get("position", "")
        )

    prompt = f"""You are performing a structured two-phase verification of a belief extracted
from conversations between {USERNAME} (human) and Claude (AI).

Belief to verify:
  Topic:      {belief.get("topic", "")}
  Position:   {belief.get("position", "")}
  Confidence: {belief.get("confidence", "")} (score: {belief.get("confidence_score", 0.5)})
  Origin:     {belief.get("origin", "")}

{evidence_section}{scout_section}

─── PHASE 1 — SUPPORT ───
Find the strongest evidence in the sources above that DIRECTLY SUPPORTS this belief.
Quote or reference specifically. If support is absent, state that explicitly — do not invent support.

─── PHASE 2 — CHALLENGE ───
Now actively search for evidence that CHALLENGES, qualifies, or contradicts this belief.
Consider: alternative interpretations, scope limitations, missing nuance, contradicting statements,
or external research that complicates the position.
If no credible challenge exists, state that explicitly — do not force a challenge.

─── PHASE 3 — VERDICT ───
Synthesizing both phases, apply these criteria:
- "verified":              Clear direct support. Challenge is absent or trivial.
                           If belief was extensively reasoned through, confidence may rise.
- "supported":             Support exists but with meaningful caveats, OR weak challenge present.
                           If belief appeared as a passing mention, lower confidence by 0.1-0.15.
- "disputed":              Challenge outweighs support, OR belief misrepresents the source.
                           Set confidence_score below 0.45.
- "insufficient_evidence": Sources are genuinely inconclusive — cannot confirm or deny.

Return a JSON object with exactly these fields:
{{
  "verdict": "verified" | "supported" | "disputed" | "insufficient_evidence",
  "confidence_score": <adjusted 0.0-1.0>,
  "reasoning": "<one paragraph synthesizing both phases and explaining your verdict>",
  "evidence_found": "<short direct quote or reference supporting the belief, or null>",
  "challenge": "<what specifically weakens or contradicts this belief, or null if none>"
}}

Return only the JSON object."""

    return ask_r1_for_json(prompt)


# ── Cross-belief contradiction check ───────────────────────────────────────────

def check_topic_contradictions(topic, beliefs):
    """Ask R1 whether any beliefs within the same topic cluster contradict each other.

    beliefs: list of belief dicts (all sharing the same topic)

    Returns list of contradiction dicts: [
        {"belief_ids": [id1, id2], "reason": "..."}
    ]
    """
    if len(beliefs) < 2:
        return []

    beliefs_summary = [
        {
            "id":       b["id"],
            "topic":    b["topic"],
            "position": b["position"],
            "status":   b["status"],
            "valid_from": b.get("valid_from", ""),
        }
        for b in beliefs
    ]

    prompt = f"""You are checking whether any beliefs about the same topic contradict each other.
These beliefs were extracted from conversations across multiple sessions and may have evolved
over time. Your job is to identify genuine logical contradictions — not just nuance or evolution.

Topic: {topic}

Beliefs ({len(beliefs_summary)} total):
{json.dumps(beliefs_summary, indent=2)}

A contradiction means two beliefs cannot both be true at the same time given the same context.
Natural evolution of position (e.g. "approach A" later replaced by "approach B") is NOT a
contradiction unless the later belief explicitly denies the earlier one.

Return a JSON array. Each item is a confirmed contradiction:
{{
  "belief_ids": [id_a, id_b],
  "reason": "one sentence explaining why these two positions logically conflict"
}}

If no contradictions exist, return an empty array [].
Return only the JSON array."""

    result = ask_r1_for_json(prompt)
    if not isinstance(result, list):
        return []
    return result


# ── Database updates ────────────────────────────────────────────────────────────

def write_position_history(conn, belief_id, status_from, status_to, reasoning, dry_run):
    """Write a row to position_history whenever a belief changes state."""
    if status_from == status_to:
        return  # No state change — nothing to record

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if dry_run:
        print(f"    [DRY RUN] position_history: belief {belief_id} "
              f"{status_from} → {status_to}")
        return

    c = conn.cursor()
    # Fetch current belief text to record as position snapshot
    c.execute("SELECT position, topic FROM beliefs WHERE id = ?", (belief_id,))
    row = c.fetchone()
    position_text = row[0] if row else ""
    topic         = row[1] if row else ""

    c.execute("""
        INSERT INTO position_history
            (belief_id, previous_position, new_position, status_from, status_to,
             what_changed_it, trigger_event, date, tags, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        belief_id,
        position_text,        # position text unchanged — it's the status that moved
        position_text,        # new_position same; delta is captured in status fields
        status_from,
        status_to,
        reasoning[:500] if reasoning else "",
        "verify_beliefs.py",
        now[:10],
        json.dumps([topic]) if topic else "[]",
        now,
    ))
    conn.commit()


def _negate_and_reembed(conn, belief_id, new_status, dry_run):
    """Re-embed memory_chunks entries for a belief that has been deprecated or disputed.

    Prepends a negation prefix to the chunk content before re-embedding so the
    vector shifts away from the positive-claim region of the space. Future semantic
    queries on that topic will no longer pull this belief to the top of results.

    Called only when new_status is 'deprecated' or 'disputed'. Silently skips if:
      - no chunk links found (belief was never embedded)
      - Ollama is unreachable (status update still proceeds)
    The belief record and all provenance remain untouched.
    """
    if new_status not in ("deprecated", "disputed"):
        return
    if dry_run:
        print(f"    [DRY RUN] Would negate-reembed chunks for belief {belief_id} ({new_status})")
        return

    c = conn.cursor()

    # Find all memory_chunks linked to this belief
    chunk_rows = c.execute(
        "SELECT chunk_id FROM belief_chunk_links WHERE belief_id = ?", (belief_id,)
    ).fetchall()

    if not chunk_rows:
        print(f"    [reembed] belief {belief_id}: no chunk links found, skipping")
        return

    # Fetch belief position for the negation prefix
    row = c.execute("SELECT position, topic FROM beliefs WHERE id = ?", (belief_id,)).fetchone()
    position = row[0] if row else ""
    topic    = row[1] if row else ""

    prefix = f"[REFUTED] — {USERNAME} no longer holds this position: "
    now    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    reembedded = 0
    for (chunk_id,) in chunk_rows:
        # Read current chunk content
        chunk_row = c.execute(
            "SELECT content FROM memory_chunks WHERE id = ?", (chunk_id,)
        ).fetchone()
        if not chunk_row:
            continue
        original_content = chunk_row[0] or ""

        # Build negated text — prefix + original unless already prefixed
        if original_content.startswith("[REFUTED]"):
            negated_text = original_content  # already negated, don't double-prefix
        else:
            negated_text = prefix + original_content

        # Call embedding model
        try:
            resp = requests.post(
                EMBED_URL,
                json={"model": EMBED_MODEL, "prompt": negated_text},
                timeout=60
            )
            resp.raise_for_status()
            vec = resp.json().get("embedding", [])
            if not vec:
                print(f"    [reembed] WARNING: empty embedding for chunk {chunk_id}")
                continue
        except requests.exceptions.ConnectionError:
            print(f"    [reembed] WARNING: Ollama unreachable — skipping re-embedding for belief {belief_id}")
            return  # Give up on all chunks for this belief; log once
        except Exception as e:
            print(f"    [reembed] WARNING: embedding error for chunk {chunk_id}: {e}")
            continue

        # Pack vector as binary float32 (same format as embed_memories.py)
        blob = struct.pack(f"{len(vec)}f", *vec)

        # Update memory_chunks with negated content and new vector
        c.execute("""
            UPDATE memory_chunks
            SET content          = ?,
                embedding_vector = ?,
                embedding_created_at = ?
            WHERE id = ?
        """, (negated_text, blob, now, chunk_id))
        reembedded += 1

    conn.commit()
    print(f"    [reembed] belief {belief_id} ({new_status}): re-embedded {reembedded}/{len(chunk_rows)} chunk(s)")


def update_belief_status(conn, belief_id, verdict, confidence_score, reasoning, challenge, dry_run, conv_id=None):
    """Apply verification result to the beliefs table and record state transition."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Map verdict to status
    status_map = {
        "verified":              "verified",
        "supported":             "supported",
        "disputed":              "disputed",
        "insufficient_evidence": "proposed",   # stay as proposed — not enough to decide
    }
    new_status          = status_map.get(verdict, "proposed")
    confidence_calibrated = 0 if verdict == "insufficient_evidence" else 1

    challenge_entry = None
    if challenge and verdict == "disputed":
        challenge_entry = json.dumps([{
            "date":      now[:10],
            "challenge": challenge,
            "source":    "verify_beliefs.py",
        }])

    if dry_run:
        print(f"    [DRY RUN] Would update belief {belief_id}: status={new_status}, "
              f"confidence_score={confidence_score:.2f}, calibrated={confidence_calibrated}")
        # Still record the dry-run position_history intent
        c = conn.cursor()
        c.execute("SELECT status FROM beliefs WHERE id = ?", (belief_id,))
        row = c.fetchone()
        old_status = row[0] if row else "proposed"
        write_position_history(conn, belief_id, old_status, new_status, reasoning, dry_run=True)
        return

    c = conn.cursor()
    # Read current status before overwriting (needed for position_history)
    c.execute("SELECT status FROM beliefs WHERE id = ?", (belief_id,))
    row = c.fetchone()
    old_status = row[0] if row else "proposed"

    if challenge_entry:
        # Append to existing challenge_history
        c.execute("SELECT challenge_history FROM beliefs WHERE id = ?", (belief_id,))
        row = c.fetchone()
        existing = row[0] if row and row[0] else "[]"
        try:
            history = json.loads(existing)
        except Exception:
            history = []
        history.extend(json.loads(challenge_entry))
        challenge_entry = json.dumps(history)

        c.execute("""
            UPDATE beliefs
            SET status               = ?,
                confidence_score     = ?,
                confidence_calibrated = ?,
                last_verified_at     = ?,
                challenge_history    = ?,
                updated_at           = ?
            WHERE id = ?
        """, (new_status, confidence_score, confidence_calibrated, now,
              challenge_entry, now, belief_id))
    else:
        c.execute("""
            UPDATE beliefs
            SET status               = ?,
                confidence_score     = ?,
                confidence_calibrated = ?,
                last_verified_at     = ?,
                updated_at           = ?
            WHERE id = ?
        """, (new_status, confidence_score, confidence_calibrated, now, now, belief_id))

    conn.commit()

    # Record state transition in position_history (no-op if status unchanged)
    write_position_history(conn, belief_id, old_status, new_status, reasoning, dry_run=False)

    # ── Negation re-embedding (Concern 2 fix) ────────────────────────────────
    # Shift the vector for deprecated/disputed beliefs away from positive-claim
    # space so they stop dominating semantic retrieval on that topic.
    _negate_and_reembed(conn, belief_id, new_status, dry_run=False)

    # ── Write sources record ──────────────────────────────────────────────────
    # Record the conversation that grounded this verdict so sources is populated
    # and challenged_belief_id tracks which beliefs have been externally checked.
    # Skipped for insufficient_evidence (no determinative source found).
    if verdict != "insufficient_evidence":
        _write_source_record(conn, belief_id, verdict, confidence_score, reasoning, conv_id)


def _write_source_record(conn, belief_id, verdict, confidence_score, reasoning, conv_id):
    """Insert one row into sources recording the conversation that verified this belief.

    Silently skips on any error so logging never blocks verification.
    """
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        url = f"conversation:{conv_id}" if conv_id else "conversation:unknown"

        # Try to get a human-readable title from the conversations table
        title = ""
        try:
            row = conn.execute(
                "SELECT source_filename FROM conversations WHERE id = ?", (conv_id,)
            ).fetchone()
            if row and row[0]:
                title = row[0]
        except Exception:
            pass

        conn.execute("""
            INSERT INTO sources
                (url, title, date_fetched, summary, relevance_tags,
                 challenged_belief_id, confidence_score, source_type, created_at)
            VALUES (?, ?, date('now'), ?, ?, ?, ?, 'conversation', ?)
        """, (
            url,
            title or url,
            (reasoning or "")[:500],
            verdict,
            belief_id if verdict == "disputed" else None,
            round(confidence_score, 4),
            now,
        ))
        conn.commit()
    except Exception:
        pass


def write_tension_record(conn, belief_ids, reason, dry_run):
    """Write an explicit tension record to the tensions table for a detected contradiction.

    Severity (importance_score) is derived from the product of both beliefs'
    confidence scores: a tension between two high-confidence core beliefs is
    genuinely urgent; a tension between two low-confidence peripheral beliefs
    is noise. The product captures this cheaply without new infrastructure.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    today = now[:10]
    if len(belief_ids) < 2:
        return
    belief_a_id = belief_ids[0]
    belief_b_id = belief_ids[1]

    # Derive topic from first belief
    row = conn.execute("SELECT topic FROM beliefs WHERE id = ?", (belief_a_id,)).fetchone()
    topic = row["topic"] if row and row["topic"] else "unknown"

    # Severity = product of both beliefs' confidence scores
    # e.g. two verified beliefs (0.9 × 0.9 = 0.81) vs two tentative ones (0.5 × 0.5 = 0.25)
    scores = []
    for bid in (belief_a_id, belief_b_id):
        r = conn.execute("SELECT confidence_score FROM beliefs WHERE id = ?", (bid,)).fetchone()
        if r and r[0]:
            scores.append(float(r[0]))
    severity = round(scores[0] * scores[1], 3) if len(scores) == 2 else 0.5

    if dry_run:
        print(f"    [DRY RUN] Would write tension record: beliefs {belief_ids} | "
              f"topic={topic} | severity={severity:.2f}")
        return

    conn.execute("""
        INSERT INTO tensions
            (topic, belief_a_id, belief_b_id, description, date_identified,
             confidence_score, is_active, importance_score, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
    """, (
        topic,
        belief_a_id,
        belief_b_id,
        reason,
        today,
        severity,
        severity,
        now, now,
    ))
    conn.commit()


def mark_beliefs_disputed(conn, belief_ids, reason, dry_run):
    """Mark a pair of beliefs as disputed due to contradiction."""
    now   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = json.dumps([{
        "date":      now[:10],
        "challenge": f"Contradiction detected: {reason}",
        "source":    "verify_beliefs.py (contradiction check)",
    }])
    for bid in belief_ids:
        if dry_run:
            print(f"    [DRY RUN] Would mark belief {bid} as disputed: {reason}")
            write_position_history(conn, bid, "proposed", "disputed", reason, dry_run=True)
            continue
        c = conn.cursor()
        c.execute("SELECT status, challenge_history FROM beliefs WHERE id = ?", (bid,))
        row = c.fetchone()
        old_status = row[0] if row else "proposed"
        existing   = row[1] if row and row[1] else "[]"
        try:
            history = json.loads(existing)
        except Exception:
            history = []
        history.extend(json.loads(entry))
        c.execute("""
            UPDATE beliefs
            SET status            = 'disputed',
                challenge_history = ?,
                last_verified_at  = ?,
                updated_at        = ?
            WHERE id = ?
        """, (json.dumps(history), now, now, bid))
        conn.commit()
        write_position_history(conn, bid, old_status, "disputed", reason, dry_run=False)


# ── Temporal decay ─────────────────────────────────────────────────────────────

def _decay_stale_beliefs(conn, days_threshold=STALE_DAYS, dry_run=False):
    """Nudge down confidence on beliefs not verified within days_threshold days.

    For each stale belief: apply a small, bounded confidence decay and append
    a challenge_history entry noting the staleness. This makes the confidence
    score more honest about recency — a belief held at 0.9 that hasn't been
    examined in 4 months should read as less certain than a freshly verified one.

    Does NOT change belief status. Does not touch 'disputed' beliefs (already
    flagged). Caps total decay at STALE_MAX_DECAY to avoid bottoming out.
    """
    from datetime import timedelta
    now    = datetime.now()
    cutoff = (now - timedelta(days=days_threshold)).strftime("%Y-%m-%d %H:%M:%S")

    c = conn.cursor()
    c.execute("""
        SELECT id, topic, position, confidence_score, last_verified_at
        FROM beliefs
        WHERE is_active = 1
          AND status != 'disputed'
          AND (last_verified_at IS NULL OR last_verified_at < ?)
        ORDER BY last_verified_at ASC
        LIMIT 50
    """, (cutoff,))
    stale = c.fetchall()

    if not stale:
        print("  No stale beliefs found.")
        return

    print(f"  {len(stale)} stale belief(s) (unverified for {days_threshold}+ days):")

    decayed = 0
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    for row in stale:
        bid, topic, position, score, last_verified = row
        score = score or 0.7

        # Days past threshold
        if last_verified:
            try:
                lv_date = datetime.strptime(last_verified[:10], "%Y-%m-%d")
                days_past_threshold = max(0, (now - lv_date).days - days_threshold)
            except Exception:
                days_past_threshold = days_threshold
        else:
            days_past_threshold = days_threshold * 2  # NULL last_verified = very stale

        months_past = max(1, days_past_threshold // 30)
        decay       = min(months_past * STALE_DECAY_PER_MONTH, STALE_MAX_DECAY)
        new_score   = max(0.20, round(score - decay, 3))

        if new_score >= score:
            continue

        last_str = (last_verified or "never")[:10]
        label    = (topic or position or "")[:60]
        print(f"    ID {bid} | {label}")
        print(f"           {score:.2f} → {new_score:.2f}  "
              f"(last verified: {last_str}, {months_past} month(s) past threshold)")

        if dry_run:
            continue

        challenge_entry = [{
            "date":      now_str[:10],
            "challenge": (f"Confidence decayed: unverified for "
                          f"{days_past_threshold + days_threshold} days "
                          f"(last: {last_str})"),
            "source":    "verify_beliefs.py (temporal decay)",
        }]

        c.execute("SELECT challenge_history FROM beliefs WHERE id = ?", (bid,))
        hist_row = c.fetchone()
        existing = hist_row[0] if hist_row and hist_row[0] else "[]"
        try:
            history = json.loads(existing)
        except Exception:
            history = []
        history.extend(challenge_entry)

        c.execute("""
            UPDATE beliefs
            SET confidence_score  = ?,
                challenge_history = ?,
                updated_at        = ?
            WHERE id = ?
        """, (new_score, json.dumps(history), now_str, bid))
        decayed += 1

    if not dry_run and decayed:
        conn.commit()

    action = "[DRY RUN] Would decay" if dry_run else "Decayed"
    total  = len(stale) if dry_run else decayed
    print(f"  {action} {total} belief(s).")


# ── Main verification pass ──────────────────────────────────────────────────────

def run_verification_pass(limit=20, dry_run=False, status_filter="proposed",
                          check_contradictions=False, all_statuses=False,
                          decay_stale=False):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c    = conn.cursor()
    now  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print(f"\n{'='*60}")
    print(f"Belief Verification Pass")
    print(f"Started:  {now}")
    print(f"Model:    {MODEL}")
    print(f"Dry run:  {dry_run}")
    print(f"{'='*60}\n")

    # ── 1. Load beliefs to verify ─────────────────────────────────────────────
    if all_statuses:
        c.execute("""
            SELECT * FROM beliefs
            WHERE is_active = 1
            ORDER BY created_at ASC
            LIMIT ?
        """, (limit,))
    else:
        c.execute("""
            SELECT * FROM beliefs
            WHERE status = ? AND is_active = 1
            ORDER BY created_at ASC
            LIMIT ?
        """, (status_filter, limit))

    beliefs = [dict(row) for row in c.fetchall()]
    print(f"Beliefs to verify: {len(beliefs)}")

    if not beliefs:
        print("No beliefs found matching criteria. Nothing to do.")
        conn.close()
        return

    # ── 2. Verify each belief against its source conversation ─────────────────
    print("\nVerifying beliefs against source conversations...\n")

    results = {"verified": 0, "supported": 0, "disputed": 0,
               "insufficient_evidence": 0, "r1_failed": 0}

    for idx, belief in enumerate(beliefs, 1):
        bid         = belief["id"]
        topic       = belief["topic"] or "(no topic)"
        position    = (belief["position"] or "")[:80]
        conv_id     = belief.get("source_conversation_id")
        orig_score  = belief.get("confidence_score", 0.5) or 0.5

        print(f"  [{idx:2d}/{len(beliefs)}] ID {bid} | {topic}")
        print(f"           {position}...")

        # Load conversation text
        conv_text = None
        if conv_id:
            conv_text = load_conversation_text(conn, conv_id)
        if not conv_text:
            print(f"           (source conversation not found — using evidence snippets)")

        result = verify_belief(belief, conv_text, conn=conn)

        if not result or not isinstance(result, dict):
            print(f"           R1 failed — skipping\n")
            results["r1_failed"] += 1
            continue

        verdict     = result.get("verdict", "insufficient_evidence")
        new_score   = result.get("confidence_score", orig_score)
        reasoning   = result.get("reasoning", "")
        challenge   = result.get("challenge")
        evidence    = result.get("evidence_found")

        score_delta = ""
        if abs(new_score - orig_score) >= 0.05:
            score_delta = f"  [score {orig_score:.2f} → {new_score:.2f}]"

        print(f"           verdict: {verdict.upper()}{score_delta}")
        if evidence:
            print(f"           evidence: {evidence[:100]}")
        if challenge:
            print(f"           challenge: {challenge[:100]}")
        print()

        results[verdict] = results.get(verdict, 0) + 1
        update_belief_status(conn, bid, verdict, new_score, reasoning, challenge, dry_run, conv_id=conv_id)

    # ── 3. Cross-belief contradiction check ───────────────────────────────────
    if check_contradictions:
        print(f"\n{'─'*60}")
        print("Cross-belief contradiction check...\n")

        # Load all active beliefs grouped by topic
        c.execute("""
            SELECT id, topic, position, status, valid_from, confidence_score
            FROM beliefs
            WHERE is_active = 1
            ORDER BY topic, valid_from
        """)
        all_beliefs = [dict(row) for row in c.fetchall()]

        # Group by topic
        from collections import defaultdict
        by_topic = defaultdict(list)
        for b in all_beliefs:
            t = (b["topic"] or "").strip().lower()
            if t:
                by_topic[t].append(b)

        multi_topic = {t: bs for t, bs in by_topic.items() if len(bs) >= 2}
        print(f"  Topics with 2+ beliefs: {len(multi_topic)}")

        total_contradictions = 0
        for topic, topic_beliefs in sorted(multi_topic.items()):
            contradictions = check_topic_contradictions(topic, topic_beliefs)
            if not contradictions:
                continue
            print(f"  Topic: {topic}")
            for ct in contradictions:
                ids    = ct.get("belief_ids", [])
                reason = ct.get("reason", "")
                print(f"    CONTRADICTION: beliefs {ids} — {reason}")
                total_contradictions += 1
                mark_beliefs_disputed(conn, ids, reason, dry_run)
                write_tension_record(conn, ids, reason, dry_run)

        if total_contradictions == 0:
            print("  No contradictions found.")
        else:
            print(f"\n  {total_contradictions} contradiction(s) found and flagged.")

    # ── 4. Temporal decay pass ────────────────────────────────────────────────
    if decay_stale:
        print(f"\n{'─'*60}")
        print(f"Temporal decay pass (threshold: {STALE_DAYS} days)...\n")
        _decay_stale_beliefs(conn, days_threshold=STALE_DAYS, dry_run=dry_run)

    # ── 5. Summary ────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("Verification complete.")
    print(f"Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    print("Results:")
    for status, count in sorted(results.items()):
        if count > 0:
            print(f"  {status:<25} {count}")

    if not dry_run:
        print()
        print("Updated DB row counts:")
        for status in ["proposed", "supported", "verified", "disputed"]:
            c.execute("SELECT COUNT(*) FROM beliefs WHERE status = ?", (status,))
            n = c.fetchone()[0]
            if n > 0:
                print(f"  {status:<25} {n}")

    print(f"{'='*60}\n")
    conn.close()


# ── Entry point ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import random
    import time

    parser = argparse.ArgumentParser(description="Verify extracted beliefs using DeepSeek R1")
    parser.add_argument("--limit", type=int, default=20,
                        help="Max beliefs to verify in this pass (default: 20)")
    parser.add_argument("--status", default="proposed",
                        choices=["proposed", "supported", "verified", "disputed"],
                        help="Only verify beliefs with this status (default: proposed)")
    parser.add_argument("--all-statuses", action="store_true",
                        help="Verify beliefs regardless of current status")
    parser.add_argument("--check-contradictions", action="store_true",
                        help="Also run cross-belief contradiction check across all active beliefs")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without writing to the database")
    parser.add_argument("--decay-stale", action="store_true",
                        help=(f"Apply confidence decay to beliefs unverified for "
                              f"{STALE_DAYS}+ days "
                              f"({int(STALE_DECAY_PER_MONTH*100)} pts/month, "
                              f"max {int(STALE_MAX_DECAY*100)} pts total)"))
    parser.add_argument("--no-jitter", action="store_true",
                        help="Skip startup jitter delay (use when running manually)")
    args = parser.parse_args()

    # Startup jitter: spread wake-triggered agents across a 5-minute window
    # so they don't all hammer Ollama simultaneously when the Mac wakes after
    # the scheduled time was missed (e.g. machine was off or sleeping at 03:00).
    if not args.no_jitter and not args.dry_run:
        delay = random.randint(0, 300)
        print(f"Startup jitter: sleeping {delay}s before beginning...")
        time.sleep(delay)

    run_verification_pass(
        limit=args.limit,
        dry_run=args.dry_run,
        status_filter=args.status,
        check_contradictions=args.check_contradictions,
        all_statuses=args.all_statuses,
        decay_stale=args.decay_stale,
    )

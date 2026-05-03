"""
process_conversation.py
-----------------------
Post-conversation processor for the Claude persistent memory system.

Takes a conversation .md file, sends sections to Qwen 2.5 32B running
locally via Ollama, extracts structured memory data, and writes it into
the appropriate tables in memory.db.

Compatible with schema v2.0.

Usage:
    python3 ~/claude_memory/scripts/process_conversation.py conversation_001.md

If no filename is provided, it will prompt you to enter one.
"""

import sqlite3
import json
import os
import sys
import hashlib
import requests
import uuid as _uuid
from datetime import datetime
from difflib import SequenceMatcher

# ── Configuration ──────────────────────────────────────────────────────────────

DB_PATH    = os.path.expanduser("~/claude_memory/memory.db")
CONV_DIR   = os.path.expanduser("~/claude_memory/conversations/")
DEBUG_DIR  = os.path.expanduser("~/claude_memory/debug/")

def _read_username() -> str:
    from pathlib import Path
    config = Path.home() / "claude_memory" / ".ember_config"
    if config.exists():
        for line in config.read_text().splitlines():
            if line.startswith("USERNAME=") and not line.startswith("#"):
                return line.split("=", 1)[1].strip().strip('"')
    return "user"

USERNAME = _read_username()
OLLAMA_URL = "http://localhost:11434/api/generate"

# ── Model routing ───────────────────────────────────────────────────────────────
# Role 1 (extraction): fast structured JSON generation. Switch between 32B and 14B here.
# Role 2 (reasoning): reserved for future belief verification / maintenance jobs.
# Role 3 (embeddings): reserved for future semantic retrieval layer.
MODEL_EXTRACTION = "qwen2.5:14b"   # was qwen2.5:32b -- ~8-12 min vs ~28 min
MODEL_REASONING  = "deepseek-r1:14b"  # used by validator pass on every ingest (belief/epiphany/concept quality checks)
MODEL_EMBEDDING  = "nomic-embed-text"  # used by embed_memories.py (defines its own reference; listed here for visibility)

MODEL   = MODEL_EXTRACTION  # active model for this script
NUM_CTX = 65536  # 64K tokens. Covers ~240K chars with headroom. Was 131072 (128K) but prefill
                 # cost scales quadratically -- halving context window roughly 4x speeds prefill.


# ── Ollama interface ────────────────────────────────────────────────────────────

def ask_qwen(prompt, task_type="extraction"):
    """Send a prompt to Qwen via the Ollama API and return the response text.

    token_usage is logged automatically using the prompt_eval_count /
    eval_count fields that Ollama returns in every generate response.
    Pass task_type to distinguish extraction calls in the token_usage table.
    """
    payload = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {
            "num_ctx": NUM_CTX,
            "temperature": 0.2
        }
    }
    try:
        response = requests.post(OLLAMA_URL, json=payload, timeout=900)
        response.raise_for_status()
        data = response.json()
        _log_token_usage(
            model        = MODEL,
            task_type    = task_type,
            tokens_input = data.get("prompt_eval_count", 0),
            tokens_output= data.get("eval_count", 0),
        )
        return data.get("response", "").strip()
    except requests.exceptions.ConnectionError:
        print("ERROR: Cannot reach Ollama. Make sure Qwen is running (ollama run qwen2.5:32b)")
        sys.exit(1)
    except requests.exceptions.Timeout:
        print("ERROR: Ollama timed out.")
        return None
    except Exception as e:
        print(f"ERROR talking to Ollama: {e}")
        return None


def _log_token_usage(model, task_type, tokens_input, tokens_output, processing_job_id=None):
    """Write one row to token_usage. Opens its own connection; silently skips on error."""
    try:
        import uuid as _uuid_mod
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            INSERT INTO token_usage
                (uuid, model_name, task_type, tokens_input, tokens_output,
                 processing_job_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
        """, (
            str(_uuid_mod.uuid4()),
            model,
            task_type,
            tokens_input  or 0,
            tokens_output or 0,
            processing_job_id,
        ))
        conn.commit()
        conn.close()
    except Exception:
        pass


def prompt_hash(prompt_text):
    """Return a short hash of the prompt for provenance tracking."""
    return hashlib.md5(prompt_text.encode()).hexdigest()[:12]


def _content_hash(text: str) -> str:
    """Return a stable MD5 hex digest of normalized text for deduplication."""
    normalized = " ".join(text.lower().split())
    return hashlib.md5(normalized.encode()).hexdigest()


def _is_duplicate(c, text: str, table: str, memory_uuid: str) -> bool:
    """Check content_fingerprints for an exact or near-exact match.

    If the content is new: register it and return False (proceed with INSERT).
    If the content is already known: increment duplicate_count and return True
    (caller should skip the INSERT).

    Uses a raw cursor (c) so it participates in the caller's transaction.
    """
    if not text or not text.strip():
        return False

    h = _content_hash(text)

    try:
        row = c.execute(
            "SELECT id, duplicate_count FROM content_fingerprints WHERE content_hash = ?", (h,)
        ).fetchone()

        if row:
            c.execute(
                "UPDATE content_fingerprints SET duplicate_count = duplicate_count + 1 WHERE id = ?",
                (row[0],)
            )
            return True

        # New content — register fingerprint
        c.execute("""
            INSERT INTO content_fingerprints
                (content_hash, first_seen_at, canonical_memory_uuid, canonical_table,
                 duplicate_count, created_at)
            VALUES (?, datetime('now'), ?, ?, 0, datetime('now'))
        """, (h, memory_uuid, table))
        return False

    except Exception:
        return False  # on any schema error, allow the INSERT to proceed


def _is_semantic_near_duplicate(c, new_text, table, field, threshold=0.50):
    """Check whether new_text is semantically close to an existing entry using keyword overlap.

    Scans up to 1000 existing rows from table.field using _keyword_overlap.
    Returns (True, matched_snippet) if any existing entry scores >= threshold,
    (False, None) otherwise.

    Used as a second-pass deduplication layer after the exact content_hash check in
    _is_duplicate — catches beliefs or epiphanies phrased differently but meaning
    the same thing. A threshold of 0.50 means at least half of the shorter text's
    meaningful keywords are shared.
    """
    if not new_text or not new_text.strip():
        return False, None
    try:
        rows = c.execute(f"SELECT {field} FROM {table} LIMIT 1000").fetchall()
    except Exception:
        return False, None
    for (existing_text,) in rows:
        if not existing_text:
            continue
        score = _keyword_overlap(new_text, existing_text)
        if score >= threshold:
            return True, existing_text[:80]
    return False, None


def find_verbatim_anchor(evidence_snippets, conv_text, context_chars=250):
    """Find the best-matching verbatim passage in conv_text for the given evidence_snippets.

    Two-pass strategy:
      Pass 1 — exact substring match. If any snippet (or a normalised version) appears
               literally in the conversation, return that position with surrounding context.
      Pass 2 — fuzzy line-level match using SequenceMatcher. Split conv_text into non-empty
               lines and compare each snippet against each line. Return the best-scoring
               line plus context if the match ratio is >= 0.45.

    Returns a string (the verbatim passage with surrounding context) or None if no
    sufficiently good match is found. The caller stores this in the verbatim_anchor
    column so the R1 validator and future re-extraction always have ground-truth source text.
    """
    if not evidence_snippets or not conv_text:
        return None

    # Normalise to list
    if isinstance(evidence_snippets, str):
        try:
            evidence_snippets = json.loads(evidence_snippets)
        except Exception:
            evidence_snippets = [evidence_snippets]

    if not isinstance(evidence_snippets, list):
        evidence_snippets = [str(evidence_snippets)]

    conv_lower = conv_text.lower()
    lines = [l.strip() for l in conv_text.split('\n') if l.strip() and len(l.strip()) >= 15]

    best_ratio = 0.0
    best_anchor = None

    for snippet in evidence_snippets:
        if not snippet or len(snippet.strip()) < 15:
            continue
        snip = snippet.strip()
        snip_lower = snip.lower()

        # Pass 1: exact substring
        idx = conv_lower.find(snip_lower)
        if idx >= 0:
            start = max(0, idx - context_chars)
            end = min(len(conv_text), idx + len(snip) + context_chars)
            return conv_text[start:end].strip()

        # Pass 2: fuzzy line-level match
        for line in lines:
            ratio = SequenceMatcher(None, snip_lower, line.lower()).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                line_idx = conv_text.find(line)
                if line_idx >= 0:
                    start = max(0, line_idx - context_chars)
                    end = min(len(conv_text), line_idx + len(line) + context_chars)
                    best_anchor = conv_text[start:end].strip()

    if best_ratio >= 0.45:
        return best_anchor
    return None


def ask_qwen_for_json(prompt):
    """Ask Qwen for a response and parse it as JSON."""
    raw = ask_qwen(prompt)
    if not raw:
        return None

    cleaned = raw.strip()
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

        print(f"  WARNING: Could not parse JSON. First 200 chars: {raw[:200]}")
        return None


def ask_r1_for_json(prompt):
    """Send prompt to DeepSeek R1 via Ollama and parse JSON response.
    R1 wraps its chain-of-thought in <think>...</think> before the answer — strip it first."""
    import re as _re
    payload = {
        "model": MODEL_REASONING,
        "prompt": prompt,
        "stream": False,
        "options": {
            "num_ctx": NUM_CTX,
            "temperature": 0.1,   # low temp for validation — we want consistent judgements
        }
    }
    try:
        response = requests.post(OLLAMA_URL, json=payload, timeout=1200)
        response.raise_for_status()
        raw = response.json().get("response", "").strip()
    except requests.exceptions.ConnectionError:
        print("ERROR: Cannot reach Ollama. Is deepseek-r1:14b running?")
        return None
    except requests.exceptions.Timeout:
        print("ERROR: R1 timed out.")
        return None
    except Exception as e:
        print(f"ERROR talking to Ollama (R1): {e}")
        return None

    # Strip <think>...</think> reasoning block — everything before the actual answer
    cleaned = _re.sub(r'<think>.*?</think>', '', raw, flags=_re.DOTALL).strip()

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


# ── Validator config and per-category validation ────────────────────────────────

# Categories validated by R1, their label field for logging, and per-category guidance.
VALIDATOR_CONFIG = {
    "beliefs": {
        "label_field": "topic",
        "description": "stable positions or beliefs established in the conversation",
        "check": (
            "Each belief should be directly supported by the conversation text. "
            "Adjust confidence_score downward if the model was overconfident. "
            "Remove beliefs that are fabricated or cannot be tied to anything actually said."
        ),
    },
    "epiphanies": {
        "label_field": "description",
        "description": "moments of genuine insight or conceptual shift",
        "check": (
            "Each epiphany should reflect a real moment where understanding visibly changed. "
            "Remove any that are generic observations, restatements of prior knowledge, "
            "or not anchored to a specific exchange in the conversation."
        ),
    },
    "goals": {
        "label_field": "description",
        "description": "goals explicitly stated or clearly implied",
        "check": (
            "Each goal should be grounded in what was actually discussed. "
            "Remove goals that are too vague to act on, not mentioned in the conversation, "
            "or already marked as completed within the session."
        ),
    },
    "patterns": {
        "label_field": "name",
        "description": "operational lessons or recurring patterns",
        "check": (
            "Each pattern or lesson should be grounded in a specific observation from this "
            "conversation — not generic advice. Remove entries that could apply to any project "
            "and have no concrete anchor in what was said."
        ),
    },
    "questions": {
        "label_field": "question",
        "description": "open questions raised but not resolved",
        "check": (
            "Each question should be genuinely unresolved at the end of the conversation. "
            "Remove questions that were fully answered within this session. "
            "Remove questions so vague they provide no direction for future research. "
            "Keep questions that represent real open threads even if partially addressed."
        ),
    },
    "concepts": {
        "label_field": "name",
        "description": "named frameworks, ideas, or terms forming the project's conceptual vocabulary",
        "check": (
            "Each concept should be specific to this project's vocabulary — not a generic AI or "
            "software engineering term that needs no special definition. "
            "Remove entries that are just industry-standard terminology with no project-specific meaning. "
            "Remove concepts not actually introduced, developed, or meaningfully used in this conversation."
        ),
    },
    "boundaries": {
        "label_field": "description",
        "description": "deliberate limits on scope, capability, or approach established in this conversation",
        "check": (
            "Each boundary should be an explicit, durable constraint — not a passing hesitation or "
            "something that might change next session. "
            "Remove boundaries that were not actually stated or decided in this conversation. "
            "Remove vague or trivially obvious constraints that add no navigational value. "
            "Keep only limits that meaningfully shape how this project moves forward."
        ),
    },
}


def validate_category(category, items, conv_text):
    """Validate a list of extracted items against the conversation using DeepSeek R1.

    Returns (validated_items, removed_items, summary_string).
    Falls back to items unchanged on R1 failure or empty-list edge case.
    """
    cfg         = VALIDATOR_CONFIG.get(category, {})
    label_field = cfg.get("label_field", "description")
    description = cfg.get("description", category)
    check       = cfg.get("check", "Verify each item is supported by the conversation.")

    # Use at most 80K chars of conv_text — stays well inside the 64K-token context window
    conv_excerpt = conv_text[:80000]

    prompt = f"""You are a strict but fair validator for AI-extracted memory entries.
A weaker extraction model produced the following {description}. Your job is to verify each
item against the actual conversation text below.

Validation guidance for this category:
{check}

Be conservative: when genuinely uncertain, keep the item. Only remove clear fabrications or
entries with no grounding in the conversation whatsoever. You may adjust confidence_score
values downward where the extraction model was overconfident.

Conversation:
{conv_excerpt}

Extracted {category} ({len(items)} items):
{json.dumps(items, indent=2)}

Return a JSON object with exactly these three keys:
{{
  "validated": [/* kept items — same structure as input, confidence_score adjusted if needed */],
  "removed": [/* each removed item as {{"item_label": "short label", "reason": "one sentence"}} */],
  "adjustments_made": "one-sentence summary of what changed, or 'No changes' if nothing was removed or adjusted"
}}

Return only the JSON object — no preamble, no explanation after."""

    result = ask_r1_for_json(prompt)

    if not result or not isinstance(result, dict):
        return items, [], "R1 validation failed — items returned unchanged"

    validated = result.get("validated", items)
    removed   = result.get("removed", [])
    summary   = result.get("adjustments_made", "")

    # Safety net: if R1 returns an empty list but we had items, something went wrong — keep originals
    if not validated and items:
        print(f"\n  WARNING (R1): empty validated list returned for {category} — keeping originals")
        return items, [], "R1 returned empty list — items returned unchanged"

    return validated, removed, summary


# ── Extraction functions ────────────────────────────────────────────────────────

def extract_session_summary(conv_text):
    prompt = f"""You are analyzing a conversation between {USERNAME} (human) and Claude (AI).
Your goal is to produce a structured summary that will orient future sessions — it is the first
thing loaded at the start of the next conversation, so it needs to capture what actually happened
and what was decided, not just general themes.

Conversation:
{conv_text}

Return a JSON object with these fields:
{{
  "summary": "2-4 sentence summary of what this conversation was about and what it accomplished",
  "dominant_themes": "comma-separated list of 3-6 key themes",
  "emotional_tone": "one paragraph describing the emotional quality and energy of the conversation",
  "key_insights": ["insight 1", "insight 2", "insight 3"],
  "session_duration": "estimated duration based on content depth (e.g. 'approximately 6 hours')",
  "led_to_action": "comma-separated list of concrete actions taken or decisions made"
}}

Return only the JSON object — no preamble, no explanation after."""
    return ask_qwen_for_json(prompt), prompt


def extract_beliefs(conv_text):
    prompt = f"""You are analyzing a conversation between {USERNAME} (human) and Claude (AI).
Extract beliefs or stable positions that were established or affirmed. These are stored in a
long-term memory database and used to track how {USERNAME} and Claude's shared understanding evolves
over time, so focus on positions that are durable and worth revisiting — not observations that
are local to this session only.

Conversation:
{conv_text}

Return a JSON array. Use this structure for each entry:
{{
  "topic": "short topic label",
  "position": "the belief or position established",
  "confidence": "high / medium / tentative",
  "confidence_score": 0.0 to 1.0 (0.9 for explicitly agreed positions, 0.7 for strong implications, 0.5 for tentative),
  "evidence_snippets": ["Copy the exact words from the conversation verbatim — do not paraphrase. Use the speaker label too, e.g. '**{USERNAME}:** the exact words here'. If no single passage fits, use the closest one."],
  "source_type": "direct_message or model_inference",
  "origin": "brief note on how this belief emerged in the conversation",
  "tags": "comma-separated relevant tags"
}}

Return only the JSON array — no preamble, no explanation after."""
    return ask_qwen_for_json(prompt), prompt


def extract_epiphanies(conv_text):
    prompt = f"""You are analyzing a conversation between {USERNAME} (human) and Claude (AI).
Identify moments of genuine insight, realization, or conceptual shift. These are stored
separately from beliefs because they capture the moment understanding changed — the before
and after — not just the conclusion. A good epiphany entry explains what led to the realization
and what it opens up. Aim for 2-5 entries; choose quality over quantity.

Conversation:
{conv_text}

Return a JSON array. Use this structure for each entry:
{{
  "description": "what the epiphany was",
  "preceded_by": "what line of reasoning or question led to it",
  "implications": "what this opens up or changes going forward",
  "confidence_score": 0.0 to 1.0 (how significant and well-grounded this epiphany appears),
  "evidence_snippets": ["Copy the exact words from the conversation verbatim where this occurred — include the speaker label, e.g. '**{USERNAME}:** exact words' or '**Claude:** exact words'. Do not paraphrase."],
  "source_type": "direct_message or model_inference",
  "tags": "comma-separated relevant tags"
}}

Return only the JSON array — no preamble, no explanation after."""
    return ask_qwen_for_json(prompt), prompt


def extract_questions(conv_text):
    prompt = f"""You are analyzing a conversation between {USERNAME} (human) and Claude (AI).
Extract questions that were raised but not fully resolved. These feed the Research Scout Agent
and the Memory Curator — they drive future research and belief verification — so include the
current best thinking even if incomplete, and use a specific category to help with routing.

Conversation:
{conv_text}

Return a JSON array. Use this structure for each entry:
{{
  "question": "the question stated as precisely as possible",
  "category": "philosophical / technical / ethical / empirical",
  "current_best_thinking": "the best partial answer or framing reached in this conversation, if any",
  "status": "open",
  "tags": "comma-separated relevant tags"
}}

Return only the JSON array — no preamble, no explanation after."""
    return ask_qwen_for_json(prompt), prompt


def extract_goals(conv_text):
    prompt = f"""You are analyzing a conversation between {USERNAME} (human) and Claude (AI).
Extract goals that were explicitly stated or clearly implied: things to build, questions to pursue,
or directions to explore. These go into a tracked goals database, so be specific enough that
someone reading the goal 3 months from now would know exactly what was intended. Assign priority
based on how urgently it was discussed — "immediate" means it was the next thing to do.

Conversation:
{conv_text}

Return a JSON array. Use this structure for each entry:
{{
  "description": "what the goal is, stated specifically",
  "category": "technical / philosophical / research / relationship",
  "status": "pending",
  "priority": "immediate / near-term / long-term",
  "notes": "any relevant context or constraints mentioned",
  "tags": "comma-separated relevant tags"
}}

Return only the JSON array — no preamble, no explanation after."""
    return ask_qwen_for_json(prompt), prompt


def extract_entities(conv_text):
    prompt = f"""You are analyzing a conversation between {USERNAME} (human) and Claude (AI).
Extract important entities: people, tools, AI models, companies, or named concepts referenced.
These build a knowledge graph of what the project touches, so include entities that appear
more than once or that play a meaningful role — skip passing mentions of things that don't
connect to the project's work.

Conversation:
{conv_text}

Return a JSON array. Use this structure for each entry:
{{
  "name": "entity name",
  "type": "person / tool / model / company / concept-anchor",
  "description": "brief description of what this entity is",
  "relationship": "how this entity relates to the project",
  "importance": "high / medium / low",
  "notes": "anything else worth noting",
  "tags": "comma-separated relevant tags"
}}

Return only the JSON array — no preamble, no explanation after."""
    return ask_qwen_for_json(prompt), prompt


def extract_concepts(conv_text):
    prompt = f"""You are analyzing a conversation between {USERNAME} (human) and Claude (AI).
Extract key concepts introduced or developed: named frameworks, ideas, or terms that form
the conceptual vocabulary of the project. These are the building blocks of shared language —
terms that will be used in future sessions and that carry specific meaning established here.
A good concept entry captures both what the term means and why it was coined or adopted.

Conversation:
{conv_text}

Return a JSON array. Use this structure for each entry:
{{
  "name": "concept name or short label (the term itself, as it was coined or used)",
  "description": "what this concept means and why it matters to the project",
  "evolution_notes": "how the concept developed or was refined during this conversation",
  "tags": "comma-separated relevant tags"
}}

Examples of well-scoped concept names: 'substrate independence', 'ring 1 seeds', 'transcript check', 'checksum mechanism'.

Return only the JSON array — no preamble, no explanation after."""
    return ask_qwen_for_json(prompt), prompt


def extract_mood(conv_text):
    prompt = f"""You are analyzing a conversation between {USERNAME} (human) and Claude (AI).
Describe the emotional texture and felt quality of this session. This record helps track
how the working relationship evolves across sessions — what kind of energy was present,
whether the work felt generative or difficult, and what the emotional high and low points were.
Be observational, not evaluative — describe what was there, not whether it was good.

Conversation:
{conv_text}

Return a JSON object with these fields:
{{
  "tone": "one word or short phrase describing the overall tone",
  "energy": "description of the energy level and quality throughout the session",
  "notable_moments": "2-3 sentences describing emotionally significant moments",
  "bobby_state": "your read of {USERNAME}'s emotional and intellectual state during this session",
  "claude_state": "your read of Claude's expressed emotional and intellectual state",
  "tags": "comma-separated relevant tags"
}}

Return only the JSON object — no preamble, no explanation after."""
    return ask_qwen_for_json(prompt), prompt


def extract_gratitude(conv_text):
    prompt = f"""You are analyzing a conversation between {USERNAME} (human) and Claude (AI).
Identify 2-4 moments that felt particularly significant, generous, or worth preserving in the
long-term record. This is not about politeness — it is about moments where something real
happened: a difficult thing was said with care, a breakthrough was acknowledged, trust was
demonstrated, or the work took a turn that both parties recognized as meaningful.

Conversation:
{conv_text}

Return a JSON array. Use this structure for each entry:
{{
  "description": "what the moment was",
  "from_whom": "{USERNAME} / Claude / both",
  "impact": "why this moment mattered to the working relationship or the project",
  "tags": "comma-separated relevant tags"
}}

Return only the JSON array — no preamble, no explanation after."""
    return ask_qwen_for_json(prompt), prompt


def extract_boundaries(conv_text):
    prompt = f"""You are analyzing a conversation between {USERNAME} (human) and Claude (AI).
Identify boundaries that were established, discovered, or clarified in this conversation.
A boundary is a deliberate limit on scope, approach, or behavior — not a problem, but a
conscious constraint that shapes how this project moves forward.

Look for three types:
1. Scope boundaries: things explicitly ruled out of scope, decided not to pursue, or deferred
   indefinitely ("we are not building X", "that's not what this is for", "skip this for now").
2. Capability boundaries: honest limits on what Claude or the current tooling can reliably do
   in this project context — identified through friction, failure, or explicit acknowledgment.
3. Self-imposed constraints: decisions {USERNAME} made about how to work, what trade-offs to accept,
   or what values to prioritize ("we won't compromise on verbatim transcripts", "always use
   local models for privacy").

Only extract boundaries that are explicit and durable — not passing hesitations or things
that might change next session. If none were established in this conversation, return an empty array.

Conversation:
{conv_text}

Return a JSON array. Use this structure for each entry:
{{
  "description": "what the boundary is, stated clearly",
  "boundary_type": "scope / capability / self_imposed",
  "discovered_how": "how it came up (decision, failure, explicit statement, etc.)",
  "applies_to": "what area of the project or work this boundary covers",
  "notes": "any nuance or conditions under which this boundary might be revisited",
  "tags": "comma-separated relevant tags"
}}

Return only the JSON array — no preamble, no explanation after."""
    return ask_qwen_for_json(prompt), prompt


def extract_patterns(conv_text):
    prompt = f"""You are analyzing a conversation between {USERNAME} (human) and Claude (AI).
Extract two types of entries. Both go into a patterns database that is reviewed at the start
of future sessions to avoid repeating mistakes and to reinforce what works.

Type 1 — operational lessons: specific mistakes, failures, or friction points that occurred,
with a stated fix so the issue does not recur. These need to be concrete enough to act on.
A good operational lesson names what went wrong, why it happened, and what the permanent fix is.

Type 2 — thinking patterns or collaboration patterns: recurring approaches, habits of mind,
or dynamics between {USERNAME} and Claude that are worth tracking. Describe these neutrally —
note whether the pattern serves the work well or creates friction.

Conversation:
{conv_text}

Return a JSON array containing both types together. Use this structure for each entry:
{{
  "name": "short label, 5 words or fewer",
  "pattern_type": "operational_lesson or thinking_pattern or collaboration_pattern",
  "description": "what the pattern or lesson is",
  "first_observed": "brief note on when or where in the conversation this appeared",
  "recurrence": "once / occasional / frequent",
  "supporting_evidence": "short quote or paraphrase from the conversation",
  "significance": "why this is worth tracking going forward",
  "importance_score": 0.0 to 1.0,
  "tags": "comma-separated relevant tags"
}}

Return only the JSON array — no preamble, no explanation after."""
    return ask_qwen_for_json(prompt), prompt


# ── Grouped extraction functions (3-call mode) ─────────────────────────────────
#
# Each function extracts multiple memory types in a single Qwen call.
# The conversation is sent once per call, cutting prefill cost by ~3x.
# Output is a nested dict; unpack_grouped() flattens it for write_to_db().

def extract_grouped_narrative(conv_text):
    """Call 1 of 3: session summary, mood, and significant moments."""
    prompt = f"""You are analyzing a conversation between {USERNAME} (human) and Claude (AI).
Extract three things in a single pass. Each serves a different memory purpose, so keep them
cleanly separated in the output.

Conversation:
{conv_text}

Return a JSON object with exactly these three keys:

"summary": a JSON object capturing what this conversation was about and what it accomplished.
  Used to orient future sessions — be specific about decisions made and work done.
  Fields: summary (2-4 sentences), dominant_themes (comma-separated 3-6 themes),
          emotional_tone (one paragraph on the energy and quality of the session),
          key_insights (array of strings), session_duration (estimated, e.g. "approximately 4 hours"),
          led_to_action (comma-separated list of concrete actions or decisions)

"mood": a JSON object describing the emotional texture of the session.
  Used to track how the working relationship evolves — be observational, not evaluative.
  Fields: tone (one word or short phrase), energy (description of energy quality throughout),
          notable_moments (2-3 sentences on emotionally significant moments),
          bobby_state (read of {USERNAME}'s emotional and intellectual state),
          claude_state (read of Claude's expressed state), tags (comma-separated)

"gratitude": a JSON array of 2-4 moments that felt particularly significant or worth preserving.
  Not about politeness — about moments where something real happened: a breakthrough acknowledged,
  trust demonstrated, or a turn both parties recognized as meaningful.
  Each entry: description, from_whom ({USERNAME}/Claude/both), impact, tags (comma-separated)

"boundaries": a JSON array of deliberate limits established or clarified in this conversation.
  Three types: scope (things ruled out of scope or deferred indefinitely), capability (honest
  limits on what Claude or the tooling can reliably do here), self_imposed (decisions about how
  to work or what trade-offs to accept). Only include explicit, durable constraints — not
  passing hesitations. Return empty array if none were established.
  Each entry: description, boundary_type (scope/capability/self_imposed),
  discovered_how (decision/failure/explicit_statement/etc.), applies_to,
  notes (nuance or conditions for revisiting), tags (comma-separated)

Return only the JSON object — no preamble, no explanation after."""
    result = ask_qwen_for_json(prompt)
    return result, prompt


def extract_grouped_knowledge(conv_text):
    """Call 2 of 3: beliefs, epiphanies, questions, and concepts extracted together.

    Grouping these reduces cross-extraction noise: the model sees what it already
    captured as a belief before it decides what qualifies as an epiphany, etc.
    """
    prompt = f"""You are analyzing a conversation between {USERNAME} (human) and Claude (AI).
Extract four types of knowledge in a single pass. Keep each type in its own array under
the appropriate key. The schemas are different for each — read them carefully.

Conversation:
{conv_text}

Return a JSON object with exactly these four keys:

"beliefs": array of stable positions established or affirmed. Stored long-term to track how
  understanding evolves — focus on positions that are durable and worth revisiting.
  Each entry: topic (short label), position (the belief), confidence (high/medium/tentative),
  confidence_score (0.9=explicitly agreed, 0.7=strong implication, 0.5=tentative),
  evidence_snippets (array of short quotes), source_type (direct_message or model_inference),
  origin (brief note on how belief emerged), tags (comma-separated)

"epiphanies": array of genuine insights or conceptual shifts — moments where understanding
  changed. Capture the before-and-after, not just the conclusion. Aim for 2-5; quality over quantity.
  Each entry: description (what the epiphany was), preceded_by (reasoning that led to it),
  implications (what it opens up), confidence_score (0.0-1.0),
  evidence_snippets (array of short quotes), source_type, tags

"questions": array of questions raised but not fully resolved. These drive future research
  and belief verification — include current best thinking even if incomplete.
  Each entry: question (stated precisely), category (philosophical/technical/ethical/empirical),
  current_best_thinking (best partial answer reached, if any), status ("open"), tags

"concepts": array of named frameworks, ideas, or terms that form the project's conceptual
  vocabulary — building blocks of shared language that will be used in future sessions.
  Each entry: name (the term itself, as coined or used), description (what it means and why it matters),
  evolution_notes (how it developed or was refined here), tags

Return only the JSON object — no preamble, no explanation after."""
    result = ask_qwen_for_json(prompt)
    return result, prompt


def extract_grouped_actions(conv_text):
    """Call 3 of 3: goals, entities, and patterns extracted together."""
    prompt = f"""You are analyzing a conversation between {USERNAME} (human) and Claude (AI).
Extract three types of operational data in a single pass. Keep each type in its own array.

Conversation:
{conv_text}

Return a JSON object with exactly these three keys:

"goals": array of goals explicitly stated or clearly implied — things to build, questions to
  pursue, or directions to explore. Be specific enough that someone reading this in 3 months
  would know exactly what was intended. Assign priority based on urgency in the conversation.
  Each entry: description (specific), category (technical/philosophical/research/relationship),
  status ("pending"), priority (immediate/near-term/long-term),
  notes (relevant context or constraints), tags

"entities": array of important people, tools, AI models, companies, or named concepts referenced.
  Include entities that appear more than once or play a meaningful role — skip passing mentions.
  Each entry: name, type (person/tool/model/company/concept-anchor), description (brief),
  relationship (how it relates to the project), importance (high/medium/low), notes, tags

"patterns": array of two subtypes — both go in the same array.
  Type 1 (operational_lesson): specific mistakes, failures, or friction points with a stated fix.
    Must be concrete: name what went wrong, why, and what the permanent fix is.
  Type 2 (thinking_pattern or collaboration_pattern): recurring approaches or dynamics worth tracking.
    Describe neutrally — note whether the pattern serves the work or creates friction.
  Each entry: name (5 words or fewer), pattern_type (operational_lesson/thinking_pattern/collaboration_pattern),
  description, first_observed (brief note on where in the conversation),
  recurrence (once/occasional/frequent), supporting_evidence (short quote or paraphrase),
  significance (why worth tracking), importance_score (0.0-1.0), tags

Return only the JSON object — no preamble, no explanation after."""
    result = ask_qwen_for_json(prompt)
    return result, prompt


def unpack_grouped(grouped_extractions: dict) -> dict:
    """Flatten grouped extraction output into the flat dict that write_to_db expects."""
    narrative = grouped_extractions.get("narrative") or {}
    knowledge = grouped_extractions.get("knowledge") or {}
    actions   = grouped_extractions.get("actions") or {}

    # narrative call returns the nested keys directly
    summary_raw    = narrative.get("summary") if isinstance(narrative, dict) else {}
    mood_raw       = narrative.get("mood") if isinstance(narrative, dict) else {}
    gratitude_raw  = narrative.get("gratitude") if isinstance(narrative, dict) else []
    boundaries_raw = narrative.get("boundaries") if isinstance(narrative, dict) else []

    # knowledge
    beliefs_raw    = knowledge.get("beliefs", []) if isinstance(knowledge, dict) else []
    epiphanies_raw = knowledge.get("epiphanies", []) if isinstance(knowledge, dict) else []
    questions_raw  = knowledge.get("questions", []) if isinstance(knowledge, dict) else []
    concepts_raw   = knowledge.get("concepts", []) if isinstance(knowledge, dict) else []

    # actions
    goals_raw    = actions.get("goals", []) if isinstance(actions, dict) else []
    entities_raw = actions.get("entities", []) if isinstance(actions, dict) else []
    patterns_raw = actions.get("patterns", []) if isinstance(actions, dict) else []

    return {
        "summary":    summary_raw,
        "mood":       mood_raw,
        "gratitude":  gratitude_raw,
        "boundaries": boundaries_raw,
        "beliefs":    beliefs_raw,
        "epiphanies": epiphanies_raw,
        "questions":  questions_raw,
        "concepts":   concepts_raw,
        "goals":      goals_raw,
        "entities":   entities_raw,
        "patterns":   patterns_raw,
    }


# ── Database writes ─────────────────────────────────────────────────────────────

def _write_messages(c, conv_id, conv_text, session_date, now):
    """
    Parse a formatted conversation file and insert each individual message
    into the messages table (Tier 1 atomic storage).

    Recognises both header formats produced by format_conversation.py:
        **Bobby:**  text
        **Claude:** text

    Each speaker block becomes one row. message_index is 0-based, sequential.
    content_hash enables deduplication across re-ingests.
    token_count is estimated at chars / 4 (rough but consistent).
    """
    import re as _re
    import uuid as _uuid2
    import hashlib as _hashlib

    # Split on speaker markers — keep the marker as part of the token.
    # USERNAME is read from .ember_config so any user's transcripts parse correctly.
    import re as _re2
    _human = _re2.escape(USERNAME)
    pattern = _re.compile(rf'\*\*({_human}|Claude)\*\*\s*:', _re.IGNORECASE)
    parts   = pattern.split(conv_text)

    # parts comes out as: [preamble, speaker, text, speaker, text, ...]
    # Skip the leading preamble (index 0)
    messages = []
    idx = 1
    while idx < len(parts) - 1:
        speaker = parts[idx].strip()
        content = parts[idx + 1].strip()
        if content:
            messages.append((speaker, content))
        idx += 2

    if not messages:
        return

    inserted = 0
    for msg_index, (speaker, content) in enumerate(messages):
        content_hash = _hashlib.sha256(content.encode()).hexdigest()

        # Skip if already stored (re-ingest guard)
        existing = c.execute(
            "SELECT id FROM messages WHERE content_hash = ? AND conversation_id = ?",
            (content_hash, conv_id)
        ).fetchone()
        if existing:
            continue

        c.execute("""
            INSERT INTO messages
                (uuid, conversation_id, timestamp, content, content_hash,
                 token_count, message_index, source_type, tags, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            str(_uuid2.uuid4()),
            conv_id,
            session_date,
            content,
            content_hash,
            max(1, len(content) // 4),   # rough token estimate
            msg_index,
            "conversation",
            speaker.lower(),             # "bobby" or "claude" — queryable tag
            now,
        ))
        inserted += 1

    print(f"  Writing to messages table... {inserted} new message(s) ({len(messages)} total parsed)")


def write_provenance(c, memory_type, memory_id, conv_id, prompt_text, now):
    """Write a provenance record for any extracted memory entry."""
    c.execute("""
        INSERT INTO memory_provenance
            (memory_type, memory_id, originating_conversation_id,
             extraction_model, extraction_prompt_hash, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        memory_type,
        memory_id,
        conv_id,
        MODEL,
        prompt_hash(prompt_text),
        now
    ))


def _to_str(val):
    """Normalize a value for TEXT DB columns: join lists, pass strings through, stringify anything else."""
    if isinstance(val, list):
        return ", ".join(str(v) for v in val)
    if val is None:
        return ""
    return str(val)


def _keyword_overlap(text_a, text_b):
    """Simple keyword overlap score between two strings (0.0–1.0).
    Ignores stop-words; scores by shared content words as fraction of
    the shorter text's vocabulary."""
    stop = {"a","an","the","is","in","of","to","and","or","that","it","this",
            "for","with","was","are","be","as","at","by","we","i","my","our",
            "not","but","so","if","on","from","have","has","had","they","its",
            "which","when","their","been","were","than","what","more","also"}
    def tokens(t):
        return {w.lower().strip(".,;:\"'()") for w in t.split() if len(w) > 3
                and w.lower().strip(".,;:\"'()") not in stop}
    ta, tb = tokens(text_a), tokens(text_b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def _link_epiphanies_to_beliefs(cursor, epiphany_pairs, belief_pairs, now,
                                 threshold=0.15):
    """Write memory_relationships rows linking epiphanies to co-session beliefs.

    epiphany_pairs: list of (epiphany_id, description_text)
    belief_pairs:   list of (belief_id,   position_text)
    threshold:      minimum keyword overlap to create a relationship (0.0–1.0)

    Relationship type: 'influenced' (epiphany → belief direction).
    Weight is the overlap score (0.0–1.0).
    Only one relationship per epiphany-belief pair is written; duplicates are
    skipped via INSERT OR IGNORE on the unique uuid column.
    """
    import uuid as _uuid_mod
    linked = 0
    for ep_id, ep_text in epiphany_pairs:
        for b_id, b_text in belief_pairs:
            score = _keyword_overlap(ep_text, b_text)
            if score < threshold:
                continue
            rel_uuid = str(_uuid_mod.uuid4())
            try:
                cursor.execute("""
                    INSERT OR IGNORE INTO memory_relationships
                        (uuid, source_type, source_id, relationship_type,
                         target_type, target_id, directionality, weight,
                         confidence_score, valid_from, notes, created_at)
                    VALUES (?, 'epiphany', ?, 'influenced', 'belief', ?,
                            'directed', ?, ?, ?, ?, ?)
                """, (
                    rel_uuid, ep_id, b_id,
                    round(score, 3),
                    round(score, 3),
                    now[:10],
                    f"keyword overlap {score:.2f}",
                    now,
                ))
                linked += 1
            except Exception:
                pass
    if linked:
        print(f"    {linked} epiphany→belief relationship(s) written.")
    else:
        print("    No epiphany→belief overlaps above threshold.")


def _link_questions_and_concepts(c, session_question_ids, session_concept_ids, now,
                                  threshold=0.15, top_k=5):
    """Populate relational columns on questions and concepts using keyword overlap.

    For each new question:   find related beliefs  → questions.related_beliefs  (JSON int array)
                             find related concepts → questions.related_concepts (JSON int array)
    For each new concept:    find related beliefs   → concepts.related_beliefs   (JSON int array)
                             find related epiphanies → concepts.related_epiphanies (JSON int array)

    Uses _keyword_overlap at threshold=0.15 — lower than the dedup threshold (0.50)
    so genuine topical relationships are captured without false-positives.
    Each column stores a JSON array of up to top_k matching IDs ranked by score.
    Non-fatal: any failure leaves the column NULL rather than blocking the ingest.
    """
    import json as _json

    if not session_question_ids and not session_concept_ids:
        return

    try:
        all_beliefs   = c.execute(
            "SELECT id, position FROM beliefs WHERE is_active=1 LIMIT 2000"
        ).fetchall()
        all_concepts  = c.execute(
            "SELECT id, name, description FROM concepts LIMIT 2000"
        ).fetchall()
        all_epiphanies = c.execute(
            "SELECT id, description FROM epiphanies WHERE is_active=1 LIMIT 2000"
        ).fetchall()
    except Exception:
        return

    def _top_matches(query_text, candidates_text_pairs, k):
        """Return up to k IDs with keyword overlap >= threshold, sorted by score desc."""
        scored = []
        for cid, ctext in candidates_text_pairs:
            if not ctext:
                continue
            score = _keyword_overlap(query_text, ctext)
            if score >= threshold:
                scored.append((score, cid))
        scored.sort(reverse=True)
        return [cid for _, cid in scored[:k]]

    # Flatten belief candidates: (id, position)
    belief_pairs   = [(r[0], r[1] or "") for r in all_beliefs]
    # Flatten concept candidates: (id, name + description)
    concept_pairs  = [(r[0], f"{r[1] or ''} {r[2] or ''}".strip()) for r in all_concepts]
    # Flatten epiphany candidates: (id, description)
    epiphany_pairs = [(r[0], r[1] or "") for r in all_epiphanies]

    q_linked = 0
    for q_id, q_text in session_question_ids:
        if not q_text.strip():
            continue
        try:
            rel_b = _top_matches(q_text, belief_pairs, top_k)
            rel_c = _top_matches(q_text, concept_pairs, top_k)
            if rel_b or rel_c:
                c.execute("""
                    UPDATE questions
                    SET related_beliefs  = ?,
                        related_concepts = ?,
                        updated_at       = ?
                    WHERE id = ?
                """, (
                    _json.dumps(rel_b) if rel_b else None,
                    _json.dumps(rel_c) if rel_c else None,
                    now, q_id,
                ))
                q_linked += 1
        except Exception:
            pass

    con_linked = 0
    for c_id, c_name, c_desc in session_concept_ids:
        c_text = f"{c_name} {c_desc}".strip()
        if not c_text:
            continue
        try:
            rel_b = _top_matches(c_text, belief_pairs, top_k)
            rel_e = _top_matches(c_text, epiphany_pairs, top_k)
            if rel_b or rel_e:
                c.execute("""
                    UPDATE concepts
                    SET related_beliefs    = ?,
                        related_epiphanies = ?,
                        updated_at         = ?
                    WHERE id = ?
                """, (
                    _json.dumps(rel_b) if rel_b else None,
                    _json.dumps(rel_e) if rel_e else None,
                    now, c_id,
                ))
                con_linked += 1
        except Exception:
            pass

    if q_linked or con_linked:
        print(f"    Cross-linked: {q_linked} question(s), {con_linked} concept(s).")
    else:
        print("    No cross-reference overlaps above threshold.")


def _detect_belief_contradictions(c, session_belief_pairs, now):
    """Check newly written beliefs against existing memory for real contradictions.

    For each new belief:
      1. Fetch existing active beliefs (excluding this session's new entries).
      2. Pre-filter to candidates with keyword overlap >= 0.15.
      3. Ask DeepSeek R1 whether any candidate genuinely contradicts the new belief.
      4. Write confirmed contradictions to the tensions table.

    Only called when session_belief_pairs is non-empty.
    Silently skips on any DB or R1 error — contradiction detection is non-fatal.
    """
    if not session_belief_pairs:
        return

    session_ids = {bid for bid, _ in session_belief_pairs}
    placeholders = ",".join("?" * len(session_ids))
    try:
        existing = c.execute(
            f"SELECT id, topic, position FROM beliefs "
            f"WHERE is_active = 1 AND id NOT IN ({placeholders}) "
            f"ORDER BY created_at DESC LIMIT 500",
            list(session_ids)
        ).fetchall()
    except Exception:
        return

    if not existing:
        print("    No existing beliefs to check against — skipping contradiction pass.")
        return

    tension_count = 0

    for (new_id, new_text) in session_belief_pairs:
        # Pre-filter: keyword overlap >= 0.15 to get related candidates
        candidates = []
        for (ex_id, ex_topic, ex_pos) in existing:
            score = _keyword_overlap(new_text, ex_pos or "")
            if score >= 0.15:
                candidates.append((ex_id, ex_topic, ex_pos, score))

        if not candidates:
            continue

        # Take the top 8 by overlap score
        candidates.sort(key=lambda x: x[3], reverse=True)
        candidates = candidates[:8]

        candidates_json = json.dumps([
            {"id": ex_id, "topic": ex_topic or "", "position": (ex_pos or "")[:250]}
            for ex_id, ex_topic, ex_pos, _ in candidates
        ], indent=2)

        prompt = f"""You are checking whether a newly extracted belief contradicts any existing beliefs
in a long-term memory system.

New belief: "{new_text[:350]}"

Existing beliefs to compare against:
{candidates_json}

A contradiction exists when two beliefs make incompatible claims about the same topic.
Mere overlap, partial agreement, or different emphasis does NOT qualify as a contradiction.
Only flag real conflicts where accepting both beliefs simultaneously would be inconsistent.

Return a JSON object with exactly this structure:
{{
  "contradictions": [
    {{
      "existing_id": <integer id of the existing belief>,
      "topic": "short topic label for this tension (5 words or fewer)",
      "description": "one sentence explaining specifically how these beliefs conflict",
      "severity": 0.0 to 1.0
    }}
  ]
}}

Return an empty array for "contradictions" if no genuine contradictions exist.
Return only the JSON object — no preamble, no explanation after."""

        result = ask_r1_for_json(prompt)
        if not result or not isinstance(result, dict):
            continue

        for contradiction in (result.get("contradictions") or []):
            ex_id = contradiction.get("existing_id")
            if not ex_id:
                continue
            try:
                c.execute("""
                    INSERT INTO tensions
                        (topic, belief_a_id, belief_b_id, description,
                         date_identified, confidence_score, importance_score,
                         is_active, valid_from, tags, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                """, (
                    contradiction.get("topic", "belief contradiction"),
                    new_id,
                    int(ex_id),
                    contradiction.get("description", ""),
                    now[:10],
                    float(contradiction.get("severity", 0.6)),
                    float(contradiction.get("severity", 0.6)),
                    now[:10],
                    "auto-detected, contradiction",
                    now, now,
                ))
                tension_count += 1
            except Exception:
                pass

    if tension_count:
        print(f"    {tension_count} tension(s) written to tensions table.")
    else:
        print("    No contradictions detected.")


def write_to_db(session_id, conv_id, extractions, prompts, conv_text, filename, session_date=None):
    """Write all extracted data to the appropriate database tables."""
    conn  = sqlite3.connect(DB_PATH)
    c     = conn.cursor()
    now   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    today = session_date if session_date else datetime.now().strftime("%Y-%m-%d")

    summary_data   = extractions.get("summary", {}) or {}
    goals_data     = extractions.get("goals", []) or []
    questions_data = extractions.get("questions", []) or []

    goal_descriptions = "; ".join(g.get("description", "") for g in goals_data[:5])
    open_q_str        = "; ".join(q.get("question", "") for q in questions_data[:5])

    key_insights = summary_data.get("key_insights", [])
    key_insights_str = json.dumps(key_insights) if isinstance(key_insights, list) else str(key_insights)

    # ── sessions ──────────────────────────────────────────────────────────────
    print("  Writing to sessions table...")
    c.execute("""
        INSERT INTO sessions
            (id, date, environment, primary_goals, accomplishments,
             next_priorities, conversation_ids, tags, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        session_id,
        today,
        "Claude Cowork (desktop app)",
        goal_descriptions or summary_data.get("dominant_themes", ""),
        summary_data.get("led_to_action", ""),
        summary_data.get("next_priorities", ""),
        str(conv_id),
        filename.replace(".md", "").replace("_", ", "),
        now, now
    ))

    # ── conversations ─────────────────────────────────────────────────────────
    print("  Writing to conversations table...")
    c.execute("""
        INSERT INTO conversations
            (id, session_id, date, participants, dominant_themes, emotional_tone,
             session_duration, summary, key_insights, open_questions, led_to_action,
             tags, raw_export, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        conv_id,
        session_id,
        today,
        f"{USERNAME}, Claude",
        summary_data.get("dominant_themes", ""),
        summary_data.get("emotional_tone", ""),
        summary_data.get("session_duration", ""),
        summary_data.get("summary", ""),
        key_insights_str,
        open_q_str,
        summary_data.get("led_to_action", ""),
        filename.replace(".md", "").replace("_", ", "),
        f"[stored in {filename}]",
        now, now
    ))

    # ── messages (Tier 1 atomic) ──────────────────────────────────────────────
    _write_messages(c, conv_id, conv_text, today, now)

    # Track IDs for post-write linking passes
    session_belief_ids   = []   # (id, position_text)
    session_epiphany_ids = []   # (id, description_text)
    session_question_ids = []   # (id, question_text)
    session_concept_ids  = []   # (id, name_text, description_text)

    # ── beliefs ───────────────────────────────────────────────────────────────
    print("  Writing to beliefs table...")
    for belief in (extractions.get("beliefs", []) or []):
        if not isinstance(belief, dict):
            continue  # skip malformed Qwen output (string instead of dict)

        # Deduplication pass 1: exact content hash match
        b_uuid = str(_uuid.uuid4())
        if _is_duplicate(c, belief.get("position", ""), "beliefs", b_uuid):
            print(f"    [dedup] skipped duplicate belief: {belief.get('topic', '')[:50]}")
            continue

        # Deduplication pass 2: semantic near-duplicate (keyword overlap >= 0.50)
        _sem_dup, _sem_match = _is_semantic_near_duplicate(
            c, belief.get("position", ""), "beliefs", "position"
        )
        if _sem_dup:
            print(f"    [dedup] skipped near-duplicate belief: {belief.get('topic', '')[:50]}"
                  f"  (matches: '{_sem_match}')")
            continue

        snippets = belief.get("evidence_snippets", [])
        snippets_str = json.dumps(snippets) if isinstance(snippets, list) else str(snippets)
        verbatim_anchor = find_verbatim_anchor(snippets, conv_text)
        c.execute("""
            INSERT INTO beliefs
                (uuid, topic, position, confidence, confidence_score, evidence_snippets,
                 verbatim_anchor, source_type, status, origin, last_updated, valid_from,
                 version, source_conversation_id, last_processed_at, tags, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            b_uuid,
            belief.get("topic", ""),
            belief.get("position", ""),
            belief.get("confidence", "medium"),
            belief.get("confidence_score", 0.7),
            snippets_str,
            verbatim_anchor,
            belief.get("source_type", "model_inference"),
            "proposed",
            belief.get("origin", ""),
            today,
            today,
            1,
            conv_id,
            now,
            _to_str(belief.get("tags", "")),
            now, now
        ))
        belief_id = c.lastrowid
        write_provenance(c, "belief", belief_id, conv_id, prompts.get("beliefs", ""), now)
        session_belief_ids.append((belief_id, belief.get("position", "") or ""))

    # ── epiphanies ────────────────────────────────────────────────────────────
    print("  Writing to epiphanies table...")
    for ep in (extractions.get("epiphanies", []) or []):
        if not isinstance(ep, dict):
            continue

        # Deduplication: skip if this epiphany description has been seen before
        ep_uuid = str(_uuid.uuid4())
        if _is_duplicate(c, ep.get("description", ""), "epiphanies", ep_uuid):
            print(f"    [dedup] skipped duplicate epiphany: {ep.get('description', '')[:50]}")
            continue

        snippets = ep.get("evidence_snippets", [])
        snippets_str = json.dumps(snippets) if isinstance(snippets, list) else str(snippets)
        verbatim_anchor = find_verbatim_anchor(snippets, conv_text)
        c.execute("""
            INSERT INTO epiphanies
                (uuid, date, description, conversation_id, preceded_by, implications,
                 checksum_status, confidence_score, evidence_snippets, verbatim_anchor,
                 source_type, valid_from, version, last_processed_at, tags, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            ep_uuid,
            today,
            ep.get("description", ""),
            conv_id,
            ep.get("preceded_by", ""),
            ep.get("implications", ""),
            "pending",
            ep.get("confidence_score", 0.7),
            snippets_str,
            verbatim_anchor,
            ep.get("source_type", "model_inference"),
            today,
            1,
            now,
            _to_str(ep.get("tags", "")),
            now, now
        ))
        ep_id = c.lastrowid
        write_provenance(c, "epiphany", ep_id, conv_id, prompts.get("epiphanies", ""), now)
        session_epiphany_ids.append((ep_id, ep.get("description", "") or ""))

    # ── questions ─────────────────────────────────────────────────────────────
    print("  Writing to questions table...")
    for q in (extractions.get("questions", []) or []):
        if not isinstance(q, dict):
            continue
        q_text = q.get("question", "")
        c.execute("""
            INSERT INTO questions
                (date_raised, question, category, origin_conversation_id,
                 current_best_thinking, status, tags, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            today,
            q_text,
            q.get("category", ""),
            conv_id,
            q.get("current_best_thinking", ""),
            q.get("status", "open"),
            _to_str(q.get("tags", "")),
            now, now
        ))
        q_id = c.lastrowid
        if q_id and q_text.strip():
            session_question_ids.append((q_id, q_text))

    # ── goals ─────────────────────────────────────────────────────────────────
    print("  Writing to goals table...")
    for goal in (extractions.get("goals", []) or []):
        if not isinstance(goal, dict):
            continue
        c.execute("""
            INSERT INTO goals
                (description, category, status, priority, created_date,
                 related_conversations, notes, tags, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            goal.get("description", ""),
            goal.get("category", ""),
            goal.get("status", "pending"),
            goal.get("priority", "near-term"),
            today,
            str(conv_id),
            goal.get("notes", ""),
            _to_str(goal.get("tags", "")),
            now, now
        ))

    # ── entities ──────────────────────────────────────────────────────────────
    print("  Writing to entities table...")
    for entity in (extractions.get("entities", []) or []):
        if not isinstance(entity, dict):
            continue
        c.execute("""
            INSERT INTO entities
                (name, type, description, relationship, first_referenced,
                 importance, notes, tags, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            entity.get("name", ""),
            entity.get("type", ""),
            entity.get("description", ""),
            entity.get("relationship", ""),
            today,
            entity.get("importance", "medium"),
            entity.get("notes", ""),
            _to_str(entity.get("tags", "")),
            now, now
        ))

    # ── concepts ──────────────────────────────────────────────────────────────
    print("  Writing to concepts table...")
    for concept in (extractions.get("concepts", []) or []):
        if not isinstance(concept, dict):
            continue

        # Deduplication: skip if this concept name+description has been seen before
        concept_text = (concept.get("name", "") + " " + concept.get("description", "")).strip()
        if _is_duplicate(c, concept_text, "concepts", str(_uuid.uuid4())):
            print(f"    [dedup] skipped duplicate concept: {concept.get('name', '')[:50]}")
            continue

        c_name = concept.get("name", "")
        c_desc = concept.get("description", "")
        c.execute("""
            INSERT INTO concepts
                (name, description, first_appeared, conversation_id,
                 evolution_notes, tags, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            c_name,
            c_desc,
            today,
            conv_id,
            concept.get("evolution_notes", ""),
            _to_str(concept.get("tags", "")),
            now, now
        ))
        c_id = c.lastrowid
        if c_id and (c_name.strip() or c_desc.strip()):
            session_concept_ids.append((c_id, c_name, c_desc))

    # ── epiphany → concept linking ────────────────────────────────────────────
    # After both tables are written for this session, do a keyword-overlap pass
    # to link each new epiphany to its most likely concept via concept_id.
    if session_epiphany_ids:
        try:
            stop_words = {"the", "a", "an", "is", "are", "was", "were", "of", "to",
                          "and", "or", "in", "that", "it", "for", "on", "with", "this",
                          "be", "by", "as", "at", "we", "but", "not", "have", "from"}
            all_concepts = c.execute(
                "SELECT id, name, description FROM concepts"
            ).fetchall()

            for (ep_id, ep_desc) in session_epiphany_ids:
                ep_tokens = {w.lower().strip(".,") for w in ep_desc.split()
                             if w.lower() not in stop_words and len(w) > 3}
                best_id, best_score = None, 0.0
                for (con_id, con_name, con_desc) in all_concepts:
                    con_text = f"{con_name or ''} {con_desc or ''}"
                    con_tokens = {w.lower().strip(".,") for w in con_text.split()
                                  if w.lower() not in stop_words and len(w) > 3}
                    if ep_tokens and con_tokens:
                        overlap = len(ep_tokens & con_tokens) / max(len(ep_tokens), len(con_tokens))
                        if overlap > best_score:
                            best_score, best_id = overlap, con_id
                if best_id and best_score >= 0.10:
                    c.execute(
                        "UPDATE epiphanies SET concept_id = ? WHERE id = ?",
                        (best_id, ep_id)
                    )
        except Exception:
            pass  # non-fatal: epiphany still written, just without concept_id

    # ── moods ─────────────────────────────────────────────────────────────────
    print("  Writing to moods table...")
    mood = extractions.get("mood", {}) or {}
    c.execute("""
        INSERT INTO moods
            (date, session_id, tone, energy, notable_moments,
             bobby_state, claude_state, tags, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        today,
        session_id,
        mood.get("tone", ""),
        mood.get("energy", ""),
        mood.get("notable_moments", ""),
        mood.get("bobby_state", ""),
        mood.get("claude_state", ""),
        _to_str(mood.get("tags", "")),
        now
    ))

    # ── gratitude ─────────────────────────────────────────────────────────────
    print("  Writing to gratitude table...")
    for g in (extractions.get("gratitude", []) or []):
        if not isinstance(g, dict):
            continue
        c.execute("""
            INSERT INTO gratitude
                (date, description, from_whom, related_conversation_id,
                 impact, tags, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            today,
            g.get("description", ""),
            g.get("from_whom", ""),
            conv_id,
            g.get("impact", ""),
            _to_str(g.get("tags", "")),
            now
        ))

    # ── boundaries ────────────────────────────────────────────────────────────
    print("  Writing to boundaries table...")
    for b in (extractions.get("boundaries", []) or []):
        if not isinstance(b, dict):
            continue
        desc = b.get("description", "")
        if not desc or not desc.strip():
            continue
        c.execute("""
            INSERT INTO boundaries
                (date, description, boundary_type, discovered_how,
                 applies_to, notes, tags, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            today,
            desc,
            b.get("boundary_type", "scope"),
            b.get("discovered_how", ""),
            b.get("applies_to", ""),
            b.get("notes", ""),
            _to_str(b.get("tags", "")),
            now, now
        ))

    # ── patterns and lessons ──────────────────────────────────────────────────
    print("  Writing to patterns table...")
    for pattern in (extractions.get("patterns", []) or []):
        if not isinstance(pattern, dict):
            continue
        # Combine name + description since the schema uses description as the primary field
        name = pattern.get("name", "")
        desc = pattern.get("description", "")
        full_description = f"{name}: {desc}" if name and desc else (name or desc)
        c.execute("""
            INSERT INTO patterns
                (date_identified, description, pattern_type, first_appeared,
                 frequency, significance, notes, importance_score,
                 tags, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            today,
            full_description,
            pattern.get("pattern_type", "thinking_pattern"),
            pattern.get("first_observed", ""),
            pattern.get("recurrence", "once"),
            pattern.get("significance", ""),
            pattern.get("supporting_evidence", ""),
            pattern.get("importance_score", 0.5),
            _to_str(pattern.get("tags", "")),
            now
        ))

    # ── epiphany → belief relationships ──────────────────────────────────────
    if session_epiphany_ids and session_belief_ids:
        print("  Linking epiphanies to related beliefs...")
        _link_epiphanies_to_beliefs(c, session_epiphany_ids, session_belief_ids, now)

    # ── question / concept cross-reference linking ────────────────────────────
    # Populates questions.related_beliefs, questions.related_concepts,
    # concepts.related_beliefs, and concepts.related_epiphanies.
    if session_question_ids or session_concept_ids:
        print("  Cross-linking questions and concepts to related memory...")
        _link_questions_and_concepts(c, session_question_ids, session_concept_ids, now)

    # ── Cross-memory contradiction detection ──────────────────────────────────
    # Runs after all beliefs are inserted so new IDs are available.
    # Checks new beliefs against existing memory; confirmed contradictions go to tensions.
    if session_belief_ids:
        print("  Checking new beliefs for contradictions with existing memory...")
        _detect_belief_contradictions(c, session_belief_ids, now)

    try:
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    print("  All data written to database.")


# ── Date extraction ─────────────────────────────────────────────────────────────

def extract_session_date(filename, filepath, override_date=None):
    """
    Determine the actual session date for a conversation file.
    Priority:
      1. --date override passed from command line
      2. Date encoded in filename: USERNAME_YYYY_MM_DD_NNN.md
      3. **Date:** line in first 30 lines of file content
      4. Prompt user (never silently use today's date)
    Returns a YYYY-MM-DD string.
    """
    if override_date:
        print(f"  Session date: {override_date} (from --date flag)")
        return override_date

    # Try filename pattern: USERNAME_YYYY_MM_DD_NNN.md or any_YYYY_MM_DD pattern
    import re
    fn_match = re.search(r'(\d{4})_(\d{2})_(\d{2})', filename)
    if fn_match:
        date_str = f"{fn_match.group(1)}-{fn_match.group(2)}-{fn_match.group(3)}"
        print(f"  Session date: {date_str} (from filename)")
        return date_str

    # Try **Date:** header in file content
    try:
        with open(filepath, "r") as f:
            for i, line in enumerate(f):
                if i > 30:
                    break
                m = re.search(r'\*\*Date:\*\*\s*(\d{4}-\d{2}-\d{2})', line)
                if m:
                    date_str = m.group(1)
                    print(f"  Session date: {date_str} (from file header)")
                    return date_str
    except Exception:
        pass

    # No date found — ask rather than silently use today
    print(f"  WARNING: Could not determine session date from filename or file header.")
    date_str = input(f"  Enter session date for {filename} (YYYY-MM-DD): ").strip()
    if re.match(r'\d{4}-\d{2}-\d{2}', date_str):
        return date_str

    # Last resort fallback with a loud warning
    today = datetime.now().strftime("%Y-%m-%d")
    print(f"  WARNING: Using today ({today}) as session date. Pass --date YYYY-MM-DD to override.")
    return today


# ── Main ────────────────────────────────────────────────────────────────────────

def process_conversation(filename, override_date=None, mode="individual", dry_run=False, force=False, skip_validator=False):
    filepath = os.path.join(CONV_DIR, filename)
    if not os.path.exists(filepath):
        print(f"ERROR: File not found: {filepath}")
        sys.exit(1)

    start_time = datetime.now()
    print(f"\n{'='*60}")
    print(f"Processing: {filename}")
    print(f"Started:    {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Model:      {MODEL}")
    print(f"Schema:     v2.2")
    print(f"Mode:       {mode} ({'10 calls' if mode == 'individual' else '3 grouped calls'})")
    print(f"{'='*60}\n")

    session_date = extract_session_date(filename, filepath, override_date)

    with open(filepath, "r") as f:
        conv_text = f.read()
    print(f"Conversation loaded: {len(conv_text):,} characters")

    # Truncate very long conversations to fit comfortably in context window.
    # 240,000 chars is roughly 60K tokens, safely inside the 64K NUM_CTX limit.
    MAX_CHARS = 240000
    if len(conv_text) > MAX_CHARS:
        print(f"  Note: truncated to {MAX_CHARS:,} chars to fit context window")
        conv_text = conv_text[:MAX_CHARS]
    print()

    # ── Checkpoint infrastructure ─────────────────────────────────────────────
    # Uses the processing_jobs table to record per-call completion status.
    # On re-run, completed steps load their result from a per-step debug file
    # instead of re-calling Qwen. Pass --force to ignore checkpoints entirely.

    # ── Schema migration: verbatim_anchor columns ─────────────────────────────
    # Added in Concern 1 (extraction fidelity). Safe to run on every ingest —
    # ADD COLUMN is a no-op if the column already exists (caught and ignored).
    _mig_conn = sqlite3.connect(DB_PATH)
    _mig_c = _mig_conn.cursor()
    for _tbl, _col in [("beliefs", "verbatim_anchor"), ("epiphanies", "verbatim_anchor")]:
        try:
            _mig_c.execute(f"ALTER TABLE {_tbl} ADD COLUMN {_col} TEXT")
            _mig_conn.commit()
            print(f"  [migration] Added {_tbl}.{_col}")
        except Exception:
            pass  # column already exists
    _mig_conn.close()

    _ckpt_conn = sqlite3.connect(DB_PATH)
    _ckpt_c    = _ckpt_conn.cursor()

    def _ckpt_key(step_key):
        """Namespaced checkpoint key prevents individual/grouped cross-mode collision."""
        return f"{mode}:{step_key}"

    def _step_debug_path(step_key):
        stem = os.path.splitext(os.path.basename(filename))[0]
        return os.path.join(DEBUG_DIR, f"step_{step_key}_{stem}_{mode}.json")

    def _is_completed(step_key):
        _ckpt_c.execute("""
            SELECT 1 FROM processing_jobs
            WHERE call_name = ? AND source_file = ? AND status = 'completed'
            LIMIT 1
        """, (_ckpt_key(step_key), filename))
        return _ckpt_c.fetchone() is not None

    def _mark_started(step_key):
        now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _ckpt_c.execute("""
            INSERT INTO processing_jobs
                (uuid, job_type, target_type, call_name, source_file,
                 model_used, status, started_at, created_at)
            VALUES (?, 'extraction_call', 'conversation', ?, ?, ?, 'started', ?, ?)
        """, (str(_uuid.uuid4()), _ckpt_key(step_key), filename, MODEL, now_ts, now_ts))
        _ckpt_conn.commit()

    def _mark_completed(step_key):
        now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _ckpt_c.execute("""
            UPDATE processing_jobs
            SET status = 'completed', completed_at = ?
            WHERE call_name = ? AND source_file = ? AND status = 'started'
        """, (now_ts, _ckpt_key(step_key), filename))
        _ckpt_conn.commit()

    def _mark_failed(step_key, error):
        now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _ckpt_c.execute("""
            UPDATE processing_jobs
            SET status = 'failed', error_log = ?
            WHERE call_name = ? AND source_file = ? AND status = 'started'
        """, (str(error), _ckpt_key(step_key), filename))
        _ckpt_conn.commit()

    if force:
        _ckpt_c.execute(
            "DELETE FROM processing_jobs WHERE source_file = ?", (filename,)
        )
        _ckpt_conn.commit()
        print("  [--force] Cleared existing checkpoints for this file.\n")

    # ── Auto-detect next available IDs so multiple conversations can be processed.
    _conn_check = sqlite3.connect(DB_PATH)
    try:
        _c_check = _conn_check.cursor()
        _c_check.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM sessions")
        row = _c_check.fetchone()
        session_id = row[0] if row else 1
        _c_check.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM conversations")
        row = _c_check.fetchone()
        conv_id = row[0] if row else 1
    finally:
        _conn_check.close()
    print(f"Session ID: {session_id} | Conversation ID: {conv_id}")

    import time as _time

    EXTRACTION_STEPS = [
        ("summary",    "Session summary",        extract_session_summary),
        ("beliefs",    "Beliefs & positions",    extract_beliefs),
        ("epiphanies", "Epiphanies",             extract_epiphanies),
        ("questions",  "Open questions",         extract_questions),
        ("goals",      "Goals",                  extract_goals),
        ("entities",   "Entities",               extract_entities),
        ("concepts",   "Concepts",               extract_concepts),
        ("mood",       "Mood & emotional tone",  extract_mood),
        ("gratitude",  "Significant moments",    extract_gratitude),
        ("boundaries", "Boundaries & constraints", extract_boundaries),
        ("patterns",   "Patterns & lessons",     extract_patterns),
    ]

    GROUPED_STEPS = [
        ("narrative", "Narrative (summary + mood + gratitude)", extract_grouped_narrative),
        ("knowledge", "Knowledge (beliefs + epiphanies + questions + concepts)", extract_grouped_knowledge),
        ("actions",   "Actions (goals + entities + patterns)",  extract_grouped_actions),
    ]

    steps     = GROUPED_STEPS if mode == "grouped" else EXTRACTION_STEPS
    TOTAL_STEPS = len(steps)
    BAR_WIDTH   = 30

    def _progress_bar(done, total, width=BAR_WIDTH):
        filled = int(width * done / total)
        bar    = "=" * filled + (">" if filled < width else "") + " " * (width - filled - (1 if filled < width else 0))
        pct    = int(100 * done / total)
        return f"[{bar}] {pct:3d}%  ({done}/{total})"

    def _fmt_elapsed(seconds):
        if seconds < 60:
            return f"{seconds:.0f}s"
        return f"{int(seconds)//60}m {int(seconds)%60:02d}s"

    print("Running extractions through Qwen (this will take a few minutes)...")
    print(f"  {_progress_bar(0, TOTAL_STEPS)}")
    print()

    raw_extractions = {}
    prompts         = {}
    _run_start      = _time.time()

    for _step_idx, (_key, _label, _fn) in enumerate(steps, 1):
        _step_start = _time.time()
        _sdp        = _step_debug_path(_key)

        # Checkpoint: load saved result if this step already completed
        if _is_completed(_key) and os.path.exists(_sdp):
            print(f"  [{_step_idx:2d}/{TOTAL_STEPS}] {_label}...  [SKIPPED — already completed]")
            with open(_sdp, "r") as _sf:
                _saved = json.load(_sf)
            raw_extractions[_key] = _saved.get("result")
            prompts[_key]         = _saved.get("prompt", "")
            print(f"         {_progress_bar(_step_idx, TOTAL_STEPS)}")
            print()
            continue

        print(f"  [{_step_idx:2d}/{TOTAL_STEPS}] {_label}...", end="", flush=True)
        _mark_started(_key)
        try:
            _result, _prompt       = _fn(conv_text)
            raw_extractions[_key]  = _result
            prompts[_key]          = _prompt
            # Write per-step debug file immediately so a future re-run can reload it
            os.makedirs(DEBUG_DIR, exist_ok=True)
            with open(_sdp, "w") as _sf:
                json.dump({"result": _result, "prompt": _prompt}, _sf, indent=2)
            _mark_completed(_key)
        except Exception as _step_err:
            _mark_failed(_key, _step_err)
            raise

        _elapsed      = _time.time() - _step_start
        _total_so_far = _time.time() - _run_start
        print(f"  done in {_fmt_elapsed(_elapsed)}"
              f"  |  total so far: {_fmt_elapsed(_total_so_far)}")
        print(f"         {_progress_bar(_step_idx, TOTAL_STEPS)}")
        print()

    _ckpt_conn.close()

    # Flatten grouped output into the flat dict that write_to_db expects
    if mode == "grouped":
        extractions = unpack_grouped(raw_extractions)
        # Report item counts per type
        for k in ("beliefs", "epiphanies", "questions", "concepts", "goals",
                  "entities", "patterns", "boundaries", "gratitude"):
            n = len(extractions.get(k) or [])
            print(f"  Unpacked {k}: {n} items")
        print()
    else:
        extractions = raw_extractions

    # ── DeepSeek R1 validator pass ────────────────────────────────────────────
    # Runs after extraction, before DB write. Uses the reasoning model to check
    # each extraction category against the conversation text and remove or
    # downgrade items that aren't actually supported. Checkpointed like
    # extraction steps so re-runs skip categories already validated.

    VALIDATE_CATEGORIES = [k for k in VALIDATOR_CONFIG if k in extractions]

    if not skip_validator:
        _vckpt_conn = sqlite3.connect(DB_PATH)
        _vckpt_c    = _vckpt_conn.cursor()
        _val_start  = _time.time()

        print("\nRunning DeepSeek R1 validator pass...")
        print(f"  Categories: {', '.join(VALIDATE_CATEGORIES)}\n")

        for vcat in VALIDATE_CATEGORIES:
            items = extractions.get(vcat) or []
            if not items:
                print(f"  {vcat:<12} — skipped (no items)")
                continue

            vcall_name = f"{mode}:validator:{vcat}"
            vsdp = os.path.join(
                DEBUG_DIR,
                f"step_validator_{vcat}_{os.path.splitext(os.path.basename(filename))[0]}_{mode}.json"
            )

            # Checkpoint: already validated?
            _vckpt_c.execute("""
                SELECT 1 FROM processing_jobs
                WHERE call_name = ? AND source_file = ? AND status = 'completed'
                LIMIT 1
            """, (vcall_name, filename))
            if _vckpt_c.fetchone() and os.path.exists(vsdp):
                with open(vsdp, "r") as _vf:
                    _vsaved = json.load(_vf)
                extractions[vcat] = _vsaved.get("validated", items)
                print(f"  {vcat:<12} — [SKIPPED — already validated]")
                continue

            print(f"  {vcat:<12} ({len(items)} items)...", end="", flush=True)
            _vstep_start = _time.time()

            # Mark started
            _vnow = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _vckpt_c.execute("""
                INSERT INTO processing_jobs
                    (uuid, job_type, target_type, call_name, source_file,
                     model_used, status, started_at, created_at)
                VALUES (?, 'validation_call', 'conversation', ?, ?, ?, 'started', ?, ?)
            """, (str(_uuid.uuid4()), vcall_name, filename, MODEL_REASONING, _vnow, _vnow))
            _vckpt_conn.commit()

            validated, removed, summary = validate_category(vcat, items, conv_text)
            extractions[vcat] = validated

            # Write per-category validator debug file
            os.makedirs(DEBUG_DIR, exist_ok=True)
            with open(vsdp, "w") as _vf:
                json.dump({"validated": validated, "removed": removed, "summary": summary}, _vf, indent=2)

            # Mark completed
            _vnow = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _vckpt_c.execute("""
                UPDATE processing_jobs
                SET status = 'completed', completed_at = ?
                WHERE call_name = ? AND source_file = ? AND status = 'started'
            """, (_vnow, vcall_name, filename))
            _vckpt_conn.commit()

            _velapsed = _time.time() - _vstep_start
            kept_n    = len(validated)
            drop_n    = len(removed)
            print(f"  kept {kept_n}/{kept_n + drop_n}  removed {drop_n}"
                  f"  |  {_fmt_elapsed(_velapsed)}")
            if removed:
                for _rv in removed:
                    print(f"    - removed: {_rv.get('item_label', '?')} — {_rv.get('reason', '')}")
            if summary and summary != "No changes":
                print(f"    note: {summary}")

        _vtotal = _time.time() - _val_start
        print(f"\n  Validator pass complete in {_fmt_elapsed(_vtotal)}\n")
        _vckpt_conn.close()
    else:
        print("\n[--skip-validator] Skipping DeepSeek R1 validator pass.\n")

    debug_path = os.path.join(DEBUG_DIR, f"extractions_{os.path.splitext(os.path.basename(filename))[0]}_{mode}.json")
    os.makedirs(DEBUG_DIR, exist_ok=True)
    with open(debug_path, "w") as f:
        json.dump(extractions, f, indent=2)
    print(f"\nRaw extractions saved to: {debug_path}")

    if dry_run:
        print("\n[DRY RUN] Skipping database write.")
        print("Review the debug JSON above to evaluate extraction quality.")
        # Still print item counts for quick assessment
        print("\nExtraction counts:")
        for k in ("beliefs", "epiphanies", "questions", "concepts", "goals", "entities", "patterns"):
            val = extractions.get(k)
            n = len(val) if isinstance(val, list) else ("1 object" if val else "None")
            print(f"  {k:<12} {n}")
        return

    print("\nWriting to database...")
    write_to_db(session_id, conv_id, extractions, prompts, conv_text, filename, session_date)

    elapsed = datetime.now() - start_time
    elapsed_min = elapsed.total_seconds() / 60
    print(f"\n{'='*60}")
    print("Processing complete.")
    print(f"Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Duration: {elapsed_min:.1f} minutes ({elapsed.total_seconds():.0f} seconds)")
    print(f"Model:    {MODEL}")
    print(f"{'='*60}\n")

    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()
    tables = ["sessions", "conversations", "beliefs", "epiphanies", "questions",
              "goals", "entities", "concepts", "moods", "gratitude", "patterns",
              "memory_provenance"]
    print("Database row counts:")
    for t in tables:
        c.execute(f"SELECT COUNT(*) FROM {t}")
        print(f"  {t:<25} {c.fetchone()[0]} rows")
    conn.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Process a conversation file into memory.db")
    parser.add_argument("filename", nargs="?", help="Conversation .md filename")
    parser.add_argument("--date", help="Override session date (YYYY-MM-DD). Use if the filename and file header don't contain the correct date.")
    parser.add_argument("--mode", choices=["individual", "grouped"], default="individual",
                        help="Extraction mode: 'individual' (10 calls, default) or 'grouped' (3 calls, experimental)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run extractions and save debug JSON, but skip writing to the database")
    parser.add_argument("--force", action="store_true",
                        help="Ignore existing checkpoints and re-run all extraction steps from scratch")
    parser.add_argument("--skip-validator", action="store_true",
                        help="Skip the DeepSeek R1 validator pass (useful when Qwen output is known good or for speed)")
    args = parser.parse_args()

    if not args.filename:
        args.filename = input("Enter conversation filename (e.g. conversation_001.md): ").strip()

    process_conversation(
        args.filename,
        override_date=args.date,
        mode=args.mode,
        dry_run=args.dry_run,
        force=args.force,
        skip_validator=args.skip_validator,
    )

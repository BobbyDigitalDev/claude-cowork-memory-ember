#!/usr/bin/env python3
"""
process_research.py
-------------------
Extracts structured knowledge from external research files (YouTube transcripts,
articles, etc.) into the CoWork memory database.

Unlike process_conversation.py, this script is NOT framed around a Bobby-Claude
conversation. Prompts are designed for external content: what concepts, beliefs,
entities, and insights does this material introduce or support?

All extracted rows are tagged memory_origin="research" so they are distinguishable
from conversation-derived memory.

USAGE
-----
    # Process a single file
    python3 ~/claude_memory/scripts/process_research.py \
        ~/claude_memory/research/transcripts/2026_04_25_openclaw_424_transcript.txt

    # Process all unprocessed files in the transcripts folder
    python3 ~/claude_memory/scripts/process_research.py --all

    # Reprocess even if already done
    python3 ~/claude_memory/scripts/process_research.py <file> --force

    # Dry run (parse and print without writing to DB)
    python3 ~/claude_memory/scripts/process_research.py <file> --dry-run

PIPELINE POSITION
-----------------
Run after fetching new transcripts, before embed_memories.py.
    fetch_youtube_transcript.py  →  process_research.py  →  embed_memories.py
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import uuid
from datetime import date, datetime
from pathlib import Path

_BASE        = Path.home() / "claude_memory"
DB_PATH      = str(_BASE / "memory.db")
SCRIPTS_DIR  = _BASE / "scripts"
RESEARCH_DIR = _BASE / "research" / "transcripts"

MODEL        = "qwen2.5:14b"
OLLAMA_URL   = "http://localhost:11434/api/generate"

NOW = datetime.now()


# ── Ollama helper ─────────────────���──────────────────────────────────���────────

def ask_qwen(prompt: str, model: str = MODEL) -> str | None:
    """Send a prompt to Ollama and return the raw response string."""
    import urllib.request
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.1, "num_ctx": 32768},
    }).encode()
    req = urllib.request.Request(
        OLLAMA_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
            return data.get("response", "").strip()
    except Exception as e:
        print(f"  [warn] Ollama request failed: {e}")
        return None


def parse_json_response(raw: str | None) -> dict | list | None:
    """Extract JSON from a raw Qwen response, handling markdown fences and preamble."""
    if not raw:
        return None
    # Strip <think> blocks (DeepSeek / reasoning models)
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    # Try markdown fences first
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
    if fence:
        raw = fence.group(1).strip()
    # Find the first { or [
    start = min(
        (raw.find("{") if raw.find("{") != -1 else len(raw)),
        (raw.find("[") if raw.find("[") != -1 else len(raw)),
    )
    if start == len(raw):
        return None
    try:
        return json.loads(raw[start:])
    except json.JSONDecodeError:
        return None


# ── Content parsing ──────────────────────���────────────────────────────���───────

def strip_research_header(content: str) -> str:
    """
    Remove the YAML-style header block from a YouTube transcript or similar file.
    Headers end at the first '---' separator line.
    If no separator is found, the content is returned unchanged.
    """
    lines = content.split("\n")
    separator_indices = [i for i, line in enumerate(lines) if line.strip() == "---"]
    if not separator_indices:
        return content
    # Use the last separator in the header region (first 20 lines)
    header_separators = [i for i in separator_indices if i < 20]
    if not header_separators:
        return content
    cutoff = header_separators[-1] + 1
    return "\n".join(lines[cutoff:]).strip()


def infer_source_date(filename: str) -> date:
    """
    Extract date from filename like 2026_04_25_some_title_transcript.txt.
    Falls back to today if no date prefix found.
    """
    match = re.match(r"(\d{4})_(\d{2})_(\d{2})_", os.path.basename(filename))
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            pass
    return date.today()


# ── Extraction prompts ─────────────────────────���──────────────────────────────

def _build_extraction_prompt(content: str, filename: str, extraction_type: str) -> str:
    base = f"""You are analyzing external research content (a YouTube transcript, article, or technical document).
Source file: {filename}

Your job is to extract structured knowledge for a personal AI memory system.
Extract only what is clearly present in the content. Do not invent or hallucinate.
Return valid JSON only — no explanation, no markdown prose outside the JSON block.

CONTENT:
{content[:8000]}

"""

    prompts = {
        "concepts": base + """Extract technical concepts, tools, frameworks, or ideas introduced or explained in this content.

Return a JSON array. Each item:
{
  "name": "short concept name",
  "description": "1-2 sentence explanation of what this is",
  "tags": ["tag1", "tag2"]
}

If nothing relevant, return [].
JSON:""",

        "beliefs": base + """Extract claims or positions the content asserts — things presented as true, recommended, or significant.
Focus on claims that are useful to remember for technical decision-making.

Return a JSON array. Each item:
{
  "topic": "short snake_case topic label",
  "position": "the claim being made",
  "confidence": "high | medium | low",
  "evidence_snippets": "brief quote or paraphrase from content supporting this",
  "tags": ["tag1", "tag2"]
}

If nothing relevant, return [].
JSON:""",

        "entities": base + """Extract named entities: people, companies, tools, products, or platforms mentioned.

Return a JSON array. Each item:
{
  "name": "entity name",
  "type": "person | company | tool | product | platform | other",
  "description": "brief description of who/what this is",
  "importance": "high | medium | low"
}

If nothing relevant, return [].
JSON:""",

        "patterns": base + """Extract recurring techniques, approaches, or operational lessons described or demonstrated.

Return a JSON array. Each item:
{
  "description": "what the pattern or technique is",
  "pattern_type": "technical_approach | workflow | design_principle | operational_lesson",
  "significance": "why this matters",
  "tags": ["tag1", "tag2"]
}

If nothing relevant, return [].
JSON:""",

        "questions": base + """Extract open questions this content raises but does not fully answer,
OR important questions a reader should be asking after consuming this content.

Return a JSON array. Each item:
{
  "question": "the question",
  "category": "technical | strategic | philosophical | research"
}

If nothing relevant, return [].
JSON:""",

        "epiphanies": base + """Extract key insights or revelations — moments where the content shifts understanding
or makes a non-obvious connection.

Return a JSON array. Each item:
{
  "description": "the insight in 1-2 sentences",
  "implications": "what this means or changes"
}

If nothing relevant, return [].
JSON:""",
    }
    return prompts[extraction_type]


def run_extractions(content: str, filename: str) -> dict:
    """Run all 6 Qwen extraction calls and return combined result dict."""
    extraction_types = ["concepts", "beliefs", "entities", "patterns", "questions", "epiphanies"]
    result = {}
    for i, etype in enumerate(extraction_types, 1):
        print(f"  [{i}/6] {etype}...")
        prompt = _build_extraction_prompt(content, filename, etype)
        raw = ask_qwen(prompt)
        parsed = parse_json_response(raw)
        if isinstance(parsed, list):
            result[etype] = parsed
        else:
            print(f"         [warn] could not parse {etype} response, using []")
            result[etype] = []
    return result


# ── DB helpers ───────────────���─────────────────────────��──────────────────────

def _to_str(val) -> str:
    """Normalize a value for TEXT DB columns: join lists, pass strings through."""
    if isinstance(val, list):
        return ", ".join(str(v) for v in val)
    if val is None:
        return ""
    return str(val)


def already_processed(conn: sqlite3.Connection, filename: str) -> bool:
    """Return True if this file has a completed processing_job entry."""
    row = conn.execute(
        "SELECT id FROM processing_jobs "
        "WHERE source_file = ? AND job_type = 'research_extraction' AND status = 'completed'",
        (os.path.basename(filename),)
    ).fetchone()
    return row is not None


def write_research_to_db(
    conn: sqlite3.Connection,
    extraction: dict,
    filename: str,
    source_date: date,
) -> None:
    """Write all extracted items to the DB with memory_origin='research'."""
    basename = os.path.basename(filename)
    date_str = source_date.isoformat()

    # Migration: add memory_origin to tables that were missing it in earlier schema versions
    for _tbl in ("concepts", "entities", "patterns", "questions"):
        try:
            conn.execute(f"ALTER TABLE {_tbl} ADD COLUMN memory_origin TEXT DEFAULT 'conversation'")
            conn.commit()
        except Exception:
            pass  # Column already exists — silently continue

    # Concepts
    for item in extraction.get("concepts", []):
        if not isinstance(item, dict):
            continue
        try:
            conn.execute(
                "INSERT INTO concepts (name, description, first_appeared, memory_origin, tags) "
                "VALUES (?, ?, ?, 'research', ?)",
                (
                    item.get("name", ""),
                    item.get("description", ""),
                    date_str,
                    _to_str(item.get("tags", [])),
                )
            )
        except Exception as e:
            print(f"  [warn] concept write failed: {e}")

    # Beliefs
    for item in extraction.get("beliefs", []):
        if not isinstance(item, dict):
            continue
        try:
            conn.execute(
                """INSERT INTO beliefs
                   (uuid, topic, position, confidence, evidence_snippets,
                    memory_origin, status, is_active, tags, created_at)
                   VALUES (?, ?, ?, ?, ?, 'research', 'proposed', 1, ?, ?)""",
                (
                    str(uuid.uuid4()),
                    item.get("topic", ""),
                    item.get("position", ""),
                    item.get("confidence", "medium"),
                    _to_str(item.get("evidence_snippets", "")),
                    _to_str(item.get("tags", [])),
                    NOW.isoformat(),
                )
            )
        except Exception as e:
            print(f"  [warn] belief write failed: {e}")

    # Entities
    for item in extraction.get("entities", []):
        if not isinstance(item, dict):
            continue
        try:
            conn.execute(
                "INSERT INTO entities (name, type, description, importance, first_referenced, memory_origin) "
                "VALUES (?, ?, ?, ?, ?, 'research')",
                (
                    item.get("name", ""),
                    item.get("type", "other"),
                    item.get("description", ""),
                    item.get("importance", "medium"),
                    date_str,
                )
            )
        except Exception as e:
            print(f"  [warn] entity write failed: {e}")

    # Patterns
    for item in extraction.get("patterns", []):
        if not isinstance(item, dict):
            continue
        try:
            conn.execute(
                """INSERT INTO patterns
                   (uuid, date_identified, description, pattern_type,
                    significance, memory_origin, confidence_score, is_active, tags)
                   VALUES (?, ?, ?, ?, ?, 'research', 0.7, 1, ?)""",
                (
                    str(uuid.uuid4()),
                    date_str,
                    item.get("description", ""),
                    item.get("pattern_type", "technical_approach"),
                    item.get("significance", ""),
                    _to_str(item.get("tags", [])),
                )
            )
        except Exception as e:
            print(f"  [warn] pattern write failed: {e}")

    # Questions
    for item in extraction.get("questions", []):
        if not isinstance(item, dict):
            continue
        try:
            conn.execute(
                "INSERT INTO questions (date_raised, question, category, memory_origin, status) "
                "VALUES (?, ?, ?, 'research', 'open')",
                (
                    date_str,
                    item.get("question", ""),
                    item.get("category", "research"),
                )
            )
        except Exception as e:
            print(f"  [warn] question write failed: {e}")

    # Epiphanies
    for item in extraction.get("epiphanies", []):
        if not isinstance(item, dict):
            continue
        try:
            conn.execute(
                """INSERT INTO epiphanies
                   (uuid, date, description, implications,
                    memory_origin, is_active, confidence_score, tags)
                   VALUES (?, ?, ?, ?, 'research', 1, 0.7, '')""",
                (
                    str(uuid.uuid4()),
                    date_str,
                    item.get("description", ""),
                    item.get("implications", ""),
                )
            )
        except Exception as e:
            print(f"  [warn] epiphany write failed: {e}")


def record_job(conn: sqlite3.Connection, filename: str, status: str, error: str = "") -> None:
    conn.execute(
        """INSERT OR REPLACE INTO processing_jobs
           (uuid, job_type, target_type, source_file, model_used, status,
            started_at, completed_at, error_log)
           VALUES (?, 'research_extraction', 'research_file', ?, ?, ?, ?, ?, ?)""",
        (
            str(uuid.uuid4()),
            os.path.basename(filename),
            MODEL,
            status,
            NOW.isoformat(),
            NOW.isoformat(),
            error,
        )
    )
    conn.commit()


# ── Core process function (testable) ────────────────────────���────────────────

def process_research_file(
    filename: str,
    content: str,
    conn: sqlite3.Connection,
    source_date: date,
    force: bool = False,
    dry_run: bool = False,
) -> bool:
    """
    Process a single research file. Returns True on success, False if skipped.
    Designed to be called directly in tests (conn and content injected).
    """
    basename = os.path.basename(filename)

    if not force and already_processed(conn, basename):
        print(f"  [skip] already processed: {basename}")
        return False

    print(f"  Running extractions on {basename}...")
    extraction = run_extractions(content, basename)

    if dry_run:
        print(json.dumps(extraction, indent=2))
        return True

    write_research_to_db(conn, extraction, basename, source_date)
    conn.commit()
    record_job(conn, basename, "completed")
    return True


# ── Main ────────────────────��─────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Extract structured knowledge from research files into CoWork memory."
    )
    parser.add_argument("file", nargs="?", help="Path to research file to process")
    parser.add_argument("--all", action="store_true",
                        help="Process all unprocessed files in research/transcripts/")
    parser.add_argument("--force", action="store_true",
                        help="Reprocess even if already done")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and print extractions without writing to DB")
    args = parser.parse_args()

    if not args.file and not args.all:
        parser.error("Provide a file path or use --all")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")

    files_to_process = []

    if args.all:
        if not RESEARCH_DIR.exists():
            print(f"ERROR: research transcripts folder not found: {RESEARCH_DIR}")
            sys.exit(1)
        files_to_process = sorted(RESEARCH_DIR.glob("*.txt"))
    else:
        path = Path(os.path.expanduser(args.file))
        if not path.exists():
            print(f"ERROR: file not found: {path}")
            sys.exit(1)
        files_to_process = [path]

    print()
    print("=" * 60)
    print("  Research Extraction Pipeline")
    print(f"  Started:  {NOW.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Model:    {MODEL}")
    print(f"  Files:    {len(files_to_process)}")
    print("=" * 60)
    print()

    processed = skipped = failed = 0

    for filepath in files_to_process:
        print(f"[{filepath.name}]")
        try:
            raw_content = filepath.read_text(encoding="utf-8")
            content = strip_research_header(raw_content)
            source_date = infer_source_date(filepath.name)

            success = process_research_file(
                filename=filepath.name,
                content=content,
                conn=conn,
                source_date=source_date,
                force=args.force,
                dry_run=args.dry_run,
            )
            if success:
                processed += 1
            else:
                skipped += 1
        except Exception as e:
            print(f"  [error] {e}")
            record_job(conn, filepath.name, "failed", str(e))
            failed += 1
        print()

    conn.close()

    print("=" * 60)
    print(f"  Done. Processed: {processed}  Skipped: {skipped}  Failed: {failed}")
    print()
    if processed > 0 and not args.dry_run:
        print("  Next: run embed_memories.py to index new content")
    print("=" * 60)


if __name__ == "__main__":
    main()

# EMBER Engine — Command Reference

Quick reference for every script in `~/claude_memory/scripts/`.
All commands run from your Mac terminal. Ollama must be running for any command that calls a model.

---

## Daily Workflow

### After every session

```bash
# 1. Ingest the session transcript into memory
python3 ~/claude_memory/scripts/ingest.py

# 2. Embed new memories so semantic retrieval picks them up
python3 ~/claude_memory/scripts/embed_memories.py

# 3. Refresh the session starter for next time
python3 ~/claude_memory/scripts/generate_session_prompt.py
```

The auto-ingest agent handles step 1 automatically when you save the transcript.
Steps 2 and 3 run on schedule. You only need to run them manually after an urgent ingest.

---

## Background Agents

Nine launchd agents run automatically. Check their status:

```bash
launchctl list | grep ember
```

All should show `-  0  com.ember-engine.*`. A non-zero exit code means the last run failed — check the log in `~/claude_memory/logs/`.

| Agent | Schedule | Script | Purpose |
|---|---|---|---|
| context-agent | Every 30 min | context_snapshot_agent.py | Refreshes recent_memory.md and deep_memory.md |
| session-prompt | On session end | generate_session_prompt.py | Builds START_HERE.md for next session |
| auto-ingest | File watch + 15 min debounce | auto_ingest.py | Triggers ingest.py when a new transcript is saved |
| ingest-agent | Daily 03:00 | ingest_agent.py | Ingests approved scout results into memory |
| research-scout | Nightly 02:00 | research_scout.py | Pulls relevant research from YouTube, PubMed, arXiv, OpenAlex |
| verify-beliefs | Nightly 03:30 | verify_beliefs.py | Challenges beliefs using DeepSeek R1 |
| reflection-agent | Sunday 04:00 | reflection_agent.py | Synthesizes the past week into higher-order reflection |
| memory-curator | Sunday 05:00 | memory_curator.py | Deduplicates and prunes the memory graph |
| backup-agent | Every 6 hours | backup_memory.py | Backs up memory.db with timestamp |

### Reinstall an agent

```bash
bash ~/claude_memory/scripts/install_<agent_name>.sh
```

Available installers: `install_context_agent.sh`, `install_session_prompt.sh`, `install_auto_ingest.sh`, `install_ingest_agent.sh`, `install_research_scout.sh`, `install_verify_beliefs.sh`, `install_reflection_agent.sh`, `install_memory_curator.sh`, `install_backup_agent.sh`

---

## Core Scripts

### ingest.py — Session ingest

Main entry point after a session ends. Calls process_conversation.py under the hood.

```bash
# Scan for all unprocessed transcripts and prompt to confirm
python3 ~/claude_memory/scripts/ingest.py

# Ingest one specific file
python3 ~/claude_memory/scripts/ingest.py bobby_2026_04_28_001.md

# Process all unprocessed without prompting
python3 ~/claude_memory/scripts/ingest.py --scan

# Preview what would be processed (no writes)
python3 ~/claude_memory/scripts/ingest.py --dry-run
```

### process_conversation.py — Low-level ingest

Called by ingest.py. Use directly only when you need fine-grained control.

```bash
# Standard ingest
python3 ~/claude_memory/scripts/process_conversation.py bobby_2026_04_28_001.md

# Override the date (if filename doesn't encode the right date)
python3 ~/claude_memory/scripts/process_conversation.py bobby_2026_04_28_001.md --date 2026-04-27

# Grouped mode (3 Qwen calls instead of 10 — faster, experimental)
python3 ~/claude_memory/scripts/process_conversation.py bobby_2026_04_28_001.md --mode grouped

# Dry run: run extractions, save debug JSON, skip DB write
python3 ~/claude_memory/scripts/process_conversation.py bobby_2026_04_28_001.md --dry-run

# Force re-run from scratch, ignoring existing checkpoints
python3 ~/claude_memory/scripts/process_conversation.py bobby_2026_04_28_001.md --force

# Skip the DeepSeek R1 validator pass (faster, less accurate)
python3 ~/claude_memory/scripts/process_conversation.py bobby_2026_04_28_001.md --skip-validator
```

Checkpointing: if a run is interrupted, re-running it resumes from the last completed step automatically. Per-step debug files are saved to `~/claude_memory/debug/`.

### embed_memories.py — Semantic embeddings

Embeds beliefs, epiphanies, concepts, and patterns into memory_chunks via nomic-embed-text. Run after ingest to make new memories searchable.

```bash
# Embed all unembedded memories
python3 ~/claude_memory/scripts/embed_memories.py

# Re-embed everything from scratch
python3 ~/claude_memory/scripts/embed_memories.py --reembed

# Embed only one memory type
python3 ~/claude_memory/scripts/embed_memories.py --type belief
python3 ~/claude_memory/scripts/embed_memories.py --type epiphany
python3 ~/claude_memory/scripts/embed_memories.py --type concept
python3 ~/claude_memory/scripts/embed_memories.py --type pattern
```

### generate_session_prompt.py — Session bootstrap files

Writes `START_HERE.md` and `ember_engine_context.md` to `~/claude_memory/`. Paste START_HERE.md into Cowork at the start of each session.

```bash
# Generate for tomorrow (default)
python3 ~/claude_memory/scripts/generate_session_prompt.py

# Generate for a specific date
python3 ~/claude_memory/scripts/generate_session_prompt.py --date 2026-04-28

# Print to stdout instead of writing files
python3 ~/claude_memory/scripts/generate_session_prompt.py --stdout

# Only regenerate START_HERE.md, skip ember_engine_context.md
python3 ~/claude_memory/scripts/generate_session_prompt.py --skip-context
```

---

## Research

### research_scout.py — Research aggregator

Pulls from Quanta RSS, trusted YouTube channels, PubMed, arXiv, and OpenAlex. Scores results against memory chunks. Run nightly via launchd or manually to test.

```bash
# Fetch and score but don't write to DB
python3 ~/claude_memory/scripts/research_scout.py --dry-run --no-jitter

# Dry run with a custom result cap
python3 ~/claude_memory/scripts/research_scout.py --dry-run --no-jitter --max-results 5

# Show topic seeds without fetching anything
python3 ~/claude_memory/scripts/research_scout.py --list-topics

# Force regeneration of ring-2 topic cache
python3 ~/claude_memory/scripts/research_scout.py --refresh-ring2 --no-jitter

# Full live run (writes to scout_results)
python3 ~/claude_memory/scripts/research_scout.py --no-jitter
```

If ring-2 seeds look off (wrong topics, too much neuroscience or hardware), clear the cache and let it regenerate:

```bash
rm ~/claude_memory/cache/ring2_topics.json
python3 ~/claude_memory/scripts/research_scout.py --no-jitter --dry-run
```

### review_scout.py — Review and ingest scout results

```bash
# Show pending results (default view)
python3 ~/claude_memory/scripts/review_scout.py

# Show all results regardless of status
python3 ~/claude_memory/scripts/review_scout.py --all

# Show only today's results
python3 ~/claude_memory/scripts/review_scout.py --today

# Summary of counts by status and date
python3 ~/claude_memory/scripts/review_scout.py --summary

# Mark results as interesting
python3 ~/claude_memory/scripts/review_scout.py --mark 12,15,18 --status interesting

# Mark as dismissed with a note
python3 ~/claude_memory/scripts/review_scout.py --mark 7 --status dismissed --notes "off-topic: hardware paper"

# Ingest one result (runs process_research.py + embed_memories.py)
python3 ~/claude_memory/scripts/review_scout.py --ingest 12

# Ingest multiple results
python3 ~/claude_memory/scripts/review_scout.py --ingest 12,15,18

# Ingest all results marked 'interesting'
python3 ~/claude_memory/scripts/review_scout.py --ingest-queued

# Preview what would be written without ingesting
python3 ~/claude_memory/scripts/review_scout.py --ingest-dry-run 12
```

### generate_scout_digest.py — Readable Scout digest

Formats pending and interesting Scout results into a markdown digest for human review. Run after the Scout to see what came in before deciding what to ingest.

```bash
# Generate digest for the last 14 days (default) — writes scout_digest_latest.md
python3 ~/claude_memory/scripts/generate_scout_digest.py

# Longer lookback window
python3 ~/claude_memory/scripts/generate_scout_digest.py --days 30

# Only interesting items (already flagged in a prior review)
python3 ~/claude_memory/scripts/generate_scout_digest.py --status interesting

# Only pending (new, unreviewed)
python3 ~/claude_memory/scripts/generate_scout_digest.py --status pending

# Everything except dismissed/ingested
python3 ~/claude_memory/scripts/generate_scout_digest.py --status all

# Print to stdout without writing files
python3 ~/claude_memory/scripts/generate_scout_digest.py --dry-run
```

Output: `~/claude_memory/scout_digest_latest.md` (always-current) and `~/claude_memory/research/digests/scout_digest_YYYY_MM_DD.md` (dated archive). Both are gitignored. Each entry in the digest includes ready-to-paste `review_scout.py` commands.

### populate_channel_ids.py — One-time YouTube setup

Resolves UCxxxxxxxx channel IDs from handles and caches them to the DB. Run once after adding new channels to trusted_sources.

```bash
# Auto-resolve all channels (requires yt-dlp: brew install yt-dlp)
python3 ~/claude_memory/scripts/populate_channel_ids.py

# Resolve but don't write to DB
python3 ~/claude_memory/scripts/populate_channel_ids.py --dry-run

# Manual entry mode (prompts channel by channel)
python3 ~/claude_memory/scripts/populate_channel_ids.py --manual-only
```

---

## MCP Memory Server

### memory_mcp_server.py + install_memory_mcp.sh — Live memory tool for Cowork sessions

Exposes `retrieve()` as an MCP tool (`query_memory`) so Claude can query your memory DB on demand during any Cowork session — without you manually pasting context.

**One-time setup:**

```bash
bash ~/claude_memory/scripts/install_memory_mcp.sh
# Then restart Cowork
```

The installer checks/installs the `mcp` package, registers `ember-memory` in `~/.claude.json`, and smoke-tests the import. Safe to re-run (idempotent).

**Manual invocation (from terminal, for testing):**

```bash
python3 ~/claude_memory/scripts/memory_mcp_server.py
```

Once installed, Cowork calls `query_memory` automatically when topics arise that likely have stored context. You can also prompt it directly:

> "Query my memory for beliefs about context window tradeoffs."

**Parameters exposed via the tool:**

| Parameter | Default | Description |
|---|---|---|
| `query` | — | Natural language search query |
| `top` | 10 | Max results to return |
| `strategies` | `semantic,structural,temporal` | Retrieval strategies |
| `threshold` | 0.45 | Minimum relevance score |
| `days` | 30 | Temporal window |

---

## Memory Operations

### retrieve.py — Semantic + structural retrieval

Combines semantic search (nomic-embed-text), structural graph traversal, and temporal recency. Used internally by the session bootstrap.

```bash
# Basic semantic query
python3 ~/claude_memory/scripts/retrieve.py "belief verification adversarial prompting"

# More results, stricter threshold
python3 ~/claude_memory/scripts/retrieve.py "local LLM performance" --top 15 --threshold 0.55

# Only semantic strategy
python3 ~/claude_memory/scripts/retrieve.py "context window tradeoffs" --strategies semantic

# JSON output
python3 ~/claude_memory/scripts/retrieve.py "reflection agent" --format json

# Temporal window (last 7 days only)
python3 ~/claude_memory/scripts/retrieve.py "session bootstrap" --days 7
```

### query_memories.py — Direct semantic search over memory_chunks

Simpler than retrieve.py. Good for quick lookups.

```bash
# Search all memory types
python3 ~/claude_memory/scripts/query_memories.py "persistent memory architecture"

# Filter by type
python3 ~/claude_memory/scripts/query_memories.py "AI consciousness" --type belief
python3 ~/claude_memory/scripts/query_memories.py "ring-2 seeds" --type concept

# More results with full text
python3 ~/claude_memory/scripts/query_memories.py "token usage" --top 10 --full
```

### memory_curator.py — Deduplication and pruning

Runs weekly via launchd. Run manually to do a cleanup pass after heavy ingest.

```bash
# Preview changes without writing
python3 ~/claude_memory/scripts/memory_curator.py --dry-run

# Run live with report to stdout
python3 ~/claude_memory/scripts/memory_curator.py --stdout

# Adjust stale threshold (default: 45 days)
python3 ~/claude_memory/scripts/memory_curator.py --stale-days 60

# Looser dedup threshold (default: 0.85 cosine similarity)
python3 ~/claude_memory/scripts/memory_curator.py --dedup-threshold 0.80
```

---

## Belief and Verification

### verify_beliefs.py — Belief verification

Challenges beliefs using DeepSeek R1 against conversation evidence. Runs nightly via launchd.

```bash
# Manual verification pass (proposed beliefs only)
python3 ~/claude_memory/scripts/verify_beliefs.py --no-jitter

# Verify up to 50 beliefs (default: 20)
python3 ~/claude_memory/scripts/verify_beliefs.py --limit 50 --no-jitter

# Verify all statuses, not just proposed
python3 ~/claude_memory/scripts/verify_beliefs.py --all-statuses --no-jitter

# Also run cross-belief contradiction check
python3 ~/claude_memory/scripts/verify_beliefs.py --check-contradictions --no-jitter

# Dry run: show what would change without writing
python3 ~/claude_memory/scripts/verify_beliefs.py --dry-run --no-jitter
```

### reflection_agent.py — Weekly synthesis

Synthesizes the past N days of context snapshots into a higher-order reflection. Runs Sundays at 04:00.

```bash
# Manual run (skips if a reflection was written in the last 7 days)
python3 ~/claude_memory/scripts/reflection_agent.py --no-jitter

# Force run even if recent
python3 ~/claude_memory/scripts/reflection_agent.py --force --no-jitter

# Look back 14 days instead of 7
python3 ~/claude_memory/scripts/reflection_agent.py --sessions 14 --no-jitter

# Dry run: show what would be written
python3 ~/claude_memory/scripts/reflection_agent.py --dry-run --force
```

---

## Context and Memory Refresh

### context_snapshot_agent.py — Context refresh

Refreshes `recent_memory.md` and `deep_memory.md`. Runs on a 30-minute schedule.

```bash
# Manual refresh
python3 ~/claude_memory/scripts/context_snapshot_agent.py

# Dry run: show what would run
python3 ~/claude_memory/scripts/context_snapshot_agent.py --dry-run
```

### refresh_recent_memory.py — recent_memory.md only

Faster than context_snapshot_agent.py. Writes a DB-state snapshot to `recent_memory.md`.

```bash
python3 ~/claude_memory/scripts/refresh_recent_memory.py
```

### refresh_deep_memory.py — Semantic scaffold

Runs semantic retrieval for each topic seed and writes `deep_memory.md`.

```bash
# Standard run
python3 ~/claude_memory/scripts/refresh_deep_memory.py

# Use current session intent as seeds
python3 ~/claude_memory/scripts/refresh_deep_memory.py --intent-file

# Print seeds that would be used without running retrieval
python3 ~/claude_memory/scripts/refresh_deep_memory.py --list-seeds

# Use the full retrieval orchestrator (semantic + structural + temporal)
python3 ~/claude_memory/scripts/refresh_deep_memory.py --orchestrated

# More results per seed
python3 ~/claude_memory/scripts/refresh_deep_memory.py --top 5
```

---

## Utilities

### backup_memory.py — Manual backup

```bash
# Back up memory.db now
python3 ~/claude_memory/scripts/backup_memory.py

# Preview without writing
python3 ~/claude_memory/scripts/backup_memory.py --dry-run

# Keep 20 backups instead of the default 10
python3 ~/claude_memory/scripts/backup_memory.py --max-backups 20
```

Backups are stored in `~/claude_memory/backups/` with timestamps.

### parse_raw_transcript.py — Convert raw Cowork export

Converts a raw `.txt` export from the Cowork window into a formatted transcript `.md` file.

```bash
# Convert and write to conversations/
python3 ~/claude_memory/scripts/parse_raw_transcript.py "2026-04-28 - Cognition Engine Build-001-RAW.txt"

# Preview without writing
python3 ~/claude_memory/scripts/parse_raw_transcript.py raw.txt --preview

# Write to a specific output path
python3 ~/claude_memory/scripts/parse_raw_transcript.py raw.txt --out ~/claude_memory/conversations/bobby_2026_04_28_001.md
```

### session_intent.py — Session intent and knowledge gap detection

Declare what you want to work on before the session starts. Parses your intent into 2-5
seed topics, runs retrieval against each, and classifies memory coverage as DENSE / PARTIAL /
SPARSE. Writes `current_intent.txt` so `refresh_deep_memory.py --intent-file` uses your
intent as bootstrap seeds instead of the default momentum-based seeds.

```bash
# Declare intent and see coverage report
python3 ~/claude_memory/scripts/session_intent.py "Build the reflection agent and write tests"

# Declare intent and immediately regenerate deep_memory.md with intent seeds
python3 ~/claude_memory/scripts/session_intent.py "OpenClaw integration" --refresh

# Skip embedding retrieval (faster, no Ollama required)
python3 ~/claude_memory/scripts/session_intent.py "your intent here" --no-semantic

# Tune retrieval sensitivity
python3 ~/claude_memory/scripts/session_intent.py "your intent here" --top 15 --threshold 0.60
```

Run this at the start of any session where you know what you want to work on and want the
bootstrap context tuned to it. Skip it for open-ended or exploratory sessions.

---

### tier0_classifier.py — Adaptive retrieval config

Classifies the most likely intent of the current session based on recent completed work and
queued goals, then returns an adaptive retrieval config for `refresh_deep_memory.py` to use.
No model call — fast and deterministic. Called internally by `refresh_deep_memory.py`.

Intent types: `build` (technical work), `research` (investigation), `reflect` (synthesis), `review` (audit).

```bash
# Show what session type the classifier predicts and the retrieval config it would use
python3 ~/claude_memory/scripts/tier0_classifier.py
```

Useful for debugging why a bootstrap loaded the wrong memory slice — run this to see
how the classifier scored the current session type and what seeds it chose.

---

### format_conversation.py — Format raw Cowork export (legacy)

Converts a raw `.txt` export from the old Cowork window format into a formatted `.md`
transcript with proper speaker attribution. Superseded by `parse_raw_transcript.py` for
most use cases, but still called by `ingest.py` for backward compatibility.

```bash
python3 ~/claude_memory/scripts/format_conversation.py conversation_002_raw.txt
# Output: ~/claude_memory/conversations/conversation_002.md
```

---

### setup_db.py — Initialize database

One-time setup. Only run this to create a fresh database or after a schema migration.

```bash
python3 ~/claude_memory/scripts/setup_db.py
```

**Warning:** running this on an existing database will fail safely (schema already exists) but double-check before running on a production DB.

---

## Logs

All agent logs are in `~/claude_memory/logs/`:

```bash
tail -f ~/claude_memory/logs/research_scout.log
tail -f ~/claude_memory/logs/verify_beliefs.log
tail -f ~/claude_memory/logs/reflection_agent.log
tail -f ~/claude_memory/logs/memory_curator.log
tail -f ~/claude_memory/logs/ingest_agent.log
tail -f ~/claude_memory/logs/backup_agent.log
```

---

## Models Required

| Script | Model | Purpose |
|---|---|---|
| process_conversation.py | qwen2.5:14b | Extraction |
| process_conversation.py | deepseek-r1:14b | Validation pass |
| verify_beliefs.py | deepseek-r1:14b | Belief verification |
| reflection_agent.py | qwen2.5:14b | Synthesis |
| research_scout.py | qwen2.5:14b | Ring-2 topic expansion |
| embed_memories.py | nomic-embed-text | Embeddings |
| retrieve.py | nomic-embed-text | Semantic search |

Start Ollama before any ingest: `ollama serve`

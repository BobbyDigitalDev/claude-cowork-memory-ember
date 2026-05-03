# Contributing to E.M.B.E.R Engine

Thanks for your interest. This document covers how the codebase is organized, what makes a good contribution, and the conventions you need to follow to keep things coherent.

---

## What this project is

E.M.B.E.R Engine is a personal cognitive architecture — it's opinionated by design, because it reflects one person's workflow and memory model. That shapes what kinds of contributions are useful.

**Good contributions:**
- Bug fixes and edge case handling
- New agent types or retrieval strategies that follow existing patterns
- Performance improvements (faster ingest, smarter embedding, better dedup)
- Schema additions that are backward-compatible
- Documentation corrections
- Linux/Windows compatibility (currently macOS-only due to launchd)

**Out of scope:**
- Changes that hardcode a specific user's preferences, paths, or name
- New dependencies that aren't strictly necessary
- Breaking changes to the DB schema without a migration script
- Features that require cloud services (this is intentionally local-first)

---

## Setup

```bash
git clone https://github.com/yourusername/ember-engine.git ~/claude_memory
cd ~/claude_memory
pip install -r requirements.txt --break-system-packages
python3 scripts/setup_db.py   # creates memory.db with full schema
```

For development you don't need Ollama running unless you're working on ingest, embedding, or retrieval. Most unit tests use a temporary SQLite database (`conftest.py` fixture `tmp_db`) and don't call any model.

---

## Running tests

```bash
cd ~/claude_memory
python3 -m pytest tests/ -v
```

Tests live in `tests/`. Every new script or meaningful behavior change should have coverage. The `conftest.py` fixture spins up a fresh DB using the real schema so tests always match production structure.

If you add a schema column, update `setup_db.py` and add a migration path — don't rely on `setup_db.py` being run fresh by existing users.

---

## Script conventions

Every script in `scripts/` follows the same patterns. New scripts should too.

**Argument parsing:** use `argparse`. Expose at minimum:
- `--dry-run` — show what would happen without writing anything
- `--no-jitter` — skip random sleep (required for any agent that has a jitter delay, so test runs don't take forever)

**Agent scripts** (anything run via launchd) also need:
- A corresponding `install_<name>.sh` in `scripts/`
- A `.plist` in `daemons/`
- A log file written to `~/claude_memory/logs/<name>.log`
- A `--no-jitter` flag

**Database access:** always use a `try/except/finally` block that closes the connection and rolls back on error. Never leave a connection open on exception.

**Paths:** use `Path.home() / "claude_memory"` as the base, not hardcoded strings. Never commit absolute paths.

**Secrets and personal data:** use `.ember_config` for anything user-specific (name, email, paths). `.ember_config` is gitignored. `.ember_config.template` is the committed version — add any new keys there with placeholder values.

---

## Database schema

The schema lives in `scripts/setup_db.py`. The main tables are:

| Table | Purpose |
|---|---|
| `beliefs` | Extracted and verified beliefs with epistemic status |
| `epiphanies` | High-significance insights |
| `concepts` | Named concepts and abstractions |
| `patterns` | Recurring behavioral or technical patterns |
| `goals` | Open questions and tracked objectives |
| `memory_chunks` | Embedded chunks for semantic search |
| `scout_results` | Research Scout findings with review lifecycle |
| `conversations` | Processed session metadata |
| `entities` | Named entities extracted from sessions |

Adding a column: add it to the `CREATE TABLE` statement in `setup_db.py` with a safe default, and add a corresponding `ALTER TABLE ... ADD COLUMN` migration block that runs only if the column doesn't exist. Pattern to follow is in the existing schema setup function.

---

## Retrieval pipeline

`retrieve.py` is the core retrieval orchestrator. It combines:
1. Semantic search via `nomic-embed-text` embeddings in `memory_chunks`
2. Epistemic damping — lower scores for deprecated/archived beliefs
3. Recency bonus — additive decay favoring recently updated memories
4. Structural traversal — graph relationships between concepts
5. Temporal filtering — configurable day window

If you're adding a new retrieval strategy, add it to the `strategies` parameter and keep it opt-in so existing callers aren't affected.

---

## Gitignore discipline

The `.gitignore` is strict by design. Personal data never commits. If you're adding a new output file or generated artifact, add it to `.gitignore` before the first commit. The categories already covered:

- `memory.db` and WAL files — personal data
- `conversations/` — session transcripts
- `personal/` — user-specific docs and scripts
- `recent_memory.md`, `deep_memory.md`, `START_HERE.md` — generated files
- `scout_digest_latest.md`, `research/digests/` — generated Scout output
- `debug/` — extraction debug JSON
- `logs/`, `cache/`, `backups/` — operational artifacts

---

## Pull requests

- One concern per PR. Don't bundle a bug fix with a refactor.
- If you're adding a new script, include a `--dry-run` flag and a test.
- If you're changing the schema, include a migration.
- Update `COMMANDS.md` if you're adding or changing a script's CLI interface.
- Keep commit messages specific: `fix: handle None tags in write_to_db` not `fix bug`.

---

## Questions

Open an issue or start a discussion. This is a small project — no formal review process, just keep the bar high and the patterns consistent.

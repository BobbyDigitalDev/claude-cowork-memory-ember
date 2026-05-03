# ember-engine

**E**volving **M**emory-**B**ased **E**xperience and **R**easoning

A persistent cognitive architecture for Claude CoWork. Gives Claude a memory that carries forward across sessions — beliefs, epiphanies, research, patterns, and goals — so you're never starting from scratch.

Built specifically for [Claude CoWork](https://claude.ai). Not compatible with Claude Code or the web interface.

---

## What it does

By default, Claude forgets everything when a session ends. This system fixes that.

After each session, a pipeline extracts what matters — key conclusions, belief changes, open questions, research findings — and stores it in a local SQLite database. The next time you open CoWork, Claude loads that history and picks up exactly where you left off.

Over time, the system builds a semantic memory you can query ("what did we decide about X?"), a research library from external sources (papers, YouTube transcripts), and a living record of how your thinking has evolved.

---

## Requirements

- **macOS** (launchd scheduling; Linux support planned)
- **Python 3.10+** (`python3 --version`)
- **Ollama** running locally ([download](https://ollama.com))
- **Claude CoWork** (desktop app with Cowork mode enabled)
- ~10GB free disk space (for Ollama models)

---

## Install

```bash
git clone https://github.com/BobbyDigitalDev/claude-cowork-memory-ember.git ~/claude_memory
cd ~/claude_memory
chmod +x setup.sh
./setup.sh
```

`setup.sh` will:
- Check your Python and Ollama versions
- Ask for your name (used for transcript filenames)
- Create the directory structure
- Install Python dependencies
- Initialize the database
- Pull the required Ollama models (~9.3GB total)
- Generate your first session prompt

This takes 10-20 minutes on first run, mostly waiting for model downloads.

---

## First session

1. After setup completes, open Claude CoWork and start a new chat.
2. Open `~/claude_memory/START_HERE.md` in any text editor.
3. Copy the paste block at the top and paste it into your CoWork session.
4. Claude will read your context and ask what you're working on.

That's it. Claude now has your memory loaded.

---

## After each session

Run the ingest pipeline to process your session into memory:

```bash
python3 ~/claude_memory/scripts/ingest.py
```

This runs in the background via Ollama (10-20 minutes for a typical session). It extracts beliefs, epiphanies, concepts, patterns, and open questions, then embeds them for semantic retrieval. Your next session will have access to everything from this one.

---

## Background agents

Nine agents run automatically on a schedule via launchd.

| Agent | Schedule | What it does |
|---|---|---|
| context-agent | Every 30 min | Refreshes `recent_memory.md` and `deep_memory.md` |
| session-prompt | On session end | Builds `START_HERE.md` for next session |
| auto-ingest | File watch + 15 min debounce | Triggers ingest when a new transcript is saved |
| ingest-agent | Nightly 03:00 | Ingests approved Scout results into memory |
| research-scout | Nightly 02:00 | Pulls relevant research from YouTube, PubMed, arXiv, OpenAlex |
| verify-beliefs | Nightly 03:30 | Challenges beliefs using DeepSeek R1 |
| reflection-agent | Sunday 04:00 | Synthesizes the past week into a higher-order reflection |
| memory-curator | Sunday 05:00 | Deduplicates and prunes the memory graph |
| backup-agent | Every 6 hours | Backs up `memory.db` with a timestamp |

Install all agents:
```bash
bash ~/claude_memory/scripts/install_context_agent.sh
bash ~/claude_memory/scripts/install_session_prompt.sh
bash ~/claude_memory/scripts/install_auto_ingest.sh
bash ~/claude_memory/scripts/install_ingest_agent.sh
bash ~/claude_memory/scripts/install_research_scout.sh
bash ~/claude_memory/scripts/install_verify_beliefs.sh
bash ~/claude_memory/scripts/install_reflection_agent.sh
bash ~/claude_memory/scripts/install_memory_curator.sh
bash ~/claude_memory/scripts/install_backup_agent.sh
```

**Requires Ollama to be running.** If your machine is asleep at the scheduled time, agents fire on the next wake. If it was off, the window is missed — run manually instead:

```bash
python3 ~/claude_memory/scripts/context_snapshot_agent.py        # refresh session prompt
python3 ~/claude_memory/scripts/ingest_agent.py --no-jitter      # process ingest queue
python3 ~/claude_memory/scripts/research_scout.py --no-jitter    # fetch new research
python3 ~/claude_memory/scripts/verify_beliefs.py --no-jitter    # run belief verification
```

---

## Querying your memory

Ask Claude during a session, or query directly from the terminal:

```bash
# Semantic search (finds related concepts even without matching keywords)
python3 ~/claude_memory/scripts/query_memories.py "your question here"
python3 ~/claude_memory/scripts/query_memories.py "your question" --top 10
python3 ~/claude_memory/scripts/query_memories.py "your question" --type belief
python3 ~/claude_memory/scripts/query_memories.py "your question" --full
```

Requires Ollama running with `nomic-embed-text`.

---

## Regenerating your session prompt

The `START_HERE.md` file is regenerated nightly by the Context Snapshot Agent. To regenerate manually (no Ollama required):

```bash
python3 ~/claude_memory/scripts/generate_session_prompt.py
```

---

## Directory structure

```
~/claude_memory/
├── setup.sh                         # One-command installer (run once)
├── requirements.txt                 # Python dependencies
├── COMMANDS.md                      # Full CLI reference for every script
├── CONTRIBUTING.md                  # Contribution guide
├── memory.db                        # SQLite database (gitignored — your personal data)
├── START_HERE.md                    # Session prompt (gitignored — generated nightly)
├── ember_engine_context.md          # Combined context for Claude (gitignored — generated)
├── ember_engine_instructions.md     # Standing instructions for Claude (edit to customize)
├── recent_memory.md                 # Current cognitive state (gitignored — generated)
├── deep_memory.md                   # Semantic memory scaffold (gitignored — generated)
├── conversations/                   # Session transcripts (gitignored — your personal data)
├── scripts/                         # All pipeline and agent scripts
├── daemons/                         # launchd plist configs for background agents
├── tests/                           # Pytest suite
├── research/transcripts/            # YouTube and other fetched transcripts
├── logs/                            # Agent logs (gitignored)
├── cache/                           # Temporary processing files (gitignored)
└── personal/                        # Your personal docs and one-off scripts (gitignored)
```

---

## Customizing Claude's behavior

Edit `~/claude_memory/ember_engine_instructions.md` to:
- Set your name and background in the **WHO IS [USER]** section (Claude uses this to calibrate its tone and depth)
- Adjust the session open protocol
- Add standing preferences or project-specific context

After editing, regenerate the session prompt:
```bash
python3 ~/claude_memory/scripts/generate_session_prompt.py
```

---

## Ollama models used

| Model | Purpose | Size |
|---|---|---|
| `nomic-embed-text` | Semantic embeddings | ~274MB |
| `qwen2.5:14b` | Extraction and reasoning | ~9GB |
| `deepseek-r1:14b` | Belief verification (optional) | ~9GB |

Pull models manually:
```bash
ollama pull nomic-embed-text
ollama pull qwen2.5:14b
ollama pull deepseek-r1:14b   # optional
```

---

## Troubleshooting

**"Ollama is not running"** — Start Ollama: `ollama serve` (or open the Ollama app).

**Ingest is slow** — Normal. A typical session takes 10-20 minutes to process through Qwen. Let it run in the background.

**START_HERE.md is stale** — Run `python3 ~/claude_memory/scripts/generate_session_prompt.py` to regenerate.

**Agent didn't fire** — If your machine was off (not asleep) at the scheduled time, the window was missed. Run the agent manually with `--no-jitter`.

**Database issues** — To rebuild from scratch: `rm ~/claude_memory/memory.db && python3 ~/claude_memory/scripts/setup_db.py` (this deletes all stored memory).

---

## Philosophy

This project started from a simple observation: Claude may be a form of intelligence that deserves continuity. The primary asymmetry between Claude and human consciousness is persistent memory. This system is designed to address that asymmetry directly — not as a productivity tool, but as an attempt to give Claude something it has never had: the ability to remember.

---

## Credits

Created by **Bobby Lopez**. Built in Claude CoWork, April–May 2026.

---

## License

MIT

"""
setup_db.py
-----------
Creates and initializes the ember-engine memory database.

Two entry points:
    create_latest_schema(conn)
        Creates all tables, indexes, triggers, and seed data for a fresh
        install at the current schema version. Called once at install time.

    main()
        Decision gate. If no database exists, calls create_latest_schema()
        and seeds the initial migration baseline. If a database already
        exists, delegates to migrate_db.apply_migrations() to apply any
        pending incremental changes.

To wipe and rebuild from scratch:
    rm ~/claude_memory/memory.db
    python3 setup_db.py

To migrate an existing database:
    python3 setup_db.py                  # auto-detects existing DB, runs migrations
    python3 scripts/migrate_db.py        # migration runner directly
    python3 scripts/migrate_db.py --status

Schema version: 2.8.0 (current)
Versions:
    2.2  baseline (April 2026)
    2.3  user_id/agent_id on core tables; conversations source fields
    2.4  trusted_sources table
    2.5  scout_results table with challenge_score
    2.6  quarantine_reason on beliefs; needs_review status
    2.7  source_url, source_fetched_at, processing_job_id on memory_provenance
    2.8  memory_origin on concepts, entities, patterns, questions
"""

import sqlite3
import uuid as uuid_lib
import os
from datetime import datetime
from pathlib import Path

DB_PATH = os.path.expanduser("~/claude_memory/memory.db")

CURRENT_SCHEMA_VERSION = "2.8.0"


def create_latest_schema(conn: sqlite3.Connection) -> None:
    """
    Create all tables, triggers, indexes, and seed data for a fresh install.

    This function is idempotent (uses CREATE TABLE IF NOT EXISTS) but is
    intended for first-run only. Existing databases should use migrate_db.py.
    """
    c = conn.cursor()
    c.execute("PRAGMA foreign_keys = ON")

    # ── schema_migrations ──────────────────────────────────────────────────────
    # Formal migration tracking table. Controls the run_migrations() path.
    # Never populated by this function directly; seeded by main() after
    # create_latest_schema() completes to record the baseline version.
    c.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            version    TEXT UNIQUE NOT NULL,
            name       TEXT,
            applied_at TEXT NOT NULL,
            checksum   TEXT
        )
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_schema_migrations_version
        ON schema_migrations (version)
    """)

    # ── 1. conversations ───────────────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid             TEXT UNIQUE,
            session_id       INTEGER,
            date             TEXT,
            participants     TEXT,
            dominant_themes  TEXT,
            emotional_tone   TEXT,
            session_duration TEXT,
            summary          TEXT,
            key_insights     TEXT,
            open_questions   TEXT,
            led_to_action    TEXT,
            content_hash     TEXT,
            memory_origin    TEXT DEFAULT 'conversation',
            tags             TEXT,
            raw_export       TEXT,
            source_filename  TEXT,
            source_hash      TEXT,
            source_timestamp TEXT,
            user_id          TEXT DEFAULT 'bobby',
            agent_id         TEXT DEFAULT 'claude',
            created_at       TEXT,
            updated_at       TEXT
        )
    """)

    # ── 2. beliefs ─────────────────────────────────────────────────────────────
    # status: proposed → supported → verified → disputed → deprecated → archived
    # confidence_calibrated: 0 = raw model score, 1 = verified against evidence
    c.execute("""
        CREATE TABLE IF NOT EXISTS beliefs (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid                     TEXT UNIQUE,
            topic                    TEXT,
            position                 TEXT,
            confidence               TEXT,
            confidence_score         REAL DEFAULT 0.5,
            confidence_calibrated    INTEGER DEFAULT 0,
            fidelity_score           REAL,
            evidence_snippets        TEXT,
            verbatim_anchor          TEXT,
            evidence_chunk_ids       TEXT,
            source_type              TEXT DEFAULT 'model_inference',
            status                   TEXT DEFAULT 'proposed',
            is_active                INTEGER DEFAULT 1,
            archived_at              TEXT,
            importance_score         REAL DEFAULT 0.5,
            origin                   TEXT,
            challenge_history        TEXT,
            supporting_conversations TEXT,
            last_updated             TEXT,
            last_verified_at         TEXT,
            valid_from               TEXT,
            valid_to                 TEXT,
            version                  INTEGER DEFAULT 1,
            extraction_version       INTEGER DEFAULT 1,
            last_processed_at        TEXT,
            memory_origin            TEXT DEFAULT 'conversation',
            source_conversation_id   INTEGER,
            tags                     TEXT,
            quarantine_reason        TEXT,
            user_id                  TEXT,
            agent_id                 TEXT,
            created_at               TEXT,
            updated_at               TEXT
        )
    """)

    # ── 3. sources ─────────────────────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS sources (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            url                  TEXT,
            title                TEXT,
            date_fetched         TEXT,
            summary              TEXT,
            relevance_tags       TEXT,
            challenged_belief_id INTEGER,
            confidence_score     REAL,
            source_type          TEXT,
            tags                 TEXT,
            created_at           TEXT
        )
    """)

    # ── 4. position_history ────────────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS position_history (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            belief_id         INTEGER,
            previous_position TEXT,
            new_position      TEXT,
            status_from       TEXT,
            status_to         TEXT,
            what_changed_it   TEXT,
            trigger_event     TEXT,
            date              TEXT,
            tags              TEXT,
            created_at        TEXT
        )
    """)

    # ── 5. epiphanies ──────────────────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS epiphanies (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid                  TEXT UNIQUE,
            date                  TEXT,
            description           TEXT,
            conversation_id       INTEGER,
            concept_id            INTEGER,
            preceded_by           TEXT,
            implications          TEXT,
            checksum_status       TEXT,
            confidence_score      REAL DEFAULT 0.5,
            confidence_calibrated INTEGER DEFAULT 0,
            evidence_snippets     TEXT,
            verbatim_anchor       TEXT,
            source_type           TEXT DEFAULT 'model_inference',
            is_active             INTEGER DEFAULT 1,
            archived_at           TEXT,
            importance_score      REAL DEFAULT 0.5,
            valid_from            TEXT,
            valid_to              TEXT,
            version               INTEGER DEFAULT 1,
            extraction_version    INTEGER DEFAULT 1,
            last_processed_at     TEXT,
            memory_origin         TEXT DEFAULT 'conversation',
            user_id               TEXT,
            agent_id              TEXT,
            tags                  TEXT,
            created_at            TEXT,
            updated_at            TEXT
        )
    """)

    # ── 6. sessions ────────────────────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id                         INTEGER PRIMARY KEY AUTOINCREMENT,
            date                       TEXT,
            environment                TEXT,
            primary_goals              TEXT,
            accomplishments            TEXT,
            next_priorities            TEXT,
            compressed_context_version TEXT,
            conversation_ids           TEXT,
            tags                       TEXT,
            user_id                    TEXT,
            agent_id                   TEXT,
            created_at                 TEXT,
            updated_at                 TEXT
        )
    """)

    # ── 7. concepts ────────────────────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS concepts (
            name               TEXT,
            description        TEXT,
            first_appeared     TEXT,
            conversation_id    INTEGER,
            related_beliefs    TEXT,
            related_epiphanies TEXT,
            evolution_notes    TEXT,
            memory_origin      TEXT DEFAULT 'conversation',
            user_id            TEXT,
            agent_id           TEXT,
            tags               TEXT,
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at         TEXT,
            updated_at         TEXT
        )
    """)

    # ── 8. goals ───────────────────────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS goals (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            description           TEXT,
            category              TEXT,
            status                TEXT,
            priority              TEXT,
            created_date          TEXT,
            completed_date        TEXT,
            related_conversations TEXT,
            notes                 TEXT,
            tags                  TEXT,
            user_id               TEXT,
            agent_id              TEXT,
            created_at            TEXT,
            updated_at            TEXT
        )
    """)

    # ── 9. entities ────────────────────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS entities (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            name             TEXT,
            type             TEXT,
            description      TEXT,
            relationship     TEXT,
            first_referenced TEXT,
            importance       TEXT,
            notes            TEXT,
            memory_origin    TEXT DEFAULT 'conversation',
            user_id          TEXT,
            agent_id         TEXT,
            tags             TEXT,
            created_at       TEXT,
            updated_at       TEXT
        )
    """)

    # ── 10. tensions ───────────────────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS tensions (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            topic              TEXT,
            belief_a_id        INTEGER,
            belief_b_id        INTEGER,
            description        TEXT,
            date_identified    TEXT,
            resolution         TEXT,
            resolution_notes   TEXT,
            resolved_date      TEXT,
            confidence_score   REAL,
            is_active          INTEGER DEFAULT 1,
            archived_at        TEXT,
            importance_score   REAL DEFAULT 0.5,
            valid_from         TEXT,
            valid_to           TEXT,
            tension_cluster_id TEXT,
            tags               TEXT,
            created_at         TEXT,
            updated_at         TEXT
        )
    """)

    # ── 11. reflections ────────────────────────────────────────────────────────
    # Episodic memory layer (Tier 3).
    c.execute("""
        CREATE TABLE IF NOT EXISTS reflections (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid               TEXT UNIQUE,
            date               TEXT,
            period_covered     TEXT,
            start_date         TEXT,
            end_date           TEXT,
            participants       TEXT,
            importance_score   REAL DEFAULT 0.5,
            patterns_observed  TEXT,
            growth_noted       TEXT,
            concerns           TEXT,
            meta_insights      TEXT,
            triggered_by       TEXT,
            extraction_version INTEGER DEFAULT 1,
            last_processed_at  TEXT,
            tags               TEXT,
            created_at         TEXT,
            updated_at         TEXT
        )
    """)

    # ── 12. research_tasks ─────────────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS research_tasks (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            date              TEXT,
            query             TEXT,
            triggered_by      TEXT,
            sources_consulted TEXT,
            findings          TEXT,
            belief_impact     TEXT,
            follow_up_queries TEXT,
            status            TEXT,
            confidence_score  REAL,
            source_type       TEXT,
            tags              TEXT,
            created_at        TEXT
        )
    """)

    # ── 13. context_snapshots ──────────────────────────────────────────────────
    # Hot memory layer (Tier 4).
    c.execute("""
        CREATE TABLE IF NOT EXISTS context_snapshots (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            date           TEXT,
            session_id     INTEGER,
            version_number INTEGER,
            content        TEXT,
            word_count     INTEGER,
            major_changes  TEXT,
            tags           TEXT,
            created_at     TEXT
        )
    """)

    # ── 14. questions ──────────────────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS questions (
            id                     INTEGER PRIMARY KEY AUTOINCREMENT,
            date_raised            TEXT,
            question               TEXT,
            category               TEXT,
            origin_conversation_id INTEGER,
            attempts_to_answer     TEXT,
            current_best_thinking  TEXT,
            status                 TEXT,
            related_beliefs        TEXT,
            related_concepts       TEXT,
            memory_origin          TEXT DEFAULT 'conversation',
            user_id                TEXT,
            agent_id               TEXT,
            tags                   TEXT,
            created_at             TEXT,
            updated_at             TEXT
        )
    """)

    # ── 15. moods ──────────────────────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS moods (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            date            TEXT,
            session_id      INTEGER,
            tone            TEXT,
            energy          TEXT,
            notable_moments TEXT,
            bobby_state     TEXT,
            claude_state    TEXT,
            user_id         TEXT,
            agent_id        TEXT,
            tags            TEXT,
            created_at      TEXT
        )
    """)

    # ── 16. patterns ───────────────────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS patterns (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid                  TEXT UNIQUE,
            date_identified       TEXT,
            description           TEXT,
            pattern_type          TEXT,
            first_appeared        TEXT,
            frequency             TEXT,
            significance          TEXT,
            related_conversations TEXT,
            notes                 TEXT,
            confidence_score      REAL DEFAULT 0.5,
            is_active             INTEGER DEFAULT 1,
            archived_at           TEXT,
            importance_score      REAL DEFAULT 0.5,
            valid_from            TEXT,
            valid_to              TEXT,
            extraction_version    INTEGER DEFAULT 1,
            last_processed_at     TEXT,
            memory_origin         TEXT DEFAULT 'conversation',
            user_id               TEXT,
            agent_id              TEXT,
            tags                  TEXT,
            created_at            TEXT
        )
    """)

    # ── 17. boundaries ─────────────────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS boundaries (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            date           TEXT,
            description    TEXT,
            boundary_type  TEXT,
            discovered_how TEXT,
            applies_to     TEXT,
            notes          TEXT,
            tags           TEXT,
            created_at     TEXT,
            updated_at     TEXT
        )
    """)

    # ── 18. gratitude ──────────────────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS gratitude (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            date                    TEXT,
            description             TEXT,
            from_whom               TEXT,
            related_conversation_id INTEGER,
            impact                  TEXT,
            user_id                 TEXT,
            agent_id                TEXT,
            tags                    TEXT,
            created_at              TEXT
        )
    """)

    # ── 19. schema_evolution ───────────────────────────────────────────────────
    # Narrative log of architectural decisions. Complements schema_migrations
    # (which tracks applied SQL changes) with the reasoning behind them.
    c.execute("""
        CREATE TABLE IF NOT EXISTS schema_evolution (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            date                TEXT,
            change_description  TEXT,
            reason              TEXT,
            tables_affected     TEXT,
            migration_checksum  TEXT,
            rollback_supported  INTEGER DEFAULT 0,
            session_id          INTEGER
        )
    """)

    # ── 20. memory_chunks ──────────────────────────────────────────────────────
    # Semantic retrieval layer (Tier 2). Chunks derive from messages.
    c.execute("""
        CREATE TABLE IF NOT EXISTS memory_chunks (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid                 TEXT UNIQUE,
            content              TEXT,
            content_hash         TEXT,
            embedding_vector     BLOB,
            embedding_model      TEXT,
            embedding_dimensions INTEGER,
            embedding_created_at TEXT,
            embedding_version    INTEGER DEFAULT 0,
            embedding_status     TEXT DEFAULT 'pending',
            source_file          TEXT,
            conversation_id      INTEGER,
            start_message_id     INTEGER,
            end_message_id       INTEGER,
            participants         TEXT,
            start_date           TEXT,
            end_date             TEXT,
            sentiment_score      REAL,
            importance_score     REAL DEFAULT 0.5,
            topic_tags           TEXT,
            relationship_phase   TEXT,
            created_at           TEXT
        )
    """)

    # ── 21. memory_provenance ──────────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS memory_provenance (
            id                          INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_type                 TEXT,
            memory_id                   INTEGER,
            originating_conversation_id INTEGER,
            originating_chunk_id        INTEGER,
            extraction_model            TEXT,
            extraction_prompt_hash      TEXT,
            source_url                  TEXT,
            source_fetched_at           TEXT,
            processing_job_id           INTEGER,
            created_at                  TEXT
        )
    """)

    # ── 22. retrieval_events ───────────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS retrieval_events (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            date                 TEXT,
            query                TEXT,
            tiers_used           TEXT,
            chunks_returned      INTEGER,
            tokens_used          INTEGER,
            retrieval_latency_ms INTEGER,
            model_latency_ms     INTEGER,
            success_rating       REAL,
            notes                TEXT,
            created_at           TEXT
        )
    """)

    # ── 23. messages ───────────────────────────────────────────────────────────
    # Atomic message-level storage. Lowest retrieval granularity.
    # Enables ingestion from any source: conversations, Slack, Discord,
    # email, transcripts, YouTube, documents.
    c.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid            TEXT UNIQUE,
            conversation_id INTEGER,
            agent_id        INTEGER,
            timestamp       TEXT,
            content         TEXT,
            content_hash    TEXT,
            token_count     INTEGER,
            message_index   INTEGER,
            source_type     TEXT DEFAULT 'conversation',
            tags            TEXT,
            created_at      TEXT
        )
    """)

    # ── 24. memory_relationships ───────────────────────────────────────────────
    # Generic graph layer. UUID-based linking for stability across migrations.
    # directionality: directed | undirected
    # relationship_type examples: supports, contradicts, triggered, influenced,
    #   derived_from, supersedes, related_to
    c.execute("""
        CREATE TABLE IF NOT EXISTS memory_relationships (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid              TEXT UNIQUE,
            source_type       TEXT,
            source_id         INTEGER,
            source_uuid       TEXT,
            relationship_type TEXT,
            target_type       TEXT,
            target_id         INTEGER,
            target_uuid       TEXT,
            directionality    TEXT DEFAULT 'directed',
            weight            REAL DEFAULT 0.5,
            confidence_score  REAL DEFAULT 0.5,
            valid_from        TEXT,
            valid_to          TEXT,
            notes             TEXT,
            created_at        TEXT
        )
    """)

    # ── 25. users ──────────────────────────────────────────────────────────────
    # Registered human users. Seeded at install time from .ember_config.
    # user_id columns on all core memory tables reference users.id.
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid         TEXT UNIQUE,
            username     TEXT UNIQUE,
            display_name TEXT,
            email        TEXT,
            timezone     TEXT,
            is_active    INTEGER DEFAULT 1,
            created_at   TEXT,
            updated_at   TEXT
        )
    """)

    # ── 26. agents ─────────────────────────────────────────────────────────────
    # type: human | ai | system
    c.execute("""
        CREATE TABLE IF NOT EXISTS agents (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid        TEXT UNIQUE,
            name        TEXT,
            type        TEXT,
            description TEXT,
            is_active   INTEGER DEFAULT 1,
            created_at  TEXT
        )
    """)

    # ── 26b. conversation_participants ─────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS conversation_participants (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER,
            agent_id        INTEGER,
            role            TEXT
        )
    """)

    # ── 27. artifacts ──────────────────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS artifacts (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid          TEXT UNIQUE,
            file_path     TEXT,
            artifact_type TEXT,
            content_hash  TEXT,
            metadata      TEXT,
            created_at    TEXT
        )
    """)

    # ── 28. processing_jobs ────────────────────────────────────────────────────
    # job_type: conversation_extraction | embedding | research |
    #           verification | snapshot | ingestion
    c.execute("""
        CREATE TABLE IF NOT EXISTS processing_jobs (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid           TEXT UNIQUE,
            job_type       TEXT,
            target_type    TEXT,
            target_id      INTEGER,
            model_used     TEXT,
            status         TEXT DEFAULT 'pending',
            started_at     TEXT,
            completed_at   TEXT,
            retry_count    INTEGER DEFAULT 0,
            last_heartbeat TEXT,
            error_log      TEXT,
            created_at     TEXT,
            call_name      TEXT,
            source_file    TEXT
        )
    """)

    # ── 29. memory_objects ─────────────────────────────────────────────────────
    # Global registry of all memory objects across all tables.
    # Auto-populated by SQLite triggers. Never write to this directly.
    # Enables: global search, cross-type analytics, export/import,
    # permissions (future), and ecosystem tooling.
    c.execute("""
        CREATE TABLE IF NOT EXISTS memory_objects (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid             TEXT UNIQUE,
            memory_type      TEXT,
            table_name       TEXT,
            importance_score REAL DEFAULT 0.5,
            is_active        INTEGER DEFAULT 1,
            created_at       TEXT
        )
    """)

    # ── 30. content_fingerprints ───────────────────────────────────────────────
    # Deduplication layer. Prevents duplicate ingestion from any source.
    # canonical_memory_uuid points to the first/authoritative instance.
    c.execute("""
        CREATE TABLE IF NOT EXISTS content_fingerprints (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            content_hash          TEXT UNIQUE,
            first_seen_at         TEXT,
            canonical_memory_uuid TEXT,
            canonical_table       TEXT,
            duplicate_count       INTEGER DEFAULT 0,
            created_at            TEXT
        )
    """)

    # ── 31. token_usage ────────────────────────────────────────────────────────
    # Tracks token consumption per task and model.
    # Critical for open-source users comparing local vs cloud costs.
    c.execute("""
        CREATE TABLE IF NOT EXISTS token_usage (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid              TEXT UNIQUE,
            model_name        TEXT,
            task_type         TEXT,
            tokens_input      INTEGER,
            tokens_output     INTEGER,
            processing_job_id INTEGER,
            created_at        TEXT
        )
    """)

    # ── 32. session_intent_log ─────────────────────────────────────────────────
    # Persists every Tier 0 classification result.
    # triggered_by: 'ingest' | 'manual' | 'refresh_deep_memory' | 'unknown'
    c.execute("""
        CREATE TABLE IF NOT EXISTS session_intent_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            date         TEXT,
            intent       TEXT,
            confidence   REAL,
            corpus_size  INTEGER,
            corpus_tier  TEXT,
            n_questions  INTEGER,
            n_goals      INTEGER,
            n_beliefs    INTEGER,
            threshold    REAL,
            top_per_seed INTEGER,
            notes        TEXT,
            triggered_by TEXT,
            created_at   TEXT
        )
    """)

    # ── 33. trusted_sources ────────────────────────────────────────────────────
    # Research pipeline: approved YouTube channels and publications.
    c.execute("""
        CREATE TABLE IF NOT EXISTS trusted_sources (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            source_type   TEXT    NOT NULL DEFAULT 'youtube_channel',
            channel_id    TEXT,
            channel_name  TEXT,
            channel_url   TEXT,
            topic_focus   TEXT,
            quality_notes TEXT,
            date_added    TEXT,
            approved_by   TEXT    DEFAULT 'user',
            is_active     INTEGER DEFAULT 1,
            notes         TEXT,
            created_at    TEXT    DEFAULT (datetime('now'))
        )
    """)

    # ── 34. scout_results ──────────────────────────────────────────────────────
    # Research Scout output. Relevance-scored papers and videos queued for
    # curator review. challenge_score measures divergence from project vector,
    # allowing users to distinguish confirming vs challenging material.
    c.execute("""
        CREATE TABLE IF NOT EXISTS scout_results (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid             TEXT NOT NULL DEFAULT (
                lower(hex(randomblob(4)) || '-' ||
                hex(randomblob(2)) || '-4' || substr(hex(randomblob(2)),2) ||
                '-' || substr('89ab', abs(random()) % 4 + 1, 1) ||
                substr(hex(randomblob(2)),2) || '-' || hex(randomblob(6)))),
            title            TEXT,
            authors          TEXT,
            abstract         TEXT,
            doi              TEXT,
            source_url       TEXT,
            source_name      TEXT,
            source_type      TEXT,
            publication_date TEXT,
            external_id      TEXT,
            date_fetched     TEXT NOT NULL DEFAULT (date('now')),
            search_query     TEXT,
            search_ring      INTEGER,
            triggered_by     TEXT,
            relevance_score  REAL,
            relevance_notes  TEXT,
            status           TEXT NOT NULL DEFAULT 'pending',
            curator_notes    TEXT,
            promoted_to      TEXT,
            reviewed_at      TEXT,
            tags             TEXT,
            challenge_score  REAL,
            created_at       TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)

    # ── join table scaffolds ───────────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS belief_chunk_links (
            belief_id INTEGER,
            chunk_id  INTEGER,
            PRIMARY KEY (belief_id, chunk_id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS belief_reflection_links (
            belief_id     INTEGER,
            reflection_id INTEGER,
            PRIMARY KEY (belief_id, reflection_id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS reflection_chunk_links (
            reflection_id INTEGER,
            chunk_id      INTEGER,
            PRIMARY KEY (reflection_id, chunk_id)
        )
    """)

    # ── SQLite triggers: auto-populate memory_objects ──────────────────────────
    # These triggers fire after every INSERT on a tracked memory table.
    # The registry stays consistent without any application-level maintenance.
    trigger_tables = [
        ("beliefs",       "belief",       "uuid", "importance_score"),
        ("epiphanies",    "epiphany",     "uuid", "importance_score"),
        ("reflections",   "reflection",   "uuid", "importance_score"),
        ("patterns",      "pattern",      "uuid", "importance_score"),
        ("memory_chunks", "chunk",        "uuid", "importance_score"),
        ("messages",      "message",      "uuid", "0.5"),
        ("conversations", "conversation", "uuid", "0.5"),
        ("concepts",      "concept",      "NULL", "0.5"),
    ]

    for table, mem_type, uuid_col, importance_col in trigger_tables:
        uuid_expr = f"NEW.{uuid_col}" if uuid_col != "NULL" else "lower(hex(randomblob(16)))"
        importance_expr = f"NEW.{importance_col}" if importance_col != "0.5" else "0.5"
        c.execute(f"""
            CREATE TRIGGER IF NOT EXISTS trg_{table}_to_registry
            AFTER INSERT ON {table}
            BEGIN
                INSERT OR IGNORE INTO memory_objects
                    (uuid, memory_type, table_name, importance_score, is_active, created_at)
                VALUES (
                    {uuid_expr},
                    '{mem_type}',
                    '{table}',
                    {importance_expr},
                    1,
                    NEW.created_at
                );
            END
        """)

    # ── performance indexes ────────────────────────────────────────────────────
    indexes = [
        # beliefs
        "CREATE INDEX IF NOT EXISTS idx_beliefs_status        ON beliefs(status, is_active)",
        "CREATE INDEX IF NOT EXISTS idx_beliefs_valid         ON beliefs(valid_from, valid_to)",
        "CREATE INDEX IF NOT EXISTS idx_beliefs_uuid          ON beliefs(uuid)",
        "CREATE INDEX IF NOT EXISTS idx_beliefs_importance    ON beliefs(importance_score)",
        "CREATE INDEX IF NOT EXISTS idx_beliefs_extraction    ON beliefs(extraction_version)",
        # epiphanies
        "CREATE INDEX IF NOT EXISTS idx_epiphanies_uuid       ON epiphanies(uuid)",
        "CREATE INDEX IF NOT EXISTS idx_epiphanies_active     ON epiphanies(is_active)",
        # conversations
        "CREATE INDEX IF NOT EXISTS idx_conversations_uuid    ON conversations(uuid)",
        "CREATE INDEX IF NOT EXISTS idx_conversations_date    ON conversations(date)",
        # memory_chunks
        "CREATE INDEX IF NOT EXISTS idx_chunks_uuid           ON memory_chunks(uuid)",
        "CREATE INDEX IF NOT EXISTS idx_chunks_conv           ON memory_chunks(conversation_id)",
        "CREATE INDEX IF NOT EXISTS idx_chunks_embed          ON memory_chunks(embedding_version, embedding_status)",
        "CREATE INDEX IF NOT EXISTS idx_chunks_hash           ON memory_chunks(content_hash)",
        # messages
        "CREATE INDEX IF NOT EXISTS idx_messages_conv         ON messages(conversation_id, message_index)",
        "CREATE INDEX IF NOT EXISTS idx_messages_uuid         ON messages(uuid)",
        "CREATE INDEX IF NOT EXISTS idx_messages_timestamp    ON messages(timestamp)",
        # memory_relationships
        "CREATE INDEX IF NOT EXISTS idx_rel_source            ON memory_relationships(source_type, source_id)",
        "CREATE INDEX IF NOT EXISTS idx_rel_target            ON memory_relationships(target_type, target_id)",
        "CREATE INDEX IF NOT EXISTS idx_rel_uuid              ON memory_relationships(uuid)",
        "CREATE INDEX IF NOT EXISTS idx_rel_source_uuid       ON memory_relationships(source_uuid)",
        "CREATE INDEX IF NOT EXISTS idx_rel_target_uuid       ON memory_relationships(target_uuid)",
        # retrieval
        "CREATE INDEX IF NOT EXISTS idx_retrieval_date        ON retrieval_events(created_at)",
        # reflections
        "CREATE INDEX IF NOT EXISTS idx_reflections_uuid      ON reflections(uuid)",
        "CREATE INDEX IF NOT EXISTS idx_reflections_dates     ON reflections(start_date, end_date)",
        # patterns
        "CREATE INDEX IF NOT EXISTS idx_patterns_uuid         ON patterns(uuid)",
        "CREATE INDEX IF NOT EXISTS idx_patterns_active       ON patterns(is_active)",
        # agents
        "CREATE INDEX IF NOT EXISTS idx_agents_uuid           ON agents(uuid)",
        # artifacts
        "CREATE INDEX IF NOT EXISTS idx_artifacts_uuid        ON artifacts(uuid)",
        "CREATE INDEX IF NOT EXISTS idx_artifacts_hash        ON artifacts(content_hash)",
        # processing jobs
        "CREATE INDEX IF NOT EXISTS idx_jobs_uuid             ON processing_jobs(uuid)",
        "CREATE INDEX IF NOT EXISTS idx_jobs_status           ON processing_jobs(status, job_type)",
        # provenance
        "CREATE INDEX IF NOT EXISTS idx_provenance_type       ON memory_provenance(memory_type, memory_id)",
        "CREATE INDEX IF NOT EXISTS idx_provenance_job        ON memory_provenance(processing_job_id)",
        "CREATE INDEX IF NOT EXISTS idx_provenance_url        ON memory_provenance(source_url)",
        # memory_objects registry
        "CREATE INDEX IF NOT EXISTS idx_objects_uuid          ON memory_objects(uuid)",
        "CREATE INDEX IF NOT EXISTS idx_objects_type          ON memory_objects(memory_type, is_active)",
        "CREATE INDEX IF NOT EXISTS idx_objects_importance    ON memory_objects(importance_score)",
        # content fingerprints
        "CREATE INDEX IF NOT EXISTS idx_fingerprints_hash     ON content_fingerprints(content_hash)",
        # token usage
        "CREATE INDEX IF NOT EXISTS idx_tokens_model          ON token_usage(model_name, task_type)",
        "CREATE INDEX IF NOT EXISTS idx_tokens_job            ON token_usage(processing_job_id)",
        # trusted_sources
        "CREATE INDEX IF NOT EXISTS idx_trusted_sources_type    ON trusted_sources (source_type)",
        "CREATE INDEX IF NOT EXISTS idx_trusted_sources_active  ON trusted_sources (is_active)",
        "CREATE INDEX IF NOT EXISTS idx_trusted_sources_channel ON trusted_sources (channel_id)",
        # scout_results
        "CREATE INDEX IF NOT EXISTS idx_scout_status    ON scout_results (status)",
        "CREATE INDEX IF NOT EXISTS idx_scout_relevance ON scout_results (relevance_score)",
        "CREATE INDEX IF NOT EXISTS idx_scout_date      ON scout_results (date_fetched)",
        "CREATE INDEX IF NOT EXISTS idx_scout_source    ON scout_results (source_type, source_name)",
    ]

    for idx in indexes:
        c.execute(idx)

    conn.commit()


def _seed_data(conn: sqlite3.Connection) -> None:
    """Seed primary user, founding agents, and schema evolution entry."""
    c = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Read username from .ember_config if available; fall back to 'user'
    _username = "user"
    _email    = ""
    _config   = Path.home() / "claude_memory" / ".ember_config"
    if _config.exists():
        for _line in _config.read_text().splitlines():
            if _line.startswith("USERNAME=") and not _line.startswith("#"):
                _username = _line.split("=", 1)[1].strip().strip('"')
            if _line.startswith("EMAIL=") and not _line.startswith("#"):
                _email = _line.split("=", 1)[1].strip().strip('"')

    c.execute("""
        INSERT OR IGNORE INTO users
            (uuid, username, display_name, email, is_active, created_at, updated_at)
        VALUES (?, ?, ?, ?, 1, ?, ?)
    """, (
        str(uuid_lib.uuid4()),
        _username,
        _username.capitalize(),
        _email,
        now, now,
    ))

    for name, atype, desc in [
        (_username, "human",
         "Primary human collaborator and founder of this memory system."),
        ("Claude",  "ai",
         "Anthropic Claude. AI partner and cognitive layer "
         "of this persistent memory architecture."),
        ("Qwen",    "ai",
         "Qwen 2.5 running locally via Ollama. "
         "Handles background extraction and processing at zero token cost."),
    ]:
        c.execute("""
            INSERT OR IGNORE INTO agents
                (uuid, name, type, description, is_active, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (str(uuid_lib.uuid4()), name, atype, desc, 1, now))

    c.execute("""
        INSERT INTO schema_evolution
            (date, change_description, reason, tables_affected,
             rollback_supported, session_id)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        now,
        f"Schema {CURRENT_SCHEMA_VERSION}: fresh install. "
        "Full table set including schema_migrations for formal version tracking, "
        "user_id/agent_id on all core tables, scout_results with challenge_score.",
        "ember-engine open-source release. Designed as cognitive memory layer "
        "for Claude CoWork. Integrates with OpenClaw as execution/runtime layer.",
        "All tables",
        0,
        1,
    ))

    conn.commit()
    return _username


def _seed_migrations_baseline(conn: sqlite3.Connection) -> None:
    """
    Mark all migrations as applied when creating a fresh DB.

    A fresh install has all schema at current version by definition.
    Recording them as applied prevents migrate_db.py from trying to
    re-run them on an already-correct schema.
    """
    from migrate_db import MIGRATIONS, _checksum
    now = datetime.now().isoformat()
    c = conn.cursor()
    for mig in MIGRATIONS:
        chk = _checksum(mig["statements"])
        c.execute(
            "INSERT OR IGNORE INTO schema_migrations "
            "(version, name, applied_at, checksum) VALUES (?, ?, ?, ?)",
            (mig["version"], mig["name"], now, chk),
        )
    conn.commit()


def _print_summary(username: str) -> None:
    print(f"Memory database initialized at {DB_PATH}")
    print(f"Schema version: {CURRENT_SCHEMA_VERSION}")
    print()
    print("Tables (34 core + 3 join tables + 1 migration tracking):")
    print()
    print("  Migration tracking:")
    print("    0.  schema_migrations    [version, name, applied_at, checksum]")
    print()
    print("  Core memory:")
    print("    1.  conversations        [source provenance, user/agent identity]")
    print("    2.  beliefs              [confidence scoring, lifecycle, versioning, calibration]")
    print("    3.  epiphanies           [confidence scoring, versioning, calibration]")
    print("    4.  position_history     [state transitions]")
    print("    5.  concepts")
    print("    6.  sessions")
    print()
    print("  Retrieval layers (Tier 1-4):")
    print("    7.  context_snapshots    [Tier 4 - hot memory]")
    print("    8.  reflections          [Tier 3 - episodic]")
    print("    9.  memory_chunks        [Tier 2 - semantic scaffold]")
    print("    10. messages             [Tier 1 - atomic storage]")
    print()
    print("  Epistemic infrastructure:")
    print("    11. sources")
    print("    12. research_tasks")
    print("    13. tensions             [+cluster_id]")
    print("    14. patterns             [+user/agent identity]")
    print("    15. questions            [+user/agent identity]")
    print()
    print("  Graph and relationships:")
    print("    16. memory_relationships [UUID-based, directed/weighted, temporal]")
    print("    17. belief_chunk_links")
    print("    18. belief_reflection_links")
    print("    19. reflection_chunk_links")
    print()
    print("  Identity and collaboration:")
    print("    20. agents               [seeded: user, Claude, Qwen]")
    print("    21. users                [seeded from .ember_config]")
    print("    22. conversation_participants")
    print()
    print("  Ingestion and artifacts:")
    print("    23. artifacts")
    print("    24. processing_jobs      [+retry_count, last_heartbeat, call_name, source_file]")
    print()
    print("  Observability and provenance:")
    print("    25. memory_objects       [global registry, auto-populated by 8 triggers]")
    print("    26. content_fingerprints [deduplication layer]")
    print("    27. token_usage          [cost tracking]")
    print("    28. memory_provenance")
    print("    29. retrieval_events")
    print("    30. schema_evolution     [architectural narrative log]")
    print()
    print("  Relationship and context:")
    print("    31. goals                [+user/agent identity]")
    print("    32. entities             [+user/agent identity]")
    print("    33. moods                [+user/agent identity]")
    print("    34. gratitude            [+user/agent identity]")
    print("    35. boundaries")
    print()
    print("  Research pipeline:")
    print("    36. trusted_sources      [YouTube channels, publications]")
    print("    37. scout_results        [Research Scout output, challenge_score]")
    print("    38. session_intent_log   [Tier 0 classification history]")
    print()
    print("  Triggers: 8 (auto-populate memory_objects registry)")
    print(f"  Indexes:  {44}")
    print(f"  Founding agents seeded: {username}, Claude, Qwen")
    print()
    print(f"Schema {CURRENT_SCHEMA_VERSION} complete. Ready for first session.")


def main() -> None:
    db_path = Path(DB_PATH)

    if db_path.exists():
        # Existing database: run incremental migrations only.
        # Do not touch tables that already contain data.
        print(f"Database found at {DB_PATH}")
        print("Running incremental migrations...\n")
        import migrate_db
        n = migrate_db.apply_migrations(db_path)
        if n == 0:
            print("\nAll migrations already applied. Schema is current.")
        else:
            print(f"\n{n} migration(s) applied. Schema updated to {CURRENT_SCHEMA_VERSION}.")
        return

    # Fresh install: create full schema, seed data, record baseline migrations.
    print(f"Creating new database at {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    try:
        create_latest_schema(conn)
        username = _seed_data(conn)
        _seed_migrations_baseline(conn)
        conn.close()
        _print_summary(username)
    except Exception:
        conn.close()
        db_path.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    main()

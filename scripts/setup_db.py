"""
setup_db.py
-----------
Creates and initializes the CoWork memory database.

To wipe and rebuild from scratch:
    rm ~/claude_memory/memory.db
    python3 setup_db.py

Schema version: 2.2
Changes from v2.1:
    - Added memory_objects registry table (auto-populated via SQLite triggers)
    - Added content_fingerprints table for deduplication
    - Added token_usage table for cost tracking
    - Extended memory_relationships: source_uuid, target_uuid, directionality, weight, valid_from, valid_to
    - Extended beliefs/epiphanies: extraction_version, last_processed_at, confidence_calibrated
    - Extended reflections/patterns: extraction_version, last_processed_at
    - Extended processing_jobs: retry_count, last_heartbeat
    - Extended tensions: tension_cluster_id
    - Added SQLite triggers to auto-populate memory_objects on INSERT
    - Added indexes for new tables and fields
    Total tables: 31 core + 3 join tables
"""

import sqlite3
import uuid as uuid_lib
import os
from datetime import datetime
from pathlib import Path

DB_PATH = os.path.expanduser("~/claude_memory/memory.db")


def setup_database():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("PRAGMA foreign_keys = ON")

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
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid                 TEXT UNIQUE,
            date                 TEXT,
            description          TEXT,
            conversation_id      INTEGER,
            concept_id           INTEGER,
            preceded_by          TEXT,
            implications         TEXT,
            checksum_status      TEXT,
            confidence_score     REAL DEFAULT 0.5,
            confidence_calibrated INTEGER DEFAULT 0,
            evidence_snippets    TEXT,
            verbatim_anchor      TEXT,
            source_type          TEXT DEFAULT 'model_inference',
            is_active            INTEGER DEFAULT 1,
            archived_at          TEXT,
            importance_score     REAL DEFAULT 0.5,
            valid_from           TEXT,
            valid_to             TEXT,
            version              INTEGER DEFAULT 1,
            extraction_version   INTEGER DEFAULT 1,
            last_processed_at    TEXT,
            memory_origin        TEXT DEFAULT 'conversation',
            tags                 TEXT,
            created_at           TEXT,
            updated_at           TEXT
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
            created_at                 TEXT,
            updated_at                 TEXT
        )
    """)

    # ── 7. concepts ────────────────────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS concepts (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            name               TEXT,
            description        TEXT,
            first_appeared     TEXT,
            conversation_id    INTEGER,
            related_beliefs    TEXT,
            related_epiphanies TEXT,
            evolution_notes    TEXT,
            memory_origin      TEXT DEFAULT 'conversation',
            tags               TEXT,
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
            tags             TEXT,
            created_at       TEXT,
            updated_at       TEXT
        )
    """)

    # ── 10. tensions ───────────────────────────────────────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS tensions (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            topic             TEXT,
            belief_a_id       INTEGER,
            belief_b_id       INTEGER,
            description       TEXT,
            date_identified   TEXT,
            resolution        TEXT,
            resolution_notes  TEXT,
            resolved_date     TEXT,
            confidence_score  REAL,
            is_active         INTEGER DEFAULT 1,
            archived_at       TEXT,
            importance_score  REAL DEFAULT 0.5,
            valid_from        TEXT,
            valid_to          TEXT,
            tension_cluster_id TEXT,
            tags              TEXT,
            created_at        TEXT,
            updated_at        TEXT
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
            tags                    TEXT,
            created_at              TEXT
        )
    """)

    # ── 19. schema_evolution ───────────────────────────────────────────────────
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

    # ── 26. conversation_participants ──────────────────────────────────────────
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
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid          TEXT UNIQUE,
            memory_type   TEXT,
            table_name    TEXT,
            importance_score REAL DEFAULT 0.5,
            is_active     INTEGER DEFAULT 1,
            created_at    TEXT
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
    # task_type: conversation_extraction | embedding | research |
    #            verification | retrieval | snapshot
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

    # ── 32. session_intent_log ────────────────────────────────────────────────
    # Persists every Tier 0 classification result so session intent history is
    # queryable. Written by tier0_classifier.py on each classify_session() call.
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

    # ── Research sources: trusted channels and publications ────────────────────
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
    c.execute("CREATE INDEX IF NOT EXISTS idx_trusted_sources_type     ON trusted_sources (source_type)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_trusted_sources_active   ON trusted_sources (is_active)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_trusted_sources_channel  ON trusted_sources (channel_id)")

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
        # memory_objects registry
        "CREATE INDEX IF NOT EXISTS idx_objects_uuid          ON memory_objects(uuid)",
        "CREATE INDEX IF NOT EXISTS idx_objects_type          ON memory_objects(memory_type, is_active)",
        "CREATE INDEX IF NOT EXISTS idx_objects_importance    ON memory_objects(importance_score)",
        # content fingerprints
        "CREATE INDEX IF NOT EXISTS idx_fingerprints_hash     ON content_fingerprints(content_hash)",
        # token usage
        "CREATE INDEX IF NOT EXISTS idx_tokens_model          ON token_usage(model_name, task_type)",
        "CREATE INDEX IF NOT EXISTS idx_tokens_job            ON token_usage(processing_job_id)",
    ]

    for idx in indexes:
        c.execute(idx)

    # ── seed primary user ─────────────────────────────────────────────────────
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

    # ── seed founding agents ───────────────────────────────────────────────────

    c.execute("""
        INSERT OR IGNORE INTO agents (uuid, name, type, description, is_active, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        str(uuid_lib.uuid4()),
        _username,
        "human",
        "Primary human collaborator and founder of this memory system.",
        1, now
    ))

    c.execute("""
        INSERT OR IGNORE INTO agents (uuid, name, type, description, is_active, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        str(uuid_lib.uuid4()),
        "Claude",
        "ai",
        "Anthropic Claude. AI partner and cognitive layer "
        "of this persistent memory architecture.",
        1, now
    ))

    c.execute("""
        INSERT OR IGNORE INTO agents (uuid, name, type, description, is_active, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        str(uuid_lib.uuid4()),
        "Qwen",
        "ai",
        "Qwen 2.5 32B running locally via Ollama. "
        "Handles background extraction and processing at zero token cost.",
        1, now
    ))

    # ── schema evolution log ───────────────────────────────────────────────────
    c.execute("""
        INSERT INTO schema_evolution
            (date, change_description, reason, tables_affected,
             rollback_supported, session_id)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        now,
        "Schema v2.2: added memory_objects registry with auto-population triggers, "
        "content_fingerprints deduplication table, token_usage tracking table, "
        "extended memory_relationships with UUID linking, directionality, weight "
        "and temporal fields, added extraction_version and confidence_calibrated "
        "to beliefs and epiphanies, added retry/heartbeat to processing_jobs, "
        "tension_cluster_id to tensions, 37 performance indexes",
        "Final pre-data schema. Designed for open-source release as cognitive layer "
        "for continuous autonomous AI agents. Intended for integration with OpenClaw "
        "as execution/runtime layer.",
        "All tables (31 core + 3 join tables)",
        0,
        2
    ))

    conn.commit()
    conn.close()

    print(f"Memory database initialized at {DB_PATH}")
    print(f"Timestamp: {now}")
    print(f"Schema version: 2.2")
    print()
    print("Tables (31 core + 3 join tables):")
    print()
    print("  Core memory:")
    print("    1.  conversations")
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
    print("    14. patterns")
    print("    15. questions")
    print()
    print("  Graph and relationships:")
    print("    16. memory_relationships [UUID-based, directed/weighted, temporal]")
    print("    17. belief_chunk_links")
    print("    18. belief_reflection_links")
    print("    19. reflection_chunk_links")
    print()
    print("  Identity and collaboration:")
    print("    20. agents               [Bobby, Claude, Qwen seeded]")
    print("    21. conversation_participants")
    print()
    print("  Ingestion and artifacts:")
    print("    22. artifacts")
    print("    23. processing_jobs      [+retry_count, last_heartbeat]")
    print()
    print("  Observability and provenance:")
    print("    24. memory_objects       [global registry, auto-populated by triggers]")
    print("    25. content_fingerprints [deduplication layer]")
    print("    26. token_usage          [cost tracking]")
    print("    27. memory_provenance")
    print("    28. retrieval_events")
    print("    29. schema_evolution")
    print()
    print("  Relationship and context:")
    print("    30. goals")
    print("    31. entities")
    print("    32. moods")
    print("    33. gratitude")
    print("    34. boundaries")
    print()
    print("  Research pipeline:")
    print("    35. trusted_sources      [YouTube channels, publications, approved sources]")
    print()
    print("  Triggers: 8 (auto-populate memory_objects registry)")
    print("  Indexes:  40")
    print(f"  Founding agents seeded: {_username}, Claude, Qwen")
    print()
    print("Schema v2.2 complete. Ready for rebuild.")


if __name__ == "__main__":
    setup_database()

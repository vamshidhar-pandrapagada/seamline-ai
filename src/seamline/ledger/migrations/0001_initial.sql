-- Seamline ledger, version 1. One file per project: .seamline/ledger.db
-- Closed vocabularies (fact kinds, interface kinds, statuses) are enforced with CHECK constraints.

CREATE TABLE services (
    name TEXT PRIMARY KEY,
    path TEXT NOT NULL
);

-- An interface is anything two services can disagree about: an event, route, RPC, topic,
-- table, env var, config key or CLI. `key` is the normalized name used for matching
-- (order.created, OrderCreated and ORDER_CREATED share one key).
CREATE TABLE interfaces (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,                       -- First name seen, for display
    kind TEXT CHECK (kind IN ('grpc', 'http', 'event', 'topic', 'table', 'env', 'config', 'cli'))
);

CREATE TABLE interface_aliases (
    alias TEXT PRIMARY KEY,                   -- Every raw spelling seen
    interface_id INTEGER NOT NULL REFERENCES interfaces(id)
);

CREATE TABLE facts (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('provides', 'consumes', 'assumes', 'decision', 'dead_end')),
    origin TEXT NOT NULL CHECK (origin IN ('session', 'code', 'user')),
    service TEXT,                             -- NULL = integration scope (no single service)
    attributed_by TEXT NOT NULL CHECK (attributed_by IN ('name', 'files', 'folder', 'code', 'none')),
    interface_id INTEGER REFERENCES interfaces(id),
    claim TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '[]',       -- JSON list of {"name", "value"}
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('proposed', 'active', 'superseded', 'stale')),
    confidence REAL NOT NULL DEFAULT 1.0 CHECK (confidence BETWEEN 0 AND 1),
    source_ref TEXT,                          -- Code facts: "path#Symbol", to recognize them on rescan
    prompt_version TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX facts_interface ON facts(interface_id, status);
CREATE INDEX facts_service ON facts(service, status);
CREATE INDEX facts_source_ref ON facts(source_ref);

-- Where each fact came from: a transcript line or a file line. A repeated fact gains
-- evidence instead of becoming a duplicate.
CREATE TABLE evidence (
    id INTEGER PRIMARY KEY,
    fact_id INTEGER NOT NULL REFERENCES facts(id),
    session_id TEXT,
    file_path TEXT,
    line_no INTEGER,
    quote TEXT NOT NULL,
    timestamp TEXT,
    CHECK ((session_id IS NULL) != (file_path IS NULL))
);
CREATE INDEX evidence_fact ON evidence(fact_id);

CREATE TABLE fact_files (
    fact_id INTEGER NOT NULL REFERENCES facts(id),
    path TEXT NOT NULL,
    PRIMARY KEY (fact_id, path)
);

CREATE TABLE fact_links (
    from_fact INTEGER NOT NULL REFERENCES facts(id),
    to_fact INTEGER NOT NULL REFERENCES facts(id),
    relation TEXT NOT NULL CHECK (relation IN ('supersedes')),
    PRIMARY KEY (from_fact, to_fact, relation)
);

-- Contract drift: a provider and a consumer's assumption that disagree. Computed, never
-- extracted.
CREATE TABLE mismatches (
    id INTEGER PRIMARY KEY,
    interface_id INTEGER NOT NULL REFERENCES interfaces(id),
    provides_fact INTEGER NOT NULL REFERENCES facts(id),
    assumes_fact INTEGER NOT NULL REFERENCES facts(id),
    field TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    resolved_at TEXT,
    UNIQUE (provides_fact, assumes_fact, field)
);

CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    service TEXT,                             -- From the start folder; NULL = integration
    transcript_path TEXT NOT NULL,
    read_offset INTEGER NOT NULL DEFAULT 0,   -- Bytes already ingested
    read_line INTEGER NOT NULL DEFAULT 0,
    dirty INTEGER NOT NULL DEFAULT 1,
    urgent INTEGER NOT NULL DEFAULT 0,
    last_activity TEXT,
    last_ingested TEXT
);

-- Append-only log; sessions compare their `seen` position against it (Phase 4 updates).
CREATE TABLE changes (
    change_id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('fact_added', 'fact_superseded', 'mismatch_opened', 'mismatch_resolved')),
    fact_id INTEGER REFERENCES facts(id),
    mismatch_id INTEGER REFERENCES mismatches(id),
    services TEXT NOT NULL DEFAULT '',        -- Comma-separated services affected
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE seen (
    session_id TEXT PRIMARY KEY,
    last_change_id INTEGER NOT NULL DEFAULT 0
);

-- Full-text search over claims and quotes (Phase 5 search_history).
CREATE VIRTUAL TABLE facts_fts USING fts5(claim, content='facts', content_rowid='id');
CREATE TRIGGER facts_fts_insert AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, claim) VALUES (new.id, new.claim);
END;
CREATE TRIGGER facts_fts_delete AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, claim) VALUES ('delete', old.id, old.claim);
END;

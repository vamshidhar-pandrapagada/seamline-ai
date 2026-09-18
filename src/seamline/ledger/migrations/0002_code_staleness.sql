-- Version 2: session facts can go stale when the code no longer backs them.

-- Why a fact is stale: 'code' = its service's code changed after the statement and no
-- longer mentions a field it names (reversible); 'removed' = a code fact whose source
-- disappeared on rescan.
ALTER TABLE facts ADD COLUMN stale_reason TEXT CHECK (stale_reason IN ('code', 'removed'));
ALTER TABLE facts ADD COLUMN stale_detail TEXT;

-- SQLite can't alter a CHECK constraint, so rebuild `changes` with the two new kinds.
CREATE TABLE changes_v2 (
    change_id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('fact_added', 'fact_superseded', 'fact_stale',
                                       'fact_restored', 'mismatch_opened', 'mismatch_resolved')),
    fact_id INTEGER REFERENCES facts(id),
    mismatch_id INTEGER REFERENCES mismatches(id),
    services TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
INSERT INTO changes_v2 SELECT * FROM changes;
DROP TABLE changes;
ALTER TABLE changes_v2 RENAME TO changes;

UPDATE facts SET stale_reason = 'removed' WHERE status = 'stale' AND origin = 'code';

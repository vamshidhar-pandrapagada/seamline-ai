-- Version 3: what each model call cost, for the daily spending cap.

CREATE TABLE spend (
    id INTEGER PRIMARY KEY,
    day TEXT NOT NULL,                        -- Local date (YYYY-MM-DD) the cap counts against
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    model TEXT NOT NULL,                      -- The model that answered (may be a fallback)
    purpose TEXT NOT NULL,                    -- The tool the model filled in: record_facts, record_verdict
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    usd REAL                                  -- NULL when the model has no known price
);
CREATE INDEX spend_day ON spend(day);

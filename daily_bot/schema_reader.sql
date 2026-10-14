-- Reader outline and on-demand section cache.
-- Apply once with psql; all statements are idempotent.

CREATE TABLE IF NOT EXISTS reader_outline (
    arxiv_id            TEXT PRIMARY KEY REFERENCES papers(arxiv_id) ON DELETE CASCADE,
    outline             JSONB NOT NULL,
    source_fingerprint  TEXT NOT NULL,
    model               TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS reader_section_cache (
    arxiv_id            TEXT NOT NULL REFERENCES papers(arxiv_id) ON DELETE CASCADE,
    section_index       INTEGER NOT NULL,
    source_fingerprint  TEXT NOT NULL,
    prompt_version      TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed')),
    html                TEXT,
    error               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (arxiv_id, section_index, source_fingerprint, prompt_version)
);

CREATE TABLE IF NOT EXISTS reader_section_usage (
    usage_date     DATE PRIMARY KEY,
    generated_count INTEGER NOT NULL DEFAULT 0
);

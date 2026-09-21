-- Classic impact enrichment, independent of daily/factor/topic score tables.
-- Eligibility is data-driven; there is intentionally no publication-age gate.
-- Idempotent: safe to run repeatedly against the existing production database.
CREATE TABLE IF NOT EXISTS paper_impact (
    paper_id                         INTEGER REFERENCES papers(id) ON DELETE CASCADE,
    arxiv_id                         TEXT PRIMARY KEY REFERENCES papers(arxiv_id) ON DELETE CASCADE,
    openalex_id                      TEXT,
    citation_count                   INTEGER,
    counts_by_year                   JSONB,
    fwci                             NUMERIC,
    citation_normalized_percentile  NUMERIC,
    active_citation_years            INTEGER,
    classic_score                    NUMERIC(6,2),
    classic_score_version            TEXT NOT NULL DEFAULT 'v2.1',
    eligible                         BOOLEAN NOT NULL DEFAULT FALSE,
    openalex_updated_at              TIMESTAMPTZ,
    refreshed_at                     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_paper_impact_classic
    ON paper_impact (eligible, classic_score DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_paper_impact_refresh
    ON paper_impact (refreshed_at);

ALTER TABLE paper_impact
    ALTER COLUMN classic_score_version SET DEFAULT 'v2.1';

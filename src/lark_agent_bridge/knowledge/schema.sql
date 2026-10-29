-- Schema for Andre's long-term knowledge retrieval (Option A: Postgres FTS).
-- Idempotent: safe to run multiple times.
-- Run as the owning role against the target database, e.g.:
--   psql "postgresql://andre:***@localhost:5432/andre_context" -f schema.sql

CREATE TABLE IF NOT EXISTS knowledge (
    id            BIGSERIAL PRIMARY KEY,
    source        TEXT NOT NULL,              -- e.g. 'LOG_SESSIONS.md', 'BACKLOG.md'
    section       TEXT NOT NULL,              -- heading path, e.g. 'sesi 12 > Review MR 1870'
    content       TEXT NOT NULL,              -- chunk body (markdown)
    content_hash  TEXT NOT NULL,              -- sha256(content) for idempotent upsert
    tsv           tsvector,                   -- full-text index vector
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source, section)
);

-- GIN index on tsvector => millisecond FTS queries.
CREATE INDEX IF NOT EXISTS knowledge_tsv_idx ON knowledge USING GIN (tsv);

-- trigram index on section for fuzzy heading match (pg_trgm must be installed).
CREATE INDEX IF NOT EXISTS knowledge_section_trgm_idx ON knowledge USING GIN (section gin_trgm_ops);

-- Keep tsv in sync with section (weight A) + content (weight B).
-- 'simple' config: no aggressive stemming, preserves tokens like 'SIT-4568', '1870', 'failover'.
CREATE OR REPLACE FUNCTION knowledge_tsv_update() RETURNS trigger AS $$
BEGIN
  NEW.tsv :=
     setweight(to_tsvector('simple', coalesce(NEW.section, '')), 'A') ||
     setweight(to_tsvector('simple', coalesce(NEW.content, '')), 'B');
  RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS knowledge_tsv_trg ON knowledge;
CREATE TRIGGER knowledge_tsv_trg BEFORE INSERT OR UPDATE ON knowledge
  FOR EACH ROW EXECUTE FUNCTION knowledge_tsv_update();

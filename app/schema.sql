CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS family_members (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL CHECK (btrim(name) <> ''),
    aliases TEXT[] NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    member_id TEXT NOT NULL REFERENCES family_members(id),
    title TEXT NOT NULL,
    report_date DATE NOT NULL,
    report_type TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK (source_type IN ('synthetic', 'uploaded')),
    raw_text TEXT NOT NULL CHECK (btrim(raw_text) <> ''),
    file_path TEXT,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'confirmed')),
    confirmed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (
        (status = 'draft' AND confirmed_at IS NULL)
        OR (status = 'confirmed' AND confirmed_at IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS report_chunks (
    id TEXT PRIMARY KEY,
    report_id TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
    section TEXT,
    content TEXT NOT NULL CHECK (btrim(content) <> ''),
    embedding VECTOR,
    embedding_model TEXT,
    embedding_dimensions INTEGER,
    search_vector TSVECTOR,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (report_id, chunk_index),
    CHECK (
        (embedding IS NULL AND embedding_model IS NULL AND embedding_dimensions IS NULL)
        OR (embedding IS NOT NULL AND embedding_model IS NOT NULL
            AND embedding_dimensions IS NOT NULL AND embedding_dimensions > 0
            AND vector_dims(embedding) = embedding_dimensions)
    )
);

CREATE TABLE IF NOT EXISTS metrics (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    report_id TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    metric_code TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    value NUMERIC NOT NULL,
    unit TEXT NOT NULL,
    reference_range TEXT,
    source_text TEXT NOT NULL CHECK (btrim(source_text) <> ''),
    confirmed BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (report_id, metric_code)
);

CREATE TABLE IF NOT EXISTS query_logs (
    id TEXT PRIMARY KEY,
    question TEXT NOT NULL,
    member_ids TEXT[] NOT NULL DEFAULT '{}',
    strategy TEXT,
    trace JSONB NOT NULL DEFAULT '[]'::jsonb,
    citations JSONB NOT NULL DEFAULT '[]'::jsonb,
    latency_ms INTEGER CHECK (latency_ms >= 0),
    token_usage JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_reports_member_date
    ON reports (member_id, report_date, id);

-- Purely additive migration: client brand-context storage for the LLM
-- content pipeline. Does not touch any existing visibility-tracking table.
--
-- Requires the pgvector extension. On a self-managed Postgres install:
--   apt install postgresql-16-pgvector   (or build from source)
-- On DigitalOcean managed Postgres: pgvector is available on PG 15+,
-- just CREATE EXTENSION.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS client_contexts (
    id SERIAL PRIMARY KEY,
    client_id INT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    context_type TEXT NOT NULL,          -- e.g. 'brand_voice', 'icp', 'journey_map', 'case_study'
    title TEXT,                          -- optional human label; '' is normalized to NULL
    content TEXT NOT NULL,               -- whole markdown document, not fragmented
    embedding vector(1536),              -- OpenAI text-embedding-3-small by default
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_file TEXT,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (client_id, context_type, COALESCE(title, ''))
);

COMMENT ON TABLE client_contexts IS
    'Per-client brand context docs (brand voice, ICP, journey maps) for the LLM content pipeline. One whole markdown doc per row.';

CREATE INDEX IF NOT EXISTS idx_client_contexts_client
    ON client_contexts (client_id, context_type);

-- Metadata filtering (e.g. metadata @> '{"lang": "en"}')
CREATE INDEX IF NOT EXISTS idx_client_contexts_metadata
    ON client_contexts USING gin (metadata);

-- Semantic search. HNSW over cosine distance.
CREATE INDEX IF NOT EXISTS idx_client_contexts_embedding
    ON client_contexts USING hnsw (embedding vector_cosine_ops);

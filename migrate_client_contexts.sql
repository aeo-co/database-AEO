-- Purely additive migration: client brand-context storage for the LLM
-- content pipeline. Does not touch any existing visibility-tracking table.
--
-- Embeddings are generated ON-SERVER with a local sentence-transformers
-- model (all-MiniLM-L6-v2, 384-dim) — no external API needed.
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
    embedding vector(384),               -- local all-MiniLM-L6-v2
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_file TEXT,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Expression unique key (COALESCE isn't allowed inline in a table
-- constraint): enables upsert on (client_id, context_type, title)
-- where a NULL title is treated as ''.
CREATE UNIQUE INDEX IF NOT EXISTS uq_client_contexts_key
    ON client_contexts (client_id, context_type, COALESCE(title, ''));

COMMENT ON TABLE client_contexts IS
    'Per-client brand context docs (brand voice, ICP, journey maps) for the LLM content pipeline. One whole markdown doc per row. Embeddings: local all-MiniLM-L6-v2 (384-dim).';

CREATE INDEX IF NOT EXISTS idx_client_contexts_client
    ON client_contexts (client_id, context_type);

-- Metadata filtering (e.g. metadata @> '{"lang": "en"}')
CREATE INDEX IF NOT EXISTS idx_client_contexts_metadata
    ON client_contexts USING gin (metadata);

-- Semantic search. HNSW over cosine distance.
CREATE INDEX IF NOT EXISTS idx_client_contexts_embedding
    ON client_contexts USING hnsw (embedding vector_cosine_ops);

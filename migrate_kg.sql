-- Knowledge-graph layer: purely additive. Does not touch clients,
-- client_contexts, or any visibility-tracking table.
--
-- kg_nodes mirrors existing entities (clients, contexts, campaigns, ...)
-- via (entity_type, entity_key) — the source row remains the source of
-- truth; the graph is an index over relationships.
-- kg_edges holds asserted (inferred=false) and computed (inferred=true)
-- directed edges between nodes.

CREATE TABLE IF NOT EXISTS kg_nodes (
    id          BIGSERIAL PRIMARY KEY,
    entity_type TEXT NOT NULL,          -- 'client' | 'context' | 'campaign' | 'case_study' | 'topic' | 'custom'
    entity_key  TEXT NOT NULL,          -- stable key, e.g. 'client:outdoor-vitals', 'context:1'
    label       TEXT NOT NULL,
    props       JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (entity_type, entity_key)
);

CREATE TABLE IF NOT EXISTS kg_edges (
    id         BIGSERIAL PRIMARY KEY,
    src_id     BIGINT NOT NULL REFERENCES kg_nodes(id) ON DELETE CASCADE,
    dst_id     BIGINT NOT NULL REFERENCES kg_nodes(id) ON DELETE CASCADE,
    rel_type   TEXT NOT NULL,           -- 'similar_to' | 'relates_to' | 'mentions' | 'belongs_to' | ...
    weight     REAL NOT NULL DEFAULT 1.0,
    props      JSONB NOT NULL DEFAULT '{}'::jsonb,
    inferred   BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (src_id, dst_id, rel_type)
);

CREATE INDEX IF NOT EXISTS idx_kg_edges_src ON kg_edges (src_id, rel_type);
CREATE INDEX IF NOT EXISTS idx_kg_edges_dst ON kg_edges (dst_id, rel_type);
CREATE INDEX IF NOT EXISTS idx_kg_nodes_type ON kg_nodes (entity_type);
CREATE INDEX IF NOT EXISTS idx_kg_nodes_props ON kg_nodes USING gin (props);

COMMENT ON TABLE kg_nodes IS
    'Knowledge-graph nodes mirroring existing entities (one source of truth stays in clients/client_contexts/etc). Keyed by (entity_type, entity_key).';
COMMENT ON TABLE kg_edges IS
    'Knowledge-graph edges. inferred=false: asserted by human/tool. inferred=true: computed (e.g. embedding similarity), safe to delete and recompute.';

-- Dimension-change safety (lesson from client_contexts): this migration is
-- idempotent and never alters existing tables, but if kg tables already
-- exist with a different shape they should be reconciled manually.

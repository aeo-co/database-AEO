-- Vector-Graph Hybrid Engine: ontology + performance migration.
-- Purely additive and idempotent — safe to re-run. Never drops or
-- alters existing tables/columns.
--
-- Ontology: 5 core entity types in kg_nodes:
--   client        — agency client (mirrors clients row)
--   product       — a client's product/SKU (key: 'product:<client_slug>:<sku>')
--   search_intent — a user query/intent (key: 'intent:<sha1 of query>')
--   ai_engine     — ChatGPT | Perplexity | Google AI Mode | Gemini | ...
--                    (key: 'engine:chatgpt', etc.)
--   authority_site— external domain cited by an AI engine
--                    (key: 'site:example.com')
--
-- Edge vocabulary (kg_edges.rel_type):
--   has_product        client  -> product
--   product_for_intent product -> search_intent   (from check query_text)
--   checks_intent      engine  -> search_intent    (engine answers this query)
--   cites_site         engine  -> authority_site   (within a query's citations)
--   intent_cites_site  search_intent -> authority_site (denormalized shortcut
--   so multi-hop bottleneck queries stay a single indexed join)
--   has_context        client  -> context
--   targets_topic / belongs_to / similar_to            (pre-existing)

-- ---------------------------------------------------------------------------
-- 1. Ontology enforcement: enumerate the 5 core types, document everything.
-- ---------------------------------------------------------------------------
INSERT INTO kg_nodes (entity_type, entity_key, label, props)
SELECT 'ai_engine', 'engine:' || lower(e), e,
       '{"core": true, "ontology": "aeo-hybrid-v1"}'::jsonb
FROM unnest(ARRAY['ChatGPT', 'Perplexity', 'Google AI Mode', 'Gemini']) AS e
ON CONFLICT (entity_type, entity_key) DO NOTHING;

COMMENT ON COLUMN kg_nodes.entity_type IS
    'Ontology (aeo-hybrid-v1): client | product | search_intent | ai_engine | authority_site (+ legacy: context | topic | campaign | case_study | custom)';
COMMENT ON COLUMN kg_edges.rel_type IS
    'Ontology (aeo-hybrid-v1): has_product | product_for_intent | checks_intent | cites_site | intent_cites_site | has_context | targets_topic | belongs_to | similar_to | mentions | relates_to';

-- ---------------------------------------------------------------------------
-- 2. Performance: B-tree indexes on the traversal hot paths.
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_kg_nodes_type ON kg_nodes (entity_type);
CREATE INDEX IF NOT EXISTS idx_kg_edges_rel_type ON kg_edges (rel_type);
CREATE INDEX IF NOT EXISTS idx_kg_edges_src ON kg_edges (src_id, rel_type);
CREATE INDEX IF NOT EXISTS idx_kg_edges_dst ON kg_edges (dst_id, rel_type);
-- Partial covering index for the citation-heavy bottleneck query:
CREATE INDEX IF NOT EXISTS idx_kg_edges_intent_cites
    ON kg_edges (src_id, dst_id) WHERE rel_type = 'intent_cites_site';
CREATE INDEX IF NOT EXISTS idx_kg_edges_product_intent
    ON kg_edges (src_id, dst_id) WHERE rel_type = 'product_for_intent';

-- client_contexts: vector search + per-client scans
CREATE INDEX IF NOT EXISTS idx_client_contexts_client
    ON client_contexts (client_id, context_type);
CREATE INDEX IF NOT EXISTS idx_client_contexts_embedding
    ON client_contexts USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- ai_visibility_checks: the extraction pass scans per client
CREATE INDEX IF NOT EXISTS idx_avc_client ON ai_visibility_checks (client_id);

ANALYZE kg_nodes;
ANALYZE kg_edges;

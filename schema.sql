-- Smart Marketer client data hub
-- One row per client per week per data source, with fixed columns for
-- the well-known/common metrics and a JSONB overflow column for anything
-- source-specific or new that shows up later without needing a migration.

CREATE TABLE IF NOT EXISTS clients (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    slug TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ga4_weekly (
    id SERIAL PRIMARY KEY,
    client_id INT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    week_start DATE NOT NULL,
    week_end DATE NOT NULL,
    sessions INT,
    users INT,
    conversions INT,
    bounce_rate NUMERIC(5,2),
    metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (client_id, week_start)
);

CREATE TABLE IF NOT EXISTS gsc_weekly (
    id SERIAL PRIMARY KEY,
    client_id INT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    week_start DATE NOT NULL,
    week_end DATE NOT NULL,
    clicks INT,
    impressions INT,
    ctr NUMERIC(5,2),
    avg_position NUMERIC(5,2),
    metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (client_id, week_start)
);

CREATE TABLE IF NOT EXISTS shopify_weekly (
    id SERIAL PRIMARY KEY,
    client_id INT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    week_start DATE NOT NULL,
    week_end DATE NOT NULL,
    orders INT,
    revenue NUMERIC(12,2),
    aov NUMERIC(10,2),
    metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (client_id, week_start)
);

-- One row per (client, platform, date, query) - matches how the AI
-- visibility tool actually exports data: a query tested against one AI
-- platform on one date, not a weekly aggregate. See ingest_ai_visibility.py.
CREATE TABLE IF NOT EXISTS ai_visibility_checks (
    id SERIAL PRIMARY KEY,
    client_id INT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    platform TEXT NOT NULL,
    check_date DATE NOT NULL,
    query_text TEXT NOT NULL,
    query_hash TEXT NOT NULL,
    raw_output TEXT,
    urls JSONB NOT NULL DEFAULT '[]'::jsonb,
    mentions JSONB NOT NULL DEFAULT '[]'::jsonb,
    visibility_score NUMERIC,
    total_brands INT,
    brand_position NUMERIC,
    competitor_analysis TEXT,
    sources JSONB NOT NULL DEFAULT '[]'::jsonb,
    related_queries JSONB NOT NULL DEFAULT '[]'::jsonb,
    source_file TEXT,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (client_id, platform, check_date, query_hash)
);

CREATE TABLE IF NOT EXISTS aeo_content_log (
    id SERIAL PRIMARY KEY,
    client_id INT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    platform TEXT NOT NULL CHECK (platform IN ('reddit', 'youtube', 'blog')),
    url TEXT,
    title TEXT,
    published_at DATE,
    metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The "-all-data.csv" report exports. These are NOT uniform across
-- clients - each file has its own set of "=== Section Name ===" blocks,
-- and even a section that exists for every client (e.g. Gsc Monthly) can
-- have different columns from one client to the next. So this stores
-- each section generically: whatever columns and rows it actually has,
-- as plain text, no type coercion. See ingest_shopify_reports.py.
CREATE TABLE IF NOT EXISTS shopify_report_sections (
    id SERIAL PRIMARY KEY,
    client_id INT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    section_name TEXT NOT NULL,
    columns JSONB NOT NULL,
    rows JSONB NOT NULL,
    source_file TEXT,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (client_id, section_name)
);

CREATE INDEX IF NOT EXISTS idx_ga4_client_week ON ga4_weekly (client_id, week_start);
CREATE INDEX IF NOT EXISTS idx_gsc_client_week ON gsc_weekly (client_id, week_start);
CREATE INDEX IF NOT EXISTS idx_shopify_client_week ON shopify_weekly (client_id, week_start);
CREATE INDEX IF NOT EXISTS idx_ai_visibility_client_date ON ai_visibility_checks (client_id, check_date);
CREATE INDEX IF NOT EXISTS idx_ai_visibility_platform ON ai_visibility_checks (platform);
CREATE INDEX IF NOT EXISTS idx_aeo_client_platform ON aeo_content_log (client_id, platform);
CREATE INDEX IF NOT EXISTS idx_shopify_sections_client ON shopify_report_sections (client_id);

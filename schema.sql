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

-- Raw comment/post research data pulled from Reddit and YouTube export
-- tools (see ingest_reddit_csv.py / ingest_youtube_csv.py). One row per
-- source (a Reddit post or YouTube video) in research_sources, one row
-- per comment in research_comments - plus, for Reddit, a synthetic
-- "post body" comment row so the post's own question and its top-level
-- answers form one connected thread (see ingest_reddit_csv.py's
-- docstring). Raw storage only - no question/answer extraction on top
-- of this yet.
CREATE TABLE IF NOT EXISTS research_sources (
    id SERIAL PRIMARY KEY,
    client_id INT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    platform TEXT NOT NULL CHECK (platform IN ('reddit', 'youtube')),
    external_id TEXT NOT NULL,          -- Reddit post_id / YouTube video_id
    url TEXT,
    title TEXT,
    container TEXT,                     -- Reddit subreddit; unused by YouTube
    published_at TIMESTAMPTZ,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (platform, external_id)
);

CREATE TABLE IF NOT EXISTS research_comments (
    id SERIAL PRIMARY KEY,
    source_id INT NOT NULL REFERENCES research_sources(id) ON DELETE CASCADE,
    client_id INT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    platform TEXT NOT NULL CHECK (platform IN ('reddit', 'youtube')),
    external_id TEXT NOT NULL,          -- comment_id, Reddit's/YouTube's own
    parent_external_id TEXT,            -- another comment's external_id, or
                                         -- the post's external_id for a Reddit
                                         -- top-level reply. No FK on purpose -
                                         -- a plain, soft reference, since a
                                         -- source file isn't guaranteed to list
                                         -- a parent comment before its child.
    author TEXT,
    author_id TEXT,
    body TEXT NOT NULL,
    score INT,
    reply_count INT,                    -- YouTube only
    posted_at TIMESTAMPTZ,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    raw JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (platform, external_id)
);

-- LLM extraction pass over research_comments (see analyze_comments.py):
-- real questions pulled out of the noise, sentiment, and raw topic tags.
-- One row per comment, additive - never touches research_comments itself,
-- same "source stays source of truth" philosophy as kg_nodes/kg_edges.
-- Re-running analyze_comments.py only processes comments with no row
-- here yet, unless told to re-analyze.
CREATE TABLE IF NOT EXISTS research_comment_analysis (
    id SERIAL PRIMARY KEY,
    comment_id INT NOT NULL REFERENCES research_comments(id) ON DELETE CASCADE,
    client_id INT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    is_question BOOLEAN NOT NULL DEFAULT false,
    question_text TEXT,                 -- cleaned-up question, NULL if not a question
    sentiment TEXT CHECK (sentiment IN ('positive', 'negative', 'neutral', 'mixed')),
    sentiment_target TEXT,              -- 'client' | a named competitor | 'general' | NULL
    topics JSONB NOT NULL DEFAULT '[]'::jsonb,
    model TEXT NOT NULL,                -- which model produced this row
    analyzed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (comment_id)
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
    report_period TEXT,                 -- NULL = monthly, else e.g. 'August 1 - August 7, 2026'
    columns JSONB NOT NULL,
    rows JSONB NOT NULL,
    source_file TEXT,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Expression unique keys aren't allowed in a table-level UNIQUE(...)
-- constraint - only CREATE UNIQUE INDEX supports them (see
-- migrate_report_period.sql, which added this to the live DB by hand
-- before this was folded into schema.sql).
CREATE UNIQUE INDEX IF NOT EXISTS shopify_report_sections_client_section_period_key
    ON shopify_report_sections (client_id, section_name, COALESCE(report_period, ''));

CREATE INDEX IF NOT EXISTS idx_ga4_client_week ON ga4_weekly (client_id, week_start);
CREATE INDEX IF NOT EXISTS idx_gsc_client_week ON gsc_weekly (client_id, week_start);
CREATE INDEX IF NOT EXISTS idx_shopify_client_week ON shopify_weekly (client_id, week_start);
CREATE INDEX IF NOT EXISTS idx_ai_visibility_client_date ON ai_visibility_checks (client_id, check_date);
CREATE INDEX IF NOT EXISTS idx_ai_visibility_platform ON ai_visibility_checks (platform);
CREATE INDEX IF NOT EXISTS idx_aeo_client_platform ON aeo_content_log (client_id, platform);
CREATE INDEX IF NOT EXISTS idx_shopify_sections_client ON shopify_report_sections (client_id);
CREATE INDEX IF NOT EXISTS idx_research_sources_client ON research_sources (client_id);
CREATE INDEX IF NOT EXISTS idx_research_comments_client ON research_comments (client_id);
CREATE INDEX IF NOT EXISTS idx_research_comments_source ON research_comments (source_id);
CREATE INDEX IF NOT EXISTS idx_research_comments_parent ON research_comments (platform, parent_external_id);
CREATE INDEX IF NOT EXISTS idx_comment_analysis_client ON research_comment_analysis (client_id);
CREATE INDEX IF NOT EXISTS idx_comment_analysis_questions ON research_comment_analysis (client_id) WHERE is_question;

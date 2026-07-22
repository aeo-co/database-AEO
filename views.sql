
-- 1. Drop the "always join clients" from every read.
CREATE OR REPLACE VIEW v_ai_visibility AS
SELECT
    c.slug                                    AS client,
    a.platform,
    a.check_date,
    LEFT(a.query_text, 60)                    AS query_preview,
    a.query_text,
    ROUND(a.visibility_score::numeric, 2)     AS visibility_score,
    a.brand_position,
    a.total_brands,
    jsonb_array_length(a.mentions)            AS mention_count,
    a.competitor_analysis
FROM ai_visibility_checks a
JOIN clients c ON c.id = a.client_id;

-- 2. Per-platform rollup that backs the dashboard cards. One query.
CREATE OR REPLACE VIEW v_ai_visibility_summary AS
SELECT
    c.slug AS client,
    a.platform,
    COUNT(*)                                  AS queries_tested,
    ROUND(AVG(a.visibility_score)::numeric, 1) AS avg_visibility,
    ROUND(AVG(a.brand_position)::numeric, 1)  AS avg_brand_position,
    MAX(a.check_date)                         AS last_checked
FROM ai_visibility_checks a
JOIN clients c ON c.id = a.client_id
GROUP BY c.slug, a.platform;

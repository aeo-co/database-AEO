-- Apply to the live DigitalOcean Postgres DB (paste in DO console / psql).
-- Adds weekly-report support: monthly sections keep report_period NULL,
-- weekly sections store their period so they never collide with monthly.
ALTER TABLE shopify_report_sections ADD COLUMN IF NOT EXISTS report_period TEXT;
DROP INDEX IF EXISTS shopify_report_sections_client_id_section_name_key;
ALTER TABLE shopify_report_sections DROP CONSTRAINT IF EXISTS shopify_report_sections_client_id_section_name_key;
CREATE UNIQUE INDEX IF NOT EXISTS shopify_report_sections_client_section_period_key
    ON shopify_report_sections (client_id, section_name, COALESCE(report_period, ''));
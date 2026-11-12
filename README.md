# Smart Marketer Data Hub

One Postgres database holding per-client data from every source we track
(AI visibility, Shopify, GA4, Reddit/YouTube research comments, brand
context), plus three ways to read it:

- a **web dashboard** (`dashboard.py`, port 8000) for the team,
- an **MCP server** (`mcp_server.py`, port 8765) for agents - Echo (the
  Slack agent), Hermes, Claude Desktop/Code,
- **CLI scripts** for loading data and one-off lookups.

Both servers run on the DigitalOcean droplet from `/root/database-AEO`.

## Layout

| File | What it does |
|---|---|
| `db.py` | The one place every DB connection comes from. Reads `.env`. Rows come back as dicts. |
| `schema.sql` / `migrate.py` | Core schema; `migrate.py` applies it. Safe to re-run. |
| `migrate_client_contexts.sql`, `migrate_kg.sql`, `migrate_hybrid_v1.sql` | Additive migrations for brand context (pgvector), the knowledge graph, and the graph ontology. Apply with `psql` after `migrate.py` on a fresh DB. |
| `migrate_report_period.sql` | Historical hand-applied migration; already folded into `schema.sql`. |
| `dashboard.py` | FastAPI app: JSON API + serves `index.html`, `reports.html`, `upload.html`. Also the n8n ingest endpoint. |
| `mcp_server.py` | MCP server (Streamable HTTP, or stdio with `--stdio`). |
| `ingest_*.py` | Loaders, one per source (see below). |
| `client_context.py` | Shared embed + upsert helpers for brand-context docs. |
| `kg.py`, `triplet_extraction.py`, `insights.py` | Knowledge graph: node backfill, similarity edges, triplet extraction, bottleneck/brief tools. |
| `analyze_comments.py`, `comment_insights.py` | LLM pass over research comments, then topic clustering + content-gap finder. |
| `client_report.py` | Terminal lookup: AI visibility summary + Shopify report for one client. |
| `clients_snapshot.json` | Snapshot of the current client list. |
| `desc_kg.py` | Prints the live `kg_*` table schema (debug helper). |
| `deploy/` | systemd unit for the dashboard + install notes. |
| `vendor/` | Chart.js, served locally to the dashboard. |

## Setup

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

`.env` (never committed):

| Var | Purpose |
|---|---|
| `DATABASE_URL` | Postgres connection string (or set `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`) |
| `UPLOAD_PASSPHRASE` | Required on every write path (uploads, n8n ingest, MCP ingest tools) when set |
| `OPENAI_API_KEY` | `analyze_comments.py` |
| `OPENROUTER_API_KEY` | Set in `.env`, but not read by any code in this repo |

Fresh database: `python migrate.py`, then apply the three additive
`migrate_*.sql` files with `psql` (pgvector must be available).

## Data model

| Table | Holds | Loaded by |
|---|---|---|
| `clients` | One row per client (name, slug, manager, seo_manager) | auto-registered by every loader |
| `client_aliases` | Alternate names that route to an existing client | by hand |
| `removed_clients` | Clients that must never be re-registered | by hand (see "Removing a client") |
| `ai_visibility_checks` | One row per client / platform / date / query: score, position, mentions, sources, raw output, `top_brand_attributes` | `ingest_ai_visibility.py`, upload box, n8n |
| `shopify_report_sections` | Sections of `{client}-all-data.csv` reports, stored as-is (columns vary per client); `report_period` NULL = monthly | `ingest_shopify_reports.py`, upload box |
| `shopify_weekly` | Shopify AI-channel sales (ChatGPT agentic sales etc.) | `ingest_shopify_ai.py` |
| `ga4_weekly` | GA4 sessions + AI-referral breakdown per period (weekly or monthly, keyed on the full window; `metrics.period_kind`) | `ingest_ga4_weekly.py` |
| `gsc_weekly` | Search Console clicks / impressions / CTR / position per period (weekly or monthly, keyed on the full window); breakdowns (branded vs non-branded, country, device, page, queries, AI features) in `metrics`. `metrics.internal_only` holds concern queries - never client-facing | `ingest_gsc_weekly.py` |
| `research_sources` / `research_comments` | YouTube videos / Reddit threads and their comments | `ingest_reddit_csv.py`, `ingest_youtube_csv.py`, upload box, n8n |
| `research_comment_analysis` | LLM-extracted questions, sentiment, topic tags per comment | `analyze_comments.py` |
| `client_contexts` | Whole brand-context markdown docs + 384-dim embeddings | `ingest_client_context.py`, MCP `ingest_client_context` |
| `kg_nodes` / `kg_edges` | Knowledge graph over the above | `kg.py`, `triplet_extraction.py`, MCP tools |
| `website_categorisation` | Domain -> category lookup | external load |
| `v_ai_visibility`, `v_ai_visibility_summary` (views) | Readable per-query rows / per-platform rollup for ad-hoc SQL; no app code uses them | `schema.sql` |
| `aeo_content_log` | In the schema, **no loader yet - empty** | - |

Every client foreign key is `ON DELETE CASCADE`. Re-running any loader
on the same file updates rows rather than duplicating them.

## Loading data

```bash
python ingest_ai_visibility.py /path/to/folder        # {Client}_-_{Platform}_-_{YYYY-MM-DD}.xlsx
python ingest_shopify_reports.py /path/to/folder      # {client}-all-data.csv
python ingest_reddit_csv.py /path/to/folder           # {client}-reddit-comments.csv
python ingest_youtube_csv.py /path/to/folder          # {client}-youtube-comments.csv
python ingest_ga4_weekly.py [files...]                # default: monthly-pulls/ + weekly-pulls/
python ingest_ga4_weekly.py --from-db                 # rebuild from ga4 sections in shopify_report_sections
python ingest_shopify_ai.py [files...]                # same long-row CSV format as GA4
python ingest_gsc_weekly.py [files...]                # gsc_* rows from the same pull CSVs
python ingest_gsc_weekly.py --from-db                 # backfill from gsc_* sections in shopify_report_sections
python ingest_client_context.py doc.md --client <slug|name|id> --type brand_voice
```

- Clients are parsed from the filename and registered automatically.
  Names are matched on a flattened key (lowercase, letters/digits only)
  and through `client_aliases`, so spelling variants collapse to one
  client. A squashed filename (`outdoorvitals`) gives a squashed name -
  fix by hand: `UPDATE clients SET name = 'OutdoorVitals' WHERE slug = 'outdoorvitals';`
- Unknown AI platforms get an auto-generated name instead of failing.
- Files whose columns don't match are skipped with a message naming
  what's missing.

**Without Python:** the dashboard's upload page takes `.xlsx` (AI
visibility) and the three CSV shapes above, dispatched by filename.

**From n8n:** `POST /api/ingest/ai-visibility-row`,
`/api/ingest/reddit-comment`, `/api/ingest/youtube-comment` take one row
each as JSON (with `passphrase`).

**Analysis passes** (re-runnable, only process what's new):

```bash
python analyze_comments.py --client outdoor-vitals [--limit 50] [--reanalyze]
python comment_insights.py topics --client outdoor-vitals
python comment_insights.py gaps --client outdoor-vitals
python kg.py backfill [--dry-run]
python kg.py refresh-similar [--threshold 0.85]
```

## Web dashboard

```bash
python dashboard.py      # http://localhost:8000
```

Pages: `/` (per-client dashboard), `/reports.html` (weekly reports),
`/upload.html` (upload). Only these three files and `/vendor` are
served - never the repo directory.

Read endpoints (all take `?client=<slug>`):
`/api/clients`, `/api/summary`, `/api/queries`, `/api/mentions`,
`/api/top-sources`, `/api/shopify-report`, `/api/report-weeks`,
`/api/trend`, `/api/research-sources`, `/api/research-comments`,
`/api/research-stats`, and `/reports/{client_slug}/{date}.json`.
Feed endpoints for the live-data banner: `/api/recent-uploads`,
`/api/latest-ingest`. Write endpoints: `POST /api/upload` plus the three
n8n endpoints above.

On the droplet it runs as `aeo-dashboard.service` (see `deploy/`).

## MCP server

```bash
python mcp_server.py           # Streamable HTTP on 0.0.0.0:8765
python mcp_server.py --stdio   # for Claude Desktop (or MCP_TRANSPORT=stdio)
```

On the droplet it runs as `aeo-mcp.service`. Most tools that take a
`client` accept an id, slug or name fragment.

**Clients & profiles:** `list_clients`, `get_client_profile` (everything
about one client in one call).

**AI visibility:** `get_ai_visibility_summary`, `get_ai_visibility_queries`,
`get_top_mentions`, `get_weekly_ai_visibility_report`,
`get_monthly_ai_visibility_report`, `get_all_clients_weekly_ai_visibility`
(all clients, latest week - the Slack batch report).

**Shopify & GA4:** `get_shopify_report`, `get_weekly_shopify_report`,
`get_monthly_shopify_report`, `get_ga4_summary`.

**Search Console:** `get_gsc_summary` (clicks, impressions, CTR, position,
branded vs non-branded, AI-features impressions per period; optional
breakdowns; concern queries only with `include_internal=True`).

**Brand context:** `get_client_context`, `ingest_client_context`.

**Knowledge graph & insights:** `get_related`, `find_similar_clients`,
`refresh_similar_clients`, `add_edge`, `build_visibility_graph`,
`get_visibility_bottlenecks`, `generate_content_brief`.

**Ingest:** `ingest_file` (AI visibility .xlsx), `ingest_shopify_file`.

Write tools check `UPLOAD_PASSPHRASE`. The tools return data only - any
"is this trending up?" judgement is left to the calling agent.

## Terminal lookup

```bash
python client_report.py rootganic
python client_report.py "outdoor vitals"   # name match, spacing ignored
```

## Removing a client

Loaders auto-register any client name they see, so deleting the row
alone isn't enough - the next n8n run or upload brings it back:

1. Back up its rows into `<table>_backup_<reason>` tables (below).
2. `DELETE FROM clients WHERE id = ...` - cascades to all its data.
3. Add its flattened name (e.g. `livingwellwithdrmichelle`) to
   `removed_clients`. Every ingest path then refuses it (n8n gets a 400,
   files are skipped with a reason). Delete that row to allow it back.

## Backup tables (`*_backup_*`)

The live DB holds snapshot tables named `<table>_backup_<what happened>`,
e.g. `clients_backup_merge89` or
`ai_visibility_checks_backup_removed_babyrx_livingwell_20261111`. Each is
a copy taken right before a merge (`merge<id>`), removal
(`removed_<client or id>`) or delete (`del<id>`), so it can be undone -
don't drop them. No code reads them and they're not in `schema.sql`.
Older ones still have the pre-rename `competitor_analysis` column.

## Known gaps

- **Read access is open.** Dashboard and MCP read endpoints have no auth;
  only writes check `UPLOAD_PASSPHRASE`. Keep the ports behind a firewall
  or add auth before exposing them.
- **No live API pulls** from GA4 / GSC / Shopify - data arrives as
  exported files or via n8n.
- **The AEO content log** has a table but no loader.
- **No dashboard panel reads `gsc_weekly` yet** (the MCP tool does).

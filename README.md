# Smart Marketer Data Hub

Single Postgres database that holds weekly per-client data from every
source in the data inventory (GA4, GSC, Shopify, AI visibility, AEO
content), so reporting and agent analysis both read from one place
instead of scattered manual pulls.

## What's here

- `schema.sql` - `clients`, weekly tables per source (`ga4_weekly`,
  `gsc_weekly`, `shopify_weekly`), `aeo_content_log` for the Reddit/YouTube/
  blog content log, `ai_visibility_checks` for the AI visibility tool
  data, and `shopify_report_sections` for the unsection-shaped
  `-all-data.csv` reports.
- `db.py` - the one place every DB connection comes from. Reads
  connection details from `.env`.
- `migrate.py` - applies `schema.sql`. Safe to re-run.
- `seed.py` - inserts two example clients so foreign keys have something
  to point at while testing.
- `ingest_example.py` - the write pattern, worked example using GA4-shaped
  data.
- `query_example.py` - the read pattern: a cross-source trend query
  joining GA4 + Shopify by client and week, the kind of thing an agent
  would run.
- `ingest_ai_visibility.py` - dynamic loader for the AI visibility tool's
  three-files-per-client-per-week exports (see "AI visibility tool data"
  below).
- `ingest_shopify_reports.py` - loads the unsection-shaped `-all-data.csv`
  reports into `shopify_report_sections` (see "Client report CSVs" below).
- `dashboard.py` - FastAPI web dashboard (see "Web dashboard" below).
- `client_report.py` - terminal lookup that prints both reports for one
  client straight to the terminal (see "Terminal lookup" below).
- `mcp_server.py` - MCP server exposing read-only tools over the same
  data so agents can pull it without raw DB access (see "MCP server"
  below).
- `static/index.html` - the dashboard frontend (a single page that
  fetches from `/api/*` and draws charts).

## Scope of what's actually in this repo

This covers the piece that's explicitly yours: **the database and the
connection layer.** It does not include live API pulls from GA4 / GSC /
Shopify / any AI visibility tool - those need real API credentials you
don't have wired up yet, and ownership of some of them (GA4/GSC/Shopify/AEO)
isn't assigned to anyone yet either. What's here is the foundation everyone
else's ingestion code plugs into.

`ingest_example.py` is a **template**, not a finished integration - it shows
the insert pattern (a "week of numbers for a client" upsert, keyed so
re-running it doesn't create duplicates) using made-up GA4-shaped numbers.
The AI visibility tool side is already handled for real (see below).
Zaryab's merge scripts for website categorization / topical relevance
should follow the same upsert shape, into a new table if what he's
merging doesn't fit the existing ones.

## Running it

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env        # if .env.example isn't checked in, just create .env with the DB_* vars below
# edit .env: either set DATABASE_URL, or fill in the DB_* values
# (DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD)

python migrate.py
python seed.py
python ingest_example.py
python query_example.py
```

All of this has been run end-to-end against a live Postgres instance
during testing, so the SQL and the Python are both confirmed working, not
just written and hoped for.

## AI visibility tool data

`ingest_ai_visibility.py` is a dynamic loader for the AI visibility tool's
exports (the three-files-per-client-per-week pattern: one file per platform
- AI Mode, GPT, Perplexity - named `{Client}_-_{Platform}_-_{YYYY-MM-DD}.xlsx`).

Drop any client's files into a folder and run:

```bash
python ingest_ai_visibility.py /path/to/folder
```

- New clients are registered automatically from the filename - nothing to
  set up per client first.
- New platforms fall back to a reasonable auto-generated name if they're
  not already known, so a tool update that adds a 4th platform doesn't
  break this.
- Re-running the same files updates rather than duplicates (same client +
  platform + date + query = same row).
- If a file's column layout doesn't match what's expected, it's skipped
  with a clear message naming what's missing, rather than silently loading
  bad data.

`query_ai_visibility_example.py` shows the payoff - average visibility
score and brand position per platform, for one client:

```bash
python query_ai_visibility_example.py altenew
```

## Client report CSVs ("-all-data.csv")

These aren't uniform, so don't expect a clean fixed schema here. Each
file has its own "=== Section Name ===" blocks, and even a section every
client has (Gsc Monthly) has different columns from one client to the
next - `ingest_shopify_reports.py` handles that by storing each section
as whatever columns/rows it actually has, tagged by client and section
name, rather than forcing one shape onto data that doesn't have one.

```bash
python ingest_shopify_reports.py /path/to/folder
```

New clients register automatically from the filename
(`{client}-all-data.csv`). One real limitation: if the filename has no
separator between words (`outdoorvitals`, not `outdoor-vitals`), the
client name gets stored as one squashed word ("Outdoorvitals") since
there's no way to know the intended capitalization from a flattened
lowercase string. Fix it by hand if the real name differs:
```sql
UPDATE clients SET name = 'OutdoorVitals' WHERE slug = 'outdoorvitals';
```

## Web dashboard

`dashboard.py` serves an actual browser dashboard - not just a script
printing to a terminal. It shows, per client: a score card per platform
(with a ring indicator), a bar chart comparing platforms, a panel of the
top brands mentioned alongside this client, and a table of every query
tested, expandable to see the full raw AI response and competitor
analysis for that row.

```bash
python dashboard.py
```

Then open `http://localhost:8000`. Pick a client from the dropdown in the
top right - everything below updates to that client's data.

It's a small FastAPI app. Five JSON endpoints back a single static HTML
page (`static/index.html`) that fetches from them and draws the chart
with Chart.js:

- `GET /api/clients` - every client (id, name, slug) for the dropdown.
- `GET /api/summary?client=<slug>` - per-platform rollup (queries tested,
  average visibility score, average brand position).
- `GET /api/queries?client=<slug>` - every query row for that client
  (platform, date, score, position, mentions, raw output, competitor
  analysis).
- `GET /api/mentions?client=<slug>&limit=6` - top non-self brands
  mentioned across this client's AI responses, by count.
- `POST /api/upload` - drag-and-drop upload of one or more AI visibility
  export files (see "Adding clients without Python" below).

Nothing here is specific to Altenew - add more clients via
`ingest_ai_visibility.py` (or the upload box) and they show up in the
dropdown automatically.

This runs fine locally for now. Moving it to the VPS is the same command,
just run there instead - though worth adding some form of access control
before it's reachable on the open internet, since this is client data.
That's not set up yet; flag it if/when this moves off your machine.

### Adding clients without Python (for teammates)

The dashboard has a "+ Add client" button in the top right. Click it,
drag the client's `.xlsx` files into the box that appears, and click
Upload - no terminal, no Python, nothing installed on their end beyond a
browser. Each file gets checked and loaded the same way
`ingest_ai_visibility.py` does it (it's the same code underneath), and
the result shows per file - a green line for anything that loaded, a red
line explaining why if something didn't match. The client picker updates
automatically once it's done.

Before this is live on the VPS for the team to use, set `UPLOAD_PASSPHRASE`
in `.env` to some shared phrase your team knows - otherwise anyone with
the URL can upload data, not just your team. Leave it unset for local
testing; the upload box works without a passphrase until one is set.

## MCP server

`mcp_server.py` is an MCP server that exposes read-only tools over the
same data, so agents (Hermes, Claude Desktop, Claude Code, etc.) can
pull reports without needing direct Postgres credentials on the host.
Run it on whatever host sits next to the DB:

```bash
python mcp_server.py          # 0.0.0.0:8765 by default
```

Point an MCP client at the URL (Streamable HTTP transport) and it gets
five tools, all JSON in, JSON out:

- `list_clients()` - same as `GET /api/clients`.
- `get_ai_visibility_summary(client)` - same per-platform rollup as the
  dashboard.
- `get_ai_visibility_queries(client, limit=200, platform=None,
  date_from=None, date_to=None)` - the per-query rows, optionally
  filtered.
- `get_top_mentions(client, limit=10)` - same as the dashboard's
  mentions panel.
- `get_shopify_report(client)` - every section of that client's
  `-all-data.csv` report.

All five are read-only - ingestion happens through the existing scripts
or the dashboard upload box, not through MCP. The agent (reasoning LLM)
is responsible for any "is this trending up?" verdict - no thresholds are
hard-coded in the server itself.

## Terminal lookup (no web UI)

`client_report.py` prints both reports for one client straight to the
terminal - the AI visibility summary and the full shopify report, every
section, in one go:

```bash
python client_report.py rootganic
python client_report.py "outdoor vitals"   # matches by name, spacing doesn't matter
```

## Moving this to the actual VPS

Nothing above changes - point `.env` at wherever Postgres actually lives:

- **Self-installed on the Droplet:** `apt install postgresql`, create a
  dedicated role + database for this (don't run the app as the default
  `postgres` superuser), fill in the `DB_*` vars.
- **DigitalOcean managed Postgres:** copy the connection string it gives
  you into `DATABASE_URL` and skip the `DB_*` vars entirely.

Either way, run `python migrate.py` once against that database and you're
live. If you also expose `mcp_server.py` and `dashboard.py` on the VPS,
front both with whatever auth your team uses - same caveat as the
dashboard: this is client data, don't make it public.

## Not built yet, on purpose

- No live pulls from GA4 / GSC / Shopify. That needs real API credentials,
  and ownership of those sources isn't assigned to anyone yet either.
  AI visibility is the one source that's fully wired up, since real
  exports were available to build against.

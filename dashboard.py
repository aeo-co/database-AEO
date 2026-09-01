import os
import shutil
import tempfile
from collections import Counter
from datetime import date as _date, datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles

from db import get_conn
from ingest_ai_visibility import ingest_file as ingest_ai_file
from ingest_shopify_reports import ingest_file as ingest_shopify_file

app = FastAPI(title="Smart Marketer Data Hub")

# Optional: set UPLOAD_PASSPHRASE in .env before this is reachable on the
# open internet, so uploading isn't wide open to anyone with the URL. If
# it's left unset, uploads work with no passphrase - fine for local use.
UPLOAD_PASSPHRASE = os.getenv("UPLOAD_PASSPHRASE")


def _num(val):
    """Decimal -> float, None stays None, so responses are plain JSON."""
    return float(val) if val is not None else None


@app.get("/api/clients")
def list_clients():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, slug FROM clients ORDER BY name;")
            return cur.fetchall()


@app.get("/api/summary")
def platform_summary(client: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    v.platform,
                    count(*) AS queries_tested,
                    avg(v.visibility_score) AS avg_visibility_score,
                    avg(v.brand_position) AS avg_brand_position,
                    avg(v.total_brands) AS avg_total_brands,
                    100.0 * count(*) FILTER (WHERE v.brand_position IS NOT NULL) / count(*) AS presence_rate
                FROM ai_visibility_checks v
                JOIN clients c ON c.id = v.client_id
                WHERE c.slug = %(slug)s
                GROUP BY v.platform
                ORDER BY v.platform;
                """,
                {"slug": client},
            )
            rows = cur.fetchall()
    for r in rows:
        r["avg_visibility_score"] = round(_num(r["avg_visibility_score"]), 1) if r["avg_visibility_score"] is not None else None
        r["avg_brand_position"] = round(_num(r["avg_brand_position"]), 1) if r["avg_brand_position"] is not None else None
        r["avg_total_brands"] = round(_num(r["avg_total_brands"]), 1) if r["avg_total_brands"] is not None else None
        r["presence_rate"] = round(_num(r["presence_rate"]), 1) if r["presence_rate"] is not None else None
    return rows


@app.get("/api/queries")
def query_detail(client: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    v.platform, v.check_date, v.query_text, v.visibility_score,
                    v.brand_position, v.total_brands, v.mentions,
                    v.raw_output, v.competitor_analysis, v.sources, v.related_queries
                FROM ai_visibility_checks v
                JOIN clients c ON c.id = v.client_id
                WHERE c.slug = %(slug)s
                ORDER BY v.check_date DESC, v.platform;
                """,
                {"slug": client},
            )
            rows = cur.fetchall()
    for r in rows:
        r["check_date"] = r["check_date"].isoformat()
        r["visibility_score"] = round(_num(r["visibility_score"]), 1) if r["visibility_score"] is not None else None
        r["brand_position"] = round(_num(r["brand_position"]), 1) if r["brand_position"] is not None else None
    return rows


@app.get("/api/mentions")
def top_mentions(client: str, limit: int = 6):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM clients WHERE slug = %(slug)s;", {"slug": client})
            client_row = cur.fetchone()
            if not client_row:
                return []
            cur.execute(
                """
                SELECT v.mentions
                FROM ai_visibility_checks v
                JOIN clients c ON c.id = v.client_id
                WHERE c.slug = %(slug)s;
                """,
                {"slug": client},
            )
            rows = cur.fetchall()

    own_name = client_row["name"].strip().lower()
    counts = Counter()
    for r in rows:
        for mention in (r["mentions"] or []):
            name = mention.strip()
            if not name or own_name in name.lower():
                continue
            counts[name] += 1

    return [{"name": name, "count": count} for name, count in counts.most_common(limit)]


def _source_domain(entry) -> str:
    """`sources` entries come in two shapes depending on which platform's
    export produced them: a plain URL string, or a {"url": ..., "type":
    "url"} dict. Normalize either to a bare domain (no 'www.') - ranking
    by exact URL would be nearly meaningless since almost none repeat."""
    url = entry.get("url") if isinstance(entry, dict) else entry
    if not url:
        return ""
    host = urlparse(url.strip()).netloc.lower()
    return host[4:] if host.startswith("www.") else host


@app.get("/api/top-sources")
def top_sources(client: str, limit: int = 8):
    """Domains the AI tool cited most often across every answer for this
    client - where content/PR effort should focus to get cited more."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM clients WHERE slug = %(slug)s;", {"slug": client})
            client_row = cur.fetchone()
            if not client_row:
                return []
            cur.execute(
                "SELECT sources FROM ai_visibility_checks WHERE client_id = %(cid)s;",
                {"cid": client_row["id"]},
            )
            rows = cur.fetchall()

    counts = Counter()
    for r in rows:
        # One count per response that cites the domain, not per link -
        # a response citing reddit.com five times still counts as one.
        domains_in_row = {_source_domain(entry) for entry in (r["sources"] or [])}
        counts.update(d for d in domains_in_row if d)

    return [{"name": name, "count": count} for name, count in counts.most_common(limit)]


@app.get("/api/shopify-report")
def shopify_report(client: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM clients WHERE slug = %(slug)s;", {"slug": client})
            client_row = cur.fetchone()
            if not client_row:
                return []
            cur.execute(
                """
                SELECT section_name, report_period, columns, rows, ingested_at
                FROM shopify_report_sections
                WHERE client_id = %(cid)s
                ORDER BY report_period NULLS FIRST, id;
                """,
                {"cid": client_row["id"]},
            )
            sections = cur.fetchall()
    for s in sections:
        s["ingested_at"] = s["ingested_at"].isoformat() if s["ingested_at"] else None
    return sections


@app.get("/api/report-weeks")
def report_weeks(client: str):
    """Every date this client has an AI-visibility report for, newest
    first - powers the week picker on the weekly-reports page."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT v.check_date
                FROM ai_visibility_checks v
                JOIN clients c ON c.id = v.client_id
                WHERE c.slug = %(slug)s
                ORDER BY v.check_date DESC;
                """,
                {"slug": client},
            )
            rows = cur.fetchall()
    return [r["check_date"].isoformat() for r in rows]


@app.get("/api/trend")
def visibility_trend(client: str):
    """Visibility score per platform per week, oldest first - the trend
    line behind the reports page's 'aggregate across all weeks' view."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    v.check_date,
                    v.platform,
                    avg(v.visibility_score) AS avg_visibility_score,
                    avg(v.brand_position) AS avg_brand_position,
                    count(*) AS queries_tested
                FROM ai_visibility_checks v
                JOIN clients c ON c.id = v.client_id
                WHERE c.slug = %(slug)s
                GROUP BY v.check_date, v.platform
                ORDER BY v.check_date, v.platform;
                """,
                {"slug": client},
            )
            rows = cur.fetchall()
    for r in rows:
        r["check_date"] = r["check_date"].isoformat()
        r["avg_visibility_score"] = round(_num(r["avg_visibility_score"]), 1) if r["avg_visibility_score"] is not None else None
        r["avg_brand_position"] = round(_num(r["avg_brand_position"]), 1) if r["avg_brand_position"] is not None else None
    return rows


@app.get("/reports/{client_slug}/{report_date}.json")
def weekly_report_json(client_slug: str, report_date: str):
    """
    Auto-generated per-week AI visibility report, computed fresh from the
    database on every request - a stable URL anyone on the team can open
    or download directly, no login or upload flow needed. There's nothing
    cached here to go stale: re-ingesting corrected data for this week
    changes what this URL returns immediately.
    """
    try:
        parsed_date = _date.fromisoformat(report_date)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"'{report_date}' is not a YYYY-MM-DD date")

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, slug FROM clients WHERE slug = %(slug)s;", {"slug": client_slug})
            client_row = cur.fetchone()
            if not client_row:
                raise HTTPException(status_code=404, detail=f"no client matching '{client_slug}'")

            cur.execute(
                """
                SELECT platform, count(*) AS queries_tested,
                       avg(visibility_score) AS avg_visibility_score,
                       avg(brand_position) AS avg_brand_position,
                       avg(total_brands) AS avg_total_brands,
                       100.0 * count(*) FILTER (WHERE brand_position IS NOT NULL) / count(*) AS presence_rate
                FROM ai_visibility_checks
                WHERE client_id = %(cid)s AND check_date = %(date)s
                GROUP BY platform ORDER BY platform;
                """,
                {"cid": client_row["id"], "date": parsed_date},
            )
            platforms = cur.fetchall()

            cur.execute(
                """
                SELECT platform, query_text, visibility_score, brand_position,
                       total_brands, mentions, urls, competitor_analysis,
                       raw_output, sources, related_queries
                FROM ai_visibility_checks
                WHERE client_id = %(cid)s AND check_date = %(date)s
                ORDER BY platform, query_text;
                """,
                {"cid": client_row["id"], "date": parsed_date},
            )
            queries = cur.fetchall()

    if not platforms:
        raise HTTPException(status_code=404, detail=f"no report for '{client_slug}' on {report_date}")

    for p in platforms:
        p["avg_visibility_score"] = round(_num(p["avg_visibility_score"]), 1) if p["avg_visibility_score"] is not None else None
        p["avg_brand_position"] = round(_num(p["avg_brand_position"]), 1) if p["avg_brand_position"] is not None else None
        p["avg_total_brands"] = round(_num(p["avg_total_brands"]), 1) if p["avg_total_brands"] is not None else None
        p["presence_rate"] = round(_num(p["presence_rate"]), 1) if p["presence_rate"] is not None else None
    for q in queries:
        q["visibility_score"] = round(_num(q["visibility_score"]), 1) if q["visibility_score"] is not None else None
        q["brand_position"] = round(_num(q["brand_position"]), 1) if q["brand_position"] is not None else None

    return {
        "client": client_row["name"],
        "slug": client_row["slug"],
        "report_date": report_date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "platforms": platforms,
        "queries": queries,
    }


@app.post("/api/upload")
async def upload_files(files: list[UploadFile] = File(...), passphrase: str = Form("")):
    if UPLOAD_PASSPHRASE and passphrase != UPLOAD_PASSPHRASE:
        raise HTTPException(status_code=401, detail="Wrong passphrase.")

    results = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        for f in files:
            # Keep the original filename - it's how both ingest_file()
            # functions identify the client/platform/date from the name.
            # Renaming would break parsing.
            dest = tmp_path / Path(f.filename).name
            with open(dest, "wb") as out:
                shutil.copyfileobj(f.file, out)
            # Dispatch on extension: AI visibility uses .xlsx, shopify
            # reports use .csv (named '{client}-all-data.csv'). Anything
            # else gets skipped with a clear reason.
            ext = dest.suffix.lower()
            if ext == ".xlsx":
                results.append(ingest_ai_file(dest))
            elif ext == ".csv":
                results.append(ingest_shopify_file(dest))
            else:
                results.append({
                    "filename": dest.name,
                    "status": "skipped",
                    "reason": f"unsupported extension '{ext}' (use .xlsx for AI visibility or .csv for shopify reports)",
                })
    return results


# Static frontend - must be mounted last so /api/* routes above take priority.
app.mount("/", StaticFiles(directory=Path(__file__).parent, html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("dashboard:app", host="0.0.0.0", port=8000)

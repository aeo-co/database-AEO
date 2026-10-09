import os
import shutil
import tempfile
from collections import Counter
from datetime import date as _date, datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from db import get_conn
from ingest_ai_visibility import ingest_file as ingest_ai_file, ingest_row as ingest_ai_row
from ingest_reddit_csv import ingest_comment as ingest_reddit_comment, ingest_file as ingest_reddit_file
from ingest_shopify_reports import ingest_file as ingest_shopify_file
from ingest_youtube_csv import ingest_comment as ingest_youtube_comment, ingest_file as ingest_youtube_file

app = FastAPI(title="Smart Marketer Data Hub")

# Optional: set UPLOAD_PASSPHRASE in .env before this is reachable on the
# open internet, so uploading isn't wide open to anyone with the URL. If
# it's left unset, uploads work with no passphrase - fine for local use.
UPLOAD_PASSPHRASE = os.getenv("UPLOAD_PASSPHRASE")


def _num(val):
    """Decimal -> float, None stays None, so responses are plain JSON."""
    return float(val) if val is not None else None


def _mention_name(entry) -> str:
    """`mentions` entries come in two shapes depending on which
    platform's export produced them: a plain string, or a
    {"mention": ..., "position": N} dict (same kind of mixed-shape data
    as `sources` - see _source_domain below)."""
    if isinstance(entry, dict):
        return (entry.get("mention") or "").strip()
    return (entry or "").strip()


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
        r["mentions"] = [n for m in (r["mentions"] or []) if (n := _mention_name(m))]
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
            name = _mention_name(mention)
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
        q["mentions"] = [n for m in (q["mentions"] or []) if (n := _mention_name(m))]

    return {
        "client": client_row["name"],
        "slug": client_row["slug"],
        "report_date": report_date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "platforms": platforms,
        "queries": queries,
    }


class AIVisibilityRow(BaseModel):
    """
    One AI-visibility check, ready to land in the database as soon as an
    automation (n8n, etc.) produces it - no spreadsheet or file in
    between. `urls`/`mentions`/`sources`/`related_queries` accept either
    a real JSON array or a plain comma/newline-separated string, since
    upstream automations often hand over flat text instead of a
    structured list. `visibility_score`/`total_brands`/`brand_position`
    accept a number or a numeric string for the same reason.
    """
    passphrase: str = ""
    client: str
    platform: str
    check_date: _date
    query_text: str
    raw_output: Optional[str] = None
    urls: Any = None
    mentions: Any = None
    visibility_score: Any = None
    total_brands: Any = None
    brand_position: Any = None
    competitor_analysis: Optional[str] = None
    sources: Any = None
    related_queries: Any = None
    source_file: str = "n8n"


@app.post("/api/ingest/ai-visibility-row")
def ingest_ai_visibility_row(row: AIVisibilityRow):
    """
    Direct-row ingestion for automations that already have one AI-
    visibility check in hand (e.g. an n8n workflow, right after it writes
    that same row to its Sheets report) and want it in the database
    immediately, instead of exporting a batch of rows to .xlsx and
    someone re-uploading it by hand. Same upsert key as every other
    ingestion path (client + platform + check_date + query hash), so
    re-sending a corrected row updates it in place rather than
    duplicating it.
    """
    if UPLOAD_PASSPHRASE and row.passphrase != UPLOAD_PASSPHRASE:
        raise HTTPException(status_code=401, detail="Wrong passphrase.")
    try:
        return ingest_ai_row(
            client=row.client,
            platform=row.platform,
            check_date=row.check_date,
            query_text=row.query_text,
            raw_output=row.raw_output,
            urls=row.urls,
            mentions=row.mentions,
            visibility_score=row.visibility_score,
            total_brands=row.total_brands,
            brand_position=row.brand_position,
            competitor_analysis=row.competitor_analysis,
            sources=row.sources,
            related_queries=row.related_queries,
            source_file=row.source_file,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


class RedditCommentRow(BaseModel):
    """
    One Reddit comment, ready to land in the database as soon as an
    automation produces it - same field names as the CSV export, so
    there's no translation layer between what the scraping tool already
    has and what this endpoint expects. The post_* fields are repeated
    on every comment from the same post (exactly like every row of the
    CSV does) - the post upsert is idempotent, so sending them every
    time is harmless. post_created_utc/comment_created_utc are Reddit's
    unix-seconds timestamps, numeric or numeric string.
    """
    passphrase: str = ""
    client: str
    post_id: str
    comment_id: str
    comment_text: str
    post_title: Optional[str] = None
    post_url: Optional[str] = None
    subreddit: Optional[str] = None
    post_author: Optional[str] = None
    post_created_utc: Any = None
    post_score: Any = None
    post_num_comments: Any = None
    post_selftext: Optional[str] = None
    parent_comment_id: Optional[str] = None
    comment_depth: Any = 0
    comment_author: Optional[str] = None
    comment_author_id: Optional[str] = None
    comment_is_op: Any = False
    comment_score: Any = 0
    comment_created_utc: Any = None
    comment_permalink: Optional[str] = None


@app.post("/api/ingest/reddit-comment")
def ingest_reddit_comment_row(row: RedditCommentRow):
    """
    Direct-comment ingestion for automations that already have one
    Reddit comment in hand and want it in the database immediately,
    instead of batching into a CSV for someone to re-upload by hand.
    Same upsert key as the CSV path (platform + comment's own id), so
    re-sending a corrected comment updates it in place.
    """
    if UPLOAD_PASSPHRASE and row.passphrase != UPLOAD_PASSPHRASE:
        raise HTTPException(status_code=401, detail="Wrong passphrase.")
    try:
        return ingest_reddit_comment(
            client=row.client,
            post_id=row.post_id,
            comment_id=row.comment_id,
            comment_text=row.comment_text,
            post_title=row.post_title,
            post_url=row.post_url,
            subreddit=row.subreddit,
            post_author=row.post_author,
            post_created_utc=row.post_created_utc,
            post_score=row.post_score,
            post_num_comments=row.post_num_comments,
            post_selftext=row.post_selftext,
            parent_comment_id=row.parent_comment_id,
            comment_depth=row.comment_depth,
            comment_author=row.comment_author,
            comment_author_id=row.comment_author_id,
            comment_is_op=row.comment_is_op,
            comment_score=row.comment_score,
            comment_created_utc=row.comment_created_utc,
            comment_permalink=row.comment_permalink,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


class YoutubeCommentRow(BaseModel):
    """
    One YouTube comment, ready to land in the database as soon as an
    automation produces it - same field names as the CSV export. The
    video_* fields are repeated on every comment from the same video
    (exactly like every row of the CSV does) - the video upsert is
    idempotent, so sending them every time is harmless.
    """
    passphrase: str = ""
    client: str
    video_id: str
    comment_id: str
    comment_text: str
    video_title: Optional[str] = None
    video_url: Optional[str] = None
    video_published_at: Optional[str] = None
    parent_comment_id: Optional[str] = None
    comment_author: Optional[str] = None
    author_channel_id: Optional[str] = None
    comment_likes: Any = 0
    comment_published_at: Optional[str] = None
    total_reply_count: Any = 0


@app.post("/api/ingest/youtube-comment")
def ingest_youtube_comment_row(row: YoutubeCommentRow):
    """
    Direct-comment ingestion for automations that already have one
    YouTube comment in hand and want it in the database immediately,
    instead of batching into a CSV for someone to re-upload by hand.
    Same upsert key as the CSV path (platform + comment's own id), so
    re-sending a corrected comment updates it in place.
    """
    if UPLOAD_PASSPHRASE and row.passphrase != UPLOAD_PASSPHRASE:
        raise HTTPException(status_code=401, detail="Wrong passphrase.")
    try:
        return ingest_youtube_comment(
            client=row.client,
            video_id=row.video_id,
            comment_id=row.comment_id,
            comment_text=row.comment_text,
            video_title=row.video_title,
            video_url=row.video_url,
            video_published_at=row.video_published_at,
            parent_comment_id=row.parent_comment_id,
            comment_author=row.comment_author,
            author_channel_id=row.author_channel_id,
            comment_likes=row.comment_likes,
            comment_published_at=row.comment_published_at,
            total_reply_count=row.total_reply_count,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


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
            # Dispatch on filename: AI visibility uses .xlsx; the three
            # .csv shapes (shopify report, reddit comments, youtube
            # comments) are told apart by their naming convention, since
            # they share an extension. Anything else gets skipped with a
            # clear reason.
            ext = dest.suffix.lower()
            name_lower = dest.name.lower()
            if ext == ".xlsx":
                results.append(ingest_ai_file(dest))
            elif ext == ".csv" and name_lower.endswith("-reddit-comments.csv"):
                r = ingest_reddit_file(dest)
                r["filename"] = r.pop("file")
                results.append(r)
            elif ext == ".csv" and name_lower.endswith("-youtube-comments.csv"):
                r = ingest_youtube_file(dest)
                r["filename"] = r.pop("file")
                results.append(r)
            elif ext == ".csv":
                results.append(ingest_shopify_file(dest))
            else:
                results.append({
                    "filename": dest.name,
                    "status": "skipped",
                    "reason": (
                        f"unsupported extension '{ext}' (use .xlsx for AI visibility, "
                        "'{client}-all-data.csv' for shopify reports, "
                        "'{client}-reddit-comments.csv' or '{client}-youtube-comments.csv' for comments)"
                    ),
                })
    return results


@app.get("/api/latest-ingest")
def latest_ingest():
    """Most recent data ingestion per type - powers the 'new data'
    banner on the dashboard."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT max(ingested_at) FROM ai_visibility_checks
                """
            )
            ai = cur.fetchone()["max"]
            cur.execute("SELECT max(ingested_at) FROM shopify_report_sections")
            shopify = cur.fetchone()["max"]
            cur.execute("SELECT max(fetched_at) FROM research_sources")
            sources = cur.fetchone()["max"]
            cur.execute("SELECT max(fetched_at) FROM research_comments")
            comments = cur.fetchone()["max"]
            return {
                "ai_visibility": ai,
                "shopify": shopify,
                "research_sources": sources,
                "research_comments": comments,
                "latest": max(x for x in (ai, shopify, sources, comments) if x is not None) if any((ai, shopify, sources, comments)) else None,
            }


@app.get("/api/research-sources")
def research_sources(client: str, limit: int = 12):
    """Recent YouTube/Reddit sources (videos, threads) ingested for this
    client, with comment counts."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM clients WHERE slug = %(slug)s;", {"slug": client})
            row = cur.fetchone()
            if not row:
                return []
            cur.execute(
                """
                SELECT rs.id, rs.platform, rs.url, rs.title, rs.published_at, rs.fetched_at,
                       (SELECT count(*) FROM research_comments rc WHERE rc.source_id = rs.id) AS comment_count
                FROM research_sources rs
                WHERE rs.client_id = %(cid)s
                ORDER BY rs.fetched_at DESC
                LIMIT %(lim)s
                """,
                {"cid": row["id"], "lim": limit},
            )
            return cur.fetchall()


@app.get("/api/research-comments")
def research_comments(client: str, limit: int = 10):
    """Top comments by upvotes across this client's YouTube/Reddit
    sources - the voice-of-customer view."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM clients WHERE slug = %(slug)s;", {"slug": client})
            row = cur.fetchone()
            if not row:
                return []
            cur.execute(
                """
                SELECT rc.author, rc.score, rc.body, rc.platform, rc.posted_at,
                       rs.title AS source_title, rs.url AS source_url
                FROM research_comments rc
                JOIN research_sources rs ON rs.id = rc.source_id
                WHERE rs.client_id = %(cid)s
                ORDER BY rc.score DESC NULLS LAST
                LIMIT %(lim)s
                """,
                {"cid": row["id"], "lim": limit},
            )
            return cur.fetchall()


@app.get("/api/research-stats")
def research_stats(client: str):
    """Totals for the research panel header."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM clients WHERE slug = %(slug)s;", {"slug": client})
            row = cur.fetchone()
            if not row:
                return {"sources": 0, "comments": 0, "platforms": []}
            cur.execute(
                "SELECT platform, count(*) n FROM research_sources WHERE client_id = %(cid)s GROUP BY platform",
                {"cid": row["id"]},
            )
            by_platform = cur.fetchall()
            cur.execute(
                """SELECT count(*) FROM research_comments rc
                   JOIN research_sources rs ON rs.id = rc.source_id
                   WHERE rs.client_id = %(cid)s""",
                {"cid": row["id"]},
            )
            return {
                "sources": sum(r["n"] for r in by_platform),
                "comments": cur.fetchone()["count"],
                "platforms": by_platform,
            }


# Static frontend - must be mounted last so /api/* routes above take priority.
app.mount("/", StaticFiles(directory=Path(__file__).parent, html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("dashboard:app", host="0.0.0.0", port=8000)

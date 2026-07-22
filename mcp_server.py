"""
MCP server exposing the Smart Marketer Data Hub to agent clients
(Hermes, Claude Desktop, Claude Code, etc.).

Transport: Streamable HTTP — sits next to Postgres on the VPS, and any
MCP-compatible client (local or remote) can point at the URL. Run with:

    python mcp_server.py            # binds 0.0.0.0:8765 by default
    MCP_HOST=127.0.0.1 MCP_PORT=8765 python mcp_server.py

The tools are read-only primitives over the existing schema. The agent
(reasoning LLM) is responsible for the "does this need improvement?"
verdict — we deliberately do not hard-code thresholds here, per the
"AI visibility data needs to be as it is, no changes" direction.

Resources: none. Tools: five. All return JSON.
"""
import os
import re
from typing import Optional

from mcp.server.fastmcp import FastMCP

from db import get_conn
from ingest_ai_visibility import slugify

# Bind to 0.0.0.0 by default so a VPS deployment is reachable from
# outside the box. Override to 127.0.0.1 for strict local-only use.
HOST = os.getenv("MCP_HOST", "0.0.0.0")
PORT = int(os.getenv("MCP_PORT", "8765"))

mcp = FastMCP(
    "smartmarketer-datahub",
    host=HOST,
    port=PORT,
    json_response=True,
    streamable_http_path="/mcp",
    instructions=(
        "Read-only access to the Smart Marketer Data Hub. Use "
        "list_clients to discover available clients, then "
        "get_ai_visibility_summary / get_ai_visibility_queries for the "
        "AI visibility data, or get_shopify_report for the multi-section "
        "shopify report. Pass `client` as a slug (e.g. 'altenew') or "
        "as a name fragment (e.g. 'outdoor vitals') — both work."
    ),
)


def _resolve_client(cur, query: str) -> Optional[dict]:
    """
    Accept either a slug ('altenew', 'outdoorvitals') or a name fragment
    ('outdoor vitals', 'OutdoorVitals'). Tries slug first, then falls
    back to a case-insensitive substring match on the client name — so
    "outdoor vitals" or "OutdoorVitals" both find the row whose name is
    "Outdoorvitals" (the README's documented flattened-name case).
    """
    slug = slugify(query)
    cur.execute(
        "SELECT id, name, slug FROM clients WHERE slug = %(slug)s;",
        {"slug": slug},
    )
    row = cur.fetchone()
    if row:
        return row
    # Fall back: match against the client name OR slug (both stripped
    # of non-alphanumerics so separator differences don't matter). So
    # "outdoor vitals" finds the row whose stored name is "Outdoorvitals"
    # and slug is "outdoorvitals" — the README's documented flattened-
    # name case where the source file had no separator between words.
    needle = re.sub(r"[^a-z0-9]", "", query.strip().lower())
    if not needle:
        return None
    cur.execute(
        "SELECT id, name, slug FROM clients "
        "WHERE regexp_replace(lower(name), '[^a-z0-9]', '', 'g') LIKE %(p1)s "
        "   OR regexp_replace(lower(slug), '[^a-z0-9]', '', 'g') LIKE %(p1)s "
        "LIMIT 1;",
        {"p1": f"%{needle}%"},
    )
    return cur.fetchone()


def _num(val):
    if val is None:
        return None
    f = float(val)
    return None if f != f else round(f, 1)


@mcp.tool()
def list_clients() -> list[dict]:
    """
    List every client in the database with their slug and id. Use this
    first when the user asks about a client by name and you need the
    slug to call the other tools.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, slug, created_at FROM clients ORDER BY name;"
            )
            rows = cur.fetchall()
    for r in rows:
        r["created_at"] = r["created_at"].isoformat()
    return rows


@mcp.tool()
def get_ai_visibility_summary(client: str) -> dict:
    """
    Per-platform AI visibility summary for one client: how many queries
    were tested on each platform, the average visibility score, and
    the average brand position. This is the high-level health read —
    start here when asked "how is client X doing on AI visibility?".
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            c = _resolve_client(cur, client)
            if not c:
                return {"error": f"no client matching '{client}'"}
            cur.execute(
                """
                SELECT
                    v.platform,
                    count(*) AS queries_tested,
                    avg(v.visibility_score) AS avg_visibility_score,
                    avg(v.brand_position)    AS avg_brand_position,
                    min(v.check_date)        AS first_check,
                    max(v.check_date)        AS last_check
                FROM ai_visibility_checks v
                WHERE v.client_id = %(cid)s
                GROUP BY v.platform
                ORDER BY v.platform;
                """,
                {"cid": c["id"]},
            )
            platforms = cur.fetchall()
    for p in platforms:
        p["avg_visibility_score"] = _num(p["avg_visibility_score"])
        p["avg_brand_position"] = _num(p["avg_brand_position"])
        p["first_check"] = p["first_check"].isoformat() if p["first_check"] else None
        p["last_check"] = p["last_check"].isoformat() if p["last_check"] else None
    return {"client": c["name"], "slug": c["slug"], "platforms": platforms}


@mcp.tool()
def get_ai_visibility_queries(
    client: str,
    platform: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """
    Every AI visibility query row for one client, newest first. Pass
    `platform` to filter to one (e.g. 'chatgpt', 'perplexity',
    'google_ai_mode'). The full raw AI response and competitor
    analysis are included so the agent can read them directly when
    judging "is this fine or does it need improvement?".
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            c = _resolve_client(cur, client)
            if not c:
                return [{"error": f"no client matching '{client}'"}]
            params = {"cid": c["id"]}
            sql = """
                SELECT
                    v.platform, v.check_date, v.query_text, v.visibility_score,
                    v.brand_position, v.total_brands, v.mentions, v.urls,
                    v.competitor_analysis, v.raw_output, v.sources,
                    v.related_queries
                FROM ai_visibility_checks v
                WHERE v.client_id = %(cid)s
            """
            if platform:
                sql += " AND v.platform = %(platform)s"
                params["platform"] = platform
            sql += " ORDER BY v.check_date DESC, v.platform LIMIT %(limit)s;"
            cur.execute(sql, {**params, "limit": limit})
            rows = cur.fetchall()
    for r in rows:
        r["check_date"] = r["check_date"].isoformat() if r["check_date"] else None
        r["visibility_score"] = _num(r["visibility_score"])
        r["brand_position"] = _num(r["brand_position"])
        r["total_brands"] = int(r["total_brands"]) if r["total_brands"] is not None else None
    return rows


@mcp.tool()
def get_top_mentions(client: str, limit: int = 10) -> list[dict]:
    """
    The brands/domains most often mentioned alongside this client in
    AI responses. Useful for "who are the competitors showing up in
    AI answers for client X?". Filters out the client's own name.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            c = _resolve_client(cur, client)
            if not c:
                return [{"error": f"no client matching '{client}'"}]
            cur.execute(
                "SELECT v.mentions FROM ai_visibility_checks v WHERE v.client_id = %(cid)s;",
                {"cid": c["id"]},
            )
            rows = cur.fetchall()
    own = c["name"].strip().lower()
    counts: dict[str, int] = {}
    for r in rows:
        for m in (r["mentions"] or []):
            name = (m or "").strip()
            if not name or own in name.lower():
                continue
            counts[name] = counts.get(name, 0) + 1
    top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return [{"name": n, "count": cnt} for n, cnt in top]


@mcp.tool()
def get_shopify_report(client: str) -> dict:
    """
    The full multi-section shopify report for one client (the
    '-all-data.csv' export, stored generically because each client's
    file has different sections and columns). Returns one entry per
    section, with whatever columns and rows the source CSV had.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            c = _resolve_client(cur, client)
            if not c:
                return {"error": f"no client matching '{client}'"}
            cur.execute(
                """
                SELECT section_name, columns, rows, source_file, ingested_at
                FROM shopify_report_sections
                WHERE client_id = %(cid)s
                ORDER BY id;
                """,
                {"cid": c["id"]},
            )
            sections = cur.fetchall()
    for s in sections:
        s["ingested_at"] = s["ingested_at"].isoformat() if s["ingested_at"] else None
    return {
        "client": c["name"],
        "slug": c["slug"],
        "section_count": len(sections),
        "sections": sections,
    }


if __name__ == "__main__":
    import sys as _sys
    # Stdio transport for Claude Desktop (no URL needed — Claude launches
    # the subprocess). HTTP transport for remote/VPS MCP clients.
    if "--stdio" in _sys.argv or os.getenv("MCP_TRANSPORT") == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http")

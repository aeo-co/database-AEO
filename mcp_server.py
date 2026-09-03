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

Resources: none. Tools: seven. All return JSON.
"""
import os
import re
from typing import Optional

from mcp.server.fastmcp import FastMCP

from db import get_conn
from client_context import fetch_client_context, upsert_client_context
from ingest_ai_visibility import slugify, ingest_file as ingest_ai_file
from ingest_shopify_reports import ingest_file as _ingest_csv

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
        "Agency-wide source of truth for client information. When asked "
        "anything about a client, START with get_client_profile(client) — "
        "it returns everything in one call: AI visibility scores, "
        "competitors, Shopify report index, brand voice/ICP docs, and "
        "graph connections. Narrower tools: list_clients (roster), "
        "get_ai_visibility_summary / get_ai_visibility_queries (visibility "
        "numbers and raw AI answers), get_top_mentions (competitor brands), "
        "get_shopify_report (GSC/sales sections), get_client_context "
        "(one context doc type), get_related / find_similar_clients "
        "(knowledge graph). Write tools: ingest_file (.xlsx visibility "
        "audit), ingest_shopify_file (.csv all-data report), "
        "ingest_client_context (save brand voice/ICP docs), add_edge "
        "(link entities), refresh_similar_clients (recompute similarity). "
        "Client names resolve flexibly — slug, partial name, or id."
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
    List every client in the database (name, slug, id, manager). Call
    this first whenever a question mentions a client by name and you
    need their slug for other tools. Natural questions this answers:
    "which clients do we have?", "who are our clients?", "list clients",
    "who manages client X?". For EVERYTHING about one client in one call
    (visibility scores, Shopify reports, brand contexts, related
    entities), prefer get_client_profile instead.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, slug, manager, created_at FROM clients ORDER BY name;"
            )
            rows = cur.fetchall()
    for r in rows:
        r["created_at"] = r["created_at"].isoformat()
    return rows


@mcp.tool()
def get_client_profile(client: str) -> dict:
    """
    ONE-CALL complete profile of a single client — the "everything we
    know about client X" tool. Assembles in one response: basic info
    (name, slug, manager), AI visibility summary per platform (scores,
    positions, query counts, date range), top competitor brands
    mentioned alongside them, their Shopify report section index, all
    brand-context documents (brand voice, ICP, journey maps, case
    studies), and knowledge-graph connections (similar clients, related
    entities). Use this for: "tell me everything about Winona", "give
    me the full picture on client X", "brief me on [client]" — any
    question that needs the whole story. For a single narrow slice
    (e.g. just visibility numbers or just the brand voice doc), the
    focused tools get_ai_visibility_summary / get_client_context are
    cheaper.
    """
    import json as _json

    with get_conn() as conn:
        with conn.cursor() as cur:
            c = _resolve_client(cur, client)
            if not c:
                return {"error": f"no client matching '{client}'"}
            cid = c["id"]

            # --- basic info
            cur.execute(
                "SELECT id, name, slug, manager, created_at FROM clients WHERE id = %s;",
                (cid,))
            basic = cur.fetchone()
            basic["created_at"] = basic["created_at"].isoformat()

            # --- AI visibility summary (per platform)
            cur.execute(
                """
                SELECT platform, count(*) AS queries_tested,
                       avg(visibility_score) AS avg_visibility_score,
                       avg(brand_position) AS avg_brand_position,
                       min(check_date) AS first_check,
                       max(check_date) AS last_check
                FROM ai_visibility_checks WHERE client_id = %s
                GROUP BY platform ORDER BY platform;
                """, (cid,))
            visibility = cur.fetchall()
            for p in visibility:
                p["avg_visibility_score"] = _num(p["avg_visibility_score"])
                p["avg_brand_position"] = _num(p["avg_brand_position"])
                p["first_check"] = p["first_check"].isoformat() if p["first_check"] else None
                p["last_check"] = p["last_check"].isoformat() if p["last_check"] else None

            # --- top mentioned competitor brands
            cur.execute(
                "SELECT mentions FROM ai_visibility_checks WHERE client_id = %s;", (cid,))
            own = c["name"].strip().lower()
            counts: dict[str, int] = {}
            for r in cur.fetchall():
                for m in (r["mentions"] or []):
                    name = (m or "").strip()
                    if not name or own in name.lower():
                        continue
                    counts[name] = counts.get(name, 0) + 1
            top_mentions = [{"name": n, "count": cnt} for n, cnt in
                            sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:10]]

            # --- Shopify report section index
            cur.execute(
                """
                SELECT section_name, report_period, source_file,
                       jsonb_array_length(columns) AS n_columns,
                       jsonb_array_length(rows) AS n_rows, ingested_at
                FROM shopify_report_sections WHERE client_id = %s
                ORDER BY report_period NULLS FIRST, section_name;
                """, (cid,))
            shopify_sections = cur.fetchall()
            for s in shopify_sections:
                s["ingested_at"] = s["ingested_at"].isoformat() if s["ingested_at"] else None

            # --- all brand-context docs (every type)
            cur.execute(
                """
                SELECT id, context_type, title, content, source_file, ingested_at
                FROM client_contexts WHERE client_id = %s
                ORDER BY context_type, ingested_at DESC;
                """, (cid,))
            contexts = cur.fetchall()
            for r in contexts:
                r["ingested_at"] = r["ingested_at"].isoformat() if r["ingested_at"] else None

            # --- knowledge-graph: related nodes (1-2 hops)
            try:
                from kg import get_related as kg_related
                cur.execute(
                    "SELECT id, entity_key FROM kg_nodes "
                    "WHERE entity_type='client' AND entity_key = 'client:' || %s;",
                    (c["slug"],))
                kg_row = cur.fetchone()
                if kg_row:
                    kg = kg_related("client", kg_row["entity_key"], depth=2, limit=30)
                    kg_related_nodes = kg.get("related", [])
                else:
                    kg_related_nodes = []
            except Exception as e:  # KG tables may not exist yet — degrade gracefully
                kg_related_nodes = [{"note": f"knowledge graph unavailable: {e}"}]

    return {
        "client": basic,
        "ai_visibility": {
            "platforms": visibility,
            "top_competitor_brands": top_mentions,
        },
        "shopify_report": {
            "section_count": len(shopify_sections),
            "sections": shopify_sections,
        },
        "brand_contexts": {
            "count": len(contexts),
            "docs": contexts,
        },
        "knowledge_graph": {
            "related": kg_related_nodes,
        },
    }


@mcp.tool()
def get_ai_visibility_summary(client: str) -> dict:
    """
    Per-platform AI visibility summary for one client: how many queries
    were tested on each platform (chatgpt, perplexity, google_ai_mode),
    the average visibility score (how often the brand appears in AI
    answers), and the average brand position. Natural questions this
    answers: "how visible is X in AI?", "how is X doing on AI
    visibility?", "is X showing up in ChatGPT/Perplexity?". For full
    query-level rows (the actual raw AI answers), use
    get_ai_visibility_queries; for the whole client picture in one call,
    use get_client_profile.
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
    Every AI visibility query row for one client, newest first. Each row
    includes the actual raw AI answer, which brands/URLs it cited, and
    competitor analysis. Pass `platform` to filter to one ('chatgpt',
    'perplexity', 'google_ai_mode'). Natural questions this answers:
    "what does ChatGPT say when asked about X?", "show me the actual AI
    answers for client X", "which queries does X fail on?", "read me
    X's AI responses". For summary numbers only, use
    get_ai_visibility_summary.
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
    The brands/domains most often mentioned alongside this client in AI
    responses — i.e. who the AI engines name instead of (or next to)
    this client. Filters out the client's own name. Natural questions
    this answers: "who are X's competitors in AI answers?", "which
    brands beat X in ChatGPT?", "what comes up instead of X?".
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
    The full multi-section Shopify/web report for one client (from the
    '-all-data.csv' upload): SEO/GSC data, sales, weekly AEO summaries —
    each section with its own columns and rows. Returns an index when
    there are many sections; pass get_client_profile first to see the
    section list. Natural questions this answers: "what's X's organic
    traffic?", "show me X's Shopify numbers", "what do the weekly
    reports say about X?", "X's search console performance".
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            c = _resolve_client(cur, client)
            if not c:
                return {"error": f"no client matching '{client}'"}
            cur.execute(
                """
                SELECT section_name, report_period, columns, rows, source_file, ingested_at
                FROM shopify_report_sections
                WHERE client_id = %(cid)s
                ORDER BY report_period NULLS FIRST, id;
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


@mcp.tool()
def ingest_file(path: str, passphrase: str = "") -> dict:
    """
    Ingest one AI-visibility .xlsx file (the export from the audit tool)
    into the database — parses the filename for client/platform/date and
    upserts query rows by client+platform+date+query_hash. Same code
    path as the /upload.html dashboard form. Use this when someone says
    "ingest this visibility file", "load the new ChatGPT audit", or
    drops a .xlsx path. Optional passphrase checked if UPLOAD_PASSPHRASE
    is set.
    """
    import os as _os
    expected = _os.getenv("UPLOAD_PASSPHRASE")
    if expected and passphrase != expected:
        return {"status": "error", "reason": "wrong passphrase"}
    from pathlib import Path as _P
    p = _P(path)
    if not p.exists():
        return {"filename": p.name, "status": "error", "reason": f"file not found: {path}"}
    return ingest_ai_file(p)


@mcp.tool()
def ingest_shopify_file(path: str, passphrase: str = "") -> dict:
    """
    Ingest one Shopify/web report .csv (named {client}-all-data.csv) —
    parses '=== Section ===' blocks (GSC, sales, weekly AEO summaries)
    and stores each section generically. Same code path as the
    dashboard upload. Use this when someone says "ingest the Shopify
    report", "load this all-data CSV", or drops a
    {client}-all-data.csv path. Optional passphrase checked if
    UPLOAD_PASSPHRASE is set.
    """
    import os as _os
    expected = _os.getenv("UPLOAD_PASSPHRASE")
    if expected and passphrase != expected:
        return {"status": "error", "reason": "wrong passphrase"}
    from pathlib import Path as _P
    p = _P(path)
    if not p.exists():
        return {"filename": p.name, "status": "error", "reason": f"file not found: {path}"}
    return _ingest_csv(p)


@mcp.tool()
def get_client_context(client: str, context_type: str) -> dict:
    """
    Fetch whole brand-context documents for one client by context_type:
    'brand_voice' (how the client writes/speaks), 'icp' (ideal customer
    profile — who they target), 'journey_map', 'case_study', etc.
    Returns the full markdown text of each matching doc. Natural
    questions this answers: "what's Nike's brand voice?", "who is X's
    target customer?", "what's X's ICP?", "give me X's brand guidelines
    so I can write a post". To see every context doc at once (plus
    everything else about the client), use get_client_profile. To find
    docs by meaning rather than type, no tool needed — the whole doc is
    returned here.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            c = _resolve_client(cur, client)
            if not c:
                return {"error": f"no client matching '{client}'"}
    rows = fetch_client_context(c["id"], context_type)
    return {
        "client": c["name"],
        "slug": c["slug"],
        "context_type": context_type,
        "count": len(rows),
        "contexts": rows,
    }


@mcp.tool()
def ingest_client_context(
    client: str,
    context_type: str,
    content: str,
    title: str = "",
    metadata: Optional[dict] = None,
    passphrase: str = "",
) -> dict:
    """
    Store or update one brand-context document for a client and embed
    it for similarity search. `content` is the WHOLE markdown doc (not
    fragmented); context_type is 'brand_voice' | 'icp' | 'journey_map' |
    'case_study' | ...; upserts on (client, context_type, title) so
    re-ingesting replaces the previous version. Embedding is LOCAL
    (all-MiniLM-L6-v2) — no API key. Use this when someone says "save
    this as X's brand voice", "update X's ICP", "add this case study
    for X". After ingesting new ICP docs, run refresh_similar_clients
    to update the similarity graph. Optional passphrase checked if
    UPLOAD_PASSPHRASE is set (same gate as the other write tools).
    """
    import os as _os
    expected = _os.getenv("UPLOAD_PASSPHRASE")
    if expected and passphrase != expected:
        return {"status": "error", "reason": "wrong passphrase"}
    with get_conn() as conn:
        with conn.cursor() as cur:
            c = _resolve_client(cur, client)
            if not c:
                return {"status": "error", "reason": f"no client matching '{client}'"}
    try:
        result = upsert_client_context(
            client_id=c["id"],
            context_type=context_type,
            content=content,
            title=title or None,
            metadata=metadata or {},
        )
    except Exception as e:
        return {"status": "error", "reason": str(e)}
    return {
        "status": "ok",
        "inserted" if result["inserted"] else "replaced": True,
        "id": result["id"],
        "client": c["name"],
        "context_type": context_type,
        "title": result["title"],
    }


# ---------------------------------------------------------------------------
# Knowledge-graph tools (kg_nodes / kg_edges — see kg.py and migrate_kg.sql)
# Nodes mirror clients / client_contexts; edges are asserted (inferred=false)
# or computed from ICP embedding similarity (inferred=true).
# ---------------------------------------------------------------------------

def _kg_node_for_client(cur, query: str) -> int:
    """Resolve (or lazily create) the kg node for a client slug/name."""
    c = _resolve_client(cur, query)
    if not c:
        raise ValueError(f"no client matching '{query}'")
    cur.execute(
        """
        INSERT INTO kg_nodes (entity_type, entity_key, label, props)
        VALUES ('client', 'client:' || %s, %s, '{}'::jsonb)
        ON CONFLICT (entity_type, entity_key) DO UPDATE SET label = EXCLUDED.label
        RETURNING id
        """,
        (c["slug"], c["name"]))
    return cur.fetchone()["id"]


def _kg_resolve_any_node(cur, entity_type: str, entity_key: str) -> int:
    """Resolve (or lazily create) a kg node for any entity type.

    entity_type 'client' resolves via _kg_node_for_client (flexible name
    matching). 'context' auto-mirrors from client_contexts by numeric id
    ('context:12'). Other types ('campaign', 'case_study', 'topic',
    'custom') are created on demand with a human label from the key.
    """
    if entity_type == "client":
        return _kg_node_for_client(cur, entity_key)
    if entity_type == "context":
        num = re.sub(r"[^0-9]", "", str(entity_key))
        if not num:
            raise ValueError(f"context key must be numeric id, got {entity_key!r}")
        key = f"context:{num}"
        cur.execute(
            "SELECT cc.id, COALESCE(NULLIF(cc.title, ''), 'untitled') AS label, "
            "c.slug AS client_slug "
            "FROM client_contexts cc JOIN clients c ON c.id = cc.client_id "
            "WHERE cc.id = %s", (int(num),))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"no client_contexts row id={num}")
        cur.execute(
            """
            INSERT INTO kg_nodes (entity_type, entity_key, label, props)
            VALUES ('context', %s, %s, jsonb_build_object('client', %s::text))
            ON CONFLICT (entity_type, entity_key)
            DO UPDATE SET label = EXCLUDED.label
            RETURNING id
            """,
            (key, row["label"], row["client_slug"]))
        return cur.fetchone()["id"]
    # generic types: campaign / case_study / topic / custom — create on demand
    if entity_type not in ("campaign", "case_study", "topic", "custom"):
        raise ValueError(f"unknown entity_type {entity_type!r}; use client, "
                         "context, campaign, case_study, topic, or custom")
    if not entity_key:
        raise ValueError("entity_key is required for non-client types")
    key = f"{entity_type}:{entity_key}" if not str(entity_key).startswith(f"{entity_type}:") else entity_key
    cur.execute(
        """
        INSERT INTO kg_nodes (entity_type, entity_key, label, props)
        VALUES (%s, %s, %s, '{}'::jsonb)
        ON CONFLICT (entity_type, entity_key) DO UPDATE SET label = EXCLUDED.label
        RETURNING id
        """,
        (entity_type, key, str(entity_key).replace(f"{entity_type}:", "").replace("-", " ").title()))
    return cur.fetchone()["id"]


@mcp.tool()
def get_related(
    entity: str = "",
    entity_type: str = "client",
    client: str = "",
    rel_type: Optional[str] = None,
    depth: int = 1,
    limit: int = 50,
) -> dict:
    """
    "What's connected to X?" — traverse the knowledge graph from ANY
    entity up to `depth` hops (1-4), following edges in both
    directions. Works for clients AND any other entity:
    entity_type='client' with a client name/slug ('outdoor vitals'),
    entity_type='context' with a context doc id ('context:1' or '1'),
    or entity_type='campaign'/'case_study'/'topic'/'custom' with a key.
    Pass the target via `entity` (+ `entity_type`); the `client` param
    is a deprecated alias for entity + entity_type='client' kept for
    older callers.
    Returns every reachable node (clients, their context docs,
    campaigns, case studies, topics) with the relationship type and
    hop distance. Natural questions: "what's connected to this case
    study?", "what links to context 1?", "what do we know around X?".
    Optional rel_type filter ('similar_to', 'belongs_to', 'mentions',
    'relates_to', ...). For the full client picture in one call, use
    get_client_profile.
    """
    if not entity and client:
        entity = client
        entity_type = "client"
    if not entity:
        return {"error": "pass entity (e.g. 'outdoor vitals' or 'context:1')"}
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                node_id = _kg_resolve_any_node(cur, entity_type, entity)
                cur.execute(
                    "SELECT entity_key FROM kg_nodes WHERE id = %s", (node_id,))
                entity_key = cur.fetchone()["entity_key"]
        from kg import get_related as kg_get_related
        result = kg_get_related(
            entity_type, entity_key, rel_type=rel_type, depth=depth, limit=limit)
        # hide the root from its own results (undirected walk revisits it)
        result["related"] = [n for n in result["related"]
                             if n["entity_key"] != entity_key]
        result["root"]["label"] = result["root"].get("label") or entity
        return result
    except ValueError as e:
        return {"error": str(e)}


@mcp.tool()
def find_similar_clients(client: str, threshold: float = 0.85) -> dict:
    """
    "Which clients have similar ICPs / target audiences?" — cosine
    similarity between this client's ICP embedding and every other
    client's, computed live from the latest stored docs. Natural
    questions: "which clients look like X?", "who else targets the
    same audience as X?", "find clients similar to X". threshold:
    0.85 = very similar only; lower to 0.7 for broader matches (unrelated
    docs score ~0.4-0.5 with our local MiniLM embeddings).
    """
    if not 0.5 <= threshold < 1.0:
        return {"error": "threshold must be in [0.5, 1.0)"}
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                c = _resolve_client(cur, client)
                if not c:
                    return {"error": f"no client matching '{client}'"}
                cur.execute(
                    """
                    SELECT c2.slug, c2.name, c2.id,
                           MIN(a.embedding <=> b.embedding) AS best_dist
                    FROM client_contexts a
                    JOIN client_contexts b
                      ON b.client_id <> a.client_id
                     AND b.context_type = a.context_type
                    JOIN clients c2 ON c2.id = b.client_id
                    WHERE a.client_id = %s
                      AND a.context_type = 'icp'
                      AND a.embedding IS NOT NULL
                      AND b.embedding IS NOT NULL
                    GROUP BY c2.slug, c2.name, c2.id
                    HAVING MIN(a.embedding <=> b.embedding) < %s
                    ORDER BY best_dist
                    """,
                    (c["id"], 1.0 - threshold))
                rows = cur.fetchall()
        for r in rows:
            r["similarity"] = round(1.0 - r.pop("best_dist"), 4)
        return {"client": c["name"], "slug": c["slug"], "threshold": threshold,
                "similar_clients": rows or []}
    except ValueError as e:
        return {"error": str(e)}


@mcp.tool()
def add_edge(
    src_entity_key: str = "",
    src_client: str = "",
    dst_entity_key: str = "",
    dst_client: str = "",
    rel_type: str = "relates_to",
    weight: float = 1.0,
    props: Optional[dict] = None,
    inferred: bool = False,
    passphrase: str = "",
) -> dict:
    """
    Assert a relationship edge in the knowledge graph — e.g. case study
    -> client ('mentions'), campaign -> client ('relates_to'), client ->
    topic ('mentions'), context -> client ('belongs_to'). Use when
    someone says "link this case study to Winona", "record that campaign
    Y belongs to X". Endpoints: pass src_client/dst_client (client
    slug/name) OR src_entity_key/dst_entity_key (existing node key like
    'context:2' or 'client:outdoor-vitals'). inferred=true marks the
    edge as auto-recomputable; leave false for human-asserted facts.
    Optional passphrase checked if UPLOAD_PASSPHRASE is set.
    """
    import os as _os
    expected = _os.getenv("UPLOAD_PASSPHRASE")
    if expected and passphrase != expected:
        return {"status": "error", "reason": "wrong passphrase"}
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                def _endpoint(client_q: str, key: str) -> int:
                    if client_q:
                        return _kg_node_for_client(cur, client_q)
                    if key:
                        # accept bare 'context:2' style keys OR 'type|key' pairs
                        m = re.match(r"^(client|context|campaign|case_study|topic|custom):(.+)$", key)
                        if m:
                            # auto-resolve/create via the universal resolver
                            try:
                                return _kg_resolve_any_node(cur, m.group(1), m.group(2))
                            except ValueError:
                                # fall through to existing-node lookup below
                                pass
                        cur.execute(
                            "SELECT id FROM kg_nodes WHERE entity_key = %s", (key,))
                        r = cur.fetchone()
                        if not r:
                            raise ValueError(f"no kg node with entity_key={key!r}")
                        return r["id"]
                    raise ValueError("each endpoint needs client or entity_key")
                src_id = _endpoint(src_client, src_entity_key)
                dst_id = _endpoint(dst_client, dst_entity_key)
        from kg import add_edge
        edge_id = add_edge(src_id, dst_id, rel_type, weight, props, inferred)
        return {"status": "ok", "edge_id": edge_id, "rel_type": rel_type,
                "src_id": src_id, "dst_id": dst_id}
    except ValueError as e:
        return {"status": "error", "reason": str(e)}


@mcp.tool()
def refresh_similar_clients(threshold: float = 0.85) -> dict:
    """
    Recompute inferred 'similar_to' edges between clients from their ICP
    embeddings. Deletes all previously inferred similar_to edges and
    inserts fresh ones above `threshold` (default 0.85). Run this after
    ingesting new ICP docs (via ingest_client_context) so the graph
    stays current. Asserted edges (inferred=false) are never touched.
    """
    from kg import refresh_similar_edges
    try:
        return refresh_similar_edges(threshold=threshold)
    except ValueError as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Vector-Graph Hybrid Engine (aeo-hybrid-v1) — see insights.py and
# triplet_extraction.py. Ontology: client / product / search_intent /
# ai_engine / authority_site. All heavy work stays in native SQL; the
# embedding model is lazy-loaded only inside runtime calls (OOM safety).
# ---------------------------------------------------------------------------

@mcp.tool()
def get_visibility_bottlenecks(client: str, gap_threshold: float = 0.3, limit: int = 5) -> dict:
    """
    Find a client's biggest AEO optimization bottlenecks in ONE call.
    For every search intent linked to the client's products where AI
    engines (ChatGPT, Perplexity, Google AI Mode) are actively citing
    external authority sites, it checks the client's brand-context docs
    (pgvector cosine distance) and flags intents where the client has a
    semantic gap (distance > gap_threshold, default 0.3) — i.e. engines
    cite competitors for queries the client's content doesn't cover well.
    Returns the top `limit` distinct bottlenecks with the query text,
    cited competitor domains, which engines cite them, and the gap score.
    Natural questions: "where are we losing AI visibility?", "what
    queries do competitors get cited for that we don't cover?", "show
    X's optimization bottlenecks".
    """
    from insights import get_visibility_bottlenecks as _impl
    with get_conn() as conn:
        with conn.cursor() as cur:
            c = _resolve_client(cur, client)
            if not c:
                return {"error": f"no client matching '{client}'"}
    return _impl(c["id"], gap_threshold=gap_threshold, limit=limit)


@mcp.tool()
def generate_content_brief(client: str, product_sku: str) -> dict:
    """
    Generate a hyper-targeted, AI-optimized content rewrite brief for one
    client product (Graph-RAG). Walks the knowledge graph: locates the
    Product node, hops 1 step to every connected SearchIntent, hops 2
    steps to aggregate the exact AuthoritySite URLs that AI engines cite
    for those intents, and semantically matches the client's brand-voice
    context docs via local pgvector search. Returns a ready-to-use
    markdown brief: target queries, competitor sites to displace, brand
    voice, and a rewrite directive — paste it straight to Claude to draft
    the content. Natural questions: "write a content brief for X's
    silicone rings", "what should we rewrite to win AI citations for
    this product?"
    """
    from insights import generate_content_brief as _impl
    with get_conn() as conn:
        with conn.cursor() as cur:
            c = _resolve_client(cur, client)
            if not c:
                return {"error": f"no client matching '{client}'"}
    return _impl(c["id"], product_sku)


@mcp.tool()
def build_visibility_graph(client: str = "", passphrase: str = "") -> dict:
    """
    Extract Subject-Predicate-Object triplets from the ingested raw data
    (AI visibility checks + Shopify reports + context docs) and map them
    into the knowledge graph as product / search_intent / ai_engine /
    authority_site nodes and product_for_intent / checks_intent /
    intent_cites_site / has_product edges (ontology aeo-hybrid-v1).
    Runs row-by-row with batched writes — bounded memory, safe on the
    1GB droplet. Idempotent: re-running upserts, never duplicates.
    Pass `client` to rebuild just that client, or leave empty for the
    whole agency. Run this after ingesting new visibility/shopify data;
    get_visibility_bottlenecks and generate_content_brief read the graph
    this creates. Optional passphrase checked if UPLOAD_PASSPHRASE is set.
    """
    import os as _os
    import json as _json
    expected = _os.getenv("UPLOAD_PASSPHRASE")
    if expected and passphrase != expected:
        return {"status": "error", "reason": "wrong passphrase"}
    from triplet_extraction import (
        extract_triplets_from_visibility, store_triplets_bulk,
        link_products_to_intents, upsert_products_from_shopify,
    )
    stats = {"rows_seen": 0, "triplets": 0, "clients": set(), "sql_nodes": 0, "sql_edges": 0}
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                params: tuple = ()
                sql = ("SELECT client_id, platform, query_text, urls, sources "
                       "FROM ai_visibility_checks")
                if client:
                    c = _resolve_client(cur, client)
                    if not c:
                        return {"status": "error", "reason": f"no client matching '{client}'"}
                    sql += " WHERE client_id = %s"
                    params = (c["id"],)
                cur.execute("SELECT id, slug FROM clients")
                slug_by_id = {r["id"]: r["slug"] for r in cur.fetchall()}
                if client:
                    cur.execute("SELECT slug FROM clients WHERE id = %s", params)
                    r = cur.fetchone()
                    slug_by_id = {params[0]: r["slug"] if r else client}
                # process ONE client at a time — each client's row set is
                # small (~hundreds), so peak RAM stays bounded; a separate
                # cursor is used for writes so the read cursor stays valid
                client_ids = sorted({c for c in (params[0],) if c} or set(slug_by_id))
                cur2 = conn.cursor()
                for cid in client_ids:
                    slug = slug_by_id.get(cid)
                    if not slug:
                        continue
                    stats["clients"].add(slug)
                    cur.execute(sql, (cid,) if client else ())
                    while True:
                        rows = cur.fetchmany(200)
                        if not rows:
                            break
                        for row in rows:
                            stats["rows_seen"] += 1
                            trip = extract_triplets_from_visibility(
                                slug, row["platform"] or "unknown",
                                row["query_text"] or "", row["urls"] or [], row["sources"] or [])
                            if trip:
                                res = store_triplets_bulk(cur2, trip)
                                stats["sql_nodes"] += res["nodes"]
                                stats["sql_edges"] += res["edges"]
                            stats["triplets"] += len(trip)
                        conn.commit()  # commit per batch; one connection total
        link_stats = link_products_to_intents(client if client else "")
        return {"status": "ok", "linking": link_stats,
                **{k: (sorted(v) if isinstance(v, set) else v)
                   for k, v in stats.items()}}
    except Exception as e:
        return {"status": "error", "reason": str(e)}


if __name__ == "__main__":
    import sys as _sys
    # Stdio transport for Claude Desktop (no URL needed — Claude launches
    # the subprocess). HTTP transport for remote/VPS MCP clients.
    if "--stdio" in _sys.argv or os.getenv("MCP_TRANSPORT") == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http")

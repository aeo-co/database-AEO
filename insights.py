"""
High-utility AEO insight tools over the vector-graph hybrid.

- get_visibility_bottlenecks: ONE native multi-hop SQL query (no Python
  path-tracing) joining Product -> SearchIntent -> AIEngine citations ->
  AuthoritySite against client_contexts pgvector distances, surfacing the
  top-N semantic-gap optimization bottlenecks.
- generate_content_brief: Graph-RAG — 2-hop SQL sub-network + local vector
  match against client_contexts, compiled into a Claude-ready brief block.

No torch/sentence-transformers import at module level — the model is
lazy-loaded inside embed_text() on first call only (OOM safety, 1GB droplet).
"""
from db import get_conn
from client_context import embed_text

DEFAULT_GAP_THRESHOLD = 0.3  # cosine distance above this = semantic gap


# ---------------------------------------------------------------------------
# 1. Bottlenecks — single native multi-hop SQL statement
# ---------------------------------------------------------------------------

_BOTTLENECK_SQL = """
WITH intents AS (
    SELECT pi.intent_id,
           n.entity_key  AS intent_key,
           n.label       AS query_text,
           pi.product_id, pi.product_key, pi.product_label
    FROM kg_edges pe
    JOIN kg_nodes pn  ON pn.id = pe.dst_id AND pn.entity_type = 'product'
    JOIN kg_edges pie ON pie.src_id = pn.id AND pie.rel_type = 'product_for_intent'
    JOIN kg_nodes n   ON n.id = pie.dst_id AND n.entity_type = 'search_intent'
    JOIN kg_nodes cn  ON cn.id = pe.src_id AND cn.entity_type = 'client'
                     AND cn.entity_key = 'client:' || %(slug)s
    WHERE pe.rel_type = 'has_product'
),
-- one pgvector distance per intent (embedding computed once, joined in)
intent_dists AS (
    SELECT i.intent_id, MIN(cc.embedding <=> %(intent_vec)s::vector) AS dist
    FROM intents i
    CROSS JOIN LATERAL (SELECT 1) _x
    LEFT JOIN client_contexts cc ON cc.client_id = %(client_id)s
    GROUP BY i.intent_id
),
citations AS (
    SELECT i.intent_id, i.query_text, i.product_key, i.product_label,
           s.entity_key AS site_key, s.label AS site,
           MIN(eng.label) FILTER (WHERE eng.entity_type = 'ai_engine') AS engines
    FROM intents i
    JOIN kg_edges ic  ON ic.src_id = i.intent_id AND ic.rel_type = 'intent_cites_site'
    JOIN kg_nodes s   ON s.id = ic.dst_id AND s.entity_type = 'authority_site'
    LEFT JOIN kg_edges ce ON ce.dst_id = i.intent_id AND ce.rel_type = 'checks_intent'
    LEFT JOIN kg_nodes eng ON eng.id = ce.src_id
    GROUP BY i.intent_id, i.query_text, i.product_key, i.product_label,
             s.entity_key, s.label
)
SELECT c.product_key, c.product_label, c.intent_id, c.intent_key,
       c.query_text, d.dist AS semantic_gap,
       c.site_key, c.site, c.engines
FROM citations c
JOIN intent_dists d ON d.intent_id = c.intent_id
WHERE COALESCE(d.dist, 1.0) > %(gap)s
ORDER BY d.dist DESC
LIMIT %(row_limit)s
"""


def get_visibility_bottlenecks(client_id: int, gap_threshold: float = DEFAULT_GAP_THRESHOLD,
                               limit: int = 5) -> dict:
    """Find the top `limit` distinct optimization bottlenecks: search
    intents reachable from the client's products where AI engines cite
    external authority sites but the client's context docs are semantically
    far (cosine distance > gap_threshold) from the intent. One SQL round
    trip; no iterative Python traversal."""
    if not 0.0 <= gap_threshold <= 2.0:
        return {"error": "gap_threshold must be in [0, 2]"}
    limit = max(1, min(int(limit), 25))

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT slug, name FROM clients WHERE id = %s", (client_id,))
            client = cur.fetchone()
            if not client:
                return {"error": f"no client with id {client_id}"}

            # Embed the client's own product labels + existing intent labels
            # via a single model call is not possible in SQL; instead we embed
            # per-intent in one batched call (lazy model load happens here).
            cur.execute("""
                SELECT DISTINCT n.id AS intent_id, n.label
                FROM kg_edges pe
                JOIN kg_nodes pn ON pn.id = pe.dst_id AND pn.entity_type = 'product'
                JOIN kg_edges pie ON pie.src_id = pn.id AND pie.rel_type = 'product_for_intent'
                JOIN kg_nodes n ON n.id = pie.dst_id AND n.entity_type = 'search_intent'
                JOIN kg_nodes cn ON cn.id = pe.src_id AND cn.entity_type = 'client'
                 AND cn.entity_key = 'client:' || %s
                WHERE pe.rel_type = 'has_product'
            """, (client["slug"],))
            intents = cur.fetchall()
            if not intents:
                return {"client": client["name"], "client_id": client_id,
                        "bottlenecks": [],
                        "note": "no product_for_intent links yet — ingest AI visibility data first"}

            from client_context import embed_texts
            vecs = embed_texts([r["label"] for r in intents])
            intent_vec = mean_vector(vecs)  # one composite query vector

            cur.execute(_BOTTLENECK_SQL, {
                "slug": client["slug"], "client_id": client_id,
                "intent_vec": to_pgvector(intent_vec),
                "gap": gap_threshold, "row_limit": limit * 8,
            })
            rows = cur.fetchall()

    # collapse citations per (intent) in Python — data already fetched
    by_intent: dict[int, dict] = {}
    for r in rows:
        b = by_intent.setdefault(r["intent_id"], {
            "product": r["product_label"], "product_key": r["product_key"],
            "query": r["query_text"], "semantic_gap": round(float(r["semantic_gap"]), 4),
            "cited_authority_sites": [], "engines": set(),
        })
        b["cited_authority_sites"].append(r["site"].removeprefix("site:"))
        if r["engines"]:
            b["engines"].add(r["engines"])
    bottlenecks = sorted(by_intent.values(),
                         key=lambda b: -b["semantic_gap"])[:limit]
    for b in bottlenecks:
        b["cited_authority_sites"] = sorted(set(b["cited_authority_sites"]))[:8]
        b["engines"] = sorted(b["engines"])

    return {
        "client": client["name"], "client_id": client_id,
        "gap_threshold": gap_threshold,
        "intents_evaluated": len(intents),
        "bottlenecks": bottlenecks,
    }


def to_pgvector(vec) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


def mean_vector(vecs) -> list:
    n = len(vecs)
    return [sum(col) / n for col in zip(*vecs)]


# ---------------------------------------------------------------------------
# 2. Content brief — Graph-RAG: 2-hop sub-network + vector context match
# ---------------------------------------------------------------------------

_BRIEF_GRAPH_SQL = """
WITH target AS (
    SELECT n.id, n.label, n.entity_key
    FROM kg_nodes n
    JOIN kg_nodes cn ON cn.entity_key = 'client:' || %(slug)s
                      AND cn.entity_type = 'client'
    JOIN kg_edges hp ON hp.src_id = cn.id AND hp.rel_type = 'has_product'
    WHERE n.entity_type = 'product'
      AND (n.entity_key = 'product:' || %(slug)s || ':' || %(sku)s
           OR lower(n.label) = lower(%(sku)s))
    LIMIT 1
),
hop1_intents AS (   -- hop 1: product -> search intents
    SELECT t.id AS product_id, t.label AS product, t.entity_key AS product_key,
           n.id AS intent_id, n.label AS query_text
    FROM target t
    JOIN kg_edges e1 ON e1.src_id = t.id AND e1.rel_type = 'product_for_intent'
    JOIN kg_nodes n  ON n.id = e1.dst_id AND n.entity_type = 'search_intent'
),
hop2_sites AS (     -- hop 2: intents -> citing engines + authority sites
    SELECT h.product, h.product_key, h.intent_id, h.query_text,
           s.entity_key AS site_key, s.label AS site,
           array_remove(array_agg(DISTINCT eng.label), NULL) AS engines
    FROM hop1_intents h
    LEFT JOIN kg_edges ic  ON ic.src_id = h.intent_id AND ic.rel_type = 'intent_cites_site'
    LEFT JOIN kg_nodes s   ON s.id = ic.dst_id AND s.entity_type = 'authority_site'
    LEFT JOIN kg_edges ce  ON ce.dst_id = h.intent_id AND ce.rel_type = 'checks_intent'
    LEFT JOIN kg_nodes eng ON eng.id = ce.src_id AND eng.entity_type = 'ai_engine'
    GROUP BY h.product, h.product_key, h.intent_id, h.query_text,
             s.entity_key, s.label
)
SELECT product, product_key, intent_id, query_text,
       COALESCE(site, '') AS site,
       engines
FROM hop2_sites
ORDER BY intent_id, site
"""


def generate_content_brief(client_id: int, product_sku: str) -> dict:
    """Graph-RAG brief: locate the product node, 1 hop to search intents,
    2 hops to AI-cited authority sites, plus a local pgvector match against
    client_contexts for brand voice. Returns a Claude-ready text block."""
    sku = (product_sku or "").strip()
    if not sku:
        return {"error": "product_sku is required"}

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT slug, name FROM clients WHERE id = %s", (client_id,))
            client = cur.fetchone()
            if not client:
                return {"error": f"no client with id {client_id}"}
            slug = client["slug"]

            cur.execute(_BRIEF_GRAPH_SQL, {"slug": slug, "sku": sku})
            rows = cur.fetchall()
            if not rows:
                return {"error": (
                    f"no product node matching '{sku}' for client {client['name']} — "
                    "ingest visibility data or add the product edge first")}

            product = rows[0]["product"] or sku

            # Vector side: brand voice + best-matching context per intent.
            cur.execute("""
                SELECT context_type, title, content, metadata
                FROM client_contexts WHERE client_id = %s AND context_type = 'brand_voice'
                ORDER BY ingested_at DESC LIMIT 1
            """, (client_id,))
            voice = cur.fetchone()

            intents = {}
            for r in rows:
                intents.setdefault(r["intent_id"], {
                    "query": r["query_text"], "sites": [], "engines": set()})
                if r["site"]:
                    intents[r["intent_id"]]["sites"].append(r["site"].removeprefix("site:"))
                intents[r["intent_id"]]["engines"].update(r["engines"] or [])

            # semantic match of brand docs to each intent (local model,
            # lazy-loaded on first call)
            from client_context import embed_texts
            intent_list = list(intents.values())
            if intent_list:
                vecs = embed_texts([i["query"] for i in intent_list])
                cur.execute("""
                    SELECT context_type, title, metadata,
                           embedding <=> %s::vector AS dist
                    FROM client_contexts
                    WHERE client_id = %s
                    ORDER BY dist LIMIT 3
                """, (to_pgvector(mean_vector(vecs)), client_id))
                matched = cur.fetchall()
            else:
                matched = []

    lines = [
        f"# CONTENT BRIEF — {client['name']} · {product}",
        "",
        "## Target search intents (what users actually ask)",
    ]
    for i, iv in enumerate(intent_list, 1):
        engines = ", ".join(sorted(iv["engines"])) or "various AI engines"
        sites = ", ".join(sorted(set(iv["sites"]))[:6]) or "(none recorded yet)"
        lines.append(f"{i}. \"{iv['query']}\"  [asked on: {engines}]")
        lines.append(f"   AI engines currently cite: {sites}")

    lines += ["", "## Competitive authority sites to displace"]
    all_sites = sorted({s for iv in intent_list for s in iv["sites"]})
    lines += [f"- {s}" for s in all_sites[:12]] or ["(none recorded)"]

    if voice:
        lines += ["", f"## Brand voice ({voice['title'] or voice['context_type']})", "", voice["content"]]
    if matched:
        lines += ["", "## Closest internal context docs (by semantic match)"]
        for m in matched:
            lines.append(f"- {m['title'] or '(untitled)'} [{m['context_type']}] "
                         f"(cosine distance {float(m['dist']):.3f})")

    lines += [
        "",
        "## Rewrite directive",
        f"Write content that directly answers the intents above in {client['name']}'s "
        f"voice, structured for AI-engine citation (clear headings, direct answers, "
        f"first-hand expertise signals) so ChatGPT/Perplexity/Google AI Mode cite "
        f"{client['name']} instead of: {', '.join(all_sites[:5]) or 'competitors'}.",
    ]

    return {
        "client": client["name"], "client_id": client_id,
        "product": product, "product_key": rows[0]["product_key"],
        "intents": len(intent_list),
        "authority_sites": all_sites[:20],
        "brief_markdown": "\n".join(lines),
    }


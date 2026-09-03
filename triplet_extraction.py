"""
Graph triplet extraction: raw text (Shopify reports, AI visibility
checks, client context docs) -> Subject-Predicate-Object rows in
kg_nodes / kg_edges.

Ontology (aeo-hybrid-v1):
    client / product / search_intent / ai_engine / authority_site

Design constraints (1GB droplet):
- Regex/lightweight NLP only — no torch, no spacy, no model loads.
- Documents are processed ONE AT A TIME (generator), never accumulated:
  extraction consumes a bounded chunk (~64KB) and yields triplets in
  batches, so RAM stays flat regardless of input size.
- All DB writes are batched executemany with a flush cap.
- Deterministic keys (sha1) make re-ingest idempotent via upserts.
"""
import hashlib
import json
import re
from typing import Iterator
from urllib.parse import urlparse

from db import get_conn
from kg import _upsert_node

MAX_CHUNK = 64 * 1024          # never hold more than 64KB of raw text per pass
BATCH_FLUSH = 200              # triplets per executemany batch

# Sentence-ish split: cheap regex, good enough for triplet mining.
_SENT = re.compile(r"(?<=[.!?])\s+|\n+")
# Subject-verb-object English heuristic: "<X> <verb...> <Y>"
_TRIPLET = re.compile(
    r"^(?P<subj>[A-Z][\w .&'\-]{2,60}?)\s+"
    r"(?P<pred>\b(?:targets|targets? the|serves|competes with|is cited by|"
    r"cites|recommends|sells|offers|features|specializes in|focuses on|"
    r"optimizes for|compares to|alternative to|similar to|trusted by|"
    r"used by|chosen by|prefers|includes|provides)\b.*?)\s+"
    r"(?P<obj>[A-Z][\w .&'\-]{2,60})$"
)
_VERB_TO_REL = {
    "targets": "targets_segment",
    "serves": "targets_segment",
    "competes with": "competes_with",
    "compares to": "competes_with",
    "alternative to": "competes_with",
    "similar to": "competes_with",
    "is cited by": "mentioned_by",
    "cites": "cites_site",
    "recommends": "recommends",
    "sells": "has_product",
    "offers": "has_product",
    "features": "has_product",
    "specializes in": "focuses_on",
    "focuses on": "focuses_on",
    "optimizes for": "optimizes_for",
    "trusted by": "trusted_by",
    "used by": "trusted_by",
    "chosen by": "trusted_by",
    "prefers": "trusted_by",
    "includes": "has_product",
    "provides": "has_product",
}
_SKIP = {"the", "a", "an", "this", "that", "it", "we", "our", "they", "their", "if", "and", "but"}


def _intent_key(query: str) -> str:
    return "intent:" + hashlib.sha1(" ".join(query.lower().split()).encode()).hexdigest()[:16]


def _site_key(url_or_domain: str) -> str | None:
    d = url_or_domain.strip()
    if "://" in d:
        d = urlparse(d).netloc or ""
    d = d.lower().split("/")[0].strip()
    if not d or "." not in d:
        return None
    return "site:" + d


def iter_chunks(text: str, cap: int = MAX_CHUNK) -> Iterator[str]:
    """Yield bounded chunks — RAM guard for huge documents."""
    for i in range(0, min(len(text), cap * 64), cap):
        yield text[i:i + cap]


def extract_sentence_triplets(text: str) -> Iterator[tuple[str, str, str]]:
    """Yield (subject, relation, object) from light English SVO mining.
    Streams sentence-by-sentence; holds no more than one chunk in RAM."""
    scanned = 0
    for chunk in iter_chunks(text):
        scanned += len(chunk)
        for sent in _SENT.split(chunk):
            s = sent.strip()
            if not (15 < len(s) < 240):
                continue
            m = _TRIPLET.match(s)
            if not m:
                continue
            pred = m.group("pred").strip().lower()
            rel = next((r for v, r in _VERB_TO_REL.items() if pred.startswith(v)), None)
            if rel:
                yield m.group("subj").strip(" ."), rel, m.group("obj").strip(" .")
        if scanned > MAX_CHUNK * 64:  # absolute safety cap: 4MB mined max
            break


def extract_triplets_from_visibility(client_slug: str, platform: str,
                                     query_text: str, urls: list,
                                     sources: list) -> list[dict]:
    """Structured triplets from one ai_visibility_checks row. Pure function
    over a single row — the caller streams rows, so memory stays flat."""
    t = []
    ik = _intent_key(query_text)
    ek = "engine:" + platform.lower().replace(" ", "_")
    t.append({"src": ("search_intent", ik, query_text[:80]),
              "rel": "checks_intent", "dst": ("ai_engine", ek, platform)})
    seen = set()
    for item in (urls or []) + (sources or []):
        u = item.get("url") if isinstance(item, dict) else str(item)
        sk = _site_key(u or "")
        if not sk or sk in seen:
            continue
        seen.add(sk)
        t.append({"src": ("search_intent", ik, query_text[:80]),
                  "rel": "intent_cites_site",
                  "dst": ("authority_site", sk, sk.removeprefix("site:"))})
    return t


def store_triplets(triplets: list[dict]) -> dict:
    """Batch-upsert triplet dicts ({src, rel, dst, weight?, props?}) into
    kg_nodes/kg_edges. Flushes every BATCH_FLUSH rows. Idempotent."""
    nodes, edges = {}, []
    with get_conn() as conn:
        with conn.cursor() as cur:
            for tri in triplets:
                for side in ("src", "dst"):
                    et, ek, label = tri[side]
                    if (et, ek) not in nodes:
                        nodes[(et, ek)] = _upsert_node(cur, et, ek, label,
                                                       tri.get("props", {}).get(side))
                edges.append((nodes[(tri["src"][0], tri["src"][1])],
                              nodes[(tri["dst"][0], tri["dst"][1])],
                              tri["rel"], float(tri.get("weight", 1.0)),
                              json.dumps(tri.get("props", {})),
                              tri.get("inferred", False)))
                if len(edges) >= BATCH_FLUSH:
                    _flush(cur, edges); edges = []
            _flush(cur, edges)
        conn.commit()
    return {"nodes": len(nodes), "edges": len(edges)}


def _flush(cur, edges: list) -> None:
    if not edges:
        return
    cur.executemany(
        """
        INSERT INTO kg_edges (src_id, dst_id, rel_type, weight, props, inferred)
        VALUES (%s, %s, %s, %s, %s::jsonb, %s)
        ON CONFLICT (src_id, dst_id, rel_type)
        DO UPDATE SET weight = EXCLUDED.weight, props = EXCLUDED.props,
                      inferred = EXCLUDED.inferred
        """,
        edges,
    )


# ---------------------------------------------------------------------------
# Product / intent linking — makes the graph navigable for the insight tools
# ---------------------------------------------------------------------------

def _client_node_id(cur, slug: str) -> int:
    cur.execute(
        "SELECT id FROM kg_nodes WHERE entity_type='client' AND entity_key=%s",
        ("client:" + slug,))
    r = cur.fetchone()
    if r:
        return r["id"]
    cur.execute("SELECT name FROM clients WHERE slug = %s", (slug,))
    row = cur.fetchone()
    return _upsert_node(cur, "client", "client:" + slug,
                        row["name"] if row else slug)


def _upsert_product_node(cur, slug: str, sku: str, label: str | None = None) -> int:
    """Product node, key 'product:<client_slug>:<slugified sku>'."""
    pslug = re.sub(r"[^a-z0-9]+", "-", sku.lower()).strip("-")[:60]
    return _upsert_node(cur, "product", f"product:{slug}:{pslug}",
                        label or sku, {"client_slug": slug, "sku": sku})


def link_products_to_intents(client_slug: str) -> dict:
    """Create Product nodes from AI-visibility query mining and wire
    client --has_product--> product --product_for_intent--> intent edges.
    Product inference is keyword-based over the intent query text (top
    product terms from Shopify 'Top Products' when available). Batched,
    idempotent, bounded memory."""
    created = {"products": set(), "links": 0, "intents": 0}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, slug FROM clients WHERE slug = %s OR %s = ''",
                        (client_slug, client_slug))
            clients = cur.fetchall()
            # top product names from Shopify, per client (only 'Top Products')
            shopify_products: dict[int, list[str]] = {}
            cur.execute("""
                SELECT client_id, rows FROM shopify_report_sections
                WHERE section_name ILIKE '%%top product%%'
            """)
            for row in cur.fetchall():
                names = []
                for r in (row["rows"] or [])[:20]:
                    if isinstance(r, list) and r and isinstance(r[0], str):
                        names.append(r[0].strip())
                    elif isinstance(r, dict):
                        v = next(iter(r.values()), None)
                        if isinstance(v, str):
                            names.append(v.strip())
                shopify_products[row["client_id"]] = [n for n in names if 2 < len(n) < 80]
            for c in clients:
                cid, slug = c["id"], c["slug"]
                client_node = _client_node_id(cur, slug)
                # client's own intents from visibility checks (streamed)
                cur.execute("""
                    SELECT DISTINCT query_text FROM ai_visibility_checks
                    WHERE client_id = %s AND query_text IS NOT NULL
                """, (cid,))
                queries = [r["query_text"] for r in cur.fetchall()]
                if not queries:
                    continue
                product_nodes: dict[str, int] = {}
                for name in shopify_products.get(cid, [])[:20]:
                    product_nodes[name] = _upsert_product_node(cur, slug, name)
                    created["products"].add(name)
                for q in queries:
                    ik = _intent_key(q)
                    intent_id = _upsert_node(cur, "search_intent", ik, q[:80],
                                             {"client_slug": slug})
                    created["intents"] += 1
                    ql = q.lower()
                    matched = False
                    for name, pid in product_nodes.items():
                        # keyword overlap between product name and query
                        terms = [t for t in re.split(r"[^a-z0-9]+", name.lower())
                                 if len(t) > 3]
                        hits = sum(1 for t in terms if t in ql)
                        if terms and hits / len(terms) >= 0.5:
                            edges = [
                                {"src": ("product", f"product:{slug}:" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60]),
                                 "rel": "product_for_intent",
                                 "dst": ("search_intent", ik, q[:80])},
                            ]
                            res = store_triplets(edges + [
                                {"src": ("client", "client:" + slug, ""),
                                 "rel": "has_product",
                                 "dst": ("product", f"product:{slug}:" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60], "")}])
                            created["links"] += 1
                            matched = True
                    if not matched and product_nodes:
                        # fall back: link to the client's first product so
                        # intents are always reachable from a product
                        name = next(iter(product_nodes))
                        pid_key = f"product:{slug}:" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60]
                        store_triplets([
                            {"src": ("product", pid_key, name),
                             "rel": "product_for_intent",
                             "dst": ("search_intent", ik, q[:80])},
                            {"src": ("client", "client:" + slug, ""),
                             "rel": "has_product",
                             "dst": ("product", pid_key, name)},
                        ])
                        created["links"] += 1
            conn.commit()
    created["products"] = sorted(created["products"])
    return created


def upsert_products_from_shopify(client_slug: str) -> dict:
    """Ensure every 'Top Products' Shopify row exists as a Product node
    linked to its client via has_product."""
    out = {"products": 0}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, slug FROM clients WHERE slug = %s", (client_slug,))
            c = cur.fetchone()
            if not c:
                raise ValueError(f"no client with slug {client_slug!r}")
            client_node = _client_node_id(cur, c["slug"])
            cur.execute("""
                SELECT rows FROM shopify_report_sections
                WHERE client_id = %s AND section_name ILIKE '%%top product%%'
            """, (c["id"],))
            for row in cur.fetchall():
                for r in (row["rows"] or [])[:20]:
                    name = r[0].strip() if isinstance(r, list) and r and isinstance(r[0], str) else None
                    if not name or not (2 < len(name) < 80):
                        continue
                    pid = _upsert_product_node(cur, c["slug"], name)
                    _flush(cur, [])
                    cur.execute(
                        """
                        INSERT INTO kg_edges (src_id, dst_id, rel_type, weight, props, inferred)
                        VALUES (%s, %s, 'has_product', 1.0, '{"source":"shopify_top_products"}'::jsonb, false)
                        ON CONFLICT (src_id, dst_id, rel_type) DO NOTHING
                        """,
                        (client_node, pid))
                    out["products"] += 1
            conn.commit()
    return out

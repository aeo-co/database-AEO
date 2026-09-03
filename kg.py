"""Knowledge-graph layer over kg_nodes / kg_edges.

Nodes mirror existing entities (clients, client_contexts, ...) via
(entity_type, entity_key) — the source tables stay the source of truth.
Edges are either asserted (inferred=false) or computed from embedding
similarity (inferred=true, safe to delete and recompute).

All traversals are plain SQL (recursive CTEs) — no graph extension needed.
"""

import json
from typing import Optional

from db import get_conn

# entity_type -> (source table, key expression, label expression)
NODE_SOURCES = {
    "client": ("clients", "id", "name"),
    "context": ("client_contexts", "id", "title"),
}

VALID_NODE_TYPES = set(NODE_SOURCES) | {
    "campaign", "case_study", "topic", "custom",
    # aeo-hybrid-v1 ontology
    "product", "search_intent", "ai_engine", "authority_site",
}


def _upsert_node(cur, entity_type: str, entity_key: str, label: str,
                 props: Optional[dict] = None) -> int:
    """Insert or return existing node id. Returns kg_nodes.id."""
    if entity_type not in VALID_NODE_TYPES:
        raise ValueError(f"unknown entity_type: {entity_type!r}")
    cur.execute(
        """
        INSERT INTO kg_nodes (entity_type, entity_key, label, props)
        VALUES (%s, %s, %s, %s::jsonb)
        ON CONFLICT (entity_type, entity_key)
        DO UPDATE SET label = EXCLUDED.label, props = EXCLUDED.props
        RETURNING id
        """,
        (entity_type, entity_key, label, json.dumps(props or {})),
    )
    return cur.fetchone()["id"]


def upsert_node(entity_type: str, entity_key: str, label: str,
                props: Optional[dict] = None) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            return _upsert_node(cur, entity_type, entity_key, label, props)


def add_edge(src_node_id: int, dst_node_id: int, rel_type: str,
             weight: float = 1.0, props: Optional[dict] = None,
             inferred: bool = False) -> int:
    """Create or refresh a directed edge. Returns kg_edges.id."""
    if src_node_id == dst_node_id:
        raise ValueError("self-edges are not allowed")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO kg_edges (src_id, dst_id, rel_type, weight, props, inferred)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s)
                ON CONFLICT (src_id, dst_id, rel_type)
                DO UPDATE SET weight = EXCLUDED.weight,
                              props = EXCLUDED.props,
                              inferred = EXCLUDED.inferred
                RETURNING id
                """,
                (src_node_id, dst_node_id, rel_type, weight,
                 json.dumps(props or {}), inferred),
            )
            return cur.fetchone()["id"]


def add_edge_by_key(src_type: str, src_key: str, dst_type: str, dst_key: str,
                    rel_type: str, weight: float = 1.0,
                    props: Optional[dict] = None, inferred: bool = False) -> dict:
    """Resolve both endpoints from (entity_type, entity_key), then add_edge.
    Missing nodes are auto-created with a placeholder label (caller should
    upsert the node first when a nice label is available)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM kg_nodes WHERE entity_type=%s AND entity_key=%s",
                (src_type, src_key))
            src = cur.fetchone()
            if src is None:
                raise ValueError(f"no node {src_type}:{src_key}")
            cur.execute(
                "SELECT id FROM kg_nodes WHERE entity_type=%s AND entity_key=%s",
                (dst_type, dst_key))
            dst = cur.fetchone()
            if dst is None:
                raise ValueError(f"no node {dst_type}:{dst_key}")
            edge_id = add_edge(src["id"], dst["id"], rel_type, weight, props, inferred)
            return {"edge_id": edge_id, "src_id": src["id"], "dst_id": dst["id"]}


def get_related(entity_type: str, entity_key: str, rel_type: Optional[str] = None,
                depth: int = 1, limit: int = 50) -> dict:
    """Traverse from a node up to `depth` hops via recursive CTE.
    Returns {root, paths: [{node..., hops, via_rel_type}], edges}."""
    if depth < 1 or depth > 4:
        raise ValueError("depth must be 1..4")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, label FROM kg_nodes WHERE entity_type=%s AND entity_key=%s",
                (entity_type, entity_key))
            root = cur.fetchone()
            if root is None:
                raise ValueError(f"no node {entity_type}:{entity_key}")
            rel_filter = "AND e.rel_type = %s" if rel_type else ""
            rel_filter_rev = rel_filter  # same filter on the reverse branch
            if rel_type:
                params = [root["id"], rel_type, root["id"], rel_type, depth, limit]
            else:
                params = [root["id"], root["id"], depth, limit]
            cur.execute(
                f"""
                WITH RECURSIVE walk AS (
                    SELECT e.dst_id AS node_id, e.rel_type AS via, 1 AS hops,
                           ARRAY[e.id] AS edge_path
                    FROM kg_edges e
                    WHERE e.src_id = %s::bigint {rel_filter}
                    UNION
                    SELECT e.src_id, e.rel_type, 1, ARRAY[e.id]
                    FROM kg_edges e
                    WHERE e.dst_id = %s::bigint {rel_filter_rev}
                    UNION
                    SELECT CASE WHEN e.src_id = w.node_id THEN e.dst_id ELSE e.src_id END,
                           e.rel_type, w.hops + 1, w.edge_path || e.id
                    FROM kg_edges e
                    JOIN walk w ON (e.src_id = w.node_id OR e.dst_id = w.node_id)
                    WHERE w.hops < %s
                      AND NOT e.id = ANY(w.edge_path)
                )
                SELECT DISTINCT ON (n.id)
                       n.entity_type, n.entity_key, n.label,
                       w.hops, w.via
                FROM walk w JOIN kg_nodes n ON n.id = w.node_id
                ORDER BY n.id, w.hops
                LIMIT %s
                """,
                params,
            )
            paths = cur.fetchall()
            cur.execute(
                """SELECT count(*) AS n FROM kg_edges
                   WHERE src_id=%s OR dst_id=%s""", (root["id"], root["id"]))
            total = cur.fetchone()["n"]
            return {
                "root": {"id": root["id"], "entity_type": entity_type,
                         "entity_key": entity_key, "label": root["label"]},
                "related": paths,
                "total_edges_on_root": total,
            }


def refresh_similar_edges(threshold: float = 0.85,
                          context_type: str = "icp",
                          max_pairs_per_client: int = 5) -> dict:
    """Recompute inferred 'similar_to' edges between clients by comparing
    their context embeddings (cosine). Deletes previously inferred edges of
    this rel_type, then inserts fresh ones above the threshold.

    Uses the nearest context per client pair (min distance across any
    embedding pair of the two clients' contexts of context_type).
    """
    if not 0.5 <= threshold < 1.0:
        raise ValueError("threshold must be in [0.5, 1.0); 1.0 = identical")
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Only clients that actually have embeddings for this context type
            cur.execute(
                """
                SELECT DISTINCT c.id, c.slug, c.name
                FROM clients c
                JOIN client_contexts cc ON cc.client_id = c.id
                WHERE cc.context_type = %s AND cc.embedding IS NOT NULL
                """,
                (context_type,),
            )
            clients = cur.fetchall()
            if len(clients) < 2:
                return {"deleted": 0, "created": 0,
                        "pairs": [], "clients_scanned": len(clients)}

            id_by_slug = {c["slug"]: c for c in clients}

            # ensure nodes exist
            node_id_by_client = {}
            for c in clients:
                cur.execute(
                    "SELECT id FROM kg_nodes WHERE entity_type='client' AND entity_key=%s",
                    (f"client:{c['slug']}",))
                row = cur.fetchone()
                if row:
                    node_id_by_client[c["slug"]] = row["id"]
                else:
                    node_id_by_client[c["slug"]] = _upsert_node(
                        cur, "client", f"client:{c['slug']}", c["name"],
                        {"context_type": context_type})

            # best (min) distance per pair, over embedding pairs
            cur.execute(
                """
                SELECT a.client_id AS a_id, b.client_id AS b_id,
                       MIN(a.embedding <=> b.embedding) AS best_dist
                FROM client_contexts a
                JOIN client_contexts b
                  ON a.client_id < b.client_id
                 AND b.context_type = a.context_type
                WHERE a.context_type = %s
                  AND a.embedding IS NOT NULL AND b.embedding IS NOT NULL
                GROUP BY a.client_id, b.client_id
                HAVING MIN(a.embedding <=> b.embedding) < %s
                ORDER BY best_dist
                LIMIT 500
                """,
                (context_type, 1.0 - threshold),
            )
            pairs = cur.fetchall()

            # map client_id -> slug for node lookup
            slug_by_id = {c["id"]: c["slug"] for c in clients}

            cur.execute("DELETE FROM kg_edges WHERE rel_type='similar_to' AND inferred=true")
            deleted = cur.rowcount

            created = []
            for p in pairs:
                s_a = slug_by_id.get(p["a_id"])
                s_b = slug_by_id.get(p["b_id"])
                if not s_a or not s_b:
                    continue
                edge_id = add_edge(
                    node_id_by_client[s_a], node_id_by_client[s_b],
                    "similar_to", weight=round(1.0 - p["best_dist"], 4),
                    props={"context_type": context_type,
                           "cosine_distance": round(p["best_dist"], 4)},
                    inferred=True)
                created.append({
                    "a": s_a, "b": s_b,
                    "similarity": round(1.0 - p["best_dist"], 4),
                    "edge_id": edge_id,
                })
            return {
                "deleted": deleted,
                "created": len(created),
                "pairs": created[:max_pairs_per_client],
                "clients_scanned": len(clients),
            }


def backfill_nodes(dry_run: bool = False) -> dict:
    """Mirror existing clients and client_contexts into kg_nodes (idempotent)."""
    results = {}
    with get_conn() as conn:
        with conn.cursor() as cur:
            for etype, (table, keycol, labelcol) in NODE_SOURCES.items():
                if etype == "client":
                    cur.execute(f"SELECT id, {labelcol} AS label FROM {table} WHERE slug IS NOT NULL")
                    rows = cur.fetchall()
                    keyed = [("client", f"client:{r['label'].lower().replace(' ', '-')}", r["label"]) for r in rows]
                    # prefer slug-based keys; fetch slugs directly
                    cur.execute("SELECT id, slug, name FROM clients WHERE slug IS NOT NULL")
                    rows = cur.fetchall()
                    keyed = [("client", f"client:{r['slug']}", r["name"]) for r in rows]
                else:
                    cur.execute(f"SELECT id, COALESCE(NULLIF(title,''), 'untitled') AS label FROM {table}")
                    rows = cur.fetchall()
                    keyed = [(etype, f"{etype}:{r['id']}", r["label"]) for r in rows]
                results[etype] = {"count": len(keyed), "dry_run": dry_run}
                if dry_run:
                    continue
                for et, key, label in keyed:
                    _upsert_node(cur, et, key, label)
                # link context nodes to their client
                if etype == "context":
                    cur.execute(
                        """
                        INSERT INTO kg_edges (src_id, dst_id, rel_type, inferred, props)
                        SELECT cn.id, kn.id, 'belongs_to', true, '{}'::jsonb
                        FROM client_contexts cc
                        JOIN kg_nodes cn ON cn.entity_type='context' AND cn.entity_key='context:' || cc.id::text
                        JOIN clients c ON c.id = cc.client_id
                        JOIN kg_nodes kn ON kn.entity_type='client' AND kn.entity_key='client:' || c.slug
                        ON CONFLICT (src_id, dst_id, rel_type) DO NOTHING
                        """)
                    results["context_belongs_to_edges"] = cur.rowcount
        if dry_run:
            conn.rollback()
    return results


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="KG utilities")
    p.add_argument("cmd", choices=["backfill", "refresh-similar"])
    p.add_argument("--threshold", type=float, default=0.85)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    if args.cmd == "backfill":
        print(backfill_nodes(dry_run=args.dry_run))
    else:
        print(refresh_similar_edges(threshold=args.threshold))

"""
Shared embedding + upsert helpers for client brand context.

One whole markdown document per row (no fragmentation). Embedding is
generated at ingest time with a LOCAL sentence-transformers model
(all-MiniLM-L6-v2, 384-dim) running on the server — no API key, no
external calls. The same helper is used by both the CLI script and the
MCP tool so they never drift.

Env:
    DATABASE_URL or DB_* vars   (see db.py)
    EMBEDDING_MODEL             HF model id; default: all-MiniLM-L6-v2
"""
import os

from pgvector.psycopg import register_vector
from psycopg.types.json import Jsonb

from db import get_conn

EMBEDDING_DIM = 384  # all-MiniLM-L6-v2

_model = None


def _get_model():
    """Lazy-load the sentence-transformers model once per process."""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        name = os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
        _model = SentenceTransformer(name)
    return _model


def embed_text(text: str) -> list[float]:
    """Embed one document with the local model. No chunking — MiniLM
    handles up to 256 word-piece tokens per pass; long docs are mean-
    pooled by the model itself."""
    return _get_model().encode(text, normalize_embeddings=True).tolist()


def embed_texts(texts: list[str], batch_size: int = 32) -> list[list[float]]:
    """Embed many strings in one batched model pass (still ONE lazy model
    load; batches keep peak RAM bounded on the 1GB droplet)."""
    if not texts:
        return []
    return _get_model().encode(texts, batch_size=batch_size,
                               normalize_embeddings=True).tolist()


def upsert_client_context(
    client_id: int,
    context_type: str,
    content: str,
    title: str | None = None,
    metadata: dict | None = None,
    source_file: str | None = None,
) -> dict:
    """Embed content and upsert one client_contexts row.

    Unique key is (client_id, context_type, COALESCE(title, '')) —
    re-ingesting the same client+type+title replaces the doc and
    refreshes the embedding instead of duplicating.
    """
    if not content or not content.strip():
        raise ValueError("content is empty")
    title = (title or "").strip() or None
    metadata = metadata or {}
    vec = embed_text(content)

    with get_conn() as conn:
        register_vector(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO client_contexts
                    (client_id, context_type, title, content, embedding, metadata, source_file)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (client_id, context_type, COALESCE(title, ''))
                DO UPDATE SET
                    content   = EXCLUDED.content,
                    embedding = EXCLUDED.embedding,
                    metadata  = EXCLUDED.metadata,
                    source_file = EXCLUDED.source_file,
                    ingested_at = now()
                RETURNING id,
                          (xmax = 0) AS inserted;
                """,
                (client_id, context_type, title, content, vec, Jsonb(metadata), source_file),
            )
            row = cur.fetchone()
    return {
        "id": row["id"],
        "inserted": bool(row["inserted"]),  # False = replaced existing row
        "client_id": client_id,
        "context_type": context_type,
        "title": title,
    }


def fetch_client_context(client_id: int, context_type: str) -> list[dict]:
    """Full markdown docs for one client + context type, newest first."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, context_type, title, content, metadata, source_file, ingested_at
                FROM client_contexts
                WHERE client_id = %s AND context_type = %s
                ORDER BY ingested_at DESC, id DESC;
                """,
                (client_id, context_type),
            )
            rows = cur.fetchall()
    for r in rows:
        r["ingested_at"] = r["ingested_at"].isoformat()
    return rows


def search_client_context(client_id: int, query: str, top_k: int = 5) -> list[dict]:
    """Semantic search over one client's context docs by cosine distance."""
    vec = embed_text(query)
    with get_conn() as conn:
        register_vector(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, context_type, title, content,
                       1 - (embedding <=> %s::vector) AS score
                FROM client_contexts
                WHERE client_id = %s
                ORDER BY embedding <=> %s::vector
                LIMIT %s;
                """,
                (vec, client_id, vec, top_k),
            )
            rows = cur.fetchall()
    for r in rows:
        r["score"] = float(r["score"])
    return rows

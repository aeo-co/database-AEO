"""
Shared embedding + upsert helpers for client brand context.

One whole markdown document per row (no fragmentation). Embedding is
generated at ingest time via OpenAI text-embedding-3-small (1536-dim);
the same helper is used by both the CLI script and the MCP tool so they
never drift.

Env:
    DATABASE_URL or DB_* vars   (see db.py)
    EMBEDDING_API_KEY           OpenAI key; falls back to OPENAI_API_KEY
    EMBEDDING_MODEL             default: text-embedding-3-small
"""
import os

import openai
from pgvector.psycopg import register_vector
from psycopg.types.json import Jsonb

from db import get_conn

EMBEDDING_DIM = 1536  # text-embedding-3-small


def get_embedding_client() -> "openai.OpenAI":
    api_key = os.getenv("EMBEDDING_API_KEY") or os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Set EMBEDDING_API_KEY (or OPENAI_API_KEY) in the environment / .env"
        )
    return openai.OpenAI(api_key=api_key)


def embed_text(text: str) -> list[float]:
    """Embed one document with the configured model. No chunking — the
    whole doc goes in one call (8191-token input cap on 3-small)."""
    model = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
    client = get_embedding_client()
    resp = client.embeddings.create(model=model, input=text)
    return resp.data[0].embedding


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

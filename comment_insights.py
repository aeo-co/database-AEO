"""
comment_insights.py - aggregation layer over research_comment_analysis
(see analyze_comments.py): consolidates noisy LLM-generated topic tags
into clean themes, and finds content gaps by comparing real extracted
questions against each client's existing context docs.

Same shape as insights.py - additive, read-only over existing data, no
new tables, safe to re-run any time. The content-gap finder is the same
semantic-gap idea as insights.get_visibility_bottlenecks (embed, compare
against client_contexts via cosine distance, flag what's far), just fed
by real organic questions instead of synthetic AI-engine search intents.

No torch/sentence-transformers import at module level - client_context's
embed_texts() lazy-loads the model on first call only (same OOM safety
as insights.py, 1GB droplet).
"""
import statistics

from db import get_conn
from insights import to_pgvector

DEFAULT_CLUSTER_THRESHOLD = 0.75
DEFAULT_GAP_PERCENTILE = 70  # top 30% most-distant-for-THIS-client become gap candidates


def _resolve_client(cur, client_slug: str) -> dict:
    cur.execute("SELECT id, name FROM clients WHERE slug = %s", (client_slug,))
    client = cur.fetchone()
    if not client:
        raise ValueError(f"no client matching '{client_slug}'")
    return client


def cluster_topics(client_slug: str, threshold: float = DEFAULT_CLUSTER_THRESHOLD,
                   limit: int = 20) -> dict:
    """Group this client's raw LLM-generated topic tags ('price',
    'pricing', 'cost') into clean themes by embedding similarity.

    A greedy pass, not a clustering library: tags sorted by frequency,
    each unclaimed tag starts a new cluster (labeled with itself, since
    it's the most frequent unclaimed tag left) and absorbs any
    remaining tag within `threshold` cosine similarity of it. Cheap -
    this runs over at most a few hundred distinct tags per client, not
    per-comment, so no need for a heavier clustering approach."""
    from client_context import embed_texts

    with get_conn() as conn:
        with conn.cursor() as cur:
            client = _resolve_client(cur, client_slug)
            cur.execute(
                """
                SELECT t.tag, count(*) AS n
                FROM research_comment_analysis a,
                     jsonb_array_elements_text(a.topics) AS t(tag)
                WHERE a.client_id = %s
                GROUP BY t.tag
                """,
                (client["id"],),
            )
            counts = {r["tag"]: r["n"] for r in cur.fetchall()}

    if not counts:
        return {
            "client": client["name"], "client_slug": client_slug,
            "cluster_count": 0, "clusters": [],
            "note": "no topics yet - run analyze_comments.py first",
        }

    tags = sorted(counts, key=lambda t: -counts[t])
    vecs = embed_texts(tags)

    clusters = []
    claimed = [False] * len(tags)
    for i, tag in enumerate(tags):
        if claimed[i]:
            continue
        claimed[i] = True
        members = [(tag, counts[tag])]
        for j in range(i + 1, len(tags)):
            if claimed[j]:
                continue
            # vectors are normalized (embed_texts uses normalize_embeddings=True),
            # so dot product == cosine similarity - no need for norms here
            sim = sum(a * b for a, b in zip(vecs[i], vecs[j]))
            if sim >= threshold:
                claimed[j] = True
                members.append((tags[j], counts[tags[j]]))
        clusters.append({
            "label": tag,
            "total_count": sum(c for _, c in members),
            "member_tags": [{"tag": t, "count": c} for t, c in members],
        })

    clusters.sort(key=lambda c: -c["total_count"])
    return {
        "client": client["name"],
        "client_slug": client_slug,
        "cluster_count": len(clusters),
        "clusters": clusters[:limit],
    }


def get_comment_content_gaps(client_slug: str, percentile: float = DEFAULT_GAP_PERCENTILE,
                             limit: int = 10, max_questions: int = None) -> dict:
    """Real questions extracted from Reddit/YouTube comments, compared
    against this client's existing context docs (brand voice, FAQs,
    case studies, ...) - surfaces the ones worth writing content for.

    Gaps are identified RELATIVE to this client's own distance
    distribution, not a fixed absolute cosine-distance cutoff: what
    counts as "far from existing content" depends on how much content
    exists and how it's written, which varies a lot between clients (a
    fixed threshold borrowed from a different comparison - AI-engine
    search intents vs brand docs - flagged nearly every single question
    as a "gap" here, since even the closest real match sat well above
    that threshold). A question in the top (100 - percentile)%
    most-distant for THIS client is a gap candidate.

    Among candidates, results are ranked by comment engagement (score)
    first, then distance - sorting by raw distance alone surfaces the
    single most random/off-topic outlier (a joke comment, an unrelated
    aside) ahead of a popular, clearly on-topic question that just
    happens to be slightly less far. Each question is compared
    individually (not pooled into one composite vector like
    get_visibility_bottlenecks does), since each is a concrete,
    directly actionable real question rather than a synthetic intent.

    max_questions caps how many of this client's extracted questions get
    evaluated, picking the highest-scored (most-upvoted) comments first -
    use it to keep a very large backlog fast; omit to evaluate all of
    them, which is the correct default for not missing a real gap."""
    from client_context import embed_texts

    with get_conn() as conn:
        with conn.cursor() as cur:
            client = _resolve_client(cur, client_slug)
            client_id = client["id"]

            cur.execute(
                "SELECT count(*) AS n FROM client_contexts WHERE client_id = %s",
                (client_id,),
            )
            if cur.fetchone()["n"] == 0:
                return {
                    "client": client["name"], "client_slug": client_slug,
                    "questions_evaluated": 0, "gaps": [],
                    "note": ("client has no context docs yet - every question would "
                             "show up as a maximal gap by default, which isn't real "
                             "signal. Ingest brand voice / FAQs / case studies first."),
                }

            sql = """
                SELECT a.question_text, rc.platform, rc.score
                FROM research_comment_analysis a
                JOIN research_comments rc ON rc.id = a.comment_id
                WHERE a.client_id = %(cid)s AND a.is_question
                  AND a.question_text IS NOT NULL
                ORDER BY rc.score DESC NULLS LAST
            """
            params = {"cid": client_id}
            if max_questions:
                sql += " LIMIT %(max_q)s"
                params["max_q"] = max_questions
            cur.execute(sql, params)
            questions = cur.fetchall()

    if not questions:
        return {
            "client": client["name"], "client_slug": client_slug,
            "questions_evaluated": 0, "gaps": [],
            "note": "no questions extracted yet - run analyze_comments.py first",
        }

    vecs = embed_texts([q["question_text"] for q in questions])

    evaluated = []
    with get_conn() as conn:
        with conn.cursor() as cur:
            for q, vec in zip(questions, vecs):
                pgvec = to_pgvector(vec)
                cur.execute(
                    """
                    SELECT title, context_type, embedding <=> %s::vector AS dist
                    FROM client_contexts WHERE client_id = %s
                    ORDER BY dist LIMIT 1
                    """,
                    (pgvec, client_id),
                )
                closest = cur.fetchone()
                evaluated.append({
                    "question": q["question_text"],
                    "platform": q["platform"],
                    "comment_score": q["score"] or 0,
                    "semantic_gap": round(float(closest["dist"]), 4),
                    "closest_existing_doc": closest["title"] or closest["context_type"],
                })

    distances = [e["semantic_gap"] for e in evaluated]
    cutoff = (statistics.quantiles(distances, n=100)[int(percentile) - 1]
              if len(distances) >= 2 else 0.0)

    gaps = [e for e in evaluated if e["semantic_gap"] >= cutoff]
    gaps.sort(key=lambda g: (-g["comment_score"], -g["semantic_gap"]))

    return {
        "client": client["name"],
        "client_slug": client_slug,
        "questions_evaluated": len(questions),
        "percentile": percentile,
        "distance_cutoff": round(cutoff, 4),
        "gaps_found": len(gaps),
        "gaps": gaps[:limit],
    }


if __name__ == "__main__":
    import argparse
    import json

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("cmd", choices=["topics", "gaps"])
    p.add_argument("--client", required=True, help="client slug")
    p.add_argument("--threshold", type=float, default=None,
                  help="(topics only) cluster similarity threshold, 0-1")
    p.add_argument("--percentile", type=float, default=None,
                  help="(gaps only) top (100-percentile)%% most-distant-for-this-client become candidates")
    p.add_argument("--max-questions", type=int, default=None,
                  help="(gaps only) cap how many questions get evaluated (highest-scored first)")
    p.add_argument("--limit", type=int, default=10)
    args = p.parse_args()

    if args.cmd == "topics":
        result = cluster_topics(args.client, threshold=args.threshold or DEFAULT_CLUSTER_THRESHOLD,
                                limit=args.limit)
    else:
        result = get_comment_content_gaps(args.client, percentile=args.percentile or DEFAULT_GAP_PERCENTILE,
                                          limit=args.limit, max_questions=args.max_questions)
    print(json.dumps(result, indent=2))

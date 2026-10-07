"""
analyze_comments.py - LLM extraction pass over research_comments: pulls
out real questions, sentiment, and topic tags via OpenAI structured
output.

    python analyze_comments.py --client outdoor-vitals
    python analyze_comments.py --client outdoor-vitals --limit 50
    python analyze_comments.py --client outdoor-vitals --reanalyze

Only comments with no research_comment_analysis row yet are processed,
unless --reanalyze is given. Low-signal comments ([deleted]/[removed],
or under MIN_BODY_LEN characters) are skipped before spending a call on
them at all.

Batches BATCH_SIZE comments into one OpenAI call (not one call per
comment) - the real cost lever. Model is configurable via OPENAI_MODEL
(default gpt-4o-mini) since the cheapest-suitable model changes over
time and whatever's hardcoded here may not be current when you read
this.
"""
import argparse
import json
import os

from dotenv import load_dotenv
from openai import OpenAI
from psycopg.types.json import Jsonb

from db import get_conn

load_dotenv()

MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
BATCH_SIZE = 25
MIN_BODY_LEN = 8
DELETED = {"[deleted]", "[removed]"}

_client = None


def _openai() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    return _client


RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "is_question": {"type": "boolean"},
                    "question_text": {"type": ["string", "null"]},
                    "sentiment": {"type": "string", "enum": ["positive", "negative", "neutral", "mixed"]},
                    "sentiment_target": {"type": ["string", "null"]},
                    "topics": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "is_question", "question_text", "sentiment", "sentiment_target", "topics"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["results"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = (
    "You analyze online comments (Reddit/YouTube) about a brand's product niche. "
    "For each comment, determine: "
    "(1) is_question - true only if the commenter is themselves asking something, "
    "seeking an answer from others. A comment that merely REFERENCES a question or "
    "answer you can't see (e.g. 'that's a good question', 'thanks for answering') is "
    "NOT itself a question - set is_question=false and question_text=null in that "
    "case, even if it contains a question mark or the word 'question'. When true, "
    "question_text is that question rewritten as a clear, standalone sentence - "
    "never a meta-question like 'what is the question being asked'. "
    "(2) sentiment toward whichever brand/product is being discussed, and "
    "sentiment_target - the specific brand, product, or company PROPER NAME if one "
    "is explicitly mentioned (e.g. 'Zpacks', 'Dudley DeBosier'). Do NOT use a "
    "generic descriptive phrase ('best lawyers around', 'this company', 'they', "
    "'the service') as sentiment_target - if no actual proper name is stated, "
    "sentiment_target is null even though sentiment itself is still scored. "
    "(3) topics - 1 to 4 short lowercase tags for what the comment is about "
    "(e.g. 'price', 'durability', 'customer service'). "
    "Return exactly one result per input id, in the same order, with matching ids."
)


def _is_low_signal(body: str) -> bool:
    b = (body or "").strip()
    return len(b) < MIN_BODY_LEN or b in DELETED


def fetch_unanalyzed(cur, client_id: int, limit, reanalyze: bool):
    sql = "SELECT rc.id, rc.body FROM research_comments rc "
    if not reanalyze:
        sql += "LEFT JOIN research_comment_analysis a ON a.comment_id = rc.id "
    sql += "WHERE rc.client_id = %(cid)s "
    if not reanalyze:
        sql += "AND a.id IS NULL "
    sql += "ORDER BY rc.id"
    if limit:
        sql += " LIMIT %(limit)s"
    cur.execute(sql, {"cid": client_id, "limit": limit})
    return cur.fetchall()


def analyze_batch(batch: list) -> tuple:
    """One OpenAI call covering up to BATCH_SIZE comments. Returns
    (valid_results, dropped_ids).

    Strict JSON-schema mode only enforces the *shape* of each result -
    it does not guarantee exactly one result per input id. The model can
    occasionally: omit an id (silently losing that comment), emit a
    stray id that doesn't belong to this batch at all (misattributing
    that result to an unrelated comment if stored blindly), or repeat
    the same id twice. So every returned id is checked against the ids
    actually sent in this batch, and de-duplicated (last one wins, same
    as the DB upsert would do anyway), before anything is accepted -
    valid_results always has at most one entry per id."""
    input_ids = {r["id"] for r in batch}
    payload = [{"id": r["id"], "text": r["body"][:2000]} for r in batch]
    resp = _openai().chat.completions.create(
        model=MODEL,
        temperature=0,  # classification/extraction, not creative generation -
                        # consistency run-to-run matters more than variety
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload)},
        ],
        response_format={"type": "json_schema", "json_schema": {
            "name": "comment_analysis", "schema": RESULT_SCHEMA, "strict": True,
        }},
    )
    raw_results = json.loads(resp.choices[0].message.content)["results"]
    by_id = {r["id"]: r for r in raw_results if r["id"] in input_ids}
    dropped_ids = input_ids - set(by_id)
    return list(by_id.values()), dropped_ids


def _no_nul(s):
    """Postgres text columns reject NUL (0x00) bytes outright. The model
    can echo a snippet of the original comment verbatim into question_text
    or topics, so if the source comment ever had a stray NUL in it (seen
    in real data), it can show up here too - strip it rather than let the
    whole batch's DB write fail on one field."""
    return s.replace("\x00", "") if isinstance(s, str) else s


def _store_result(cur, client_id: int, r: dict):
    cur.execute(
        """
        INSERT INTO research_comment_analysis
            (comment_id, client_id, is_question, question_text, sentiment,
             sentiment_target, topics, model)
        VALUES (%(comment_id)s, %(client_id)s, %(is_q)s, %(qtext)s, %(sentiment)s,
                %(target)s, %(topics)s, %(model)s)
        ON CONFLICT (comment_id) DO UPDATE SET
            is_question      = EXCLUDED.is_question,
            question_text    = EXCLUDED.question_text,
            sentiment        = EXCLUDED.sentiment,
            sentiment_target = EXCLUDED.sentiment_target,
            topics           = EXCLUDED.topics,
            model            = EXCLUDED.model,
            analyzed_at       = now()
        """,
        {
            "comment_id": r["id"],
            "client_id": client_id,
            "is_q": r["is_question"],
            "qtext": _no_nul(r["question_text"]),
            "sentiment": r["sentiment"],
            "target": _no_nul(r["sentiment_target"]),
            "topics": Jsonb([_no_nul(t) for t in r["topics"]]),
            "model": MODEL,
        },
    )


def analyze_comments(client_slug: str, limit: int = None, reanalyze: bool = False,
                     batch_size: int = BATCH_SIZE) -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name FROM clients WHERE slug = %(s)s", {"s": client_slug})
            client = cur.fetchone()
            if not client:
                raise ValueError(f"no client matching '{client_slug}'")
            client_id = client["id"]
            rows = fetch_unanalyzed(cur, client_id, limit, reanalyze)

    candidates = [r for r in rows if not _is_low_signal(r["body"])]
    skipped_low_signal = len(rows) - len(candidates)
    by_id = {r["id"]: r for r in candidates}

    analyzed = questions_found = batches_run = 0
    errors = []
    dropped_ids = set()

    def run_pass(items):
        nonlocal analyzed, questions_found, batches_run
        still_dropped = set()
        for i in range(0, len(items), batch_size):
            batch = items[i:i + batch_size]
            try:
                results, dropped = analyze_batch(batch)
            except Exception as e:
                errors.append(f"batch starting at comment {batch[0]['id']}: {e}")
                still_dropped.update(r["id"] for r in batch)
                continue
            batches_run += 1
            still_dropped.update(dropped)
            try:
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        for r in results:
                            _store_result(cur, client_id, r)
            except Exception as e:
                # transient connection hiccup during the write, not the API
                # call - nothing in this batch committed (one transaction
                # per batch), so these ids genuinely need re-processing,
                # not just a lost stat count
                errors.append(f"DB write failed for batch starting at comment {batch[0]['id']}: {e}")
                still_dropped.update(r["id"] for r in results)
                continue
            for r in results:
                analyzed += 1
                if r["is_question"]:
                    questions_found += 1
        return still_dropped

    dropped_ids = run_pass(candidates)
    if dropped_ids:
        # one retry pass for anything the model dropped the first time -
        # usually recovers most of them
        dropped_ids = run_pass([by_id[i] for i in dropped_ids if i in by_id])

    return {
        "client": client["name"],
        "client_slug": client_slug,
        "candidates": len(candidates),
        "skipped_low_signal": skipped_low_signal,
        "analyzed": analyzed,
        "questions_found": questions_found,
        "batches_run": batches_run,
        "model": MODEL,
        "errors": errors,
        "dropped_comment_ids": sorted(dropped_ids),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--client", required=True, help="client slug")
    ap.add_argument("--limit", type=int, default=None, help="max comments to analyze this run")
    ap.add_argument("--reanalyze", action="store_true", help="re-analyze comments that already have a result")
    ap.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    args = ap.parse_args()

    result = analyze_comments(
        args.client, limit=args.limit, reanalyze=args.reanalyze, batch_size=args.batch_size,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

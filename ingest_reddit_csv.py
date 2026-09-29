"""
ingest_reddit_csv.py - load the Reddit comment tool's CSV export into Postgres.

    python ingest_reddit_csv.py /path/to/folder
    python ingest_reddit_csv.py rootganic-reddit-comments.csv

Client is parsed from the filename ('{client}-reddit-comments.csv' - same
convention the web upload box uses), so a folder can hold files for many
different clients at once. Pass --client to override that for every file
in the run instead, e.g. for a file that isn't named that way:

    python ingest_reddit_csv.py comments.csv --client rootganic

Expects the 19-column export:
    post_id, post_title, post_url, subreddit, post_author, post_created_utc,
    post_score, post_num_comments, post_selftext,
    comment_id, parent_comment_id, comment_depth, comment_author,
    comment_author_id, comment_is_op, comment_score, comment_created_utc,
    comment_permalink, comment_text

Re-running is safe: comment_id is Reddit's own id, so a second run updates
scores instead of inserting duplicates.

The post body is stored as a comment row
---------------------------------------
On Reddit the post's selftext IS the question, and top-level comments are
answers to it - Reddit itself models this, giving top-level comments a
parent_id of t3_<post_id>. The export blanks that out, so we restore it:
the selftext goes in as a row keyed by post_id, and top-level comments
point at it. That makes the whole thread one connected tree.

The post row is tagged raw->>'is_post_body' = 'true', so it can be
excluded when counting actual comments:

    where raw->>'is_post_body' is null

Link posts with no selftext get no post row, and their top-level comments
keep a null parent.

Nothing is filtered on score. In real exports 93% of Reddit comments sit
at exactly 1 (the author's own automatic upvote), so a score threshold
removes almost nothing except downvoted complaints.
"""

import argparse
import csv
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from db import get_conn
from ingest_ai_visibility import get_or_create_client, slugify

# Web-upload convention (see upload.html): '{client}-reddit-comments.csv'.
# The CLI --client flag still works and takes priority when given, so
# this only matters for files ingested without one.
FILENAME_RE = re.compile(r"^(?P<client>.+?)-reddit-comments\.csv$", re.IGNORECASE)

EXPECTED = {
    "post_id", "post_title", "post_url", "subreddit", "post_author",
    "post_created_utc", "post_score", "post_num_comments", "post_selftext",
    "comment_id", "parent_comment_id", "comment_depth", "comment_author",
    "comment_author_id", "comment_is_op", "comment_score",
    "comment_created_utc", "comment_permalink", "comment_text",
}

DELETED = {"[deleted]", "[removed]"}


def _clean(v):
    if v is None:
        return None
    v = v.strip()
    return v or None


def _int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _ts(v):
    """Unix seconds -> aware datetime. 0/blank/junk -> None."""
    n = _int(v, 0)
    if n <= 0:
        return None
    try:
        dt = datetime.fromtimestamp(n, timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None
    return dt if dt.year >= 2005 else None


def _bool(v):
    return str(v).strip().lower() in {"true", "1", "yes", "t"}


def _strip_prefix(v):
    """t1_abc / t3_abc -> abc. Harmless if already stripped."""
    v = _clean(v)
    if v and len(v) > 3 and v[:3] in ("t1_", "t3_"):
        return v[3:]
    return v


def ingest_file(path: Path, client_slug: str = None) -> dict:
    if client_slug:
        client_name = client_slug
    else:
        m = FILENAME_RE.match(path.name)
        if not m:
            return {
                "file": path.name,
                "status": "skipped",
                "reason": "name doesn't match '{client}-reddit-comments.csv' and no --client given",
            }
        client_name = m.group("client").replace("-", " ").replace("_", " ").strip().title()

    try:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
    except (OSError, UnicodeDecodeError) as e:
        return {"file": path.name, "status": "skipped", "reason": f"unreadable: {e}"}

    if not rows:
        return {"file": path.name, "status": "skipped", "reason": "no rows"}

    missing = EXPECTED - set(rows[0].keys())
    if missing:
        return {
            "file": path.name,
            "status": "skipped",
            "reason": f"missing columns: {', '.join(sorted(missing))}",
        }

    with get_conn() as conn:
        with conn.cursor() as cur:
            client_id = get_or_create_client(cur, client_name)

            source_ids, post_bodies = {}, {}

            # one file may hold several threads
            for r in rows:
                pid = _clean(r["post_id"])
                if not pid or pid in source_ids:
                    continue
                cur.execute(
                    """
                    insert into research_sources
                        (client_id, platform, external_id, url, title,
                         container, published_at, fetched_at, metadata)
                    values (%(cid)s, 'reddit', %(pid)s, %(url)s, %(title)s,
                            %(sub)s, %(pub)s, now(), %(meta)s)
                    on conflict (platform, external_id) do update
                        set title        = excluded.title,
                            url          = excluded.url,
                            container    = excluded.container,
                            published_at = excluded.published_at,
                            fetched_at   = now(),
                            metadata     = excluded.metadata
                    returning id
                    """,
                    {
                        "cid": client_id,
                        "pid": pid,
                        "url": _clean(r["post_url"]),
                        "title": _clean(r["post_title"]),
                        "sub": _clean(r["subreddit"]),
                        "pub": _ts(r["post_created_utc"]),
                        # num_comments is Reddit's cached count and is often
                        # badly stale - kept for reference, not for validation
                        "meta": (
                            '{"post_author": %s, "post_score": %d, '
                            '"declared_num_comments": %d}'
                            % (
                                '"%s"' % (_clean(r["post_author"]) or ""),
                                _int(r["post_score"]),
                                _int(r["post_num_comments"]),
                            )
                        ),
                    },
                )
                source_ids[pid] = cur.fetchone()["id"]
                post_bodies[pid] = {
                    "text": _clean(r["post_selftext"]),
                    "author": _clean(r["post_author"]),
                    "score": _int(r["post_score"]),
                    "created": _ts(r["post_created_utc"]),
                }

            # the post body, as the root of the thread
            post_rows = 0
            for pid, pb in post_bodies.items():
                if not pb["text"] or pb["text"] in DELETED:
                    continue
                cur.execute(
                    """
                    insert into research_comments
                        (source_id, client_id, platform, external_id,
                         parent_external_id, author, author_id, body, score,
                         posted_at, raw)
                    values (%(sid)s, %(cid)s, 'reddit', %(ext)s, null,
                            %(author)s, null, %(body)s, %(score)s,
                            %(posted)s, '{"is_post_body": true}')
                    on conflict (platform, external_id) do update
                        set author     = excluded.author,
                            body       = excluded.body,
                            score      = excluded.score,
                            posted_at  = excluded.posted_at,
                            fetched_at = now()
                    """,
                    {
                        "sid": source_ids[pid],
                        "cid": client_id,
                        "ext": pid,
                        "author": pb["author"],
                        "body": pb["text"],
                        "score": pb["score"],
                        "posted": pb["created"],
                    },
                )
                post_rows += 1

            loaded = skipped = top_level = replies = 0

            for r in rows:
                cid_ = _clean(r["comment_id"])
                body = r.get("comment_text")
                if not cid_ or not (body or "").strip():
                    skipped += 1
                    continue

                pid = _clean(r["post_id"])
                parent = _strip_prefix(r["parent_comment_id"])
                if parent:
                    replies += 1
                else:
                    top_level += 1
                    # restore the link Reddit actually has: top-level
                    # comments are replies to the post itself
                    if pid in post_bodies and post_bodies[pid]["text"]:
                        parent = pid

                author = _clean(r["comment_author"])
                cur.execute(
                    """
                    insert into research_comments
                        (source_id, client_id, platform, external_id,
                         parent_external_id, author, author_id, body, score,
                         posted_at, raw)
                    values (%(sid)s, %(cid)s, 'reddit', %(ext)s, %(parent)s,
                            %(author)s, %(author_id)s, %(body)s, %(score)s,
                            %(posted)s, %(raw)s)
                    on conflict (platform, external_id) do update
                        set parent_external_id = excluded.parent_external_id,
                            author             = excluded.author,
                            author_id          = excluded.author_id,
                            body               = excluded.body,
                            score              = excluded.score,
                            posted_at          = excluded.posted_at,
                            raw                = excluded.raw,
                            fetched_at         = now()
                    """,
                    {
                        "sid": source_ids[pid],
                        "cid": client_id,
                        "ext": cid_,
                        "parent": parent,
                        "author": author,
                        "author_id": _clean(r["comment_author_id"]),
                        "body": body.strip(),
                        "score": _int(r["comment_score"]),
                        "posted": _ts(r["comment_created_utc"]),
                        "raw": '{"depth": %d, "is_op": %s, "permalink": "%s"}'
                        % (
                            _int(r["comment_depth"]),
                            "true" if _bool(r["comment_is_op"]) else "false",
                            (_clean(r["comment_permalink"]) or "").replace('"', ""),
                        ),
                    },
                )
                loaded += 1

    return {
        "file": path.name,
        "status": "ok",
        "client": client_name,
        "client_slug": slugify(client_name),
        "posts": len(source_ids),
        "post_bodies": post_rows,
        "loaded": loaded,
        "top_level": top_level,
        "replies": replies,
        "skipped": skipped,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="a .csv file, or a folder of them")
    ap.add_argument(
        "--client", default=None,
        help="client slug/name - overrides the filename for every file in this run. "
             "Omit to parse each file's client from '{client}-reddit-comments.csv'.",
    )
    args = ap.parse_args()

    p = Path(args.path)
    files = sorted(p.glob("*.csv")) if p.is_dir() else [p]
    if not files:
        print(f"No .csv files found at {p}")
        sys.exit(1)

    total = 0
    clients_seen = set()
    for f in files:
        r = ingest_file(f, args.client)
        if r["status"] == "ok":
            total += r["loaded"]
            clients_seen.add(r["client"])
            print(f"  OK   {r['file']} -> client='{r['client']}': {r['loaded']} comments "
                  f"({r['top_level']} top-level, {r['replies']} replies) "
                  f"from {r['posts']} post(s), {r['post_bodies']} post body row(s)"
                  + (f", {r['skipped']} skipped" if r["skipped"] else ""))
        else:
            print(f"  SKIP {r['file']}: {r['reason']}")
    print(f"\n{total} comments loaded across {len(clients_seen)} client(s): {', '.join(sorted(clients_seen)) or '(none)'}")


if __name__ == "__main__":
    main()

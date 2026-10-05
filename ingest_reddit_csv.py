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
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from psycopg.types.json import Jsonb

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


def _upsert_source(cur, client_id, external_id, url, title, container, published_at, metadata: dict) -> int:
    """The one research_sources upsert both the CSV path and the
    single-comment API path funnel through.

    Every comment on the same post repeats that post's own fields - true
    for every row of a CSV file, but a single-comment API caller might
    legitimately send the full post context on only the first comment
    and omit it on later ones. So a NULL/absent field here must *not*
    blank out previously-stored data: title/url/container/published_at
    fall back to the existing value via COALESCE, and metadata is
    merged key-by-key (jsonb `||`) rather than replaced outright."""
    cur.execute(
        """
        insert into research_sources
            (client_id, platform, external_id, url, title,
             container, published_at, fetched_at, metadata)
        values (%(cid)s, 'reddit', %(ext)s, %(url)s, %(title)s,
                %(container)s, %(pub)s, now(), %(meta)s)
        on conflict (platform, external_id) do update
            set title        = coalesce(excluded.title, research_sources.title),
                url          = coalesce(excluded.url, research_sources.url),
                container    = coalesce(excluded.container, research_sources.container),
                published_at = coalesce(excluded.published_at, research_sources.published_at),
                fetched_at   = now(),
                metadata     = research_sources.metadata || excluded.metadata
        returning id
        """,
        {
            "cid": client_id,
            "ext": external_id,
            "url": url,
            "title": title,
            "container": container,
            "pub": published_at,
            "meta": Jsonb(metadata),
        },
    )
    return cur.fetchone()["id"]


def _upsert_comment(cur, source_id, client_id, external_id, parent_external_id,
                     author, author_id, body, score, posted_at, raw: dict):
    """The one research_comments upsert both the CSV path and the
    single-comment API path funnel through - used for both the post-body
    row and regular comments."""
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
            "sid": source_id,
            "cid": client_id,
            "ext": external_id,
            "parent": parent_external_id,
            "author": author,
            "author_id": author_id,
            "body": body,
            "score": score,
            "posted": posted_at,
            "raw": Jsonb(raw),
        },
    )


def ingest_comment(
    client: str,
    post_id: str,
    comment_id: str,
    comment_text: str,
    post_title=None, post_url=None, subreddit=None, post_author=None,
    post_created_utc=None, post_score=None, post_num_comments=None, post_selftext=None,
    parent_comment_id=None, comment_depth=0, comment_author=None,
    comment_author_id=None, comment_is_op=False, comment_score=0,
    comment_created_utc=None, comment_permalink=None,
) -> dict:
    """
    Ingest exactly one Reddit comment row - no file involved. Same field
    names as the CSV export, so an automation (n8n, etc.) that already
    has one row in hand can push it straight to the database the moment
    it's produced, instead of batching into a CSV for someone to
    re-upload by hand. Every call repeats the post's own fields (title,
    url, subreddit, selftext) same as every row of the CSV does - the
    source upsert is idempotent, so that's harmless even called once per
    comment on the same post.
    """
    post_id = _clean(post_id)
    comment_id = _clean(comment_id)
    comment_text = (comment_text or "").strip()
    if not post_id:
        raise ValueError("post_id is required")
    if not comment_id:
        raise ValueError("comment_id is required")
    if not comment_text:
        raise ValueError("comment_text is required")

    post_selftext = _clean(post_selftext)
    parent = _strip_prefix(parent_comment_id)
    is_top_level = parent is None
    if not parent and post_selftext:
        # restore the link Reddit actually has: a top-level comment is a
        # reply to the post itself. Done *after* recording is_top_level,
        # which describes the original export, not this restored link.
        parent = post_id

    with get_conn() as conn:
        with conn.cursor() as cur:
            client_id = get_or_create_client(cur, client.strip())
            metadata = {}
            if post_author is not None:
                metadata["post_author"] = _clean(post_author) or ""
            if post_score is not None:
                metadata["post_score"] = _int(post_score)
            if post_num_comments is not None:
                metadata["declared_num_comments"] = _int(post_num_comments)
            source_id = _upsert_source(
                cur, client_id, post_id,
                url=_clean(post_url), title=_clean(post_title), container=_clean(subreddit),
                published_at=_ts(post_created_utc),
                metadata=metadata,
            )
            post_body_written = False
            if post_selftext and post_selftext not in DELETED:
                _upsert_comment(
                    cur, source_id, client_id, post_id, None,
                    author=_clean(post_author), author_id=None, body=post_selftext,
                    score=_int(post_score), posted_at=_ts(post_created_utc),
                    raw={"is_post_body": True},
                )
                post_body_written = True
            _upsert_comment(
                cur, source_id, client_id, comment_id, parent,
                author=_clean(comment_author), author_id=_clean(comment_author_id),
                body=comment_text, score=_int(comment_score),
                posted_at=_ts(comment_created_utc),
                raw={
                    "depth": _int(comment_depth),
                    "is_op": _bool(comment_is_op),
                    "permalink": _clean(comment_permalink) or "",
                },
            )

    return {
        "status": "ok",
        "client": client.strip(),
        "client_slug": slugify(client),
        "post_id": post_id,
        "comment_id": comment_id,
        "post_body_written": post_body_written,
        "top_level": is_top_level,
    }


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
                source_ids[pid] = _upsert_source(
                    cur, client_id, pid,
                    url=_clean(r["post_url"]), title=_clean(r["post_title"]),
                    container=_clean(r["subreddit"]), published_at=_ts(r["post_created_utc"]),
                    # num_comments is Reddit's cached count and is often
                    # badly stale - kept for reference, not for validation
                    metadata={
                        "post_author": _clean(r["post_author"]) or "",
                        "post_score": _int(r["post_score"]),
                        "declared_num_comments": _int(r["post_num_comments"]),
                    },
                )
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
                _upsert_comment(
                    cur, source_ids[pid], client_id, pid, None,
                    author=pb["author"], author_id=None, body=pb["text"],
                    score=pb["score"], posted_at=pb["created"],
                    raw={"is_post_body": True},
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

                _upsert_comment(
                    cur, source_ids[pid], client_id, cid_, parent,
                    author=_clean(r["comment_author"]), author_id=_clean(r["comment_author_id"]),
                    body=body.strip(), score=_int(r["comment_score"]),
                    posted_at=_ts(r["comment_created_utc"]),
                    raw={
                        "depth": _int(r["comment_depth"]),
                        "is_op": _bool(r["comment_is_op"]),
                        "permalink": _clean(r["comment_permalink"]) or "",
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

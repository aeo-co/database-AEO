"""
ingest_youtube_csv.py - load the YouTube comment tool's CSV export into Postgres.

    python ingest_youtube_csv.py /path/to/folder
    python ingest_youtube_csv.py rootganic-youtube-comments.csv

Client is parsed from the filename ('{client}-youtube-comments.csv' - same
convention the web upload box uses), so a folder can hold files for many
different clients at once. Pass --client to override that for every file
in the run instead, e.g. for a file that isn't named that way:

    python ingest_youtube_csv.py one-file.csv --client rootganic

Expects the 12-column export:
    video_id, video_title, video_url, video_published_at,
    comment_id, parent_comment_id, comment_author, author_channel_id,
    comment_likes, comment_published_at, total_reply_count, comment_text

Re-running is safe: comment_id is YouTube's own stable id, so a second
run updates like counts instead of inserting duplicates. Unlike a content
hash, this also survives someone editing their comment.

Nothing is filtered on like count. 69% of comments (and 72% of questions)
in real exports have zero likes - filtering here would throw away most of
what the research is for. Gate the expensive model calls instead.
"""

import argparse
import csv
import re
import sys
from pathlib import Path

from psycopg.types.json import Jsonb

from db import get_conn
from ingest_ai_visibility import get_or_create_client, slugify

# Web-upload convention (see upload.html): '{client}-youtube-comments.csv'.
# The CLI --client flag still works and takes priority when given, so
# this only matters for files ingested without one.
FILENAME_RE = re.compile(r"^(?P<client>.+?)-youtube-comments\.csv$", re.IGNORECASE)

EXPECTED = {
    "video_id", "video_title", "video_url", "video_published_at",
    "comment_id", "parent_comment_id", "comment_author", "author_channel_id",
    "comment_likes", "comment_published_at", "total_reply_count", "comment_text",
}


def _clean(v):
    """Empty strings from CSV should be NULL, not ''."""
    if v is None:
        return None
    v = v.strip()
    return v or None


def _int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _upsert_source(cur, client_id, external_id, url, title, published_at) -> int:
    """The one research_sources upsert both the CSV path and the
    single-comment API path funnel through.

    Every comment on the same video repeats that video's own fields -
    true for every row of a CSV file, but a single-comment API caller
    might legitimately send the full video context on only the first
    comment and omit it on later ones. So a NULL/absent field here must
    *not* blank out previously-stored data - fall back to the existing
    value via COALESCE."""
    cur.execute(
        """
        insert into research_sources
            (client_id, platform, external_id, url, title,
             published_at, fetched_at)
        values (%(cid)s, 'youtube', %(ext)s, %(url)s, %(title)s,
                %(pub)s::timestamptz, now())
        on conflict (platform, external_id) do update
            set title        = coalesce(excluded.title, research_sources.title),
                url          = coalesce(excluded.url, research_sources.url),
                published_at = coalesce(excluded.published_at, research_sources.published_at),
                fetched_at   = now()
        returning id
        """,
        {"cid": client_id, "ext": external_id, "url": url, "title": title, "pub": published_at},
    )
    return cur.fetchone()["id"]


def _upsert_comment(cur, source_id, client_id, external_id, parent_external_id,
                     author, author_id, body, score, reply_count, posted_at):
    """The one research_comments upsert both the CSV path and the
    single-comment API path funnel through."""
    cur.execute(
        """
        insert into research_comments
            (source_id, client_id, platform, external_id,
             parent_external_id, author, author_id, body, score,
             reply_count, posted_at, raw)
        values (%(sid)s, %(cid)s, 'youtube', %(ext)s, %(parent)s,
                %(author)s, %(author_id)s, %(body)s, %(score)s,
                %(replies)s, %(posted)s::timestamptz, %(raw)s)
        on conflict (platform, external_id) do update
            set parent_external_id = excluded.parent_external_id,
                author             = excluded.author,
                author_id          = excluded.author_id,
                body               = excluded.body,
                score              = excluded.score,
                reply_count        = excluded.reply_count,
                posted_at          = excluded.posted_at,
                fetched_at         = now()
        """,
        {
            "sid": source_id, "cid": client_id, "ext": external_id, "parent": parent_external_id,
            "author": author, "author_id": author_id, "body": body, "score": score,
            "replies": reply_count, "posted": posted_at, "raw": Jsonb({}),
        },
    )


def ingest_comment(
    client: str,
    video_id: str,
    comment_id: str,
    comment_text: str,
    video_title=None, video_url=None, video_published_at=None,
    parent_comment_id=None, comment_author=None, author_channel_id=None,
    comment_likes=0, comment_published_at=None, total_reply_count=0,
) -> dict:
    """
    Ingest exactly one YouTube comment row - no file involved. Same field
    names as the CSV export, so an automation (n8n, etc.) that already
    has one row in hand can push it straight to the database the moment
    it's produced, instead of batching into a CSV for someone to
    re-upload by hand. Every call repeats the video's own fields (title,
    url, published date) same as every row of the CSV does - the source
    upsert is idempotent, so that's harmless even called once per
    comment on the same video.
    """
    video_id = _clean(video_id)
    comment_id = _clean(comment_id)
    comment_text = (comment_text or "").strip()
    if not video_id:
        raise ValueError("video_id is required")
    if not comment_id:
        raise ValueError("comment_id is required")
    if not comment_text:
        raise ValueError("comment_text is required")

    parent = _clean(parent_comment_id)

    with get_conn() as conn:
        with conn.cursor() as cur:
            client_id = get_or_create_client(cur, client.strip())
            source_id = _upsert_source(
                cur, client_id, video_id,
                url=_clean(video_url), title=_clean(video_title), published_at=_clean(video_published_at),
            )
            _upsert_comment(
                cur, source_id, client_id, comment_id, parent,
                author=_clean(comment_author), author_id=_clean(author_channel_id),
                body=comment_text, score=_int(comment_likes),
                reply_count=_int(total_reply_count), posted_at=_clean(comment_published_at),
            )

    return {
        "status": "ok",
        "client": client.strip(),
        "client_slug": slugify(client),
        "video_id": video_id,
        "comment_id": comment_id,
        "top_level": parent is None,
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
                "reason": "name doesn't match '{client}-youtube-comments.csv' and no --client given",
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

            # one file may hold several videos; register each once
            source_ids = {}
            for r in rows:
                vid = _clean(r["video_id"])
                if not vid or vid in source_ids:
                    continue
                source_ids[vid] = _upsert_source(
                    cur, client_id, vid,
                    url=_clean(r["video_url"]), title=_clean(r["video_title"]),
                    published_at=_clean(r["video_published_at"]),
                )

            loaded = skipped = 0
            top_level = replies = 0

            for r in rows:
                comment_id = _clean(r["comment_id"])
                body = r.get("comment_text")
                if not comment_id or not (body or "").strip():
                    skipped += 1
                    continue

                parent = _clean(r["parent_comment_id"])
                if parent:
                    replies += 1
                else:
                    top_level += 1

                _upsert_comment(
                    cur, source_ids[_clean(r["video_id"])], client_id, comment_id, parent,
                    author=_clean(r["comment_author"]), author_id=_clean(r["author_channel_id"]),
                    body=body.strip(), score=_int(r["comment_likes"]),
                    reply_count=_int(r["total_reply_count"]), posted_at=_clean(r["comment_published_at"]),
                )
                loaded += 1

    return {
        "file": path.name,
        "status": "ok",
        "client": client_name,
        "client_slug": slugify(client_name),
        "videos": len(source_ids),
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
             "Omit to parse each file's client from '{client}-youtube-comments.csv'.",
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
                  f"from {r['videos']} video(s)"
                  + (f", {r['skipped']} skipped" if r["skipped"] else ""))
        else:
            print(f"  SKIP {r['file']}: {r['reason']}")
    print(f"\n{total} comments loaded across {len(clients_seen)} client(s): {', '.join(sorted(clients_seen)) or '(none)'}")


if __name__ == "__main__":
    main()
